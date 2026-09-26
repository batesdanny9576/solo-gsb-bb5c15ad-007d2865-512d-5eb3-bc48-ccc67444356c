"""收款余额读取链路的分段取数测试（DB 层，仅依赖标准库 + app.calc/app.db）。

覆盖：对发票 ID 集合查收款、对收款 ID 集合查冲销时按固定批量分段取数——
* 单条语句的绑定参数数始终低于 SQLite 上限（999），截止时点参数计入预算；
* 空 ID 集合不发起任何查询；
* 超大 ID 集合（跨多个分段）下，逾期列表、应收账龄汇总、收款概览、历史快照、
  来款分配余额读取与按批次撤销的结果，与逐张发票独立查询的小批量参照结果一致；
* 分段后仍在同一足量局部精度内用 Decimal 逐笔合并，UTC 截止秒闭区间口径不变。

运行：python -m unittest tests.test_db_batching
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from app import db


class _TrackingConnection:
    """包装真实连接，记录 execute 调用次数与单语句最大绑定参数数。

    sqlite3.Connection 用 C 实现、无法继承，这里用组合 + __getattr__ 透传；
    db 层只读属性（row_factory 等）、上下文管理与事务语句都透传给真实连接。
    """

    def __init__(self, raw) -> None:  # noqa: ANN001
        self._raw = raw
        self.execute_count = 0
        self.max_bind_count = 0

    def execute(self, sql, params=()):  # noqa: ANN001
        self.execute_count += 1
        # sqlite3 支持的可迭代绑定：tuple/list/dict
        try:
            bind_count = len(params)
        except TypeError:
            bind_count = sum(1 for _ in params)
        self.max_bind_count = max(self.max_bind_count, bind_count)
        return self._raw.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._raw, name)


def _q2(text: str) -> Decimal:
    return Decimal(text).quantize(Decimal("0.01"))


def _q0(text: str) -> Decimal:
    return Decimal(text).quantize(Decimal("1"))


class ChunkedBalanceReadTests(unittest.TestCase):
    N_PER_CURRENCY = 1005  # 跨 900 大小的分段边界（900 + 105）

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "batch.db")
        os.environ["DB_PATH"] = self.db_path
        raw = db.connect(self.db_path)
        db.init_db(raw)
        self._bulk_seed(raw)
        raw.commit()
        self.conn = _TrackingConnection(raw)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    # ---- 造数：两种币种、固定金额、固定时间，便于逐张参照核对 ----

    def _bulk_seed(self, conn) -> None:  # noqa: ANN001
        """写入 2*N 张发票及收款/冲销流水。

        * 奇数序号发票 USD（2 位），偶数序号发票 JPY（0 位）；
        * 每张发票在 2000-01-01 登记 2 笔收款（USD 10.00/5.00；JPY 1000/500）；
        * 每张发票第 1 笔收款在 2000-01-05 冲销 3.00 / JPY 300；
        * 2000-01-31 再补一笔收款（USD 1.00 / JPY 100），随后 2000-02-05 冲销 1.00/100。
        """

        n = self.N_PER_CURRENCY
        created = "2000-01-01T00:00:00Z"
        invoices: list[tuple] = []
        lines: list[tuple] = []
        for seq in range(1, 2 * n + 1):
            if seq % 2 == 1:
                currency, digits = "USD", 2
                gross = "100.00"
                net, tax = "90.00", "10.00"
            else:
                currency, digits = "JPY", 0
                gross = "10000"
                net, tax = "9000", "1000"
            due = "1999-12-15"  # 相对 2000-02-01 均已逾期
            invoices.append(
                (
                    currency,
                    digits,
                    None,
                    None,
                    None,
                    due,
                    net,
                    tax,
                    gross,
                    None,
                    None,
                    None,
                    0,
                    "test_rule",
                    "{}",
                    created,
                )
            )
            lines.append((seq, 1, None, "0.10", net, tax, tax, gross, None, None, None))
        conn.executemany(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, target_digits,
                exchange_rate, due_date,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            invoices,
        )
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross, conv_net, conv_tax, conv_gross
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            lines,
        )

        payments: list[tuple] = []
        for seq in range(1, 2 * n + 1):
            currency = "USD" if seq % 2 == 1 else "JPY"
            amount1 = "10.00" if currency == "USD" else "1000"
            amount2 = "5.00" if currency == "USD" else "500"
            amount3 = "1.00" if currency == "USD" else "100"
            payments.append((seq, amount1, currency, "2000-01-01T01:00:00Z"))
            payments.append((seq, amount2, currency, "2000-01-02T01:00:00Z"))
            payments.append((seq, amount3, currency, "2000-01-31T01:00:00Z"))
        conn.executemany(
            "INSERT INTO payments (invoice_id, amount, currency, created_at)"
            " VALUES (?, ?, ?, ?)",
            payments,
        )

        # 收款自增 id 与 invoices 对齐：发票 seq 的 3 笔收款 id = 3*(seq-1)+1..3
        reversals: list[tuple] = []
        for seq in range(1, 2 * n + 1):
            currency = "USD" if seq % 2 == 1 else "JPY"
            payment_first = 3 * (seq - 1) + 1
            payment_third = 3 * (seq - 1) + 3
            rev1 = "3.00" if currency == "USD" else "300"
            rev3 = "1.00" if currency == "USD" else "100"
            reversals.append(
                (payment_first, rev1, currency, "2000-01-05T01:00:00Z")
            )
            reversals.append(
                (payment_third, rev3, currency, "2000-02-05T01:00:00Z")
            )
        conn.executemany(
            "INSERT INTO payment_reversals (payment_id, amount, currency, created_at)"
            " VALUES (?, ?, ?, ?)",
            reversals,
        )

    # ---- 参照：逐张发票独立查询（小批量口径），不经过分段 IN 取数 ----

    def _reference_paid(self, conn, invoice_id: int, at_text: str | None = None):  # noqa: ANN001
        """单张发票净已收参照：参数化单行查询 + Decimal 合并。"""

        if at_text is None:
            pay_rows = conn.execute(
                "SELECT id, amount FROM payments WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchall()
        else:
            pay_rows = conn.execute(
                "SELECT id, amount FROM payments"
                " WHERE invoice_id = ? AND created_at <= ?",
                (invoice_id, at_text),
            ).fetchall()
        unit = Decimal("0.01") if invoice_id % 2 == 1 else Decimal("1")
        total = Decimal("0")
        for pr in pay_rows:
            pid = int(pr["id"])
            if at_text is None:
                rev_rows = conn.execute(
                    "SELECT amount FROM payment_reversals WHERE payment_id = ?",
                    (pid,),
                ).fetchall()
            else:
                rev_rows = conn.execute(
                    "SELECT amount FROM payment_reversals"
                    " WHERE payment_id = ? AND created_at <= ?",
                    (pid, at_text),
                ).fetchall()
            reversed_sum = sum(
                (Decimal(r["amount"]) for r in rev_rows), Decimal("0")
            ).quantize(unit)
            total += Decimal(pr["amount"]) - reversed_sum
        return total

    def _all_invoice_ids(self) -> list[int]:
        return list(range(1, 2 * self.N_PER_CURRENCY + 1))

    # ---- 空集合不查询 ----

    def test_empty_collections_issue_no_query(self) -> None:
        tracker = _TrackingConnection(db.connect(":memory:"))
        db.init_db(tracker)
        before = tracker.execute_count
        self.assertEqual(db._paid_totals(tracker, []), {})
        self.assertEqual(db._paid_totals_as_of(tracker, [], "2000-01-31T23:59:59Z"), {})
        self.assertEqual(db._reversed_totals(tracker, []), {})
        self.assertEqual(db._reversed_totals(tracker, [], at_text="2000-01-31T23:59:59Z"), {})
        self.assertEqual(db._payment_rows(tracker, []), [])
        self.assertEqual(db._payment_rows(tracker, [], at_text="2000-01-31T23:59:59Z"), [])
        self.assertEqual(db._reversal_rows(tracker, []), [])
        self.assertEqual(db._reversal_rows(tracker, [], at_text="2000-01-31T23:59:59Z"), [])
        self.assertEqual(tracker.execute_count, before)
        tracker.close()

    # ---- 固定批量：绑定参数始终 < 999，且确实发生了分段 ----

    def test_batch_size_fixed_and_below_sqlite_limit(self) -> None:
        self.assertEqual(db._ID_QUERY_BATCH_SIZE, 900)
        ids = self._all_invoice_ids()
        db._paid_totals(self.conn, ids)
        # 2010 张发票的收款 id 约 6030 条 -> 收款 3 段 + 冲销 7 段，必发生分段
        self.assertGreater(self.conn.execute_count, 2)
        # 900 个 ID + 至多 1 个截止参数 = 901，严格低于 SQLite 默认上限 999
        self.assertLessEqual(self.conn.max_bind_count, 901)
        self.assertLess(self.conn.max_bind_count, 999)

    def test_cutoff_parameter_counts_in_budget(self) -> None:
        ids = self._all_invoice_ids()
        db._paid_totals_as_of(self.conn, ids, "2000-01-31T23:59:59Z")
        # 带截止参数的分段语句恰好是 901 个绑定（900 id + 1 截止）
        self.assertEqual(self.conn.max_bind_count, 901)

    # ---- 超大集合：净入账合计与逐张参照完全一致 ----

    def test_paid_totals_match_reference_across_chunks(self) -> None:
        ids = self._all_invoice_ids()
        paid = db._paid_totals(self.conn, ids)
        as_of_paid = db._paid_totals_as_of(self.conn, ids, "2000-01-31T23:59:59Z")
        self.assertEqual(set(paid), set(ids))
        self.assertEqual(set(as_of_paid), set(ids))
        for iid in ids:
            unit = Decimal("0.01") if iid % 2 == 1 else Decimal("1")
            # 当前口径：收款 10+5+1=16，冲销 3（1 月）+1（2 月）=4 -> 净 12
            expected_now = (_q2("12") if iid % 2 == 1 else _q0("1200")).quantize(unit)
            self.assertEqual(paid[iid], expected_now)
            # 截至 2000-01-31 末：2 月冲销不计入 -> 16-3=13
            expected_cutoff = (_q2("13") if iid % 2 == 1 else _q0("1300")).quantize(unit)
            self.assertEqual(as_of_paid[iid], expected_cutoff)
            # 与逐张小批量参照（当前口径与截止口径）一致
            self.assertEqual(
                paid[iid], self._reference_paid(self.conn, iid).quantize(unit)
            )
            self.assertEqual(
                as_of_paid[iid],
                self._reference_paid(
                    self.conn, iid, "2000-01-31T23:59:59Z"
                ).quantize(unit),
            )

    def test_cutoff_closed_second_boundary(self) -> None:
        ids = self._all_invoice_ids()
        # 截止到 2000-01-31T01:00:00Z：第 3 笔收款当秒写入，闭区间须计入；
        # 2000-02-05 的冲销不计入。
        at = "2000-01-31T01:00:00Z"
        paid = db._paid_totals_as_of(self.conn, ids, at)
        for iid in ids:
            unit = Decimal("0.01") if iid % 2 == 1 else Decimal("1")
            expected = (_q2("13") if iid % 2 == 1 else _q0("1300")).quantize(unit)
            self.assertEqual(paid[iid], expected)
        # 再早一秒：第 3 笔收款不计入 -> 10+5-3=12
        paid_before = db._paid_totals_as_of(self.conn, ids, "2000-01-31T00:59:59Z")
        for iid in ids:
            unit = Decimal("0.01") if iid % 2 == 1 else Decimal("1")
            expected = (_q2("12") if iid % 2 == 1 else _q0("1200")).quantize(unit)
            self.assertEqual(paid_before[iid], expected)

    # ---- 逾期列表：分页/排序/余额与参照一致 ----

    def test_overdue_listing_matches_reference(self) -> None:
        items, total = db.list_overdue_invoices(
            self.conn, date(2000, 2, 1), 1, 100
        )
        n_total = 2 * self.N_PER_CURRENCY
        self.assertEqual(total, n_total)
        self.assertEqual(len(items), 100)
        # 按 id 倒序
        self.assertEqual([it["id"] for it in items], list(range(n_total, n_total - 100, -1)))
        # 每页都核对一遍余额
        for page in range(1, total // 100 + 2):
            page_items, _ = db.list_overdue_invoices(
                self.conn, date(2000, 2, 1), page, 100
            )
            for it in page_items:
                iid = it["id"]
                unit = Decimal("0.01") if iid % 2 == 1 else Decimal("1")
                gross = Decimal(it["total_gross"])
                paid_ref = self._reference_paid(self.conn, iid).quantize(unit)
                self.assertEqual(Decimal(it["paid_total"]), paid_ref)
                self.assertEqual(Decimal(it["outstanding"]), (gross - paid_ref).quantize(unit))
                self.assertGreater(Decimal(it["outstanding"]), 0)
        # 逾期查询的余额为**当前**净入账（as_of 只过滤到期日）：净已收 12
        items_early, total_early = db.list_overdue_invoices(
            self.conn, date(2000, 1, 3), 1, 1000
        )
        self.assertEqual(total_early, n_total)
        for it in items_early:
            iid = it["id"]
            if iid % 2 == 1:
                self.assertEqual(it["paid_total"], "12.00")
                self.assertEqual(it["outstanding"], "88.00")
            else:
                self.assertEqual(it["paid_total"], "1200")
                self.assertEqual(it["outstanding"], "8800")

    # ---- 应收账龄汇总：分币种汇总与参照一致 ----

    def test_aging_summary_matches_reference(self) -> None:
        summary = db.receivables_aging_summary(self.conn, date(2000, 2, 1))
        by_ccy = {c["currency"]: c for c in summary["currencies"]}
        self.assertEqual(set(by_ccy), {"USD", "JPY"})
        n = self.N_PER_CURRENCY

        usd = by_ccy["USD"]
        self.assertEqual(usd["currency_digits"], 2)
        self.assertEqual(usd["invoice_count"], n)
        # 每张：gross 100 - 净已收 13 = 87
        self.assertEqual(Decimal(usd["total_outstanding"]), _q2(87 * n))
        overdue_bucket = next(b for b in usd["buckets"] if b["bucket"] == "overdue_31_60")
        self.assertEqual(overdue_bucket["invoice_count"], n)
        self.assertEqual(Decimal(overdue_bucket["outstanding"]), _q2(87 * n))
        empty_buckets = {
            b["bucket"]: b for b in usd["buckets"] if b["invoice_count"] == 0
        }
        self.assertTrue(empty_buckets)
        for b in empty_buckets.values():
            self.assertEqual(b["outstanding"], "0.00")

        jpy = by_ccy["JPY"]
        self.assertEqual(jpy["currency_digits"], 0)
        self.assertEqual(jpy["invoice_count"], n)
        # 每张：10000 - 1300 = 8700
        self.assertEqual(Decimal(jpy["total_outstanding"]), _q0(8700 * n))
        overdue_bucket_j = next(
            b for b in jpy["buckets"] if b["bucket"] == "overdue_31_60"
        )
        self.assertEqual(Decimal(overdue_bucket_j["outstanding"]), _q0(8700 * n))

        # 桶未收合计之和恒等于币种总未收额
        for block, total in ((usd, _q2(87 * n)), (jpy, _q0(8700 * n))):
            bucket_sum = sum(
                (Decimal(b["outstanding"]) for b in block["buckets"]), Decimal("0")
            )
            self.assertEqual(bucket_sum, Decimal(block["total_outstanding"]))
            self.assertEqual(bucket_sum, total)

        # 截止到 1 月 2 日（第 1 笔冲销 1 月 5 日尚未发生）：
        # 净已收 = 10+5=15、未收 = 85 USD / 8500 JPY
        early = db.receivables_aging_summary(self.conn, date(2000, 1, 2))
        early_usd = next(c for c in early["currencies"] if c["currency"] == "USD")
        early_jpy = next(c for c in early["currencies"] if c["currency"] == "JPY")
        self.assertEqual(Decimal(early_usd["total_outstanding"]), _q2(85 * n))
        self.assertEqual(Decimal(early_jpy["total_outstanding"]), _q0(8500 * n))

        # 截止到 1999 年：发票均未创建，无任何币种分组
        none = db.receivables_aging_summary(self.conn, date(1999, 12, 31))
        self.assertEqual(none["currencies"], [])

    # ---- 收款概览：单张发票（内部冲销取数同样走分段助手） ----

    def test_payment_overview_and_snapshot(self) -> None:
        for iid in (1, 2, 3, 2 * self.N_PER_CURRENCY):
            inv = db.get_invoice(self.conn, iid)
            ov = db.payment_overview(self.conn, inv)
            unit = Decimal("0.01") if iid % 2 == 1 else Decimal("1")
            paid_ref = self._reference_paid(self.conn, iid).quantize(unit)
            self.assertEqual(Decimal(ov["paid_total"]), paid_ref)
            self.assertEqual(ov["payment_count"], 3)
            # 每条流水的 reversed_total 与参照一致、按 id 升序
            self.assertEqual([p["id"] for p in ov["payments"]], sorted(p["id"] for p in ov["payments"]))
            for p in ov["payments"]:
                rev_rows = self.conn.execute(
                    "SELECT amount FROM payment_reversals WHERE payment_id = ?",
                    (p["id"],),
                ).fetchall()
                rev_sum = sum((Decimal(r["amount"]) for r in rev_rows), Decimal("0")).quantize(unit)
                self.assertEqual(Decimal(p["reversed_total"]), rev_sum)
                self.assertEqual(
                    Decimal(p["net_amount"]),
                    (Decimal(p["amount"]) - rev_sum).quantize(unit),
                )

            # 历史快照：2000-01-31 当秒（闭区间）与参照一致；2 月冲销不计入
            from datetime import datetime, timezone

            snap = db.payment_snapshot(
                self.conn,
                inv,
                datetime(2000, 1, 31, 1, 0, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(snap["payment_count"], 3)
            self.assertEqual(snap["reversal_count"], 1)
            self.assertEqual(
                Decimal(snap["received_total"]),
                (Decimal("16.00") if iid % 2 == 1 else Decimal("1600")).quantize(unit),
            )
            self.assertEqual(
                Decimal(snap["reversed_total"]),
                (Decimal("3.00") if iid % 2 == 1 else Decimal("300")).quantize(unit),
            )
            self.assertEqual(
                Decimal(snap["net_paid"]),
                (Decimal("13.00") if iid % 2 == 1 else Decimal("1300")).quantize(unit),
            )
            self.assertEqual(
                [r["id"] for r in snap["reversals"]],
                sorted(r["id"] for r in snap["reversals"]),
            )

    # ---- 来款分配：超大候选集合下的余额读取可查询 ----

    def test_allocation_balance_read_with_huge_id_set(self) -> None:
        ids = self._all_invoice_ids()
        # 全部 USD 发票（奇数 id）：每张正未收 87.00；来款 1.00 只抵到期最早一张
        usd_ids = [i for i in ids if i % 2 == 1]
        result = db.allocate_payment(
            self.conn, "USD", Decimal("1.00"), usd_ids
        )
        self.assertEqual(result["allocated_total"], "1.00")
        nonzero = [a for a in result["allocations"] if a["payment_id"] is not None]
        self.assertEqual(len(nonzero), 1)
        self.assertEqual(nonzero[0]["allocated"], "1.00")
        # 所有 USD 发票到期日相同，按 id 升序先抵 id=1
        self.assertEqual(nonzero[0]["invoice_id"], 1)
        self.assertLessEqual(self.conn.max_bind_count, 900)

    # ---- 按批次整批撤销：超大批次下分段取数、幂等回显 ----

    def _seed_tiny_unsettled_invoices(self, conn, n: int) -> list[int]:  # noqa: ANN001
        """再造 n 张含税 0.01 USD、无收款的发票，返回其 id（每张正未收 0.01）。"""

        created = "2000-01-01T00:00:00Z"
        conn.executemany(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, target_digits,
                exchange_rate, due_date,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES ('USD', 2, NULL, NULL, NULL, '1999-12-15',
                      '0.01', '0.00', '0.01', NULL, NULL, NULL,
                      0, 'test_rule', '{}', ?)
            """,
            [(created,)] * n,
        )
        first_id = conn.execute("SELECT MAX(id) AS m FROM invoices").fetchone()["m"] - n + 1
        ids = list(range(first_id, first_id + n))
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross, conv_net, conv_tax, conv_gross
            ) VALUES (?, 1, NULL, '0.00', '0.01', '0.00', '0.00', '0.01',
                      NULL, NULL, NULL)
            """,
            [(iid,) for iid in ids],
        )
        return ids

    def test_allocation_batch_reversal_huge_batch(self) -> None:
        n = self.N_PER_CURRENCY  # 1005 张未收 0.01 的发票 -> 每一张分到 0.01
        tiny_ids = self._seed_tiny_unsettled_invoices(self.conn._raw, n)
        self.conn._raw.commit()

        result = db.allocate_payment(
            self.conn, "USD", (Decimal(n) * Decimal("0.01")).quantize(Decimal("0.01")),
            tiny_ids,
        )
        batch_id = result["batch_id"]
        self.assertEqual(result["allocated_total"], f"{n * 0.01:.2f}")
        nonzero = [a for a in result["allocations"] if a["payment_id"] is not None]
        self.assertEqual(len(nonzero), n)

        self.conn.execute_count = 0
        self.conn.max_bind_count = 0
        reversal, created = db.reverse_allocation_batch(self.conn, batch_id)
        self.assertTrue(created)
        self.assertEqual(reversal["reversal_count"], n)
        self.assertEqual(
            Decimal(reversal["reversed_total"]),
            (Decimal(n) * Decimal("0.01")).quantize(Decimal("0.01")),
        )
        # 对收款 id 集合取冲销在事务内同样分段：单语句绑定 < 999
        self.assertLessEqual(self.conn.max_bind_count, 900)

        # 重复请求幂等：不新增冲销、合计恒定（此时 prior reversal id 集合
        # 同样需要分段取回）
        self.conn.execute_count = 0
        self.conn.max_bind_count = 0
        again, created_again = db.reverse_allocation_batch(self.conn, batch_id)
        self.assertFalse(created_again)
        self.assertEqual(again["reversed_total"], reversal["reversed_total"])
        self.assertEqual(again["reversal_count"], n)
        self.assertLessEqual(self.conn.max_bind_count, 900)

        # 撤销后这些发票净入账归零、未收恢复 0.01
        paid = db._paid_totals(self.conn, tiny_ids)
        self.assertEqual({iid: paid[iid] for iid in tiny_ids}, {iid: Decimal("0.00") for iid in tiny_ids})

    # ---- 冲销按收款 ID 集合分段：大量收款手工冲销后合计与参照一致 ----

    def test_reversed_totals_huge_payment_set(self) -> None:
        # 全部收款 id（每张发票 3 笔）
        payment_ids = list(range(1, 2 * self.N_PER_CURRENCY * 3 + 1))
        totals = db._reversed_totals(self.conn, payment_ids)
        self.assertLessEqual(self.conn.max_bind_count, 900)
        # 第 1、3 笔收款各有冲销；第 2 笔无
        for seq in range(1, 2 * self.N_PER_CURRENCY + 1):
            p1 = 3 * (seq - 1) + 1
            p2 = 3 * (seq - 1) + 2
            p3 = 3 * (seq - 1) + 3
            unit = Decimal("0.01") if seq % 2 == 1 else Decimal("1")
            self.assertEqual(
                totals.get(p1),
                (Decimal("3.00") if seq % 2 == 1 else Decimal("300")).quantize(unit),
            )
            self.assertNotIn(p2, totals)
            self.assertEqual(
                totals.get(p3),
                (Decimal("1.00") if seq % 2 == 1 else Decimal("100")).quantize(unit),
            )


if __name__ == "__main__":
    unittest.main()
