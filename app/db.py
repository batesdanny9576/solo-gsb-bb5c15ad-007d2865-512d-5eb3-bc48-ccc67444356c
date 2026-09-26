"""SQLite 持久化层。

存储十张表：
* ``invoices``          —— 发票头（含原始请求 JSON，用于复算）与汇总金额；
  ``currency_digits`` / ``target_digits`` 固定该发票入库时的原币 / 目标币小数位，
  币种精度配置（CURRENCY_DIGITS）变更后读取与复算仍沿用入库精度（历史行
  ``target_digits`` 可能为 NULL，由读取层从已持久化换算金额恢复）；
* ``invoice_lines``     —— 逐行结果（税额残差分配后的最终值，含换算列）；
* ``payments``          —— 收款登记（每笔收款的金额、币种与登记时间）；
* ``payment_reversals`` —— 收款冲销（每笔冲销的金额、币种与登记时间，
  关联原收款流水；原收款不删除，余额一律按净入账额 = 收款 − 冲销 计算）；
* ``payment_allocation_batches``      —— 跨发票来款分配批次头（批次币种、来款金额、
  创建时间），每次 ``POST /payment-allocations`` 成功生成一个批次，响应回显批次 id；
* ``payment_allocation_batch_items``  —— 批次明细表：批次内本次**实际生成**的收款关联
  （批次 + 收款流水 + 发票、本次分配金额、抵扣顺序序号、整批撤销生成的冲销流水 id）；
  ``payment_id`` 唯一，一条收款至多属于一个批次，旧的无批次收款不在本表中，仍可按原接口冲销；
* ``installment_plans``      —— 分期回款计划头（每张发票至多一份，invoice_id 唯一）；
* ``installment_plan_items`` —— 计划期次（期次号、到期日、金额，按期次号升序），
  期次金额合计等于发票含税总额；查询时不改写计划，覆盖/逾期状态实时按净已收额计算；
* ``credit_memos``       —— 原发票贷项凭证头（每张凭证关联一张已保存发票，
  只追加、永不改写原发票与收款流水）与本次抵扣净额/税额/含税额合计；
* ``credit_memo_lines``  —— 凭证逐行（原行号、本次抵扣净额/税额/含税额，
  以及抵扣后该原行的累计抵扣净额/税额），``UNIQUE(memo_id, line_no)`` 兜底行号不重复。

精度策略（与 :mod:`app.calc` 统一）：所有金额的最小单位校验、累计、量化与余额
比较都在 :func:`app.calc.money_context` 的局部精度内进行——按本次涉及金额的
完整展开位数自动放大，不读不写线程全局 Decimal 上下文。FastAPI 工作线程默认
28 位精度下，大额发票（如 30 位整数净额）的收款、分次冲销与当前/历史余额
依然精确到币种最小单位：余额不丢分位，大额量化不抛 ``InvalidOperation``。

跨发票的两类汇总（应收账龄汇总、期间税额归集）面临**混合保存精度**：同一币种
代码下各发票持久化的 ``currency_digits`` 可能不同（币种精度配置变更后旧发票
保留旧位数）。口径统一为：每张发票/每行只按**自身入库精度**计算并以 Decimal
原样累加，输出时才按**该组实际参与记录中最高的入库小数位**格式化（账龄桶取
落桶发票、税额组取组内发票行与贷项凭证行），组精度取 ``max`` 而非首条记录的
位数，因此查询结果与插入顺序无关。

收款余额读取链路的分段取数：按发票 ID 取收款、按收款 ID 取冲销的 ``IN (...)``
查询统一按固定批量 :data:`_IN_CHUNK_SIZE` 分段（UTC 截止时点参数同样计入单条
语句的绑定参数预算，任何 SQLite 构建下都低于其上限），空集合不发起查询；各段
取回的行全部合并后，仍在同一局部精度下用 Decimal 逐笔累计（不走 SQL 浮点聚合），
超大 ID 集合（如全库逾期列表、应收账龄汇总）因此可查询，且结果与小批量完全一致。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .calc import money_context

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    currency        TEXT NOT NULL,
    currency_digits INTEGER NOT NULL,
    target_currency TEXT,
    target_digits   INTEGER,
    exchange_rate   TEXT,
    due_date        TEXT,
    total_net       TEXT NOT NULL,
    total_tax       TEXT NOT NULL,
    total_gross     TEXT NOT NULL,
    conv_total_net   TEXT,
    conv_total_tax   TEXT,
    conv_total_gross TEXT,
    residual_units  INTEGER NOT NULL,
    rule_id         TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    description  TEXT,
    tax_rate     TEXT NOT NULL,
    net          TEXT NOT NULL,
    exact_tax    TEXT NOT NULL,
    tax          TEXT NOT NULL,
    gross        TEXT NOT NULL,
    conv_net     TEXT,
    conv_tax     TEXT,
    conv_gross   TEXT,
    UNIQUE(invoice_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);

-- 逾期查询只关心已设置到期日的发票
CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);

CREATE TABLE IF NOT EXISTS payment_reversals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_reversals_payment_id ON payment_reversals(payment_id);

-- 跨发票来款分配批次：一次分配成功生成一个批次头
CREATE TABLE IF NOT EXISTS payment_allocation_batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 批次明细：只登记批次内实际生成（allocated > 0）的收款关联。
-- payment_id 唯一：一条收款至多属于一个批次（含其整批撤销的冲销归属）；
-- reversal_id 记录整批撤销为该收款生成的那一条冲销（最多一条），
-- 旧的手工冲销不计入；SET NULL 兜底：即使冲销流水被级联删除，明细仍保留。
CREATE TABLE IF NOT EXISTS payment_allocation_batch_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES payment_allocation_batches(id) ON DELETE CASCADE,
    payment_id  INTEGER NOT NULL UNIQUE REFERENCES payments(id) ON DELETE CASCADE,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    allocated   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    reversal_id INTEGER UNIQUE REFERENCES payment_reversals(id) ON DELETE SET NULL,
    UNIQUE(batch_id, payment_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_allocation_batch_items_batch_id
    ON payment_allocation_batch_items(batch_id);

-- 每张发票至多一份分期回款计划：invoice_id 上的唯一约束是并发兜底
CREATE TABLE IF NOT EXISTS installment_plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installment_plan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
    installment INTEGER NOT NULL,
    due_date    TEXT NOT NULL,
    amount      TEXT NOT NULL,
    UNIQUE(plan_id, installment)
);

CREATE INDEX IF NOT EXISTS idx_installment_plan_items_plan_id
    ON installment_plan_items(plan_id);

-- 原发票贷项凭证：只追加凭证，不改动 invoices / invoice_lines / payments
CREATE TABLE IF NOT EXISTS credit_memos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    currency   TEXT NOT NULL,
    net_total  TEXT NOT NULL,
    tax_total  TEXT NOT NULL,
    gross_total TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_memos_invoice_id
    ON credit_memos(invoice_id);

CREATE TABLE IF NOT EXISTS credit_memo_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id       INTEGER NOT NULL REFERENCES credit_memos(id) ON DELETE CASCADE,
    line_no       INTEGER NOT NULL,
    net           TEXT NOT NULL,
    tax           TEXT NOT NULL,
    gross         TEXT NOT NULL,
    cumulative_net TEXT NOT NULL,
    cumulative_tax TEXT NOT NULL,
    UNIQUE(memo_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_credit_memo_lines_memo_id
    ON credit_memo_lines(memo_id);
"""


class InvoiceNotFoundError(Exception):
    """收款登记/查询时发票不存在。"""

    def __init__(self, invoice_id: int) -> None:
        self.invoice_id = invoice_id
        super().__init__(f"发票 {invoice_id} 不存在")


class PaymentNotFoundError(Exception):
    """冲销时原收款流水不存在。"""

    def __init__(self, payment_id: int) -> None:
        self.payment_id = payment_id
        super().__init__(f"收款流水 {payment_id} 不存在")


class AllocationBatchNotFoundError(Exception):
    """整批撤销时来款分配批次不存在。"""

    def __init__(self, batch_id: int) -> None:
        self.batch_id = batch_id
        super().__init__(f"来款分配批次 {batch_id} 不存在")


class PlanAlreadyExistsError(Exception):
    """为已存在分期回款计划的发票再次创建计划。"""

    def __init__(self, invoice_id: int, plan_id: int) -> None:
        self.invoice_id = invoice_id
        self.plan_id = plan_id
        super().__init__(
            f"发票 {invoice_id} 已存在分期回款计划（plan_id={plan_id}），"
            f"每张发票只允许一份计划"
        )


