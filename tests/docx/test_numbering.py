"""Word 自动编号解析的测试。

为什么值得单独测
----------------
`paragraph.text` **不含**自动编号：Word 把编号存在 `numbering.xml`，渲染时才拼上去。
所以 `3.6 楔键` 在文本里只有 `楔键`，`clause_label` 会空掉，引用全部退化成 `[¶n]`。
解析编号就是把这层"渲染期信息"补回数据模型。

这里要求的是**有界**解析：只支持 Word 里真实用于条款的几种格式，其它格式
回退十进制并记录（不假装支持），拿不到就返回 None（不编一个假条号）。
"""

from __future__ import annotations

import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from docqa.docx.numbering import NumberingLabel, NumberingResolver

# ---------------------------------------------------------------- 构造工具


def _lvl(
    ilvl: int,
    *,
    fmt: str = "decimal",
    text: str | None = "%1.",
    start: int = 1,
    restart: int | None = None,
) -> str:
    restart_xml = "" if restart is None else f'<w:lvlRestart w:val="{restart}"/>'
    text_xml = "" if text is None else f'<w:lvlText w:val="{text}"/>'
    return (
        f'<w:lvl w:ilvl="{ilvl}">'
        f'<w:start w:val="{start}"/>{restart_xml}'
        f'<w:numFmt w:val="{fmt}"/>{text_xml}'
        "</w:lvl>"
    )


def _abstract(abstract_id: int, *levels: str) -> str:
    return f'<w:abstractNum w:abstractNumId="{abstract_id}">{"".join(levels)}</w:abstractNum>'


def _num(num_id: int, abstract_id: int, *overrides: str) -> str:
    return (
        f'<w:num w:numId="{num_id}">'
        f'<w:abstractNumId w:val="{abstract_id}"/>'
        f'{"".join(overrides)}'
        "</w:num>"
    )


def _start_override(ilvl: int, value: int) -> str:
    return f'<w:lvlOverride w:ilvl="{ilvl}"><w:startOverride w:val="{value}"/></w:lvlOverride>'


def _lvl_override(ilvl: int, level_xml: str) -> str:
    return f'<w:lvlOverride w:ilvl="{ilvl}">{level_xml}</w:lvlOverride>'


def _resolver(*parts: str) -> NumberingResolver:
    return NumberingResolver(parse_xml(f'<w:numbering {nsdecls("w")}>{"".join(parts)}</w:numbering>'))


def _single_level(start: int = 1, *, fmt: str = "decimal", text: str | None = "%1.") -> NumberingResolver:
    return _resolver(_abstract(1, _lvl(0, fmt=fmt, text=text, start=start)), _num(1, 1))


# ---------------------------------------------------------------- 基本形态


def test_missing_numbering_part_yields_no_label():
    resolver = NumberingResolver(None)
    label = resolver.resolve(1, 0)
    assert isinstance(label, NumberingLabel)
    assert label.text is None
    assert label.unsupported_formats == ()


def test_unknown_num_id_yields_no_label():
    resolver = _single_level()
    assert resolver.resolve(99, 0).text is None


def test_zero_num_id_means_no_numbering():
    resolver = _single_level()
    assert resolver.resolve(0, 0).text is None


def test_unknown_level_yields_no_label():
    resolver = _single_level()
    assert resolver.resolve(1, 3).text is None


def test_level_without_lvl_text_yields_no_label():
    resolver = _single_level(text=None)
    assert resolver.resolve(1, 0).text is None


def test_decimal_label_is_returned_without_trailing_separator():
    assert _single_level(start=3).resolve(1, 0).text == "3"


def test_label_text_keeps_surrounding_words():
    resolver = _single_level(start=3, text="第%1章")
    label = resolver.resolve(1, 0)
    assert label.text == "第3章"
    # 展示串有"第…章"，裸条号只有 3：citation_validity 比对的是后者。
    assert label.number == "3"


def test_multi_level_bare_number_uses_dots():
    resolver = _resolver(
        _abstract(1, _lvl(0), _lvl(1, text="%1.%2")),
        _num(1, 1),
    )
    resolver.resolve(1, 0)
    label = resolver.resolve(1, 1)
    assert label.text == "1.1"
    assert label.number == "1.1"


# ---------------------------------------------------------------- 多级与重置


def test_multi_level_counters_increment_and_reset():
    resolver = _resolver(
        _abstract(1, _lvl(0), _lvl(1, text="%1.%2")),
        _num(1, 1),
    )
    assert resolver.resolve(1, 0).text == "1"
    assert resolver.resolve(1, 1).text == "1.1"
    assert resolver.resolve(1, 1).text == "1.2"
    assert resolver.resolve(1, 0).text == "2"
    assert resolver.resolve(1, 1).text == "2.1"


def test_level_one_without_parent_uses_parent_start_value():
    resolver = _resolver(
        _abstract(1, _lvl(0, start=3), _lvl(1, text="%1.%2")),
        _num(1, 1),
    )
    assert resolver.resolve(1, 1).text == "3.1"


