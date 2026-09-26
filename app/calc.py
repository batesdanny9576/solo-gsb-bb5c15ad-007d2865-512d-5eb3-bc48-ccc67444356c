"""发票税额计算核心。

规则（固定且可复算）：

1. 每行税额 = 不含税净额 × 税率，全程使用 ``Decimal`` 高精度运算，
   不出现任何 float。所有十进制运算都在**按本次输入量级计算的局部精度**
   （``localcontext``）内进行：``Decimal`` 上下文是线程本地的，模块导入时
   设置的全局精度管不到 FastAPI 工作线程（那里回落到默认 28 位），因此
   本模块不读不写全局上下文，任何线程下结果一致。
2. 每行含税 / 税额按 **币种最小单位** 做 ``ROUND_HALF_UP``（四舍五入）。
3. 发票总税额 = Σ(各行精确税额) 后，再按币种最小单位做 HALF_UP；
   它与「各行已舍入税额之和」之间的差即为 *残差*（最小单位整数个，
   必有 ``|残差| <= 行数``）。
4. 残差按 **最大余数法（Hare quota / largest remainder）** 分配，
   排序完全确定，任何时候重新计算结果一致：

   * 残差 > 0（需补分到行）：按「精确税额超出其已舍入值的零头」从大到小排序，
     每行 +1 个最小单位，分完为止；
   * 残差 < 0（需从行扣减）：按同一零头从小到大排序，每行 -1 个最小单位。
   * 零头相同按行在请求中的顺序（line_no）排前的优先，保证结果确定。

   最终各行税额之和恰好等于总税额，残差被完全分配。

5. 跨币种：当 ``target_currency`` 与发票币种不同时，必须提供
   ``exchange_rate``（标价方式：1 单位发票币种 = exchange_rate 单位目标币种）。
   原币金额原样保留；目标币每行净额/税额同样按目标币种最小单位 HALF_UP，
   并在目标币上再做一次相同的残差分配。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Iterable

from .currency import UnknownCurrencyError, currency_digits

RULE_ID = "half_up_plus_largest_remainder_v1"

# 局部精度下限：常规金额沿用历史精度 50 位；超大金额按输入量级自动放大
# （见 _sufficient_prec）
_BASE_PREC = 50

_ZERO = Decimal("0")
_QUANT_CACHE: dict[int, Decimal] = {}


def _unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    if digits not in _QUANT_CACHE:
        _QUANT_CACHE[digits] = Decimal(1).scaleb(-digits)
    return _QUANT_CACHE[digits]


def _quantize(value: Decimal, digits: int) -> Decimal:
    """按币种最小单位做 ROUND_HALF_UP 舍入。"""

    return value.quantize(_unit(digits), rounding=ROUND_HALF_UP)


def digits_from_quantized_text(value: str | None) -> int | None:
    """从已按最小单位量化持久化的金额字符串恢复其币种小数位。

    入库金额一律经 ``_s(Decimal.quantize(unit))`` 以无指数定点字符串保存
    （``"712.35"`` -> 2、``"150"`` -> 0、``"0.100"`` -> 3），因此尾随小数位即
    币种小数位；用于为没有独立精度列的历史记录恢复**目标币**精度。无法解析或
    位数超出 0~4 时返回 ``None``。
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text or "." not in text:
        return 0 if text else None
    frac = text.rsplit(".", 1)[1]
    if not frac.isdigit():
        return None
    digits = len(frac)
    return digits if 0 <= digits <= 4 else None


def to_decimal(value: Any, field: str) -> Decimal:
    """把入参（字符串/int/Decimal）安全转成 Decimal；禁止 float。"""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝
        raise ValueError(f"{field} 必须是十进制数字字符串，不能是布尔值")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} 不能为空")
        try:
            return Decimal(text)
        except Exception as exc:  # InvalidOperation 等
            raise ValueError(f"{field} 不是合法的十进制数字: {value!r}") from exc
    raise ValueError(f"{field} 必须以字符串形式提交十进制数字，收到: {type(value).__name__}")


