"""API 数据模型。金额一律以 *字符串* 收发，杜绝 float 精度问题。"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .duplicates import MAX_INVOICE_NUMBER_CHARS, is_blank

_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$|^-?\.\d+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_UTC_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse_iso_date(value: str, field: str = "日期") -> date:
    """严格解析 ISO 8601 **日期**（YYYY-MM-DD）。

    只接受日期形状，拒绝带时间的字符串（如 ``2026-09-23T00:00:00Z``）与
    不存在的日期（如 ``2026-02-30``）；失败抛 ``ValueError``（API 层映射为 422）。
    """

    text = value.strip()
    if not _ISO_DATE_RE.match(text):
        raise ValueError(f"{field} 必须是 ISO 日期（YYYY-MM-DD），收到 {value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法日期（YYYY-MM-DD），收到 {value!r}") from exc


def parse_utc_second(value: str, field: str = "at") -> datetime:
    """严格解析 UTC 秒精度时间 ``YYYY-MM-DDTHH:MM:SSZ``。

    只接受这一种形状：必须带 ``T`` 分隔符与 ``Z`` 后缀、秒精度（拒绝
    ``+00:00`` 偏移、带毫秒/微秒、空格分隔等变体），且日期/时间必须真实存在
    （拒绝 ``2026-13-01T00:00:00Z``、``2026-02-30T00:00:00Z`` 等）。
    失败抛 ``ValueError``（API 层映射为 422），成功返回 UTC 感知时间（秒精度）。
    """

    text = value.strip()
    if not _ISO_UTC_SECOND_RE.match(text):
        raise ValueError(
            f"{field} 必须是 UTC 秒精度时间（YYYY-MM-DDTHH:MM:SSZ），收到 {value!r}"
        )
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            f"{field} 不是合法的 UTC 秒精度时间（YYYY-MM-DDTHH:MM:SSZ），收到 {value!r}"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


class LineIn(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    net: str = Field(description="不含税净额，币种最小单位的整数倍，如 '100.00'")
    tax_rate: str = Field(description="税率，0~1 的小数字符串，如 '0.13'")

    @field_validator("net", "tax_rate")
    @classmethod
    def _check_decimal(cls, v: str, info: Any) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"{info.field_name} 必须是十进制数字字符串，收到 {v!r}")
        return v


class InvoiceCreate(BaseModel):
    currency: str = Field(description="发票币种 ISO 4217 代码，如 USD / CNY / JPY")
    lines: list[LineIn] = Field(min_length=1)
    target_currency: str | None = Field(
        default=None, description="可选：换算目标币种；提供时必须同时给 exchange_rate"
    )
    exchange_rate: str | None = Field(
        default=None, description="汇率，1 单位发票币种 = exchange_rate 单位目标币种"
    )
    due_date: str | None = Field(
        default=None, description="可选：到期日，ISO 8601 日期 YYYY-MM-DD（如 '2026-10-01'）"
    )

    @field_validator("currency", "target_currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper().strip() if v is not None else None

    @field_validator("due_date")
    @classmethod
    def _check_due_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return parse_iso_date(v, "due_date").isoformat()

    @field_validator("exchange_rate")
    @classmethod
    def _check_rate(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"exchange_rate 必须是十进制数字字符串，收到 {v!r}")
        return v


# ---- 出参（宽松模型，extra 允许，主要服务于 OpenAPI 文档）----

class AllocationOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    delta_units: int
    remainder: str
    rank: int


class RoundingOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str
    minor_unit: str
    residual_units: int
    residual_amount: str
    method: str
    allocations: list[AllocationOut]


class ConvertedLineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    net: str
    tax: str
    gross: str


class LineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    description: str | None = None
    tax_rate: str
    currency: str
    net: str
    exact_tax: str
    tax: str
    gross: str
    converted: ConvertedLineOut | None = None


class TotalsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    total_net: str
    total_tax: str
    total_gross: str


class FxOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    target_currency: str
    target_digits: int
    exchange_rate: str
    rate_direction: str
    total_net: str
    total_tax: str
    total_gross: str
    residual_units: int
    residual_unit: str
    allocations: list[AllocationOut]


class InvoiceResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    rule_id: str
    currency: str
    currency_digits: int
    lines: list[LineOut]
    totals: TotalsOut
    rounding: RoundingOut
    fx: FxOut | None = None
    due_date: str | None = None
    created_at: str
    recompute_matched: bool


class InvoiceSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    currency: str
    target_currency: str | None = None
    total_net: str
    total_tax: str
    total_gross: str
    line_count: int
    created_at: str


class PagedInvoices(BaseModel):
    items: list[InvoiceSummary]
    page: int
    page_size: int
    total: int
    pages: int


class Health(BaseModel):
    status: str
    database: str


# ---- 收款登记 ----

class PaymentCreate(BaseModel):
    amount: str = Field(
        description="收款金额，发票币种的十进制字符串，必须大于 0 且为币种最小单位的整数倍，如 '33.11'"
    )

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v


class PaymentOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str
    reversed_total: str
    net_amount: str


class PaymentResult(PaymentOut):
    """登记收款成功后的响应：收款记录 + 最新余额。"""

    paid_total: str
    outstanding: str


class PaymentAllocationCreate(BaseModel):
    """一笔来款在多张同币种待结发票间的分配请求。"""

    currency: str = Field(
        description="来款币种 ISO 4217 代码；只分配给同币种发票，不做跨币种换算，如 USD / JPY"
    )
    amount: str = Field(
        description=(
            "来款总金额，十进制字符串，必须大于 0；不按单一币种精度统一校验——"
            "按实际抵扣顺序逐张计算分配额，并以每张发票保存的 currency_digits "
            "最小单位校验：整笔可精确分配才成功（如 USD 两位批次 '100.00'、"
            "JPY 零位批次 '1000'；混合精度时两位零头只可落到两位发票）"
        )
    )
    invoice_ids: list[int] = Field(
        min_length=1,
        description="待结发票 ID 列表，不得重复；只处理列表内发票，"
        "实际按到期日升序（无到期日排最后）、同日按 ID 升序逐张抵扣；"
        "负额冲红或已结清（未收余额 ≤ 0）的发票分配 0，不消耗也不增加剩余来款",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v

    @field_validator("invoice_ids")
    @classmethod
    def _check_invoice_ids(cls, v: list[int]) -> list[int]:
        for invoice_id in v:
            if invoice_id <= 0:
                raise ValueError(f"invoice_ids 必须是正整数，收到 {invoice_id}")
        if len(v) != len(set(v)):
            raise ValueError(f"invoice_ids 不得包含重复的发票 ID，收到 {v}")
        return v


class PaymentAllocationItem(BaseModel):
    """单张发票在本次来款中的分配结果（按实际抵扣顺序排列）。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    due_date: str | None = None
    allocated: str
    outstanding: str
    payment_id: int | None = None


