"""供应商发票号码批次查重（纯函数，无 FastAPI / Pydantic 依赖）。

规范化规则（供应商编号与发票号码都做同样处理）：

1. Unicode **NFKC** 规范化（兼容分解后按规范组合，全角字母数字、连字等
   折叠为规范形式）；
2. 去除首尾空白（``str.strip``，覆盖所有 Unicode 空白）；
3. 大小写折叠（``str.casefold``，比 ``lower`` 更彻底，如 ß → ss）。

规范化后为空或原始号码超过 100 个 Unicode 字符视为非法输入，由调用方
（schemas 层）映射为 422；本模块只负责查重本身。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

MAX_INVOICE_NUMBER_CHARS = 100


def normalize_text(value: str) -> str:
    """NFKC 规范化 + 去首尾空白 + 大小写折叠。"""

    return unicodedata.normalize("NFKC", value).strip().casefold()


def is_blank(value: str) -> bool:
    """规范化（NFKC + strip，不做 casefold 以免掩盖空白判断）后为空。"""

    return unicodedata.normalize("NFKC", value).strip() == ""


@dataclass(frozen=True)
class DuplicateGroup:
    """同一供应商内规范化后重复的一个号码组。"""

    supplier_code: str
    normalized_invoice_number: str
    positions: tuple[int, ...]
    original_invoice_numbers: tuple[str, ...]


def find_duplicates(items: list[tuple[str, str]]) -> list[DuplicateGroup]:
    """在同一供应商内识别规范化后重复的发票号码。

    :param items: ``(供应商编号原始值, 发票号码原始值)`` 列表，顺序即输入顺序
                  （调用方已完成空值/长度校验）。
    :return: 重复组列表，按组内最早输入位置升序；组内 ``positions`` 也按
             输入位置升序。未重复的号码不列出。
    """

    # key = (规范化供应商编号, 规范化发票号码)
    first_seen: dict[tuple[str, str], int] = {}
    positions: dict[tuple[str, str], list[int]] = {}
    raw_numbers: dict[tuple[str, str], list[str]] = {}
    first_supplier: dict[tuple[str, str], str] = {}

    for index, (supplier, invoice_number) in enumerate(items, start=1):
        key = (normalize_text(supplier), normalize_text(invoice_number))
        if key not in first_seen:
            first_seen[key] = index
            positions[key] = []
            raw_numbers[key] = []
            first_supplier[key] = supplier
        positions[key].append(index)
        raw_numbers[key].append(invoice_number)

    groups: list[DuplicateGroup] = []
    for key, first_index in first_seen.items():
        if len(positions[key]) < 2:
            continue
        groups.append(
            DuplicateGroup(
                supplier_code=first_supplier[key],
                normalized_invoice_number=key[1],
                positions=tuple(positions[key]),
                original_invoice_numbers=tuple(raw_numbers[key]),
            )
        )
    groups.sort(key=lambda group: group.positions[0])
    return groups
