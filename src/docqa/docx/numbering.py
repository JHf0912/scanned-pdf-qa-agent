"""Word 自动编号 → 条号标签（有界解析）。

问题
----
Word 的自动编号**不在段落文本里**。`3.6 楔键` 在 `paragraph.text` 中只有
`楔键`，`3.6` 存在 `numbering.xml` 里、渲染时才拼上去。如果我们只读文本：

- `clause_label` 为空 → 引用全部退化成 `[¶n]`；
- 条号一致性校验失去锚点（答案说的 3.6 与证据的 3.6 无法比对）。

所以这里把编号"算回来"。范围是**有界**的：

- 支持 decimal / decimalZero / chineseCounting / chineseCountingThousand /
  upperLetter / lowerLetter / upperRoman / lowerRoman 与 `%1..%9` 的 lvlText；
- 其它数字格式回退十进制并**记录格式名**（不假装支持）；
- `bullet` / `none` 不是条号，直接返回"无标签"——把它们当条号会让每个
  列表项都变成伪造的条款引用，比没有标签更糟；
- 解析不出来就返回 `None`，**绝不编一个条号**。

调用方（parser）负责决定"文本里已有条号时优先用文本"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(tag: str) -> str:
    return f"{_W}{tag}"


# 支持的数字格式。其余格式一律回退十进制并记录。
SUPPORTED_FORMATS = frozenset(
    {
        "decimal",
        "decimalZero",
        "chineseCounting",
        "chineseCountingThousand",
        "upperLetter",
        "lowerLetter",
        "upperRoman",
        "lowerRoman",
    }
)
# 这些不是"条号"：项目符号与"无编号"。按十进制回退会伪造条号。
NON_NUMBERING_FORMATS = frozenset({"bullet", "none"})

_PLACEHOLDER_RE = re.compile(r"%([1-9])")


@dataclass(frozen=True)
class NumberingLabel:
    """一次编号解析的结果。

    `text` 是**展示串**（`第3章`、`3.6`、`（二）`），用于 `section` 与阅读；
    `number` 是**裸条号**（`3`、`3.6`、`二`），用于 `clause_label` 与
    `citation_validity` 的一致性比对——引用里出现 `[§3 ¶4]` 时，比对的是 `3`，
    不是 `第3章`。

    `unsupported_formats` 随结果返回而不是只在内部记日志：manifest 要能回答
    "这份文档有多少条号是我们算不出来的"，否则退化是隐形的。
    """

    text: str | None
    number: str | None = None
    unsupported_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class _LevelDef:
    start: int = 1
    num_fmt: str = "decimal"
    lvl_text: str | None = None
    # Word 语义：None = 父级递增时重置；0 = 永不重置；N = 第 N 级递增时重置。
    restart: int | None = None


@dataclass(frozen=True)
class _LevelOverride:
    start: int | None = None
    level: _LevelDef | None = None


@dataclass(frozen=True)
class _NumDef:
    abstract_id: int
    overrides: dict[int, _LevelOverride] = field(default_factory=dict)


class NumberingResolver:
    """按 `numId` 维护计数器，把 (numId, ilvl) 解析成条号标签。

    每次 ingest 新建一个实例：计数器是有状态的，跨文档复用会串号。
    """

    def __init__(self, numbering_element: Any | None) -> None:
        self._abstracts: dict[int, dict[int, _LevelDef]] = {}
        self._nums: dict[int, _NumDef] = {}
        self._counters: dict[int, dict[int, int]] = {}
        if numbering_element is not None:
            self._load(numbering_element)

    # ------------------------------------------------------------ 装载

    def _load(self, element: Any) -> None:
        for abstract_el in element.findall(_w("abstractNum")):
            abstract_id = _as_int(abstract_el.get(_w("abstractNumId")))
            if abstract_id is None:
                continue
            levels: dict[int, _LevelDef] = {}
            for lvl_el in abstract_el.findall(_w("lvl")):
                ilvl = _as_int(lvl_el.get(_w("ilvl")))
                if ilvl is None:
                    continue
                levels[ilvl] = _parse_level(lvl_el)
            self._abstracts[abstract_id] = levels

        for num_el in element.findall(_w("num")):
            num_id = _as_int(num_el.get(_w("numId")))
            if num_id is None:
                continue
            ref = num_el.find(_w("abstractNumId"))
            abstract_id = _as_int(ref.get(_w("val"))) if ref is not None else None
            if abstract_id is None:
                continue
            overrides: dict[int, _LevelOverride] = {}
            for override_el in num_el.findall(_w("lvlOverride")):
                ilvl = _as_int(override_el.get(_w("ilvl")))
                if ilvl is None:
                    continue
                start_el = override_el.find(_w("startOverride"))
                start = _as_int(start_el.get(_w("val"))) if start_el is not None else None
                lvl_el = override_el.find(_w("lvl"))
                overrides[ilvl] = _LevelOverride(
                    start=start,
                    level=_parse_level(lvl_el) if lvl_el is not None else None,
                )
            self._nums[num_id] = _NumDef(abstract_id=abstract_id, overrides=overrides)

    # ------------------------------------------------------------ 解析

    def resolve(self, num_id: int, ilvl: int) -> NumberingLabel:
        """推进计数器并返回该段落的编号标签。

        **每次调用都代表文档里出现了一个编号段落**——乱序调用会得到错误的编号，
        调用方必须按文档顺序遍历。
        """
        if num_id == 0 or ilvl < 0:
            return NumberingLabel(None)
        num = self._nums.get(num_id)
        if num is None:
            return NumberingLabel(None)
        level = self._level(num, ilvl)
        if level is None:
            return NumberingLabel(None)

        counters = self._counters.setdefault(num_id, {})
        if ilvl in counters:
            counters[ilvl] += 1
        else:
            counters[ilvl] = self._start_value(num, ilvl, level)
        for deeper in [depth for depth in counters if depth > ilvl]:
            if self._restarts(num, deeper, ilvl):
                del counters[deeper]

        return self._render(num, level, counters)

    def _level(self, num: _NumDef, ilvl: int) -> _LevelDef | None:
        override = num.overrides.get(ilvl)
        if override is not None and override.level is not None:
            return override.level
        return self._abstracts.get(num.abstract_id, {}).get(ilvl)

    def _start_value(self, num: _NumDef, ilvl: int, level: _LevelDef) -> int:
        override = num.overrides.get(ilvl)
        if override is not None and override.start is not None:
            return override.start
        return level.start

    def _restarts(self, num: _NumDef, level_index: int, incremented: int) -> bool:
        level = self._level(num, level_index)
        restart = level.restart if level is not None else None
        if restart is None:
            return True
        if restart == 0:
            return False
        return incremented <= restart - 1

    def _render(self, num: _NumDef, level: _LevelDef, counters: dict[int, int]) -> NumberingLabel:
        if level.num_fmt in NON_NUMBERING_FORMATS:
            return NumberingLabel(None, unsupported_formats=(level.num_fmt,))
        if level.lvl_text is None:
            return NumberingLabel(None)

        unsupported: list[str] = []
        bare_parts: list[str] = []

        def replace(match: re.Match[str]) -> str:
            index = int(match.group(1)) - 1
            referenced = self._level(num, index)
            if index in counters:
                value = counters[index]
            elif referenced is not None:
                value = self._start_value(num, index, referenced)
            else:
                value = 1
            fmt = referenced.num_fmt if referenced is not None else "decimal"
            rendered, fallback = _format_value(value, fmt)
            if fallback is not None:
                unsupported.append(fallback)
            bare_parts.append(rendered)
            return rendered

        text = _PLACEHOLDER_RE.sub(replace, level.lvl_text)
        # 裸条号只由数字占位符拼成：lvlText 里的字面装饰（"第…章"、"（…）"）
        # 属于展示层，混进 clause_label 会让引用一致性校验对不上。
        number = ".".join(bare_parts) if bare_parts else None
        cleaned = text.strip().rstrip(".").strip()
        if not cleaned:
            return NumberingLabel(None, number=number, unsupported_formats=_dedupe(unsupported))
        return NumberingLabel(cleaned, number=number, unsupported_formats=_dedupe(unsupported))


# ---------------------------------------------------------------- 工具


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _child_val(element: Any, tag: str) -> str | None:
    child = element.find(_w(tag))
    return child.get(_w("val")) if child is not None else None


def _parse_level(lvl_el: Any) -> _LevelDef:
    start = _as_int(_child_val(lvl_el, "start"))
    restart = _as_int(_child_val(lvl_el, "lvlRestart"))
    num_fmt = _child_val(lvl_el, "numFmt")
    lvl_text = _child_val(lvl_el, "lvlText")
    return _LevelDef(
        start=1 if start is None else start,
        num_fmt=num_fmt or "decimal",
        lvl_text=lvl_text,
        restart=restart,
    )


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _format_value(value: int, fmt: str) -> tuple[str, str | None]:
    """把数字格式化成标签片段，返回 (文本, 回退格式名或 None)。"""
    if fmt == "decimal":
        return str(value), None
    if fmt == "decimalZero":
        return f"{value:02d}", None
    if fmt in ("chineseCounting", "chineseCountingThousand"):
        return _chinese(value), None
    if fmt == "upperLetter":
        return _letters(value), None
    if fmt == "lowerLetter":
        return _letters(value).lower(), None
    if fmt == "upperRoman":
        return _roman(value), None
    if fmt == "lowerRoman":
        return _roman(value).lower(), None
    # 不认识的格式：回退十进制，并把格式名交给调用方上报。
    return str(value), fmt


_DIGITS = "零一二三四五六七八九"
_UNITS = ("", "十", "百", "千")


def _chinese(value: int) -> str:
    if value <= 0:
        return str(value)
    if value > 9999:
        return str(value)
    if 10 <= value < 20:
        remainder = value % 10
        return "十" + (_DIGITS[remainder] if remainder else "")
    text = str(value)
    parts: list[str] = []
    pending_zero = False
    for index, digit in enumerate(text):
        number = int(digit)
        position = len(text) - 1 - index
        if number == 0:
            pending_zero = True
            continue
        if pending_zero and parts:
            parts.append("零")
        pending_zero = False
        parts.append(_DIGITS[number] + _UNITS[position])
    return "".join(parts)


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ROMAN_TABLE = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _letters(value: int) -> str:
    if value <= 0:
        return str(value)
    out: list[str] = []
    while value > 0:
        value -= 1
        out.append(_LETTERS[value % 26])
        value //= 26
    return "".join(reversed(out))


def _roman(value: int) -> str:
    if value <= 0 or value >= 4000:
        return str(value)
    out: list[str] = []
    for number, symbol in _ROMAN_TABLE:
        while value >= number:
            out.append(symbol)
            value -= number
    return "".join(out)