@dataclass(frozen=True)
class Allocation:
    """某一币种维度上的残差分配结果。"""

    rounded_taxes: list[Decimal]      # 分配后各行税额（最小单位整数倍）
    line_totals: list[Decimal]        # 分配后各行含税总额
    total_tax: Decimal                # 发票总税额（按最小单位舍入）
    total_net: Decimal                # 不含税总额（输入即最小单位整数倍）
    total_gross: Decimal              # 含税总额 = total_net + total_tax
    residual: int                     # 残差（最小单位个数，正负整数）
    residual_unit: str                # 最小单位，如 '0.01'
    allocations: list[dict[str, Any]] # 可审计的逐行分配明细


def _allocate(
    nets: list[Decimal],
    exact_taxes: list[Decimal],
    digits: int,
) -> Allocation:
    """逐行舍入后，把总税额残差按最大余数法分配回各行。"""

    unit = _unit(digits)
    n = len(nets)

    rounded = [_quantize(t, digits) for t in exact_taxes]
    # 零头：精确税额超过「向下取整到最小单位」的部分，落在 [0, unit)
    floors = [ (t / unit).to_integral_value(rounding="ROUND_FLOOR") * unit for t in exact_taxes ]
    remainders = [exact_taxes[i] - floors[i] for i in range(n)]

    sum_rounded = sum(rounded, _ZERO)
    exact_total = sum(exact_taxes, _ZERO)
    total_tax = _quantize(exact_total, digits)
    residual_q = total_tax - sum_rounded  # 必为最小单位整数倍
    residual = int(residual_q / unit)

    if not (-n <= residual <= n):
        # 数学上不可能；作为不变量防御
        raise AssertionError(f"残差 {residual} 超出行数 {n}")

    final_taxes = list(rounded)
    allocations: list[dict[str, Any]] = []
    if residual > 0:
        order = sorted(range(n), key=lambda i: (-remainders[i], i))
        for rank, idx in enumerate(order[:residual]):
            final_taxes[idx] += unit
            allocations.append(
                {
                    "line_no": idx + 1,
                    "delta_units": 1,
                    "remainder": _canon(exact_taxes[idx] - floors[idx]),
                    "rank": rank + 1,
                }
            )
    elif residual < 0:
        order = sorted(range(n), key=lambda i: (remainders[i], i))
        for rank, idx in enumerate(order[: -residual]):
            final_taxes[idx] -= unit
            allocations.append(
                {
                    "line_no": idx + 1,
                    "delta_units": -1,
                    "remainder": _canon(exact_taxes[idx] - floors[idx]),
                    "rank": rank + 1,
                }
            )

    line_totals = [nets[i] + final_taxes[i] for i in range(n)]
    total_net = sum(nets, _ZERO)
    total_gross = total_net + total_tax

    # 不变量：行税额之和必须严格等于总税额
    if sum(final_taxes, _ZERO) != total_tax:
        raise AssertionError("残差分配后行税额之和与总税额不一致")

    return Allocation(
        rounded_taxes=final_taxes,
        line_totals=line_totals,
        total_tax=total_tax,
        total_net=total_net,
        total_gross=total_gross,
        residual=residual,
        residual_unit=_s(unit),
        allocations=allocations,
    )


def _s(value: Decimal) -> str:
    """Decimal -> 字符串，保留有效位且不带指数（如 0.005 -> '0.005'）。"""

    return format(value, "f")


def _canon(value: Decimal) -> str:
    """规范化展示：去掉无意义尾随零（0.0050 -> '0.005'，0.10 -> '0.1'）。

    仅用于 *精确值*（exact_tax、余数审计字段）；已按最小单位舍入的金额仍用
    :func:`_s` 保留固定位数（如税额 '1.00'）。
    """

    normalized = value - _ZERO  # -0 归一成 +0
    return format(normalized.normalize(), "f")