def test_start_attribute_is_respected():
    resolver = _single_level(start=7)
    assert resolver.resolve(1, 0).text == "7"
    assert resolver.resolve(1, 0).text == "8"


def test_start_override_applies_to_the_first_value():
    resolver = _resolver(
        _abstract(1, _lvl(0)),
        _num(1, 1, _start_override(0, 5)),
    )
    assert resolver.resolve(1, 0).text == "5"
    assert resolver.resolve(1, 0).text == "6"


def test_lvl_override_replaces_the_abstract_level():
    resolver = _resolver(
        _abstract(1, _lvl(0)),
        _num(1, 1, _lvl_override(0, _lvl(0, fmt="upperLetter", text="%1."))),
    )
    assert resolver.resolve(1, 0).text == "A"


def test_lvl_restart_zero_means_never_restart():
    resolver = _resolver(
        _abstract(1, _lvl(0), _lvl(1, text="%1.%2", restart=0)),
        _num(1, 1),
    )
    assert resolver.resolve(1, 0).text == "1"
    assert resolver.resolve(1, 1).text == "1.1"
    assert resolver.resolve(1, 0).text == "2"
    # restart=0：父级递增不重置本级，继续累加。
    assert resolver.resolve(1, 1).text == "2.2"


def test_lvl_restart_two_restarts_on_level_two():
    resolver = _resolver(
        _abstract(1, _lvl(0), _lvl(1, text="%1.%2", restart=2)),
        _num(1, 1),
    )
    assert resolver.resolve(1, 0).text == "1"
    assert resolver.resolve(1, 1).text == "1.1"
    assert resolver.resolve(1, 0).text == "2"
    assert resolver.resolve(1, 1).text == "2.1"


def test_counters_are_per_num_instance():
    resolver = _resolver(
        _abstract(1, _lvl(0)),
        _num(1, 1),
        _num(2, 1),
    )
    assert resolver.resolve(1, 0).text == "1"
    assert resolver.resolve(1, 0).text == "2"
    assert resolver.resolve(2, 0).text == "1"


# ---------------------------------------------------------------- 格式覆盖


@pytest.mark.parametrize(
    ("fmt", "value", "expected"),
    [
        ("decimal", 3, "3"),
        ("decimalZero", 3, "03"),
        ("decimalZero", 12, "12"),
        ("chineseCounting", 1, "一"),
        ("chineseCounting", 10, "十"),
        ("chineseCounting", 11, "十一"),
        ("chineseCounting", 21, "二十一"),
        ("chineseCounting", 101, "一百零一"),
        ("chineseCountingThousand", 1000, "一千"),
        ("chineseCountingThousand", 1001, "一千零一"),
        ("upperLetter", 1, "A"),
        ("upperLetter", 26, "Z"),
        ("upperLetter", 27, "AA"),
        ("lowerLetter", 1, "a"),
        ("lowerLetter", 27, "aa"),
        ("upperRoman", 4, "IV"),
        ("upperRoman", 9, "IX"),
        ("upperRoman", 14, "XIV"),
        ("lowerRoman", 4, "iv"),
    ],
)
def test_supported_number_formats(fmt, value, expected):
    assert _single_level(start=value, fmt=fmt).resolve(1, 0).text == expected


def test_unsupported_format_falls_back_to_decimal_and_is_reported():
    resolver = _single_level(start=2, fmt="decimalEnclosedCircle")
    label = resolver.resolve(1, 0)
    assert label.text == "2"
    assert "decimalEnclosedCircle" in label.unsupported_formats


def test_bullet_numbering_does_not_become_a_clause_label():
    # 项目符号不是条号：把它当条号会让每个列表项都变成伪造的条款引用。
    resolver = _single_level(fmt="bullet", text="•")
    label = resolver.resolve(1, 0)
    assert label.text is None
    assert "bullet" in label.unsupported_formats


def test_none_numbering_does_not_become_a_clause_label():
    resolver = _single_level(fmt="none")
    label = resolver.resolve(1, 0)
    assert label.text is None
    assert "none" in label.unsupported_formats


def test_lvl_text_referencing_unknown_level_uses_its_start():
    resolver = _resolver(
        _abstract(1, _lvl(0), _lvl(1, text="%1.%2"), _lvl(2, text="%1.%2")),
        _num(1, 1),
    )
    # 只解析到 ilvl=1，但 lvlText 引用 %2 → 用 level 2 的 start=1。
    assert resolver.resolve(1, 1).text == "1.1"


def test_resolver_ignores_malformed_entries_without_crashing():
    resolver = _resolver(
        '<w:abstractNum w:abstractNumId="broken"/>',
        '<w:num w:numId="broken"><w:abstractNumId w:val="1"/></w:num>',
        _abstract(1, _lvl(0)),
        _num(1, 1),
    )
    assert resolver.resolve(1, 0).text == "1"
    assert resolver.resolve(5, 0).text is None