def utc_now_iso() -> str:
    """UTC 时间，ISO 8601 带 Z 后缀，秒精度。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def utc_second_text(value: datetime) -> str:
    """感知时间转 UTC 秒精度 ISO 字符串（``YYYY-MM-DDTHH:MM:SSZ``）。

    与 :func:`utc_now_iso` 同构，用于把外部传入的截止时点归一到与库内
    ``created_at`` 相同的形状，供闭区间（``<=``）字符串比较。
    """

    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def db_path() -> str:
    return os.getenv("DB_PATH", "/data/invoices.db")


def connect(database: str | None = None) -> sqlite3.Connection:
    path = database or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False：FastAPI 的依赖注入与端点可能运行在线程池的
    # 不同线程；连接始终是每请求一条、单请求内串行使用，跨线程创建/使用是安全的
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # 兼容 v1.1 旧库：先为已存在的 invoices 表补上 due_date 列，
    # 再执行 CREATE ... IF NOT EXISTS（新库则无表可迁，直接由 SCHEMA 建出）
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invoices'"
    ).fetchone()
    if table_exists is not None:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "due_date" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN due_date TEXT")
        # 目标币小数位在创建时随发票固定：旧库先补列（历史行 NULL，读取时从
        # 已持久化的换算金额恢复目标币精度），再由 SCHEMA 建出其余新表
        if "target_digits" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN target_digits INTEGER")
        conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()


def save_invoice(conn: sqlite3.Connection, payload: dict[str, Any], result: dict[str, Any]) -> int:
    """在单个事务内写入发票头与行项目，返回发票 id。"""

    with conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, target_digits,
                exchange_rate, due_date,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["currency"],
                result["currency_digits"],
                result.get("fx", {}).get("target_currency"),
                (result.get("fx") or {}).get("target_digits"),
                result.get("fx", {}).get("exchange_rate"),
                payload.get("due_date"),
                result["totals"]["total_net"],
                result["totals"]["total_tax"],
                result["totals"]["total_gross"],
                (result.get("fx") or {}).get("total_net"),
                (result.get("fx") or {}).get("total_tax"),
                (result.get("fx") or {}).get("total_gross"),
                result["rounding"]["residual_units"],
                result["rule_id"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )
        invoice_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross,
                conv_net, conv_tax, conv_gross
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    invoice_id,
                    line["line_no"],
                    line.get("description"),
                    line["tax_rate"],
                    line["net"],
                    line["exact_tax"],
                    line["tax"],
                    line["gross"],
                    (line.get("converted") or {}).get("net"),
                    (line.get("converted") or {}).get("tax"),
                    (line.get("converted") or {}).get("gross"),
                )
                for line in result["lines"]
            ],
        )
    return invoice_id


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def get_lines(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (invoice_id,),
        ).fetchall()
    )


def list_invoices(
    conn: sqlite3.Connection, page: int, page_size: int
) -> tuple[list[sqlite3.Row], int]:
    total = int(conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    offset = (page - 1) * page_size
    rows = list(
        conn.execute(
            "SELECT * FROM invoices ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    )
    return rows, total


# ---- 逾期查询 ----


def list_overdue_invoices(
    conn: sqlite3.Connection, as_of: date, page: int, page_size: int
) -> tuple[list[dict[str, Any]], int]:
    """逾期发票分页查询。

    逾期口径：``due_date`` 非空且 **严格早于** ``as_of``，且按 Decimal 逐笔求和
    得到的未收余额（total_gross − 净入账合计，净入账 = Σ payments − Σ reversals）
    **大于 0**；缺少到期日、已收满（余额 0）的发票不纳入。结果按 id 倒序。

    未收余额不走 SQLite 浮点聚合：一次性取回候选发票的收款与冲销流水，在
    Python 侧用 Decimal 逐发票（每张发票币种单一）求和。
    """

    as_of_text = as_of.isoformat()
    rows = list(
        conn.execute(
            """
            SELECT * FROM invoices
            WHERE due_date IS NOT NULL AND due_date < ?
            ORDER BY id DESC
            """,
            (as_of_text,),
        ).fetchall()
    )
    if not rows:
        return [], 0

    paid_map = _paid_totals(conn, [int(r["id"]) for r in rows])

    grosses = [Decimal(r["total_gross"]) for r in rows]
    overdue: list[dict[str, Any]] = []
    with money_context([*grosses, *paid_map.values()], len(rows)):
        for r, gross in zip(rows, grosses):
            unit = _minor_unit(int(r["currency_digits"]))
            paid = paid_map.get(int(r["id"]), Decimal("0")).quantize(unit)
            outstanding = gross - paid
            if outstanding > 0:
                overdue.append(
                    {
                        "id": int(r["id"]),
                        "currency": r["currency"],
                        "due_date": r["due_date"],
                        "total_gross": format(gross, "f"),
                        "paid_total": format(paid, "f"),
                        "outstanding": format(outstanding, "f"),
                        "created_at": r["created_at"],
                    }
                )

    total = len(overdue)
    offset = (page - 1) * page_size
    return overdue[offset : offset + page_size], total


# ---- 收款登记 ----


def _minor_unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    return Decimal(1).scaleb(-digits)


# SQLite 单条语句的绑定参数上限（SQLITE_MAX_VARIABLE_NUMBER）在旧版构建中为 999
# （3.32+ 提高到 32766）。收款余额读取链路中「按发票 ID 取收款、按收款 ID 取冲销」
# 的 IN (...) 查询统一按该固定批量分段：单条语句至多 500 个 ID 参数，连同同样计入
# 预算的 UTC 截止时点参数共 501 个绑定参数，在任何受支持的 SQLite 构建下都低于上限。
_IN_CHUNK_SIZE = 500


def _in_chunks(ids: list[int]) -> Iterator[list[int]]:
    """把 ID 列表按固定批量 :data:`_IN_CHUNK_SIZE` 顺序切成若干段。

    空列表不产生任何分段——调用方因此对空集合不发起任何查询；分段保持输入顺序，
    各段取回的行由调用方在 Python 侧合并后统一计算。
    """

    for start in range(0, len(ids), _IN_CHUNK_SIZE):
        yield ids[start : start + _IN_CHUNK_SIZE]


def _paid_totals_as_of(
    conn: sqlite3.Connection, invoice_ids: list[int], at_text: str
) -> dict[int, Decimal]:
    """一批发票截至 UTC 秒精度时点 ``at_text`` 的净入账合计（收款 − 冲销）。

    与 :func:`_paid_totals` 同口径，但只计入 ``created_at <= at_text``（截止秒
    闭区间，同秒流水全部计入）的收款；冲销沿用 :func:`_reversed_totals` 的
    ``at_text`` 过滤，按其自身登记时间独立筛选。用 Decimal 逐笔求和，不走 SQLite
    的浮点 SUM 聚合。供应收账龄汇总（截止到查询日 UTC 23:59:59）使用。

    发票 ID 集合按 :data:`_IN_CHUNK_SIZE` 分段取数（截止时点参数计入单条语句的
    绑定参数预算），空集合不查询；各段取回的行合并后在同一局部精度下逐笔累计，
    超大 ID 集合可查询且结果与小批量一致。
    """

    if not invoice_ids:
        return {}
    rows: list[sqlite3.Row] = []
    for chunk in _in_chunks(invoice_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows += conn.execute(
            "SELECT id, invoice_id, amount FROM payments"
            f" WHERE invoice_id IN ({placeholders}) AND created_at <= ?",
            (*chunk, at_text),
        ).fetchall()
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows], at_text=at_text)
    amounts = [Decimal(r["amount"]) for r in rows]
    paid: dict[int, Decimal] = {}
    with money_context([*amounts, *reversed_map.values()], len(rows)):
        for r, amount in zip(rows, amounts):
            iid = int(r["invoice_id"])
            net = amount - reversed_map.get(int(r["id"]), Decimal("0"))
            paid[iid] = paid.get(iid, Decimal("0")) + net
    return paid


def _reversed_totals(
    conn: sqlite3.Connection,
    payment_ids: list[int],
    at_text: str | None = None,
) -> dict[int, Decimal]:
    """一批收款流水的已冲销合计。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。

    ``at_text`` 给出 UTC 秒精度截止时点（``YYYY-MM-DDTHH:MM:SSZ``）时，只合计
    ``created_at <= at_text`` 的冲销（截止秒为闭区间，同秒计入），供历史快照
    使用；缺省则合计全部冲销。

    收款 ID 集合按 :data:`_IN_CHUNK_SIZE` 分段取数（截止时点参数计入单条语句的
    绑定参数预算），空集合不查询；各段取回的行合并后在同一局部精度下逐笔累计，
    超大 ID 集合可查询且结果与小批量一致。
    """

    if not payment_ids:
        return {}
    rows: list[sqlite3.Row] = []
    for chunk in _in_chunks(payment_ids):
        placeholders = ",".join("?" for _ in chunk)
        if at_text is None:
            rows += conn.execute(
                "SELECT payment_id, amount FROM payment_reversals"
                f" WHERE payment_id IN ({placeholders})",
                chunk,
            ).fetchall()
        else:
            rows += conn.execute(
                "SELECT payment_id, amount FROM payment_reversals"
                f" WHERE payment_id IN ({placeholders}) AND created_at <= ?",
                (*chunk, at_text),
            ).fetchall()
    amounts = [Decimal(r["amount"]) for r in rows]
    totals: dict[int, Decimal] = {}
    with money_context(amounts, len(rows)):
        for r, amount in zip(rows, amounts):
            pid = int(r["payment_id"])
            totals[pid] = totals.get(pid, Decimal("0")) + amount
    return totals


def _paid_total(conn: sqlite3.Connection, invoice_id: int) -> Decimal:
    """净入账合计：收款 − 冲销。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。"""

    rows = conn.execute(
        "SELECT id, amount FROM payments WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    amounts = [Decimal(r["amount"]) for r in rows]
    with money_context([*amounts, *reversed_map.values()], len(rows)):
        return sum(
            (
                amount - reversed_map.get(int(r["id"]), Decimal("0"))
                for r, amount in zip(rows, amounts)
            ),
            Decimal("0"),
        )


def register_payment(
    conn: sqlite3.Connection, invoice_id: int, amount: Decimal
) -> dict[str, Any]:
    """登记一笔收款：余额校验与写入在 **同一个事务** 中完成。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，把并发的收款请求串行化，
    「读净入账合计 -> 校验未收余额 -> 写入」之间不会被另一笔收款/冲销插队，
    避免超收。

    最小单位校验、量化与余额计算全部在 :func:`app.calc.money_context` 的局部
    精度内进行（按本次金额量级自动放大）：大额发票（如 30 位整数净额）登记
    0.01 后未收余额不丢分位，登记大额收款也不会因默认 28 位精度抛
    ``InvalidOperation``。

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 金额不是币种最小单位整数倍、或累计净入账将超过含税总额 -> ``ValueError``
      （事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        digits = int(inv["currency_digits"])
        unit = _minor_unit(digits)
        gross = Decimal(inv["total_gross"])
        paid = _paid_total(conn, invoice_id)
        with money_context([amount, gross, paid]):
            if (amount / unit) != (amount / unit).to_integral_value():
                raise ValueError(
                    f"收款金额 {amount} 不是 {inv['currency']} 最小单位 {unit} 的整数倍"
                )
            amount = amount.quantize(unit)

            outstanding = gross - paid
            if amount > outstanding:
                raise ValueError(
                    f"收款 {amount} 超出未收余额 {outstanding}"
                    f"（含税总额 {gross}，已收 {paid}），未登记任何记录"
                )

            created_at = utc_now_iso()
            cur = conn.execute(
                """
                INSERT INTO payments (invoice_id, amount, currency, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (invoice_id, format(amount, "f"), inv["currency"], created_at),
            )
            new_paid = paid + amount
            new_outstanding = gross - new_paid
            zero = Decimal("0").quantize(unit)
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "id": int(cur.lastrowid),
        "invoice_id": invoice_id,
        "amount": format(amount, "f"),
        "currency": inv["currency"],
        "created_at": created_at,
        "reversed_total": format(zero, "f"),
        "net_amount": format(amount, "f"),
        "paid_total": format(new_paid, "f"),
        "outstanding": format(new_outstanding, "f"),
    }


# ---- 收款冲销 ----


def reverse_payment(
    conn: sqlite3.Connection, payment_id: int, amount: Decimal
) -> dict[str, Any]:
    """冲销一笔收款（可同一笔分次冲销）：累计计算与写入在 **同一个事务** 中完成。

    冲销记录写入独立的 ``payment_reversals`` 表并关联原流水，**不删除原收款**；
    全库的未收余额、逾期查询与来款分配都按净入账额（收款 − 冲销）计算，
    冲销后发票的未收余额相应回升。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，与 :func:`register_payment`、
    :func:`allocate_payment` 及其他冲销请求串行化：「读原流水与累计冲销 ->
    校验不超额 -> 写入」在同一事务内完成，并发冲销不会超额。

    * 原收款流水不存在 -> :class:`PaymentNotFoundError`
    * 金额 ≤ 0、不是币种最小单位整数倍、或累计冲销将超过原收款金额 ->
      ``ValueError``（事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        pay = conn.execute(
            """
            SELECT p.id, p.invoice_id, p.amount, p.currency, i.currency_digits
            FROM payments p JOIN invoices i ON i.id = p.invoice_id
            WHERE p.id = ?
            """,
            (payment_id,),
        ).fetchone()
        if pay is None:
            raise PaymentNotFoundError(payment_id)

        unit = _minor_unit(int(pay["currency_digits"]))
        original = Decimal(pay["amount"])
        reversed_so_far = _reversed_totals(conn, [payment_id]).get(
            payment_id, Decimal("0")
        )
        with money_context([amount, original, reversed_so_far]):
            if amount <= 0:
                raise ValueError(f"冲销金额必须大于 0，收到 {amount}")
            if (amount / unit) != (amount / unit).to_integral_value():
                raise ValueError(
                    f"冲销金额 {amount} 不是 {pay['currency']} 最小单位 {unit} 的整数倍"
                )
            amount = amount.quantize(unit)

            if reversed_so_far + amount > original:
                raise ValueError(
                    f"冲销 {amount} 后累计冲销将达 {reversed_so_far + amount}，"
                    f"超过原收款 {original}（已冲销 {reversed_so_far}），未写入任何记录"
                )

            created_at = utc_now_iso()
            cur = conn.execute(
                """
                INSERT INTO payment_reversals (payment_id, amount, currency, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (payment_id, format(amount, "f"), pay["currency"], created_at),
            )
            new_reversed = reversed_so_far + amount
            net_after = original - new_reversed
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "id": int(cur.lastrowid),
        "payment_id": payment_id,
        "invoice_id": int(pay["invoice_id"]),
        "amount": format(amount, "f"),
        "currency": pay["currency"],
        "created_at": created_at,
        "reversed_total": format(new_reversed, "f"),
        "net_amount": format(net_after, "f"),
    }


def _paid_totals(conn: sqlite3.Connection, invoice_ids: list[int]) -> dict[int, Decimal]:
    """一批发票的净入账合计（收款 − 冲销）。用 Decimal 逐笔求和，不走浮点 SUM 聚合。

    发票 ID 集合按 :data:`_IN_CHUNK_SIZE` 分段取数，空集合不查询；各段取回的
    收款行合并后在同一局部精度下逐笔累计（冲销侧同样分段，见
    :func:`_reversed_totals`），超大 ID 集合可查询且结果与小批量一致。
    """

    if not invoice_ids:
        return {}
    rows: list[sqlite3.Row] = []
    for chunk in _in_chunks(invoice_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows += conn.execute(
            "SELECT id, invoice_id, amount FROM payments"
            f" WHERE invoice_id IN ({placeholders})",
            chunk,
        ).fetchall()
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    amounts = [Decimal(r["amount"]) for r in rows]
    paid: dict[int, Decimal] = {}
    with money_context([*amounts, *reversed_map.values()], len(rows)):
        for r, amount in zip(rows, amounts):
            iid = int(r["invoice_id"])
            net = amount - reversed_map.get(int(r["id"]), Decimal("0"))
            paid[iid] = paid.get(iid, Decimal("0")) + net
    return paid


def allocate_payment(
    conn: sqlite3.Connection,
    currency: str,
    amount: Decimal,
    invoice_ids: list[int],
) -> dict[str, Any]:
    """把一笔来款分配到多张同币种发票上，整笔成功或整笔拒绝。

    分配顺序：``due_date`` 升序（未设置到期日的排最后）、同一到期日按发票 ID
    升序；只处理 ``invoice_ids`` 中的发票，不做跨币种换算。按顺序逐张抵扣：
    每张分配 ``min(剩余来款, 该发票正未收余额)``，来款用尽即止。未收余额按
    **净入账额**（收款 − 冲销）计算，冲销释放的余额可以再次被分配。

    **负额冲红 / 已结清发票不参与抵扣**：未收余额 ≤ 0 的候选发票（负数冲红单、
    已收满的发票）在结果中分配 ``0``（``payment_id`` 为 ``null``），既不消耗
    也不增加剩余来款；可分配余额合计同样只计入**正未收额**。若把负未收余额当作
    分配额，剩余来款会被越滚越大（如 50.00 来款最终写入 150.00 收款），批次
    金额与实际入账不符——本函数保证成功时各笔收款合计恒等于来款金额。

    **混合精度**：币种代码相同只代表币种一致，各发票保存的 ``currency_digits``
    可能不同（币种精度配置变更后，旧发票保留旧位数、新发票按新位数保存）。因此
    整笔来款**不按某一张发票的精度统一校验**，而是先按实际抵扣顺序算出完整分配
    方案，再逐张以**该发票保存的**最小单位校验其实际分配额（>0 部分）：整笔能
    精确分配才写入，任一张落不下对应零头（如 0.01 落到 0 位小数发票）即整笔
    拒绝。只需抵扣两位小数发票的合法来款（如 0.01，列表内另有 0 位发票但本次
    不实际抵扣）不会被误拒绝。

    成功时在**同一事务**内额外保存：

    * ``payment_allocation_batches`` 批次头一条（批次标识即其自增 id），响应回显
      ``batch_id``；
    * ``payment_allocation_batch_items`` 批次明细：本次每笔实际生成（allocated > 0）
      的收款关联（批次 + 收款流水 + 发票、金额、抵扣顺序序号），供整批撤销使用。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，与 :func:`register_payment`、
    :func:`reverse_payment` 及彼此的来款分配请求串行化：「读发票/净入账合计 ->
    校验 -> 写入批次头、多条 payments、批次明细」在同一事务内完成，并发收款不会超收。

    任一前置条件不满足都回滚，**不留下任何收款记录与批次记录**：

    * 列表中任一发票不存在 -> :class:`InvoiceNotFoundError`
    * 来款金额 ≤ 0 -> ``ValueError``
    * 发票币种与来款不一致 -> ``ValueError``
    * 按实际抵扣顺序计算出的某张发票实际分配额（>0）不是**该发票保存的**
      ``currency_digits`` 最小单位整数倍（整笔无法按各发票精度精确分配）
      -> ``ValueError``
    * 列表内发票**正未收余额**合计 < 来款金额（负额冲红与已结清发票不计入）
      -> ``ValueError``
    """

    batch_id: int | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 调用方（schema 层）已保证列表非空且无重复，这里再防御性校验一次，
        # 避免绕过 API 直接调用本函数时出现部分写入
        if not invoice_ids:
            raise ValueError("invoice_ids 不能为空")
        if len(invoice_ids) != len(set(invoice_ids)):
            raise ValueError("invoice_ids 不得包含重复的发票 ID")

        placeholders = ",".join("?" for _ in invoice_ids)
        rows = list(
            conn.execute(
                f"SELECT id, currency, currency_digits, due_date, total_gross"
                f" FROM invoices WHERE id IN ({placeholders})",
                invoice_ids,
            ).fetchall()
        )
        found = {int(r["id"]): r for r in rows}
        for invoice_id in invoice_ids:
            if invoice_id not in found:
                raise InvoiceNotFoundError(invoice_id)

        digits_by_id = {int(r["id"]): int(r["currency_digits"]) for r in rows}
        currencies = {r["currency"] for r in rows}
        mismatched = sorted(c for c in currencies if c != currency)
        if mismatched:
            raise ValueError(
                f"来款币种 {currency} 与发票币种不一致：{', '.join(mismatched)}；"
                f"来款分配不做跨币种换算"
            )

        paid_map = _paid_totals(conn, invoice_ids)
        grosses = [Decimal(found[i]["total_gross"]) for i in invoice_ids]
        with money_context(
            [amount, *grosses, *paid_map.values()], len(invoice_ids)
        ):
            if amount <= 0:
                raise ValueError(f"来款金额必须大于 0，收到 {amount}")

            outstanding: dict[int, Decimal] = {}
            for idx, invoice_id in enumerate(invoice_ids):
                outstanding[invoice_id] = grosses[idx] - paid_map.get(
                    invoice_id, Decimal("0")
                )

            # 可分配余额只计入**正未收额**：负额冲红发票（未收余额为负）与
            # 已结清发票（未收余额为 0）不参与抵扣。若把负余额计入合计，既会
            # 虚减可分配总额，又会在下面的抵扣循环中被当作分配额、把剩余来款
            # 越滚越大（50.00 来款可能写入 150.00 收款）。
            total_outstanding = sum(
                (due for due in outstanding.values() if due > 0), Decimal("0")
            )
            if amount > total_outstanding:
                raise ValueError(
                    f"来款 {amount} {currency} 超出列表内发票正未收余额合计 "
                    f"{format(total_outstanding, 'f')}"
                    f"（负额冲红与已结清发票不参与抵扣），整笔拒绝，未登记任何记录"
                )

            # 到期日升序、NULL 排最后（is None -> True=1）；同日（含都为空）按 ID 升序
            ordered_ids = sorted(
                invoice_ids,
                key=lambda i: (found[i]["due_date"] is None, found[i]["due_date"], i),
            )

            # 先按实际抵扣顺序算出完整分配方案，再逐张以**该发票保存的**最小单位
            # 校验：同币种旧发票/新发票的 currency_digits 可能不同（精度配置变更
            # 后旧发票保留旧位数），整笔来款不能按某一张发票的位数统一校验。
            # 方案全部合法后才开始写库，保证批次、收款与明细同落或同不落。
            plan: list[dict[str, Any]] = []
            remaining = amount
            for invoice_id in ordered_ids:
                due = outstanding[invoice_id]
                if due <= 0:
                    # 负额冲红 / 已结清发票：分配 0，不消耗也不增加剩余来款；
                    # 结果中仍按抵扣顺序回显（allocated 为 0、payment_id 为 null）
                    allocated = Decimal("0")
                else:
                    allocated = remaining if remaining < due else due
                item_digits = digits_by_id[invoice_id]
                item_unit = _minor_unit(item_digits)
                if allocated > 0 and (allocated / item_unit) != (
                    allocated / item_unit
                ).to_integral_value():
                    raise ValueError(
                        f"发票 {invoice_id} 的本次实际分配额 {format(allocated, 'f')} "
                        f"不是 {currency} 保存精度（{item_digits} 位小数，最小单位 "
                        f"{format(item_unit, 'f')}）的整数倍；整笔来款 "
                        f"{format(amount, 'f')} 无法按各发票精度精确分配，"
                        f"整笔拒绝，未登记任何记录"
                    )
                # 通过校验后按该发票自己的保存位数规范化：落到 0 位发票的分配额
                # 存为整数（"3" 而非 "3.00"），与该发票单张收款的展示口径一致
                allocated = allocated.quantize(item_unit)
                due_left = (due - allocated).quantize(item_unit)
                plan.append(
                    {
                        "invoice_id": invoice_id,
                        "due_date": found[invoice_id]["due_date"],
                        "allocated": allocated,
                        "outstanding": due_left,
                    }
                )
                remaining -= allocated
            if remaining != 0:
                # 不变量防御：上面已校验正未收合计充足、且逐张分配额均为各自
                # 最小单位整数倍，理论上不可能有剩余
                raise AssertionError(
                    f"来款分配后仍有余额 {format(remaining, 'f')} 未抵扣"
                )

            # 各发票分配额均已按各自精度校验，其和（= 整笔来款）是列表内最细最小
            # 单位的整数倍；以该单位规范化展示，0 位发票批次即 "50" 而非 "50.00"
            finest_unit = _minor_unit(max(digits_by_id.values()))
            canonical_amount = amount.quantize(finest_unit)

            created_at = utc_now_iso()
            # 批次头与收款流水、批次明细在同一事务内写入；批次 id 用于整批撤销
            batch_cur = conn.execute(
                """
                INSERT INTO payment_allocation_batches (amount, currency, created_at)
                VALUES (?, ?, ?)
                """,
                (format(canonical_amount, "f"), currency, created_at),
            )
            batch_id = int(batch_cur.lastrowid)
            items: list[dict[str, Any]] = []
            seq = 0
            for entry in plan:
                invoice_id = entry["invoice_id"]
                allocated = entry["allocated"]
                payment_id: int | None = None
                if allocated > 0:
                    seq += 1
                    cur = conn.execute(
                        """
                        INSERT INTO payments (invoice_id, amount, currency, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (invoice_id, format(allocated, "f"), currency, created_at),
                    )
                    payment_id = int(cur.lastrowid)
                    # 记录本次生成的收款关联（批次撤销据此逐笔冲销）
                    conn.execute(
                        """
                        INSERT INTO payment_allocation_batch_items (
                            batch_id, payment_id, invoice_id, allocated, seq, reversal_id
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        """,
                        (batch_id, payment_id, invoice_id, format(allocated, "f"), seq),
                    )
                items.append(
                    {
                        "invoice_id": invoice_id,
                        "due_date": entry["due_date"],
                        "allocated": format(allocated, "f"),
                        "outstanding": format(entry["outstanding"], "f"),
                        "payment_id": payment_id,
                    }
                )
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "batch_id": batch_id,
        "currency": currency,
        "amount": format(canonical_amount, "f"),
        "allocated_total": format(canonical_amount, "f"),
        "created_at": created_at,
        "allocations": items,
    }


def reverse_allocation_batch(
    conn: sqlite3.Connection, batch_id: int
) -> tuple[dict[str, Any], bool]:
    """整批撤销一次跨发票来款分配，全部计算与写入在 **同一个写事务** 内完成。

    逐笔处理批次明细（``seq`` 升序，即原分配抵扣顺序）中本次生成的收款关联：

    1. 计算该收款当前的**净入账额** = 原收款金额 − 其全部已有冲销
       （既包含整批撤销已生成的冲销，也包含通过
       ``POST /payments/{id}/reversals`` 做的手工冲销）；
    2. **仅对正余额（净入账额 > 0）追加一条等额冲销**，并把冲销流水 id 回写到
       批次明细的 ``reversal_id``；净入账额已为 0（已被手工冲销完，或本批次此前
       已撤销过）则**不新增任何冲销**。

    因此接口天然**幂等**：第一次请求为仍有正余额的收款各追加一条冲销；重复请求
    时这些收款净入账额已为 0，不会新增第二条冲销。返回各笔的**撤销金额**（本次
    新增时为其当时正余额；重复请求时沿用批次明细 ``reversal_id`` 已记录冲销的金额）、
    撤销后的净入账额，以及批次合计。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，与 :func:`register_payment`、
    :func:`reverse_payment`、:func:`allocate_payment` 串行化，并发整批撤销/手工冲销
    不会对同一收款超额冲销。

    返回 ``(结果字典, created)``：``created`` 为本次是否**新写入了至少一条冲销**
    （首次撤销通常为 ``True``，重复请求或批次收款此前已全部手工冲完时为 ``False``）。

    * 批次不存在 -> :class:`AllocationBatchNotFoundError`
    """

    created = False
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT id, amount, currency, created_at"
            " FROM payment_allocation_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise AllocationBatchNotFoundError(batch_id)

        item_rows = list(
            conn.execute(
                """
                SELECT bi.seq AS seq, bi.payment_id AS payment_id,
                       bi.invoice_id AS invoice_id, bi.allocated AS allocated,
                       bi.reversal_id AS reversal_id,
                       p.amount AS payment_amount,
                       i.currency_digits AS currency_digits
                FROM payment_allocation_batch_items bi
                JOIN payments p ON p.id = bi.payment_id
                JOIN invoices i ON i.id = bi.invoice_id
                WHERE bi.batch_id = ?
                ORDER BY bi.seq
                """,
                (batch_id,),
            ).fetchall()
        )
        payment_ids = [int(r["payment_id"]) for r in item_rows]
        reversed_map = _reversed_totals(conn, payment_ids)

        # 批次明细此前已记录的整批撤销冲销（重复请求时回显其金额/流水号/时间）
        prior_reversal_ids = [
            int(r["reversal_id"]) for r in item_rows if r["reversal_id"] is not None
        ]
        prior_rows: dict[int, sqlite3.Row] = {}
        if prior_reversal_ids:
            placeholders = ",".join("?" for _ in prior_reversal_ids)
            prior_rows = {
                int(r["id"]): r
                for r in conn.execute(
                    f"SELECT id, amount, created_at FROM payment_reversals"
                    f" WHERE id IN ({placeholders})",
                    prior_reversal_ids,
                ).fetchall()
            }

        prec_amounts = [Decimal(r["payment_amount"]) for r in item_rows]
        prec_amounts += [Decimal(r["allocated"]) for r in item_rows]
        prec_amounts += reversed_map.values()
        prec_amounts += [Decimal(p["amount"]) for p in prior_rows.values()]
        with money_context(prec_amounts, len(item_rows)):
            created_at = utc_now_iso()
            result_items: list[dict[str, Any]] = []
            batch_reversed_total = Decimal("0")
            for r in item_rows:
                payment_id = int(r["payment_id"])
                item_unit = _minor_unit(int(r["currency_digits"]))
                original = Decimal(r["payment_amount"])
                net_before = (
                    original - reversed_map.get(payment_id, Decimal("0"))
                ).quantize(item_unit)

                reversal_id = r["reversal_id"]
                if net_before > 0:
                    # 正余额：追加一条等额冲销（净余额为最小单位整数倍，天然不超额）
                    cur = conn.execute(
                        """
                        INSERT INTO payment_reversals (payment_id, amount, currency, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (payment_id, format(net_before, "f"), batch["currency"], created_at),
                    )
                    reversal_id = int(cur.lastrowid)
                    conn.execute(
                        "UPDATE payment_allocation_batch_items SET reversal_id = ?"
                        " WHERE batch_id = ? AND payment_id = ?",
                        (reversal_id, batch_id, payment_id),
                    )
                    reversed_map[payment_id] = (
                        reversed_map.get(payment_id, Decimal("0")) + net_before
                    )
                    this_amount = net_before
                    this_created_at: str | None = created_at
                    created = True
                    net_after = Decimal("0").quantize(item_unit)
                elif reversal_id is not None:
                    # 重复请求：沿用本批次此前已生成的冲销，不新增
                    prior = prior_rows[int(reversal_id)]
                    this_amount = Decimal(prior["amount"]).quantize(item_unit)
                    this_created_at = prior["created_at"]
                    net_after = net_before  # 已为 0
                else:
                    # 批次撤销前该收款已被手工冲销完：本批次不产生冲销
                    this_amount = Decimal("0").quantize(item_unit)
                    this_created_at = None
                    net_after = net_before  # 已为 0

                batch_reversed_total += this_amount
                result_items.append(
                    {
                        "seq": int(r["seq"]),
                        "payment_id": payment_id,
                        "invoice_id": int(r["invoice_id"]),
                        "allocated": Decimal(r["allocated"]).quantize(item_unit),
                        "reversal_id": reversal_id,
                        "reversed_amount": this_amount,
                        "reversal_created_at": this_created_at,
                        "net_amount": net_after,
                    }
                )

            # 批次内各发票保存的 currency_digits 可能不同（混合精度）：各笔撤销额
            # 已按自身发票单位取整，合计以批次内最细最小单位规范化，避免把 0.01
            # 误舍入成 0 位批次的整数
            finest_digits = (
                max(int(r["currency_digits"]) for r in item_rows) if item_rows else 2
            )
            unit = _minor_unit(finest_digits)
            reversed_total = batch_reversed_total.quantize(unit)
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    items_out = [
        {
            "seq": it["seq"],
            "payment_id": it["payment_id"],
            "invoice_id": it["invoice_id"],
            "allocated": format(it["allocated"], "f"),
            "reversed_amount": format(it["reversed_amount"], "f"),
            "reversal_id": it["reversal_id"],
            "reversal_created_at": it["reversal_created_at"],
            "net_amount": format(it["net_amount"], "f"),
            "currency": batch["currency"],
        }
        for it in result_items
    ]
    result = {
        "batch_id": batch_id,
        "currency": batch["currency"],
        "created_at": batch["created_at"],
        "allocated_total": batch["amount"],
        "reversed_total": format(reversed_total, "f"),
        "reversal_count": sum(1 for it in items_out if it["reversal_id"] is not None),
        "items": items_out,
    }
    return result, created


def payment_overview(conn: sqlite3.Connection, invoice: sqlite3.Row) -> dict[str, Any]:
    """某张发票的收款记录与未收余额（供 GET 使用）。

    每条流水附带已冲销金额 ``reversed_total`` 与净入账额 ``net_amount``；
    顶层 ``paid_total`` 为全发票净入账合计，``outstanding`` 按净入账额计算。
    冲销合计经 :func:`_reversed_totals` 按固定批量分段取数，单张发票流水数
    超过批量上限时同样可查询、结果不变。
    """

    rows = list(
        conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
    )
    unit = _minor_unit(int(invoice["currency_digits"]))
    reversed_map = _reversed_totals(conn, [int(r["id"]) for r in rows])
    gross = Decimal(invoice["total_gross"])
    amounts = [Decimal(r["amount"]) for r in rows]
    with money_context([*amounts, *reversed_map.values(), gross], len(rows)):
        paid = Decimal("0")
        reversed_total = Decimal("0")
        payments: list[dict[str, Any]] = []
        for r, amount in zip(rows, amounts):
            rev = reversed_map.get(int(r["id"]), Decimal("0")).quantize(unit)
            net = amount - rev
            paid += net
            reversed_total += rev
            payments.append(
                {
                    "id": r["id"],
                    "invoice_id": r["invoice_id"],
                    "amount": r["amount"],
                    "currency": r["currency"],
                    "created_at": r["created_at"],
                    "reversed_total": format(rev, "f"),
                    "net_amount": format(net, "f"),
                }
            )
        paid = paid.quantize(unit)
        reversed_total = reversed_total.quantize(unit)
        outstanding = gross - paid
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "total_gross": invoice["total_gross"],
        "paid_total": format(paid, "f"),
        "reversed_total": format(reversed_total, "f"),
        "outstanding": format(outstanding, "f"),
        "payment_count": len(rows),
        "payments": payments,
    }


# ---- 历史收款快照 ----


def payment_snapshot(
    conn: sqlite3.Connection, invoice: sqlite3.Row, at: datetime
) -> dict[str, Any]:
    """单张发票在指定 UTC 时点的历史收款快照（供 GET 使用）。

    截止时点 ``at`` 为**闭区间、秒精度**：``created_at <= at`` 的流水全部计入，
    即截止秒当秒写入的收款/冲销都包含在内；库内 ``created_at`` 与 ``at`` 均为
    定长的 ``YYYY-MM-DDTHH:MM:SSZ`` UTC 文本，直接用字符串字典序比较即可。

    分别筛选快照时点之前及当秒的：

    * **收款**：该发票 ``payments.created_at <= at`` 的流水；
    * **冲销**：关联上述收款且 ``payment_reversals.created_at <= at`` 的流水
      （按所属收款连接到发票，并独立按冲销自身登记时间过滤）。

    随后按发票币种最小单位用 ``Decimal`` 计算收款总额、冲销总额、净已收
    （收款 − 冲销）与未收余额（含税总额 − 净已收）。查询只读取快照时点之前的
    历史行，**截止后写入的任何流水都不参与，历史结果不会随后续流水改变**。
    收款与冲销流水均按固定批量 :data:`_IN_CHUNK_SIZE` 分段取数（截止时点参数
    计入单条语句预算），单张发票流水数超过批量上限时同样可查询、结果不变。

    ``at`` 早于发票创建时间（``invoices.created_at``，秒精度）时抛
    ``ValueError``（由 API 层映射为 422）。
    """

    at_text = utc_second_text(at)
    created_text = invoice["created_at"]
    if at_text < created_text:
        raise ValueError(
            f"截止时点 {at_text} 早于发票创建时间 {created_text}，"
            f"无法生成历史收款快照"
        )

    payment_rows = list(
        conn.execute(
            """
            SELECT * FROM payments
            WHERE invoice_id = ? AND created_at <= ?
            ORDER BY id
            """,
            (invoice["id"], at_text),
        ).fetchall()
    )
    payment_ids = [int(r["id"]) for r in payment_rows]
    reversed_map = _reversed_totals(conn, payment_ids, at_text=at_text)

    # 冲销明细同样按固定批量分段取数（截止时点参数计入单条语句预算），空集合
    # 不查询；各段合并后按冲销流水 id 重排，与单条 ORDER BY r.id 的结果一致
    reversal_rows: list[sqlite3.Row] = []
    for chunk in _in_chunks(payment_ids):
        placeholders = ",".join("?" for _ in chunk)
        reversal_rows += conn.execute(
            f"""
            SELECT r.* FROM payment_reversals r
            WHERE r.payment_id IN ({placeholders}) AND r.created_at <= ?
            ORDER BY r.id
            """,
            (*chunk, at_text),
        ).fetchall()
    reversal_rows.sort(key=lambda r: r["id"])

    unit = _minor_unit(int(invoice["currency_digits"]))
    gross = Decimal(invoice["total_gross"])
    amounts = [Decimal(r["amount"]) for r in payment_rows]
    amounts += [Decimal(r["amount"]) for r in reversal_rows]
    with money_context([*amounts, gross], len(amounts)):
        received_total = Decimal("0")
        reversed_total = Decimal("0")
        payments: list[dict[str, Any]] = []
        for r in payment_rows:
            amount = Decimal(r["amount"])
            rev = reversed_map.get(int(r["id"]), Decimal("0")).quantize(unit)
            net = amount - rev
            received_total += amount
            reversed_total += rev
            payments.append(
                {
                    "id": r["id"],
                    "invoice_id": r["invoice_id"],
                    "amount": format(amount.quantize(unit), "f"),
                    "currency": r["currency"],
                    "created_at": r["created_at"],
                    "reversed_total": format(rev, "f"),
                    "net_amount": format(net, "f"),
                }
            )

        reversals: list[dict[str, Any]] = []
        for r in reversal_rows:
            amount = Decimal(r["amount"]).quantize(unit)
            reversals.append(
                {
                    "id": r["id"],
                    "payment_id": r["payment_id"],
                    "invoice_id": invoice["id"],
                    "amount": format(amount, "f"),
                    "currency": r["currency"],
                    "created_at": r["created_at"],
                }
            )

        received_total = received_total.quantize(unit)
        reversed_total = reversed_total.quantize(unit)
        net_paid = (received_total - reversed_total).quantize(unit)
        total_gross = gross.quantize(unit)
        outstanding = (gross - net_paid).quantize(unit)
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "at": at_text,
        "invoice_created_at": created_text,
        "total_gross": format(total_gross, "f"),
        "received_total": format(received_total, "f"),
        "reversed_total": format(reversed_total, "f"),
        "net_paid": format(net_paid, "f"),
        "outstanding": format(outstanding, "f"),
        "payment_count": len(payment_rows),
        "reversal_count": len(reversal_rows),
        "payments": payments,
        "reversals": reversals,
    }


# ---- 分期回款计划 ----


def get_installment_plan(
    conn: sqlite3.Connection, invoice_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM installment_plans WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()


def get_installment_plan_items(
    conn: sqlite3.Connection, plan_id: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM installment_plan_items WHERE plan_id = ? ORDER BY installment",
            (plan_id,),
        ).fetchall()
    )


def create_installment_plan(
    conn: sqlite3.Connection,
    invoice_id: int,
    installments: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    """为单张发票创建唯一一份分期回款计划：全部校验与写入在 **同一事务** 完成。

    ``installments`` 为 ``(due_date, amount)`` 列表，调用方（schema 层）已保证
    非空且到期日非递减（升序、允许相同）。事务内（``BEGIN IMMEDIATE``）再做
    全部业务校验，任一不满足都回滚、**不留下任何计划行**：

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 该发票已存在计划 -> :class:`PlanAlreadyExistsError`
    * 任一期金额 ≤ 0、不是发票币种最小单位的正整数倍 -> ``ValueError``
    * 期次金额合计不等于发票含税总额 -> ``ValueError``

    成功后返回按当前 UTC 日期计算的完整计划视图（与查询接口同构）。
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT id, currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        existing = conn.execute(
            "SELECT id FROM installment_plans WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if existing is not None:
            raise PlanAlreadyExistsError(invoice_id, int(existing["id"]))

        unit = _minor_unit(int(inv["currency_digits"]))
        gross = Decimal(inv["total_gross"])

        raw_amounts = [raw_amount for _due, raw_amount in installments]
        with money_context([*raw_amounts, gross], len(installments)):
            amounts: list[Decimal] = []
            for idx, raw_amount in enumerate(raw_amounts, start=1):
                if raw_amount <= 0:
                    raise ValueError(f"第 {idx} 期金额必须大于 0，收到 {raw_amount}")
                if (raw_amount / unit) != (raw_amount / unit).to_integral_value():
                    raise ValueError(
                        f"第 {idx} 期金额 {raw_amount} 不是 {inv['currency']} 最小单位 "
                        f"{unit} 的整数倍"
                    )
                amounts.append(raw_amount.quantize(unit))

            total = sum(amounts, Decimal("0"))
            if total != gross:
                raise ValueError(
                    f"期次合计 {format(total, 'f')} 与发票含税总额 "
                    f"{format(gross, 'f')} 不一致，未创建任何计划"
                )

        created_at = utc_now_iso()
        cur = conn.execute(
            "INSERT INTO installment_plans (invoice_id, created_at) VALUES (?, ?)",
            (invoice_id, created_at),
        )
        plan_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO installment_plan_items (plan_id, installment, due_date, amount)
            VALUES (?, ?, ?, ?)
            """,
            [
                (plan_id, idx, due, format(amount, "f"))
                for idx, ((due, _raw), amount) in enumerate(
                    zip(installments, amounts), start=1
                )
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return installment_plan_status(
        conn, inv, plan_id, created_at,
        get_installment_plan_items(conn, plan_id),
        datetime.now(timezone.utc).date(),
    )


def installment_plan_status(
    conn: sqlite3.Connection,
    invoice: sqlite3.Row | dict[str, Any],
    plan_id: int,
    created_at: str,
    item_rows: list[sqlite3.Row],
    as_of: date,
) -> dict[str, Any]:
    """按期次顺序用净已收额（收款 − 冲销）逐期抵扣，生成计划视图。

    抵扣为**先进先出（FIFO）**：净已收总额先覆盖第 1 期，剩余再覆盖第 2 期，
    依次类推；每期得到 ``covered``（已覆盖额）与 ``uncovered``（未覆盖额 =
    期次金额 − 已覆盖额）。**只有到期日严格早于 ``as_of`` 的未覆盖额计入
    ``overdue``（逾期额）**，到期日当天不计逾期。

    计算只读取 ``payments`` / ``payment_reversals``（沿用现有净已收口径），
    **从不改写原计划**；收款被冲销后净已收回降，较后期次会重新打开（uncovered
    回升），计划行本身保持创建时的期次与金额不变。
    """

    invoice_id = int(invoice["id"])
    currency = invoice["currency"]
    unit = _minor_unit(int(invoice["currency_digits"]))
    gross_raw = Decimal(invoice["total_gross"])
    net_paid_raw = _paid_total(conn, invoice_id)
    item_amounts = [Decimal(r["amount"]) for r in item_rows]

    with money_context([gross_raw, net_paid_raw, *item_amounts], len(item_rows)):
        gross = gross_raw.quantize(unit)
        net_paid = net_paid_raw.quantize(unit)

        as_of_text = as_of.isoformat()
        remaining_credit = net_paid
        items: list[dict[str, Any]] = []
        covered_total = Decimal("0")
        uncovered_total = Decimal("0")
        overdue_total = Decimal("0")
        for r, raw_amount in zip(item_rows, item_amounts):
            amount = raw_amount.quantize(unit)
            covered = remaining_credit if remaining_credit < amount else amount
            if covered > 0:
                remaining_credit -= covered
            uncovered = amount - covered
            overdue = uncovered if r["due_date"] < as_of_text else Decimal("0").quantize(unit)
            covered_total += covered
            uncovered_total += uncovered
            overdue_total += overdue
            items.append(
                {
                    "installment": int(r["installment"]),
                    "due_date": r["due_date"],
                    "amount": format(amount, "f"),
                    "covered": format(covered, "f"),
                    "uncovered": format(uncovered, "f"),
                    "overdue": format(overdue, "f"),
                }
            )

        covered_total = covered_total.quantize(unit)
        uncovered_total = uncovered_total.quantize(unit)
        overdue_total = overdue_total.quantize(unit)

    return {
        "plan_id": plan_id,
        "invoice_id": invoice_id,
        "currency": currency,
        "created_at": created_at,
        "as_of": as_of_text,
        "total_gross": format(gross, "f"),
        "net_paid": format(net_paid, "f"),
        "covered_total": format(covered_total, "f"),
        "uncovered_total": format(uncovered_total, "f"),
        "overdue_total": format(overdue_total, "f"),
        "installments": items,
    }


# ---- 原发票贷项凭证 ----


def _credited_totals(
    conn: sqlite3.Connection, invoice_id: int
) -> dict[int, tuple[Decimal, Decimal]]:
    """该发票各原行**截至当前**的历次抵扣（净额, 税额）合计，用 Decimal 逐行求和。

    不使用 SQLite 浮点 SUM 聚合：凭证头与凭证行各自存放字符串金额，在 Python 侧
    按原行号累加。从未被抵扣过的行不出现在返回字典中（由调用方按 0 处理）。
    """

    rows = conn.execute(
        """
        SELECT l.line_no AS line_no, l.net AS net, l.tax AS tax
        FROM credit_memo_lines l
        JOIN credit_memos m ON m.id = l.memo_id
        WHERE m.invoice_id = ?
        """,
        (invoice_id,),
    ).fetchall()
    parsed = [(int(r["line_no"]), Decimal(r["net"]), Decimal(r["tax"])) for r in rows]
    totals: dict[int, tuple[Decimal, Decimal]] = {}
    with money_context([a for _ln, na, ta in parsed for a in (na, ta)], len(parsed)):
        for line_no, net, tax in parsed:
            prev_net, prev_tax = totals.get(line_no, (Decimal("0"), Decimal("0")))
            totals[line_no] = (prev_net + net, prev_tax + tax)
    return totals


def create_credit_memo(
    conn: sqlite3.Connection,
    invoice_id: int,
    items: list[tuple[int, Decimal]],
) -> dict[str, Any]:
    """为已保存发票创建一张可含多行的部分抵扣贷项凭证：校验与写入在**同一事务**。

    ``items`` 为 ``(line_no, net)`` 列表（调用方/schema 层已保证非空且行号不重复，
    事务内再防御性校验一次）。``BEGIN IMMEDIATE`` 事务内完成全部业务校验，任一不
    满足都回滚、**不留下任何凭证行**：

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 行号重复、引用的原行不存在、原行净额不为正（净额 ≤ 0 的行不能用本接口抵扣）、
      本次抵扣净额 ≤ 0、不是发票币种最小单位的正整数倍、累计抵扣将超过原行净额
      -> ``ValueError``

    每行本次抵扣税额口径（全程 Decimal 高精度，最后按币种最小单位 HALF_UP）：

        累计应记税额 = round(原行已确认税额 × 抵扣后累计净额 / 原行净额)
        本次抵扣税额 = 累计应记税额 − 历次已记税额

    因此当 ``抵扣后累计净额 == 原行净额``（完全抵扣）时，累计应记税额必然恰好等于
    原行已确认税额，**原行税额被恰好抵完**；多次部分抵扣的税额之和也恒等于该累计
    口径下的应记值。

    原发票（``invoices`` / ``invoice_lines``）与收款流水（``payments`` /
    ``payment_reversals``）在整个过程中保持原样，凭证只追加到独立的两张表。
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT id, currency, currency_digits FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        # 防御性再校验：明细非空且行号不得重复（schema 层通常已拦截）
        if not items:
            raise ValueError("贷项凭证明细不能为空")
        line_nos = [line_no for line_no, _raw in items]
        if len(line_nos) != len(set(line_nos)):
            dupes = sorted(
                {line_no for line_no in line_nos if line_nos.count(line_no) > 1}
            )
            raise ValueError(f"贷项凭证明细包含重复行号：{dupes}")

        unit = _minor_unit(int(inv["currency_digits"]))
        original_rows = {
            int(r["line_no"]): r
            for r in conn.execute(
                "SELECT line_no, net, tax FROM invoice_lines WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchall()
        }
        prev_totals = _credited_totals(conn, invoice_id)

        # 保持请求顺序（而非按行号排序）：逐行展示的行序与提交一致
        result_lines: list[dict[str, Any]] = []
        memo_net = Decimal("0")
        memo_tax = Decimal("0")
        memo_gross = Decimal("0")
        # 比例摊算（税额 × 累计净额 ÷ 原净额）的精度同样按本次涉及金额量级放大，
        # 大额原行（如 30 位整数净额）的摊算与量化不被默认 28 位精度截断
        prec_amounts = [raw_net for _line_no, raw_net in items]
        for r in original_rows.values():
            prec_amounts += [Decimal(r["net"]), Decimal(r["tax"])]
        for prev_net, prev_tax in prev_totals.values():
            prec_amounts += [prev_net, prev_tax]
        with money_context(prec_amounts, len(items)):
            for line_no, raw_net in items:
                original = original_rows.get(line_no)
                if original is None:
                    raise ValueError(
                        f"原发票 {invoice_id} 不存在行号 {line_no}，无法抵扣"
                    )
                original_net = Decimal(original["net"])
                original_tax = Decimal(original["tax"])
                if original_net <= 0:
                    raise ValueError(
                        f"原发票行 {line_no} 的净额 {format(original_net, 'f')} 不为正，"
                        f"贷项凭证只能抵扣正净额行"
                    )
                if raw_net <= 0:
                    raise ValueError(
                        f"行 {line_no} 的本次抵扣净额必须大于 0，收到 {raw_net}"
                    )
                if (raw_net / unit) != (raw_net / unit).to_integral_value():
                    raise ValueError(
                        f"行 {line_no} 的本次抵扣净额 {raw_net} 不是 "
                        f"{inv['currency']} 最小单位 {format(unit, 'f')} 的整数倍"
                    )
                net = raw_net.quantize(unit)

                prev_net, prev_tax = prev_totals.get(line_no, (Decimal("0"), Decimal("0")))
                cumulative_net = prev_net + net
                if cumulative_net > original_net:
                    raise ValueError(
                        f"行 {line_no} 本次抵扣 {format(net, 'f')} 后累计抵扣将达 "
                        f"{format(cumulative_net, 'f')}，超过原行净额 "
                        f"{format(original_net, 'f')}（历次已抵扣 {format(prev_net, 'f')}），"
                        f"未写入任何凭证"
                    )

                cumulative_tax = (
                    original_tax * cumulative_net / original_net
                ).quantize(unit, rounding=ROUND_HALF_UP)
                if cumulative_net == original_net:
                    # 不变量保证：完全抵扣时恰好抵完原行已确认税额
                    cumulative_tax = original_tax
                tax = cumulative_tax - prev_tax
                gross = net + tax

                result_lines.append(
                    {
                        "line_no": line_no,
                        "net": format(net, "f"),
                        "tax": format(tax, "f"),
                        "gross": format(gross, "f"),
                        "cumulative_net": format(cumulative_net, "f"),
                        "cumulative_tax": format(cumulative_tax, "f"),
                    }
                )
                memo_net += net
                memo_tax += tax
                memo_gross += gross

        created_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO credit_memos (
                invoice_id, currency, net_total, tax_total, gross_total, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_id,
                inv["currency"],
                format(memo_net, "f"),
                format(memo_tax, "f"),
                format(memo_gross, "f"),
                created_at,
            ),
        )
        memo_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO credit_memo_lines (
                memo_id, line_no, net, tax, gross, cumulative_net, cumulative_tax
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    memo_id,
                    line["line_no"],
                    line["net"],
                    line["tax"],
                    line["gross"],
                    line["cumulative_net"],
                    line["cumulative_tax"],
                )
                for line in result_lines
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "id": memo_id,
        "invoice_id": invoice_id,
        "currency": inv["currency"],
        "created_at": created_at,
        "net_total": format(memo_net, "f"),
        "tax_total": format(memo_tax, "f"),
        "gross_total": format(memo_gross, "f"),
        "lines": result_lines,
    }


def get_credit_memos(
    conn: sqlite3.Connection, invoice_id: int
) -> list[dict[str, Any]]:
    """按原发票查询全部贷项凭证（按凭证 id 升序，凭证行按行号升序）。"""

    memo_rows = list(
        conn.execute(
            """
            SELECT * FROM credit_memos
            WHERE invoice_id = ? ORDER BY id
            """,
            (invoice_id,),
        ).fetchall()
    )
    memos: list[dict[str, Any]] = []
    for m in memo_rows:
        line_rows = conn.execute(
            "SELECT * FROM credit_memo_lines WHERE memo_id = ? ORDER BY line_no",
            (m["id"],),
        ).fetchall()
        memos.append(
            {
                "id": int(m["id"]),
                "invoice_id": int(m["invoice_id"]),
                "currency": m["currency"],
                "created_at": m["created_at"],
                "net_total": m["net_total"],
                "tax_total": m["tax_total"],
                "gross_total": m["gross_total"],
                "lines": [
                    {
                        "line_no": int(r["line_no"]),
                        "net": r["net"],
                        "tax": r["tax"],
                        "gross": r["gross"],
                        "cumulative_net": r["cumulative_net"],
                        "cumulative_tax": r["cumulative_tax"],
                    }
                    for r in line_rows
                ],
            }
        )
    return memos


def credit_memo_overview(
    conn: sqlite3.Connection, invoice: sqlite3.Row
) -> dict[str, Any]:
    """某张发票的全部贷项凭证视图（供 GET 使用），含逐原行累计抵扣汇总。

    逐原行累计净额/税额直接在 Python 侧用 Decimal 汇总全部凭证行（不走 SQLite
    浮点聚合）；``remaining_net`` 为原行净额减去累计抵扣净额。只列出被凭证引用
    过的原行，按 ``line_no`` 升序。
    """

    memos = get_credit_memos(conn, int(invoice["id"]))
    unit = _minor_unit(int(invoice["currency_digits"]))
    credited = _credited_totals(conn, int(invoice["id"]))

    original_nets = {
        int(r["line_no"]): Decimal(r["net"])
        for r in conn.execute(
            "SELECT line_no, net FROM invoice_lines WHERE invoice_id = ?",
            (invoice["id"],),
        ).fetchall()
    }
    prec_amounts = list(original_nets.values())
    for net_total, tax_total in credited.values():
        prec_amounts += [net_total, tax_total]
    with money_context(prec_amounts, len(credited)):
        credited_net = sum((net for net, _tax in credited.values()), Decimal("0"))
        credited_tax = sum((tax for _net, tax in credited.values()), Decimal("0"))

        line_credits: list[dict[str, Any]] = []
        for line_no in sorted(credited):
            net_total, tax_total = credited[line_no]
            net_total = net_total.quantize(unit)
            tax_total = tax_total.quantize(unit)
            line_credits.append(
                {
                    "line_no": line_no,
                    "credited_net": format(net_total, "f"),
                    "credited_tax": format(tax_total, "f"),
                    "credited_gross": format(net_total + tax_total, "f"),
                    "remaining_net": format(
                        original_nets.get(line_no, Decimal("0")) - net_total, "f"
                    ),
                }
            )

        credited_net = credited_net.quantize(unit)
        credited_tax = credited_tax.quantize(unit)
        credited_gross = (credited_net + credited_tax).quantize(unit)

    return {
        "invoice_id": int(invoice["id"]),
        "currency": invoice["currency"],
        "total_net": invoice["total_net"],
        "total_tax": invoice["total_tax"],
        "total_gross": invoice["total_gross"],
        "credit_count": len(memos),
        "credited_net": format(credited_net, "f"),
        "credited_tax": format(credited_tax, "f"),
        "credited_gross": format(credited_gross, "f"),
        "line_credits": line_credits,
        "memos": memos,
    }


# ---- 应收账龄汇总 ----


# 账龄桶键（顺序即返回顺序）：无到期日单列，有到期日按查询日与到期日之差
AGING_BUCKET_KEYS: tuple[str, ...] = (
    "no_due_date",
    "current",
    "overdue_1_30",
    "overdue_31_60",
    "overdue_61_90",
    "overdue_91_plus",
)


def _aging_bucket(due_date: str | None, as_of: date) -> str:
    """按查询日与到期日之差决定一张发票唯一落入的账龄桶。

    到期日当天（差 0）及更晚（差为负）计入 ``current``（未到期，含到期当日）；
    逾期天数（正整数）按 1~30 / 31~60 / 61~90 / 91+ 分桶。
    """

    if due_date is None:
        return "no_due_date"
    overdue_days = (as_of - date.fromisoformat(due_date)).days
    if overdue_days <= 0:
        return "current"
    if overdue_days <= 30:
        return "overdue_1_30"
    if overdue_days <= 60:
        return "overdue_31_60"
    if overdue_days <= 90:
        return "overdue_61_90"
    return "overdue_91_plus"


def receivables_aging_summary(
    conn: sqlite3.Connection, as_of: date
) -> dict[str, Any]:
    """按发票币种汇总查询日 UTC 结束时的应收账龄。

    纳入条件（全部满足）：

    * ``created_at <= as_of 当日 23:59:59Z``（查询日 UTC 结束时已创建，字符串
      字典序闭区间比较，与库内秒精度 UTC 文本同构）；
    * 含税总额 ``total_gross`` 严格为正（负数冲红发票不纳入）；
    * 截至该秒的未收额（含税总额 − 净入账合计，净入账 = 截止前收款 − 截止前
      冲销，闭区间含当秒）严格大于 0。

    每张纳入发票的**全部未收额只进入一个账龄桶**（见 :func:`_aging_bucket`）。
    按发票币种分别汇总、**不跨币种相加**：返回按币种代码升序排列的列表，每个
    币种给出总未收额与六个桶各自的发票数、未收合计（空桶也返回 0）。

    **混合保存精度**：同一币种代码下各发票的入库小数位可能不同（币种精度配置
    变更后旧发票保留旧位数、新发票按新位数保存）。每张发票的未收额仍只按**该
    发票自己入库时的** ``currency_digits`` 计算（含税总额与净入账都在该发票单位
    上取整），绝不用其他发票的位数舍入；随后在 Python 侧用 Decimal 原样累加，
    输出时才按**该组实际参与发票中最高的入库小数位**格式化——币种合计与回显的
    ``currency_digits`` 取该币种全部参与发票的最高位数，单个账龄桶取落入该桶的
    参与发票最高位数（空桶无参与记录，沿用币种位数）。因此合计既不会把 0.55
    舍成 1，也与发票插入顺序无关；各桶未收额按 Decimal 求和恒等于币种总未收额。
    """

    cutoff_text = f"{as_of.isoformat()}T23:59:59Z"
    rows = list(
        conn.execute(
            "SELECT id, currency, currency_digits, due_date, total_gross, created_at"
            " FROM invoices WHERE created_at <= ? ORDER BY id",
            (cutoff_text,),
        ).fetchall()
    )

    paid_map = _paid_totals_as_of(conn, [int(r["id"]) for r in rows], cutoff_text)

    # 每个币种维护：参与发票的最高入库位数、总未收额、各桶发票数与未收合计；
    # 桶位数单独记录为该桶实际参与发票的最高入库位数（空桶沿用币种位数）。
    # 用 max（而非首条记录的位数）决定组精度，结果不受发票插入顺序影响。
    digits_by_currency: dict[str, int] = {}
    total_outstanding: dict[str, Decimal] = {}
    invoice_counts: dict[str, int] = {}
    bucket_counts: dict[str, dict[str, int]] = {}
    bucket_amounts: dict[str, dict[str, Decimal]] = {}
    bucket_digits: dict[str, dict[str, int]] = {}

    def _ensure_currency(currency: str) -> None:
        if currency not in digits_by_currency:
            digits_by_currency[currency] = 0
            total_outstanding[currency] = Decimal("0")
            invoice_counts[currency] = 0
            bucket_counts[currency] = {key: 0 for key in AGING_BUCKET_KEYS}
            bucket_amounts[currency] = {
                key: Decimal("0") for key in AGING_BUCKET_KEYS
            }
            bucket_digits[currency] = {key: 0 for key in AGING_BUCKET_KEYS}

    grosses = [Decimal(r["total_gross"]) for r in rows]
    currencies: list[dict[str, Any]] = []
    with money_context([*grosses, *paid_map.values()], len(rows)):
        for r, gross in zip(rows, grosses):
            currency = r["currency"]
            # 未收额只按该发票自己的入库精度计算，不沿用同币种其他发票的位数
            digits = int(r["currency_digits"])
            unit = _minor_unit(digits)
            if gross <= 0:
                continue
            paid = paid_map.get(int(r["id"]), Decimal("0")).quantize(unit)
            outstanding = (gross - paid).quantize(unit)
            if outstanding <= 0:
                continue

            _ensure_currency(currency)
            # 组精度随参与记录动态取最高位数，与记录遍历（插入）顺序无关
            digits_by_currency[currency] = max(digits_by_currency[currency], digits)
            bucket = _aging_bucket(r["due_date"], as_of)
            bucket_digits[currency][bucket] = max(
                bucket_digits[currency][bucket], digits
            )
            total_outstanding[currency] += outstanding
            invoice_counts[currency] += 1
            bucket_counts[currency][bucket] += 1
            bucket_amounts[currency][bucket] += outstanding

        for currency in sorted(digits_by_currency):
            currency_digits = digits_by_currency[currency]
            unit = _minor_unit(currency_digits)
            currencies.append(
                {
                    "currency": currency,
                    "currency_digits": currency_digits,
                    "invoice_count": invoice_counts[currency],
                    "total_outstanding": format(
                        total_outstanding[currency].quantize(unit), "f"
                    ),
                    "buckets": [
                        {
                            "bucket": key,
                            "invoice_count": bucket_counts[currency][key],
                            # 非空桶按该桶参与发票的最高入库位数格式化；
                            # 空桶无参与记录，沿用币种位数
                            "outstanding": format(
                                bucket_amounts[currency][key].quantize(
                                    _minor_unit(
                                        bucket_digits[currency][key]
                                        if bucket_counts[currency][key] > 0
                                        else currency_digits
                                    )
                                ),
                                "f",
                            ),
                        }
                        for key in AGING_BUCKET_KEYS
                    ],
                }
            )

    return {
        "as_of": as_of.isoformat(),
        "cutoff_at": cutoff_text,
        "currencies": currencies,
    }


# ---- 期间税额归集 ----


def _normalize_tax_rate(rate_text: str) -> tuple[Decimal, str]:
    """税率按**数值**规范化，返回 ``(数值 Decimal, 规范文本)``。

    ``"0.13"``、``"0.130"`` 数值相同即视为同一税率，归入同一分组；规范文本取
    ``Decimal.normalize()`` 后的无指数形式（``0.130 -> "0.13"``、``0.00 -> "0"``）。
    """

    value = Decimal(rate_text)
    canonical = value - Decimal(0)  # -0 归一为 +0
    return value, format(canonical.normalize(), "f")


def tax_ledger_summary(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, Any]:
    """按 UTC 自然日闭区间归集期间开票与贷项抵减，按原币种 + 原发票行税率分组。

    期间边界（均为 UTC，闭区间）：

    * ``start 00:00:00Z``（含）~ ``end 23:59:59Z``（含），与库内秒精度 UTC
      文本同构，直接用字符串字典序比较；
    * **发票行**按发票保存时间（``invoices.created_at``）选取；
    * **贷项凭证行**按**凭证创建时间**（``credit_memos.created_at``）选取——即使
      对应发票更早期开票，抵减也计入凭证所在期间；凭证行税率取自被抵扣的原发票行。

    分组键为 ``(原币种, 规范化税率文本)``：税率按数值规范化（``0.13`` 与
    ``0.130`` 同组），不同币种绝不合并。金额只取发票行的**原币列**
    （``net/tax/gross``），不读取 ``conv_*`` 换算列；逐行在 Python 侧用 Decimal
    累计（不走 SQLite 浮点聚合），每组分别给出开票（``invoiced``）、抵减
    （``credited``）与净额（``net = 开票 − 抵减``）的未税/税/含税金额。

    **混合保存精度**：同币种同税率的组可能由不同入库小数位的发票行构成（币种
    精度配置变更后旧发票保留旧位数、新发票按新位数保存）。各行金额按自身发票
    入库精度保存并原样 Decimal 累加，输出时按**该组实际参与记录中最高的入库
    小数位**统一格式化（``currency_digits`` 回显该最高位数）；**贷项凭证行同样
    参与组精度判定**——即使组内发票行都是 0 位，本期有 2 位精度的凭证抵减时，
    该组也按 2 位展示，抵减额不会被抹零。组精度由参与记录取 ``max`` 而非沿用
    首条记录，查询结果与发票/凭证的插入顺序无关；``invoiced`` 与 ``credited``
    在同一组单位上取整后相减得到 ``net``，三列严格满足 ``net = invoiced −
    credited``。只有本期抵减、本期未开票的分组（凭证对应更早期发票）同样列出。
    分组固定按币种代码升序、税率**数值**升序排列；期间内无任何行时分组为空。
    """

    period_start = f"{start.isoformat()}T00:00:00Z"
    period_end = f"{end.isoformat()}T23:59:59Z"

    # 期间内保存的发票行（只用原币列，不读 conv_* 换算列）
    invoice_rows = conn.execute(
        """
        SELECT i.currency AS currency, i.currency_digits AS currency_digits,
               l.tax_rate AS tax_rate, l.net AS net, l.tax AS tax, l.gross AS gross
        FROM invoice_lines l
        JOIN invoices i ON i.id = l.invoice_id
        WHERE i.created_at >= ? AND i.created_at <= ?
        """,
        (period_start, period_end),
    ).fetchall()

    # 期间内创建的贷项凭证行；税率取自被抵扣的原发票行，币种取自原发票，
    # 选取只看凭证自身创建时间——原发票更早开票不影响其计入本期抵减
    credit_rows = conn.execute(
        """
        SELECT i.currency AS currency, i.currency_digits AS currency_digits,
               ol.tax_rate AS tax_rate, l.net AS net, l.tax AS tax, l.gross AS gross
        FROM credit_memo_lines l
        JOIN credit_memos m ON m.id = l.memo_id
        JOIN invoices i ON i.id = m.invoice_id
        JOIN invoice_lines ol ON ol.invoice_id = i.id AND ol.line_no = l.line_no
        WHERE m.created_at >= ? AND m.created_at <= ?
        """,
        (period_start, period_end),
    ).fetchall()

    # key=(币种, 规范税率文本) -> 累加器；金额逐行 Decimal 求和，不使用浮点聚合。
    # currency_digits 保存该组**全部参与记录（发票行与贷项凭证行）中最高的入库
    # 小数位**，随每次累加取 max：组精度不沿用首条记录，与插入顺序无关。
    accum: dict[tuple[str, str], dict[str, Any]] = {}

    def _accumulate(rows: list[sqlite3.Row], side: str) -> None:
        for r in rows:
            currency = r["currency"]
            digits = int(r["currency_digits"])
            rate_value, rate_text = _normalize_tax_rate(r["tax_rate"])
            key = (currency, rate_text)
            bucket = accum.get(key)
            if bucket is None:
                bucket = {
                    "currency": currency,
                    "currency_digits": digits,
                    "rate_value": rate_value,
                    "invoiced": [Decimal("0"), Decimal("0"), Decimal("0")],
                    "credited": [Decimal("0"), Decimal("0"), Decimal("0")],
                }
                accum[key] = bucket
            # 贷项凭证行同样参与精度判定：抵减侧记录的入库位数也会抬升组精度
            bucket["currency_digits"] = max(bucket["currency_digits"], digits)
            amounts = bucket[side]
            amounts[0] += Decimal(r["net"])
            amounts[1] += Decimal(r["tax"])
            amounts[2] += Decimal(r["gross"])

    # 税率也计入精度量级：规范化（normalize）与累计、量化同一局部精度策略
    prec_amounts: list[Decimal] = []
    for r in list(invoice_rows) + list(credit_rows):
        prec_amounts += [
            Decimal(r["net"]),
            Decimal(r["tax"]),
            Decimal(r["gross"]),
            Decimal(r["tax_rate"]),
        ]
    with money_context(prec_amounts, len(prec_amounts)):
        _accumulate(list(invoice_rows), "invoiced")
        _accumulate(list(credit_rows), "credited")

        groups: list[dict[str, Any]] = []
        for key in sorted(accum, key=lambda k: (k[0], accum[k]["rate_value"])):
            bucket = accum[key]
            unit = _minor_unit(bucket["currency_digits"])
            invoiced = [v.quantize(unit) for v in bucket["invoiced"]]
            credited = [v.quantize(unit) for v in bucket["credited"]]
            net_amounts = [invoiced[i] - credited[i] for i in range(3)]

            def _block(values: list[Decimal]) -> dict[str, str]:
                return {
                    "net": format(values[0], "f"),
                    "tax": format(values[1], "f"),
                    "gross": format(values[2], "f"),
                }

            groups.append(
                {
                    "currency": bucket["currency"],
                    "currency_digits": bucket["currency_digits"],
                    "tax_rate": key[1],
                    "invoiced": _block(invoiced),
                    "credited": _block(credited),
                    "net": _block(net_amounts),
                }
            )

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "period_start": period_start,
        "period_end": period_end,
        "groups": groups,
    }