def _validate_lines(raw_lines: Iterable[Any]) -> list[tuple[str | None, Decimal, Decimal]]:
    """校验并规范化行项目，返回 (description, net, rate) 列表。"""

    lines = list(raw_lines)
    if not lines:
        raise ValueError("至少需要一行发票行 (lines 不能为空)")

    result: list[tuple[str | None, Decimal, Decimal]] = []
    for idx, line in enumerate(lines, start=1):
        description = line.get("description") if isinstance(line, dict) else None
        net = to_decimal(
            line.get("net") if isinstance(line, dict) else getattr(line, "net", None),
            f"lines[{idx}].net",
        )
        if net == _ZERO:
            raise ValueError(f"lines[{idx}].net 不能为 0")
        rate = to_decimal(
            line.get("tax_rate") if isinstance(line, dict) else getattr(line, "tax_rate", None),
            f"lines[{idx}].tax_rate",
        )
        if not (_ZERO <= rate <= 1):
            raise ValueError(f"lines[{idx}].tax_rate 必须在 0~1 之间（例如 0.13 表示 13%）")
        result.append((description, net, rate))
    return result


def _decimal_span(value: Decimal) -> int:
    """非零十进制数从最高整数位到最低小数位完整展开所需的位数。

    例如 100.00 -> 5、0.001 -> 3、1E+30 -> 31。用于估算局部精度上界。
    """

    if not value.is_finite() or value.is_zero():
        return 1
    integer_digits = max(value.adjusted() + 1, 0)        # 整数部分位数（纯小数为 0）
    fraction_digits = max(-value.as_tuple().exponent, 0)  # 小数部分深度
    return integer_digits + fraction_digits


def _sufficient_prec(
    lines_in: list[tuple[str | None, Decimal, Decimal]],
    exchange_rate_raw: Any,
    n_lines: int,
) -> int:
    """按本次输入的实际量级计算「足够」的局部精度（与线程无关）。

    记 span 为各输入（净额/税率/汇率）完整展开位数的上界，则：

    * 净额 × 税率（× 汇率）连乘：有效数字不超过 3 × span；
    * 逐行求和：整数位最多再进 log10(n) 位，小数深度不超过 3 × span，
      合计完整展开不超过 6 × span + 行数位数；
    * 按币种最小单位 quantize 最多再需 4 位（币种小数位上限 0~4）。

    取 max(50, 6 × span + 行数位数 + 16)：常规金额维持历史精度 50，
    超大金额（如 30 位整数净额）自动放大，保证所有中间结果精确、不被截断。
    汇率仅做尽力解析（非法值由后续正常校验流程报出），此处只为估算精度。
    """

    span = 1
    for _description, net, rate in lines_in:
        span = max(span, _decimal_span(net), _decimal_span(rate))
    if exchange_rate_raw is not None:
        try:
            span = max(
                span, _decimal_span(to_decimal(exchange_rate_raw, "exchange_rate"))
            )
        except ValueError:
            pass
    return max(_BASE_PREC, 6 * span + len(str(n_lines)) + 16)


def money_prec(amounts: Iterable[Decimal], count: int = 1) -> int:
    """按一批金额的实际量级计算「足够」的局部精度（与 :func:`_sufficient_prec` 同一策略）。

    用于收款/冲销/余额等收支累计与量化场景：常规金额维持 ``_BASE_PREC``；金额
    完整展开位数（:func:`_decimal_span`，整数位 + 小数位）更大时自动放大——
    预留累计求和的进位（log10(count)）与乘除中间结果（如贷项凭证按累计净额
    比例摊算税额）的余量，保证任何线程（含默认 28 位精度的 FastAPI 工作线程）
    下加减、比较与 ``quantize`` 都精确：大额发票（如 30 位整数净额）的余额
    不丢分位，大额金额量化不抛 ``InvalidOperation``。
    """

    span = 1
    for amount in amounts:
        span = max(span, _decimal_span(amount))
    return max(_BASE_PREC, 4 * span + len(str(max(count, 1))) + 16)