class PaymentAllocationResult(BaseModel):
    """来款分配成功后的响应：批次标识、逐发票分配金额与剩余余额。"""

    model_config = ConfigDict(extra="allow")
    batch_id: int
    currency: str
    amount: str
    allocated_total: str
    created_at: str
    allocations: list[PaymentAllocationItem]


class AllocationBatchReversalItem(BaseModel):
    """整批撤销结果中一笔被分配收款的撤销明细（按原分配抵扣顺序）。"""

    model_config = ConfigDict(extra="allow")
    seq: int
    payment_id: int
    invoice_id: int
    allocated: str
    reversed_amount: str
    reversal_id: int | None = None
    reversal_created_at: str | None = None
    net_amount: str
    currency: str


class AllocationBatchReversalResult(BaseModel):
    """按批次整笔撤销来款分配的结果：逐笔撤销金额与批次合计。

    幂等：同一批次重复撤销不会新增冲销——撤销前净入账额（收款 − 已有冲销，
    含手工冲销）已为 0 的收款不再追加冲销，其撤销金额沿用本批次已记录的冲销；
    批次撤销前已被手工冲销完的收款撤销金额为 ``0.00``、``reversal_id`` 为 ``null``。
    """

    model_config = ConfigDict(extra="allow")
    batch_id: int
    currency: str
    created_at: str
    allocated_total: str
    reversed_total: str
    reversal_count: int
    items: list[AllocationBatchReversalItem]


