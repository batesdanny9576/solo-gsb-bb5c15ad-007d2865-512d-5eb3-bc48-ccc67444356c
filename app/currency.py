"""ISO 4217 货币最小单位（小数位数）表。

未收录的币种代码会抛出 :class:`UnknownCurrencyError`，避免把不认识的币种
默默按 2 位小数处理。扩展方式（任选其一）：

1. 修改本文件中的常量；
2. 通过环境变量 ``CURRENCY_DIGITS`` 追加/覆盖，格式为 ``"KWD:3,XYZ:0"``。
"""

from __future__ import annotations

import os

# 0 位小数：最小单位为 1（ISO 4217 exponent = 0）
ZERO_DIGITS = frozenset(
    {
        "BIF",  # 布隆迪法郎
        "CLP",  # 智利比索
        "DJF",  # 吉布提法郎
        "GNF",  # 几内亚法郎
        "ISK",  # 冰岛克朗
        "JPY",  # 日元
        "KMF",  # 科摩罗法郎
        "KRW",  # 韩元
        "PYG",  # 巴拉圭瓜拉尼
        "RWF",  # 卢旺达法郎
        "UGX",  # 乌干达先令
        "UYI",  # 乌拉圭指数单位
        "VND",  # 越南盾
        "VUV",  # 瓦努阿图瓦图
        "XAF",  # 中非法郎
        "XOF",  # 西非法郎
        "XPF",  # 太平洋法郎
    }
)

# 3 位小数：最小单位为 1/1000
THREE_DIGITS = frozenset(
    {
        "BHD",  # 巴林第纳尔
        "IQD",  # 伊拉克第纳尔
        "JOD",  # 约旦第纳尔
        "KWD",  # 科威特第纳尔
        "LYD",  # 利比亚第纳尔
        "OMR",  # 阿曼里亚尔
        "TND",  # 突尼斯第纳尔
    }
)

# 4 位小数
FOUR_DIGITS = frozenset(
    {
        "CLF",  # 智利发展单位
        "UYW",  # 乌拉圭工资指数单位
    }
)

# 2 位小数：ISO 4217 exponent = 2 的常用币种白名单
TWO_DIGITS = frozenset(
    {
        "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
        "BAM", "BBD", "BDT", "BGN", "BMD", "BND", "BOB", "BOV", "BRL", "BSD",
        "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHE", "CHF", "CHW", "CNY",
        "COP", "CRC", "CUC", "CUP", "CVE", "CZK", "DKK", "DOP", "DZD", "EGP",
        "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GGP", "GHS", "GIP",
        "GMD", "GTQ", "GYD", "HKD", "HNL", "HRK", "HTG", "HUF", "IDR", "ILS",
        "IMP", "INR", "IRR", "JEP", "JMD", "KES", "KGS", "KHR", "KID", "KZT",
        "LAK", "LBP", "LKR", "LRD", "LSL", "MAD", "MDL", "MGA", "MKD", "MMK",
        "MNT", "MOP", "MRO", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN",
        "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "PAB", "PEN", "PGK", "PHP",
        "PKR", "PLN", "PRB", "QAR", "RON", "RSD", "RUB", "SAR", "SBD", "SCR",
        "SDG", "SEK", "SGD", "SHP", "SLL", "SLE", "SOS", "SRD", "SSP", "STD",
        "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TOP", "TRY", "TTD",
        "TVD", "TWD", "TZS", "UAH", "USD", "USN", "UYI", "UYU", "UZS", "VED",
        "VEF", "VES", "WST", "XCD", "YER", "ZAR", "ZMW", "ZWL",
    }
)


def _env_overrides() -> dict[str, int]:
    raw = os.getenv("CURRENCY_DIGITS", "").strip()
    overrides: dict[str, int] = {}
    if not raw:
        return overrides
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        code, sep, digits = item.partition(":")
        code = code.strip().upper()
        if not code or not sep:
            raise ValueError(f"非法的 CURRENCY_DIGITS 配置项: {item!r}")
        digits = digits.strip()
        if digits not in {"0", "1", "2", "3", "4"}:
            raise ValueError(f"货币 {code} 的小数位数必须在 0~4 之间: {digits!r}")
        overrides[code] = int(digits)
    return overrides


class UnknownCurrencyError(ValueError):
    """请求中使用了系统不认识的币种代码。"""

    def __init__(self, currency: str) -> None:
        self.currency = currency
        super().__init__(
            f"未知币种 {currency!r}；请使用 ISO 4217 代码，或通过 CURRENCY_DIGITS 环境变量扩展"
        )


def currency_digits(currency: str) -> int:
    """返回币种的最小单位小数位数（USD -> 2，JPY -> 0，KWD -> 3）。"""

    currency = currency.upper()
    overrides = _env_overrides()
    if currency in overrides:
        return overrides[currency]
    if currency in ZERO_DIGITS:
        return 0
    if currency in THREE_DIGITS:
        return 3
    if currency in FOUR_DIGITS:
        return 4
    if currency in TWO_DIGITS:
        return 2
    raise UnknownCurrencyError(currency)