@contextmanager
def money_context(amounts: Iterable[Decimal], count: int = 1) -> Iterator[None]:
    """进入按本次涉及金额量级计算的局部精度上下文（策略见 :func:`money_prec`）。

    Decimal 上下文是线程本地的，模块级全局设置管不到工作线程；收款登记、
    冲销、余额累计与量化等所有金额运算都必须放进该上下文，任何线程下结果一致。
    """

    with localcontext() as ctx:
        ctx.prec = money_prec(amounts, count)
        yield


def _validate_digits(digits: int | None, field: str) -> int | None:
    """校验外部（入库时）固定下来的币种小数位，取值须在 0~4。"""

    if digits is None:
        return None
    if not isinstance(digits, int) or isinstance(digits, bool) or not (0 <= digits <= 4):
        raise ValueError(f"{field} 必须是 0~4 之间的整数，收到 {digits!r}")
    return digits


def compute_invoice(
    payload: dict[str, Any],
    digits: int | None = None,
    target_digits: int | None = None,
) -> dict[str, Any]:
    """执行完整计算，返回可直接 JSON 序列化（金额全部为字符串）的结果字典。

    该函数是纯函数：同样输入必然得到同样输出，持久化与复算都以它为准。
    不读不写全局 Decimal 上下文，任何线程（含默认 28 位精度的 FastAPI
    工作线程）下结果一致。

    ``digits`` / ``target_digits`` 给出原币 / 目标币**入库时固定**的小数位：
    传入时直接沿用，不再查询当前 ``CURRENCY_DIGITS`` 配置——币种精度配置变更后，
    读取与复算旧发票仍按该发票保存时的精度进行（旧的两位 USD 发票不会在 USD
    被改成零位后复算失败）；缺省（创建发票）则按当前配置解析币种小数位，新发票
    仍按当前配置校验。
    """

    currency = str(payload["currency"]).strip().upper()
    if not currency:
        raise ValueError("currency 不能为空")
    digits = _validate_digits(digits, "currency_digits")
    target_digits = _validate_digits(target_digits, "target_digits")
    if digits is None:
        try:
            digits = currency_digits(currency)
        except UnknownCurrencyError:
            raise

    lines_in = _validate_lines(payload.get("lines", []))
    n = len(lines_in)

    # Decimal 上下文是线程本地的：FastAPI 工作线程拿到的是默认 28 位精度，
    # 模块级全局设置管不到它。净额最小单位校验、原币/目标币税额分配与结果
    # 组装全部放进同一个按输入量级计算的局部上下文，任何线程下结果一致。
    with localcontext() as ctx:
        ctx.prec = _sufficient_prec(lines_in, payload.get("exchange_rate"), n)

        # 净额必须是币种最小单位的整数倍（发票上不存在更小单位）；
        # 通过后统一 quantize 一次，使展示/存储固定为币种位数（1.5 -> 1.50）
        unit = _unit(digits)
        nets: list[Decimal] = []
        normalized_lines: list[tuple[str | None, Decimal, Decimal]] = []
        for idx, (description, net, rate) in enumerate(lines_in, start=1):
            if (net / unit) != (net / unit).to_integral_value():
                raise ValueError(
                    f"lines[{idx}].net={_s(net)} 不是 {currency} 最小单位 {_s(unit)} 的整数倍"
                )
            net = net.quantize(unit)
            nets.append(net)
            normalized_lines.append((description, net, rate))
        lines_in = normalized_lines

        exact_taxes = [nets[i] * lines_in[i][2] for i in range(n)]
        alloc = _allocate(nets, exact_taxes, digits)

        # ---- 跨币种换算 ----
        target_currency = payload.get("target_currency")
        fx_section: dict[str, Any] | None = None
        converted_nets: list[Decimal] | None = None
        converted_alloc: Allocation | None = None
        exchange_rate: Decimal | None = None

        if target_currency is not None:
            target_currency = str(target_currency).strip().upper()
            if not target_currency:
                raise ValueError("target_currency 不能为空字符串")
            if target_currency == currency:
                raise ValueError("target_currency 与 currency 相同，无需换算")
            if payload.get("exchange_rate") is None:
                raise ValueError("跨币种换算必须提供 exchange_rate")
            exchange_rate = to_decimal(payload["exchange_rate"], "exchange_rate")
            if exchange_rate <= _ZERO:
                raise ValueError("exchange_rate 必须为正数")
            if target_digits is None:
                target_digits = currency_digits(target_currency)

            converted_nets = [
                _quantize(nets[i] * exchange_rate, target_digits) for i in range(n)
            ]
            # 目标币逐行精确税额按「原币净额 × 税率 × 汇率」口径计算，
            # 然后在目标币种上独立做一次逐行舍入 + 残差分配
            converted_exact_taxes = [
                nets[i] * lines_in[i][2] * exchange_rate for i in range(n)
            ]
            converted_alloc = _allocate(
                converted_nets, converted_exact_taxes, target_digits
            )

            fx_section = {
                "target_currency": target_currency,
                "target_digits": target_digits,
                "exchange_rate": _s(exchange_rate),
                "rate_direction": f"1 {currency} = {_s(exchange_rate)} {target_currency}",
                "total_net": _s(converted_alloc.total_net),
                "total_tax": _s(converted_alloc.total_tax),
                "total_gross": _s(converted_alloc.total_gross),
                "residual_units": converted_alloc.residual,
                "residual_unit": converted_alloc.residual_unit,
                "allocations": converted_alloc.allocations,
            }

        # 结果组装也包含十进制运算（_canon 的 -0 归一与 normalize、目标币行
        # 含税合计），必须留在局部上下文内，避免被工作线程的 28 位精度截断。
        result_lines: list[dict[str, Any]] = []
        for i, (description, net, rate) in enumerate(lines_in):
            line_out: dict[str, Any] = {
                "line_no": i + 1,
                "description": description,
                "tax_rate": _s(rate),
                "currency": currency,
                "net": _s(net),
                "exact_tax": _canon(exact_taxes[i]),
                "tax": _s(alloc.rounded_taxes[i]),
                "gross": _s(alloc.line_totals[i]),
            }
            if converted_alloc is not None:
                line_out["converted"] = {
                    "currency": target_currency,
                    "net": _s(converted_nets[i]),
                    "tax": _s(converted_alloc.rounded_taxes[i]),
                    "gross": _s(
                        converted_nets[i] + converted_alloc.rounded_taxes[i]
                    ),
                }
            result_lines.append(line_out)

        result: dict[str, Any] = {
            "rule_id": RULE_ID,
            "currency": currency,
            "currency_digits": digits,
            "lines": result_lines,
            "totals": {
                "currency": currency,
                "total_net": _s(alloc.total_net),
                "total_tax": _s(alloc.total_tax),
                "total_gross": _s(alloc.total_gross),
            },
            "rounding": {
                "mode": "ROUND_HALF_UP",
                "minor_unit": alloc.residual_unit,
                "residual_units": alloc.residual,
                "residual_amount": _s(alloc.residual * _unit(digits)),
                "method": "largest_remainder",
                "allocations": alloc.allocations,
            },
        }
        if fx_section is not None:
            result["fx"] = fx_section
    return result


def recompute_matches(
    stored: dict[str, Any],
    payload: dict[str, Any],
    digits: int | None = None,
    target_digits: int | None = None,
) -> bool:
    """用原始请求重新计算，与已存结果逐字段比对，用于 GET 时的可复算校验。

    读取已保存发票时必须传入该发票**入库时固定**的 ``digits`` / ``target_digits``，
    使复算沿用保存时的币种精度而不是当前 ``CURRENCY_DIGITS`` 配置：配置变更前后
    已存的行、合计与残差分配保持一致，配置真正改变后新发票才按新精度计算。
    """

    fresh = compute_invoice(payload, digits=digits, target_digits=target_digits)
    return fresh == stored