class PaymentOverview(BaseModel):
    """按发票查询的收款记录与未收余额（余额按净入账额 = 收款 − 冲销 计算）。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    currency: str
    total_gross: str
    paid_total: str
    reversed_total: str
    outstanding: str
    payment_count: int
    payments: list[PaymentOut]


# ---- 历史收款快照 ----


class SnapshotPaymentOut(BaseModel):
    """快照中一笔计入截止时点的收款流水（含当秒）。"""

    model_config = ConfigDict(extra="allow")
    id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str
    reversed_total: str
    net_amount: str


class SnapshotReversalOut(BaseModel):
    """快照中一笔计入截止时点的冲销流水（含当秒，须关联快照时点已存在的收款）。"""

    model_config = ConfigDict(extra="allow")
    id: int
    payment_id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str


class PaymentSnapshot(BaseModel):
    """单张发票在指定 UTC 时点的历史收款快照。

    只统计 ``created_at <= at``（截止秒为闭区间，同秒流水全部计入）的收款与
    对应冲销；截止后写入的任何流水都不改变历史结果。所有金额按发票币种最小
    单位用 Decimal 计算，``net_paid = received_total - reversed_total``，
    ``outstanding = total_gross - net_paid``。
    """

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    currency: str
    at: str
    invoice_created_at: str
    total_gross: str
    received_total: str
    reversed_total: str
    net_paid: str
    outstanding: str
    payment_count: int
    reversal_count: int
    payments: list[SnapshotPaymentOut]
    reversals: list[SnapshotReversalOut]


# ---- 收款冲销 ----

class ReversalCreate(BaseModel):
    amount: str = Field(
        description="本次冲销金额，发票币种的十进制字符串；必须大于 0、为币种最小单位的整数倍，"
        "且与历史冲销累计不得超过原收款金额，如 '5.00'"
    )

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v


class ReversalResult(BaseModel):
    """冲销成功后的响应：冲销记录 + 原流水的累计冲销与净入账额。"""

    model_config = ConfigDict(extra="allow")
    id: int
    payment_id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str
    reversed_total: str
    net_amount: str


# ---- 逾期查询 ----


class OverdueInvoice(BaseModel):
    """逾期发票条目：到期日早于 as_of 且仍有未收余额。"""

    model_config = ConfigDict(extra="allow")
    id: int
    currency: str
    due_date: str
    total_gross: str
    paid_total: str
    outstanding: str
    created_at: str


class PagedOverdueInvoices(BaseModel):
    items: list[OverdueInvoice]
    page: int
    page_size: int
    total: int
    pages: int
    as_of: str


# ---- 应收账龄汇总 ----


class AgingBucketOut(BaseModel):
    """单个账龄桶：发票数与未收合计（金额为发票币种十进制字符串）。"""

    model_config = ConfigDict(extra="allow")
    bucket: str
    invoice_count: int
    outstanding: str


class AgingCurrencyOut(BaseModel):
    """单个币种的账龄汇总：各桶发票数/未收合计与该币种总未收额，不跨币种相加。

    混合保存精度下，``currency_digits`` 为该币种全部参与发票的最高入库小数位，
    合计按其格式化；非空桶可按本桶参与发票的最高位数展示（空桶沿用币种位数）。
    """

    model_config = ConfigDict(extra="allow")
    currency: str
    currency_digits: int
    invoice_count: int
    total_outstanding: str
    buckets: list[AgingBucketOut]


class ReceivablesAgingSummary(BaseModel):
    """按查询日 UTC 结束时点（含当秒）的应收账龄汇总，按发票币种分别成列。

    只统计查询日 23:59:59Z 前已创建、含税总额为正且仍有未收额的发票；每张
    发票的全部未收额只进入一个账龄桶（无到期日单列；未到期含到期当日；
    逾期 1~30 / 31~60 / 61~90 / 91 天及以上）。
    """

    as_of: str
    cutoff_at: str
    currencies: list[AgingCurrencyOut]


# ---- 分期回款计划 ----


class InstallmentIn(BaseModel):
    """创建分期回款计划时的单个期次。"""

    due_date: str = Field(
        description="本期到期日，ISO 8601 日期 YYYY-MM-DD（如 '2026-10-01'）；"
        "只接受日期形状，不接受带时间成分"
    )
    amount: str = Field(
        description="本期应收金额，发票币种的十进制字符串；必须大于 0 且为币种最小单位的正整数倍，"
        "如 USD 下 '10.00'、JPY 下 '1000'"
    )

    @field_validator("due_date")
    @classmethod
    def _check_due_date(cls, v: str) -> str:
        return parse_iso_date(v, "installments[].due_date").isoformat()

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v


class InstallmentPlanCreate(BaseModel):
    """为单张发票创建分期回款计划的请求体。"""

    installments: list[InstallmentIn] = Field(
        min_length=1,
        description="分期期次列表，至少 1 期；必须按到期日 **升序** 排列"
        "（允许同日多期），各期金额为币种最小单位的正数，期次合计须等于发票含税总额",
    )

    @field_validator("installments")
    @classmethod
    def _check_ascending_due_dates(cls, v: list[InstallmentIn]) -> list[InstallmentIn]:
        for idx in range(1, len(v)):
            prev_due = v[idx - 1].due_date
            cur_due = v[idx].due_date
            if cur_due < prev_due:
                raise ValueError(
                    "installments 必须按到期日升序排列："
                    f"第 {idx} 期 {prev_due} 晚于第 {idx + 1} 期 {cur_due}"
                )
        return v


class InstallmentOut(BaseModel):
    """单个期次的查询结果：计划金额 + 按净已收额 FIFO 抵扣后的覆盖情况。"""

    model_config = ConfigDict(extra="allow")
    installment: int
    due_date: str
    amount: str
    covered: str
    uncovered: str
    overdue: str


class InstallmentPlanResult(BaseModel):
    """分期回款计划视图（创建与查询共用）。

    净已收额（收款 − 冲销）按期次顺序逐期抵扣（FIFO）；只有到期日严格早于
    ``as_of`` 的未覆盖额计入 ``overdue_total``。查询不改写原计划：冲销后
    净已收回降，较后期次的 ``uncovered`` 会相应回升。
    """

    model_config = ConfigDict(extra="allow")
    plan_id: int
    invoice_id: int
    currency: str
    created_at: str
    as_of: str
    total_gross: str
    net_paid: str
    covered_total: str
    uncovered_total: str
    overdue_total: str
    installments: list[InstallmentOut]


# ---- 供应商发票号码批次查重 ----


class InvoiceNumberCheckItem(BaseModel):
    """查重批次中的一条：供应商编号 + 供应商发票号码（原始值，原样回显）。"""

    model_config = ConfigDict(extra="ignore")
    supplier_code: str = Field(description="供应商编号；规范化（NFKC/去空白/折叠大小写）后不得为空")
    invoice_number: str = Field(
        description=f"供应商发票号码；不得为空，原始长度不超过 {MAX_INVOICE_NUMBER_CHARS} 个 Unicode 字符"
    )


class InvoiceNumberCheckRequest(BaseModel):
    """供应商发票号码批次查重请求：1~500 条。"""

    model_config = ConfigDict(extra="ignore")
    items: list[InvoiceNumberCheckItem] = Field(
        min_length=1,
        max_length=500,
        description="待查重条目，1~500 条；位置（positions）从 1 开始按输入顺序编号",
    )

    @field_validator("items")
    @classmethod
    def _check_items(cls, v: list[InvoiceNumberCheckItem]) -> list[InvoiceNumberCheckItem]:
        for index, item in enumerate(v, start=1):
            if is_blank(item.supplier_code):
                raise ValueError(f"items[{index}].supplier_code 供应商编号不得为空")
            if is_blank(item.invoice_number):
                raise ValueError(f"items[{index}].invoice_number 原始号码不得为空")
            if len(item.invoice_number) > MAX_INVOICE_NUMBER_CHARS:
                raise ValueError(
                    f"items[{index}].invoice_number 原始号码不得超过 "
                    f"{MAX_INVOICE_NUMBER_CHARS} 个 Unicode 字符，"
                    f"收到 {len(item.invoice_number)} 个"
                )
        return v


class InvoiceNumberDuplicateGroup(BaseModel):
    """一个重复组：同一供应商内规范化后相同的所有号码。"""

    supplier_code: str = Field(description="供应商编号（组内首次出现位置的原始值）")
    normalized_invoice_number: str = Field(
        description="规范化后的发票号码（NFKC、去首尾空白、大小写折叠后）"
    )
    positions: list[int] = Field(description="组内每条记录的输入位置（从 1 开始，升序）")
    original_invoice_numbers: list[str] = Field(
        description="组内每条记录的原始发票号码（按 positions 顺序原样回显）"
    )


class InvoiceNumberCheckResult(BaseModel):
    """查重响应：只包含重复组，按组内最早输入位置升序；无重复时为空列表。"""

    items: list[InvoiceNumberDuplicateGroup]


# ---- 原发票贷项凭证 ----


class CreditMemoLineIn(BaseModel):
    """创建贷项凭证时对单个原行的本次抵扣。"""

    line_no: int = Field(
        description="被抵扣的原发票行号（创建发票时按输入顺序从 1 开始）"
    )
    net: str = Field(
        description="本次抵扣净额，发票币种的十进制字符串；必须大于 0、为币种最小单位"
        "的正整数倍，且与该原行历次抵扣净额合计不得超过原行净额，如 '3.34'"
    )

    @field_validator("line_no")
    @classmethod
    def _check_line_no(cls, v: int) -> int:
        if isinstance(v, bool) or v <= 0:
            raise ValueError(f"line_no 必须是正整数，收到 {v!r}")
        return v

    @field_validator("net")
    @classmethod
    def _check_net(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"net 必须是十进制数字字符串，收到 {v!r}")
        return v


class CreditMemoCreate(BaseModel):
    """为已保存发票创建一张部分抵扣贷项凭证的请求体（可含多行）。"""

    lines: list[CreditMemoLineIn] = Field(
        min_length=1,
        description="本次抵扣明细，至少 1 行；同一凭证内 line_no 不得重复",
    )

    @field_validator("lines")
    @classmethod
    def _check_unique_line_no(cls, v: list[CreditMemoLineIn]) -> list[CreditMemoLineIn]:
        seen: set[int] = set()
        for idx, item in enumerate(v, start=1):
            if item.line_no in seen:
                raise ValueError(
                    f"贷项凭证明细第 {idx} 行的 line_no={item.line_no} 与前面的行重复，"
                    f"同一凭证内行号不得重复"
                )
            seen.add(item.line_no)
        return v


class CreditMemoLineOut(BaseModel):
    """凭证单个抵扣行：本次净额/税额/含税额 + 抵扣后该原行的累计抵扣。"""

    model_config = ConfigDict(extra="allow")
    line_no: int
    net: str
    tax: str
    gross: str
    cumulative_net: str
    cumulative_tax: str


class CreditMemoResult(BaseModel):
    """贷项凭证创建结果：凭证头合计 + 逐行本次与累计抵扣。"""

    model_config = ConfigDict(extra="allow")
    id: int
    invoice_id: int
    currency: str
    created_at: str
    net_total: str
    tax_total: str
    gross_total: str
    lines: list[CreditMemoLineOut]


class CreditMemoLineCumulative(BaseModel):
    """按原发票查询时，单个原行截至当前的累计抵扣与剩余净额。"""

    model_config = ConfigDict(extra="allow")
    line_no: int
    credited_net: str
    credited_tax: str
    credited_gross: str
    remaining_net: str


class CreditMemoOverview(BaseModel):
    """按原发票查询的全部贷项凭证与逐原行累计抵扣（创建接口只返回单张结果）。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    currency: str
    total_net: str
    total_tax: str
    total_gross: str
    credit_count: int
    credited_net: str
    credited_tax: str
    credited_gross: str
    line_credits: list[CreditMemoLineCumulative]
    memos: list[CreditMemoResult]


# ---- 期间税额归集 ----


class TaxLedgerAmounts(BaseModel):
    """一个税额分组中某一口径（开票/抵减/净额）的未税、税、含税金额。"""

    model_config = ConfigDict(extra="allow")
    net: str
    tax: str
    gross: str


class TaxLedgerGroup(BaseModel):
    """原币种 + 原发票行税率下的期间开票、抵减与净额（net = invoiced − credited）。

    混合保存精度下，``currency_digits`` 为该组全部参与记录（发票行与贷项凭证行）
    的最高入库小数位，开票/抵减/净额均按其格式化，结果与插入顺序无关。
    """

    model_config = ConfigDict(extra="allow")
    currency: str
    currency_digits: int
    tax_rate: str
    invoiced: TaxLedgerAmounts
    credited: TaxLedgerAmounts
    net: TaxLedgerAmounts


class TaxLedgerSummary(BaseModel):
    """期间税额归集：按 UTC 自然日闭区间选取发票行与贷项凭证行，原币种/税率分组。

    发票行按发票保存时间、贷项凭证行按凭证创建时间分别落入期间；凭证即使对应
    更早期开票也计入本期抵减。金额只取原币列（不使用目标币换算列），分组固定
    按币种代码升序、税率数值升序；期间内无任何行时 ``groups`` 为空列表。
    """

    start_date: str
    end_date: str
    period_start: str
    period_end: str
    groups: list[TaxLedgerGroup]
