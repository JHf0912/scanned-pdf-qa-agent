"""DOCX 正文解析的测试。

解析器的职责是"**不丢内容、不编内容**"：

- 按 body 真实顺序走（`document.paragraphs` + `document.tables` 会打乱交错顺序）；
- run walker 覆盖超链接、制表符、换行、修订插入、字段结果，排除删除文本与域指令；
- 标题按 `w:outlineLvl` → 样式名（`Heading N` / `标题 N`）判定，并去掉开头条号；
- 表格线性化保住数值（Phase 1 不解析合并网格）；
- 不支持的构造**计数上报**，不静默丢。
"""

from __future__ import annotations

import io

import pytest
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx_fixtures import DocxFactory
from PIL import Image

from docqa.config import DomainProfile
from docqa.docx import parser as parser_module
from docqa.docx import security
from docqa.docx.parser import DocxBlock, DocxDocument, DocxStats, parse_docx
from docqa.errors import InputError


@pytest.fixture
def docx_factory(tmp_path):
    """现场生成 DOCX 的工厂（见 `docx_fixtures.py`）。"""
    return DocxFactory(tmp_path)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
V_NS = "urn:schemas-microsoft-com:vml"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


# ---------------------------------------------------------------- 工具


def _parse(docx_factory, builder=None, *, profile=None) -> DocxDocument:
    return parse_docx(docx_factory.bytes(builder), filename="test.docx", profile=profile)


def _blocks(docx_factory, builder=None) -> list[DocxBlock]:
    return list(_parse(docx_factory, builder).blocks)


def _kinds(blocks: list[DocxBlock]) -> list[str]:
    return [b.kind for b in blocks]


def _append(paragraph, xml: str) -> None:
    paragraph._p.append(parse_xml(xml))


def _set_outline(paragraph, level: int) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pPr.append(parse_xml(f'<w:outlineLvl {nsdecls("w")} w:val="{level}"/>'))


def _add_numbering(doc, *, num_id: int, levels: str) -> None:
    numbering = doc.part.numbering_part.element
    numbering.append(
        parse_xml(
            f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{num_id}">{levels}</w:abstractNum>'
        )
    )
    numbering.append(
        parse_xml(
            f'<w:num {nsdecls("w")} w:numId="{num_id}">'
            f'<w:abstractNumId w:val="{num_id}"/></w:num>'
        )
    )


def _set_numbering(paragraph, num_id: int, ilvl: int = 0) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pPr.append(
        parse_xml(
            f'<w:numPr {nsdecls("w")}>'
            f'<w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/>'
            "</w:numPr>"
        )
    )


def _level(
    ilvl: int = 0, *, fmt: str = "decimal", text: str = "%1.", start: int = 1
) -> str:
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/>'
        f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/></w:lvl>'
    )


def _wrap_in_sdt(parent, element) -> None:
    """把 `element` 原地包进 `w:sdt/w:sdtContent`，保持原来的兄弟顺序。"""
    index = list(parent).index(element)
    sdt = parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtPr/></w:sdt>')
    content = parse_xml(f'<w:sdtContent {nsdecls("w")}/>')
    sdt.append(content)
    parent.remove(element)
    content.append(element)
    parent.insert(index, sdt)


def _add_part(doc, *, partname: str, content_type: str, xml: str, reltype: str) -> None:
    part = Part(
        PackURI(partname),
        content_type,
        xml.encode("utf-8"),
        doc.part.package,
    )
    doc.part.relate_to(part, reltype)


def _png_bytes(color: str = "white") -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------- 正文顺序


def test_preserves_interleaved_paragraph_table_paragraph_order(docx_factory):
    def build(doc):
        doc.add_paragraph("第一段")
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "值"
        doc.add_paragraph("第二段")

    blocks = _blocks(docx_factory, build)
    assert _kinds(blocks) == ["paragraph", "table", "paragraph"]
    assert [b.para_no for b in blocks] == [1, 2, 3]
    assert blocks[0].text == "第一段"
    assert blocks[2].text == "第二段"


def test_empty_paragraphs_are_not_counted_but_body_index_is_kept(docx_factory):
    def build(doc):
        doc.add_paragraph("A")
        doc.add_paragraph("")
        doc.add_paragraph("B")

    blocks = _blocks(docx_factory, build)
    assert [b.text for b in blocks] == ["A", "B"]
    assert [b.para_no for b in blocks] == [1, 2]
    assert [b.body_index for b in blocks] == [0, 2]


def test_sdt_content_is_descended_into(docx_factory):
    def build(doc):
        sdt = parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtPr/><w:sdtContent>'
            "<w:p><w:r><w:t>控件内</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>"
        )
        doc.element.body.insert(0, sdt)
        doc.add_paragraph("控件外")

    document = _parse(docx_factory, build)
    assert [b.text for b in document.blocks] == ["控件内", "控件外"]
    assert document.stats.unsupported["content_controls"] == 1


# ---------------------------------------------------------------- run walker


def test_tab_and_line_break_are_preserved(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("前")
        run = paragraph.add_run()
        run.add_tab()
        run.add_text("后")
        run.add_break()
        run.add_text("末行")

    block = _blocks(docx_factory, build)[0]
    assert "\t" in block.text
    assert "\n" in block.text
    assert block.text.startswith("前")
    assert block.text.endswith("末行")


def test_hyperlink_text_is_included(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("见")
        _append(
            paragraph,
            f'<w:hyperlink {nsdecls("w")} w:anchor="x"><w:r><w:t>附件一</w:t></w:r></w:hyperlink>',
        )

    assert "附件一" in _blocks(docx_factory, build)[0].text


def test_tracked_insertion_text_is_included(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("保留")
        _append(
            paragraph,
            f'<w:ins {nsdecls("w")} w:id="1" w:author="a" w:date="2024-01-01T00:00:00Z">'
            "<w:r><w:t>插入</w:t></w:r></w:ins>",
        )

    assert "插入" in _blocks(docx_factory, build)[0].text


def test_tracked_deletion_is_excluded_and_counted(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("正文")
        _append(
            paragraph,
            f'<w:del {nsdecls("w")} w:id="2" w:author="a" w:date="2024-01-01T00:00:00Z">'
            "<w:r><w:delText>已删除</w:delText></w:r></w:del>",
        )

    document = _parse(docx_factory, build)
    assert "已删除" not in document.blocks[0].text
    assert document.stats.unsupported["deleted_runs"] == 1


def test_field_instructions_are_excluded_but_results_are_included(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("页码")
        _append(
            paragraph,
            f'<w:r {nsdecls("w")}><w:instrText> PAGE </w:instrText></w:r>',
        )
        _append(
            paragraph,
            f'<w:fldSimple {nsdecls("w")} w:instr=" PAGE "><w:r><w:t>7</w:t></w:r></w:fldSimple>',
        )

    document = _parse(docx_factory, build)
    text = document.blocks[0].text
    assert "PAGE" not in text
    assert "7" in text
    assert document.stats.unsupported["fields"] >= 1


def test_no_break_hyphen_becomes_ascii_hyphen(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("AB")
        _append(paragraph, f'<w:r {nsdecls("w")}><w:noBreakHyphen/></w:r>')
        paragraph.add_run("CD")

    assert "AB-CD" in _blocks(docx_factory, build)[0].text


def test_hidden_text_is_dropped_and_counted(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("可见")
        hidden = paragraph.add_run("不可见")
        hidden.font.hidden = True

    document = _parse(docx_factory, build)
    block = document.blocks[0]
    # 冻结契约：隐藏文字丢弃并计数，绝不进入正文/证据。
    assert "不可见" not in block.text
    assert block.text == "可见"
    assert block.hidden_runs == 1
    assert document.stats.unsupported["hidden_runs"] == 1


def test_hidden_text_via_character_style_is_dropped(docx_factory):
    def build(doc):
        hidden_style = doc.styles.add_style("隐藏字符", WD_STYLE_TYPE.CHARACTER)
        hidden_style.font.hidden = True
        paragraph = doc.add_paragraph("可见")
        run = paragraph.add_run("样式隐藏")
        run.style = "隐藏字符"

    document = _parse(docx_factory, build)
    block = document.blocks[0]
    assert block.text == "可见"
    assert block.hidden_runs == 1
    assert document.stats.unsupported["hidden_runs"] == 1


def test_hidden_text_via_derived_character_style_is_dropped(docx_factory):
    def build(doc):
        base = doc.styles.add_style("隐藏字符", WD_STYLE_TYPE.CHARACTER)
        base.font.hidden = True
        derived = doc.styles.add_style("派生隐藏", WD_STYLE_TYPE.CHARACTER)
        derived.base_style = base
        paragraph = doc.add_paragraph("可见")
        run = paragraph.add_run("派生隐藏文字")
        run.style = "派生隐藏"

    document = _parse(docx_factory, build)
    assert document.blocks[0].text == "可见"
    assert document.blocks[0].hidden_runs == 1


def test_hidden_text_via_paragraph_style_is_dropped(docx_factory):
    def build(doc):
        style = doc.styles.add_style("隐藏段落", WD_STYLE_TYPE.PARAGRAPH)
        style.font.hidden = True
        doc.add_paragraph("整段隐藏", style="隐藏段落")
        doc.add_paragraph("可见段落")

    document = _parse(docx_factory, build)
    assert [block.text for block in document.blocks] == ["可见段落"]
    assert document.stats.unsupported["hidden_runs"] == 1


def test_text_box_content_is_not_body_text_and_is_counted(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("正文")
        _append(
            paragraph,
            f'<w:r {nsdecls("w")}><w:pict>'
            f'<v:shape xmlns:v="{V_NS}" style="width:10pt;height:10pt">'
            "<v:textbox><w:txbxContent><w:p><w:r><w:t>框内文字</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>",
        )

    document = _parse(docx_factory, build)
    assert "框内文字" not in document.blocks[0].text
    assert document.stats.unsupported["text_boxes"] == 1


def test_equation_text_is_best_effort_extracted_and_counted(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("公式")
        _append(
            paragraph,
            f'<m:oMath xmlns:m="{M_NS}"><m:r><m:t>x=1</m:t></m:r></m:oMath>',
        )

    document = _parse(docx_factory, build)
    assert "x=1" in document.blocks[0].text
    assert document.stats.unsupported["equations"] == 1


def test_embedded_object_is_counted(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("附件")
        _append(paragraph, f'<w:r {nsdecls("w")}><w:object><w:embed/></w:object></w:r>')

    document = _parse(docx_factory, build)
    assert document.stats.unsupported["embedded_objects"] == 1


def test_image_is_counted_and_not_parsed(docx_factory):
    def build(doc):
        doc.add_picture(_png_bytes())
        doc.add_paragraph("图后正文")

    document = _parse(docx_factory, build)
    assert document.stats.unsupported["images"] == 1
    assert "图后正文" in document.blocks[0].text


def test_alt_chunk_is_counted_and_not_silently_dropped(docx_factory):
    def build(doc):
        doc.add_paragraph("可见段落")
        doc.element.body.insert(
            1, parse_xml(f'<w:altChunk {nsdecls("w", "r")} r:id="rId9"/>')
        )

    document = _parse(docx_factory, build)
    assert [block.text for block in document.blocks] == ["可见段落"]
    assert document.stats.unsupported["alt_chunks"] == 1


def test_table_row_wrapped_in_content_control_is_linearised(docx_factory):
    def build(doc):
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "控件行"
        tbl = table._tbl
        row = tbl.tr_lst[0]
        sdt = parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtPr/></w:sdt>')
        content = parse_xml(f'<w:sdtContent {nsdecls("w")}/>')
        sdt.append(content)
        tbl.remove(row)
        content.append(row)
        tbl.append(sdt)

    block = _blocks(docx_factory, build)[0]
    assert block.text == "控件行"
    assert block.rows == (("控件行",),)


def test_body_level_alternate_content_is_descended_and_counted(docx_factory):
    def build(doc):
        doc.add_paragraph("普通段落")
        alt = parse_xml(
            f'<mc:AlternateContent xmlns:mc="{MC_NS}" {nsdecls("w")}>'
            '<mc:Choice Requires="wps"><w:p><w:r><w:t>优先分支</w:t></w:r></w:p></mc:Choice>'
            '<mc:Fallback><w:p><w:r><w:t>回退分支</w:t></w:r></w:p></mc:Fallback>'
            "</mc:AlternateContent>"
        )
        doc.element.body.insert(1, alt)

    document = _parse(docx_factory, build)
    texts = [block.text for block in document.blocks]
    assert "优先分支" in texts
    assert "回退分支" not in texts
    assert document.stats.unsupported["alternate_content"] == 1


# ---------------------------------------------------------------- 标题判定


def test_outline_level_wins_over_style(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("范围")
        _set_outline(paragraph, 2)

    block = _blocks(docx_factory, build)[0]
    assert block.kind == "heading"
    assert block.level == 3


def test_outline_level_nine_is_body_text(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("正文")
        _set_outline(paragraph, 9)

    assert _blocks(docx_factory, build)[0].kind == "paragraph"


@pytest.mark.parametrize(
    ("style", "level"),
    [("Heading 1", 1), ("Heading 4", 4)],
)
def test_english_heading_style_is_recognised(docx_factory, style, level):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("标题行", style=style))[0]
    assert block.kind == "heading"
    assert block.level == level


@pytest.mark.parametrize(
    ("name", "level"),
    [("标题 1", 1), ("标题 3", 3)],
)
def test_chinese_heading_style_is_recognised(docx_factory, name, level):
    def build(doc):
        doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        doc.add_paragraph("标题行", style=name)

    block = _blocks(docx_factory, build)[0]
    assert block.kind == "heading"
    assert block.level == level


def test_custom_style_inheriting_a_heading_style_is_recognised(docx_factory):
    def build(doc):
        custom = doc.styles.add_style("我的标题", WD_STYLE_TYPE.PARAGRAPH)
        custom.base_style = doc.styles["Heading 2"]
        doc.add_paragraph("范围", style="我的标题")

    block = _blocks(docx_factory, build)[0]
    assert block.kind == "heading"
    assert block.level == 2


def test_normal_paragraph_is_not_a_heading(docx_factory):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("普通正文"))[0]
    assert block.kind == "paragraph"
    assert block.level is None


def test_heading_leading_clause_number_is_split(docx_factory):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("3.6 楔键", style="Heading 2"))[0]
    assert block.clause_label == "3.6"
    assert block.label_text == "3.6"
    assert block.title == "楔键"
    assert block.text == "3.6 楔键"


def test_heading_chinese_chapter_label_is_split(docx_factory):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("第三章 总则", style="Heading 1"))[0]
    # clause_label 是裸条号（引用比对用），label_text 是展示串（section 用）。
    assert block.clause_label == "三"
    assert block.label_text == "第三章"
    assert block.title == "总则"


def test_heading_parenthesised_chinese_label_is_split(docx_factory):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("（二）范围", style="Heading 2"))[0]
    assert block.clause_label == "二"
    assert block.label_text == "（二）"
    assert block.title == "范围"


def test_heading_without_label_keeps_full_title(docx_factory):
    block = _blocks(docx_factory, lambda doc: doc.add_paragraph("总则", style="Heading 1"))[0]
    assert block.clause_label is None
    assert block.label_text is None
    assert block.title == "总则"


# ---------------------------------------------------------------- 无样式标题兜底

FALLBACK_PROFILE = DomainProfile(
    name="docx-test",
    clause_patterns=[r"^(?P<label>\d+(?:\.\d+)+)\s*(?P<title>.+)$"],
)


def test_short_paragraph_with_clause_pattern_becomes_heading(docx_factory):
    document = _parse(
        docx_factory, lambda doc: doc.add_paragraph("3.6 斜度公差"), profile=FALLBACK_PROFILE
    )
    block = document.blocks[0]
    assert block.kind == "heading"
    assert block.level == 2
    assert block.clause_label == "3.6"
    assert block.title == "斜度公差"


def test_explicit_body_outline_blocks_the_clause_pattern_fallback(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("3.6 斜度公差")
        _set_outline(paragraph, 9)

    document = _parse(docx_factory, build, profile=FALLBACK_PROFILE)
    assert document.blocks[0].kind == "paragraph"


def test_fallback_heading_requires_a_profile(docx_factory):
    document = _parse(docx_factory, lambda doc: doc.add_paragraph("3.6 斜度公差"))
    assert document.blocks[0].kind == "paragraph"


def test_long_paragraph_with_clause_pattern_is_not_a_heading(docx_factory):
    text = "3.6 " + "正文内容" * 40
    document = _parse(docx_factory, lambda doc: doc.add_paragraph(text), profile=FALLBACK_PROFILE)
    assert document.blocks[0].kind == "paragraph"


def test_numeric_paragraph_not_matching_profile_pattern_is_not_a_heading(docx_factory):
    document = _parse(
        docx_factory, lambda doc: doc.add_paragraph("2024 年度总结"), profile=FALLBACK_PROFILE
    )
    assert document.blocks[0].kind == "paragraph"


STANDARD_PROFILE = DomainProfile(
    name="standard-like",
    clause_patterns=[
        r"^\s*(?P<label>\d+(?:\.\d+){0,4})\s+(?P<title>\S.*)$",
        r"^\s*(?P<label>[a-z])\)\s*(?P<title>\S.*)$",
    ],
)


@pytest.mark.parametrize(
    "text",
    [
        "42 MPa 是最小抗压强度。",  # 数值句 + 句末标点
        "42 MPa 是最小抗压强度",  # 无句末标点，但"数值 + 单位"不是条号
        "3.6 斜度公差。",  # 条号形态，但整段是句子
        "a) 列项内容",  # 列项模式，不单独成条
    ],
)
def test_fallback_heading_rejects_sentence_like_paragraphs(docx_factory, text):
    document = _parse(docx_factory, lambda doc: doc.add_paragraph(text), profile=STANDARD_PROFILE)
    assert document.blocks[0].kind == "paragraph"


def test_fallback_heading_accepts_a_real_clause(docx_factory):
    document = _parse(
        docx_factory, lambda doc: doc.add_paragraph("3.6 斜度公差"), profile=STANDARD_PROFILE
    )
    assert document.blocks[0].kind == "heading"
    assert document.blocks[0].level == 2


# ---------------------------------------------------------------- 自动编号


def test_numbered_heading_gets_computed_label(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=90, levels=_level(0) + _level(1, text="%1.%2"))
        first = doc.add_paragraph("适用范围", style="Heading 1")
        _set_numbering(first, 90, 1)

    document = _parse(docx_factory, build)
    block = document.blocks[0]
    assert block.clause_label == "1.1"
    assert block.title == "适用范围"
    assert document.stats.numbering_computed == 1
    assert document.stats.numbering_literal == 0


def test_numbered_chapter_heading_keeps_bare_label(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=95, levels=_level(0, text="第%1章", start=3))
        paragraph = doc.add_paragraph("总则", style="Heading 1")
        _set_numbering(paragraph, 95, 0)

    block = _parse(docx_factory, build).blocks[0]
    # "第3章" 是展示串；引用一致性比对用的是裸的 "3"。
    assert block.clause_label == "3"
    assert block.label_text == "第3章"
    assert block.title == "总则"


def test_literal_label_takes_priority_over_computed(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=91, levels=_level(0))
        paragraph = doc.add_paragraph("3.6 楔键", style="Heading 2")
        _set_numbering(paragraph, 91, 0)

    document = _parse(docx_factory, build)
    assert document.blocks[0].clause_label == "3.6"
    assert document.stats.numbering_literal == 1
    assert document.stats.numbering_computed == 0


def test_unknown_num_id_is_counted_as_missing_and_warned(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("无编号定义", style="Heading 2")
        _set_numbering(paragraph, 12345, 0)

    document = _parse(docx_factory, build)
    assert document.blocks[0].clause_label is None
    assert document.stats.numbering_missing == 1
    assert any("编号" in warning for warning in document.stats.warnings)


def test_unsupported_number_format_is_reported(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=92, levels=_level(0, fmt="decimalEnclosedCircle"))
        paragraph = doc.add_paragraph("条款", style="Heading 2")
        _set_numbering(paragraph, 92, 0)

    document = _parse(docx_factory, build)
    assert document.blocks[0].clause_label == "1"
    assert "decimalEnclosedCircle" in document.stats.numbering_unsupported_formats


def test_bullet_numbering_does_not_become_a_clause_label(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=93, levels=_level(0, fmt="bullet", text="•"))
        paragraph = doc.add_paragraph("列表项")
        _set_numbering(paragraph, 93, 0)

    document = _parse(docx_factory, build)
    assert document.blocks[0].clause_label is None
    assert document.stats.numbering_missing == 0
    assert "bullet" in document.stats.numbering_unsupported_formats


# ---------------------------------------------------------------- 表格线性化


def test_table_is_linearized_with_pipe_separators(docx_factory):
    def build(doc):
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "值"
        table.cell(1, 0).text = "抗压强度"
        table.cell(1, 1).text = "42"

    block = _blocks(docx_factory, build)[0]
    assert block.kind == "table"
    assert block.text == "项目 | 值 | 抗压强度 | 42"
    assert block.table_id == "t1"
    assert block.structure_parsed is False
    assert block.rows == (("项目", "值"), ("抗压强度", "42"))


def test_table_second_row_wrapped_in_content_control_is_linearised(docx_factory):
    def build(doc):
        table = doc.add_table(rows=2, cols=2)
        for row, values in enumerate((("抗压", "42 MPa"), ("外观", "无裂纹"))):
            for column, value in enumerate(values):
                table.cell(row, column).text = value
        rows = table._tbl.tr_lst
        _wrap_in_sdt(table._tbl, rows[1])

    block = _blocks(docx_factory, build)[0]
    assert block.rows == (("抗压", "42 MPa"), ("外观", "无裂纹"))
    assert "42 MPa" in block.text


def test_table_cell_wrapped_in_content_control_is_linearised(docx_factory):
    def build(doc):
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "42 MPa"
        row = table._tbl.tr_lst[0]
        _wrap_in_sdt(row, row.tc_lst[1])

    block = _blocks(docx_factory, build)[0]
    assert block.rows == (("项目", "42 MPa"),)


def test_table_ids_are_sequential(docx_factory):
    def build(doc):
        for _ in range(2):
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).text = "值"

    blocks = _blocks(docx_factory, build)
    assert [b.table_id for b in blocks] == ["t1", "t2"]


def test_horizontally_merged_cell_is_emitted_once(docx_factory):
    def build(doc):
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "合并值"
        table.cell(0, 1).text = "另一列"
        tc = table._tbl.tr_lst[0].tc_lst[0]
        tcPr = tc.get_or_add_tcPr()
        tcPr.append(parse_xml(f'<w:gridSpan {nsdecls("w")} w:val="2"/>'))

    block = _blocks(docx_factory, build)[0]
    # 合并单元格本身只出现一次（gridSpan 不重复展开）。
    assert block.text.count("合并值") == 1
    assert "另一列" in block.text


def test_vertically_merged_continuation_cell_is_skipped(docx_factory):
    def build(doc):
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "跨行"
        table.cell(0, 1).text = "A"
        table.cell(1, 0).text = ""
        table.cell(1, 1).text = "B"
        first_tcPr = table._tbl.tr_lst[0].tc_lst[0].get_or_add_tcPr()
        first_tcPr.append(parse_xml(f'<w:vMerge {nsdecls("w")} w:val="restart"/>'))
        second_tcPr = table._tbl.tr_lst[1].tc_lst[0].get_or_add_tcPr()
        second_tcPr.append(parse_xml(f'<w:vMerge {nsdecls("w")}/>'))

    block = _blocks(docx_factory, build)[0]
    assert block.rows[0] == ("跨行", "A")
    assert block.rows[1] == ("B",)


def test_adjacent_identical_values_are_not_deduplicated(docx_factory):
    # 值相同不等于合并：一列里两格都写"是"是合法数据，盲目去重会丢值。
    def build(doc):
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "是"
        table.cell(0, 1).text = "是"

    block = _blocks(docx_factory, build)[0]
    assert block.rows == (("是", "是"),)
    assert block.text == "是 | 是"


def test_nested_table_is_counted(docx_factory):
    def build(doc):
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "外层"
        nested = table.cell(0, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "内层"

    document = _parse(docx_factory, build)
    assert document.stats.unsupported["nested_tables"] == 1


def test_empty_table_is_not_a_block(docx_factory):
    def build(doc):
        doc.add_table(rows=1, cols=1)
        doc.add_paragraph("正文")

    document = _parse(docx_factory, build)
    assert [b.kind for b in document.blocks] == ["paragraph"]
    assert document.stats.tables == 1
    assert document.stats.tables_linearized == 1


# ---------------------------------------------------------------- 部件级计数


def test_footnotes_and_endnotes_are_counted_excluding_separators(docx_factory):
    def build(doc):
        _add_part(
            doc,
            partname="/word/footnotes.xml",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
            xml=(
                f'<w:footnotes {nsdecls("w")}>'
                '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
                '<w:footnote w:type="continuationSeparator" w:id="0"><w:p/></w:footnote>'
                '<w:footnote w:id="1"><w:p><w:r><w:t>脚注</w:t></w:r></w:p></w:footnote>'
                "</w:footnotes>"
            ),
            reltype=RT.FOOTNOTES,
        )
        _add_part(
            doc,
            partname="/word/endnotes.xml",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
            xml=(
                f'<w:endnotes {nsdecls("w")}>'
                '<w:endnote w:id="1"><w:p><w:r><w:t>尾注</w:t></w:r></w:p></w:endnote>'
                "</w:endnotes>"
            ),
            reltype=RT.ENDNOTES,
        )
        doc.add_paragraph("正文")

    document = _parse(docx_factory, build)
    assert document.stats.unsupported["footnotes"] == 1
    assert document.stats.unsupported["endnotes"] == 1


def test_comments_are_counted(docx_factory):
    def build(doc):
        _add_part(
            doc,
            partname="/word/comments.xml",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
            xml=(
                f'<w:comments {nsdecls("w")}>'
                '<w:comment w:id="1"><w:p><w:r><w:t>批注</w:t></w:r></w:p></w:comment>'
                "</w:comments>"
            ),
            reltype=RT.COMMENTS,
        )
        doc.add_paragraph("正文")

    assert _parse(docx_factory, build).stats.unsupported["comments"] == 1


def test_headers_and_footers_are_counted(docx_factory):
    def build(doc):
        section = doc.sections[0]
        section.header.paragraphs[0].text = "页眉"
        section.footer.paragraphs[0].text = "页脚"
        doc.add_paragraph("正文")

    document = _parse(docx_factory, build)
    assert document.stats.unsupported["headers"] == 1
    assert document.stats.unsupported["footers"] == 1


# ---------------------------------------------------------------- 统计与限制


def test_stats_summarise_the_document(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("正文一")
        doc.add_paragraph("正文二")
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "值"

    document = _parse(docx_factory, build)
    stats = document.stats
    assert stats.paragraphs == 3
    assert stats.headings == 1
    assert stats.tables == 1
    assert stats.tables_linearized == 1
    assert len(document.blocks) == 4
    payload = stats.to_dict()
    assert payload["paragraphs"] == 3
    assert payload["numbering"] == {
        "literal": 1,
        "computed": 0,
        "missing": 0,
        "unsupported_formats": [],
    }
    assert set(payload["unsupported"]) == set(parser_module.UNSUPPORTED_KEYS)


def test_block_to_dict_exposes_block_level_fields(docx_factory):
    document = _parse(
        docx_factory, lambda doc: doc.add_paragraph("3.6 楔键", style="Heading 2")
    )
    payload = document.blocks[0].to_dict()
    assert payload["body_index"] == 0
    assert payload["para_no"] == 1
    assert payload["kind"] == "heading"
    assert payload["label"] == "3.6"
    assert payload["label_text"] == "3.6"
    assert payload["title"] == "楔键"
    assert payload["text"] == "3.6 楔键"
    assert payload["level"] == 2
    assert payload["rows"] == []


def test_unsupported_counter_always_has_every_key(docx_factory):
    stats = _parse(docx_factory, lambda doc: doc.add_paragraph("正文")).stats
    assert set(stats.unsupported) == set(parser_module.UNSUPPORTED_KEYS)
    assert all(value == 0 for value in stats.unsupported.values())


def test_empty_document_is_rejected(docx_factory):
    with pytest.raises(InputError) as exc:
        parse_docx(docx_factory.bytes())
    assert "正文" in str(exc.value)


def test_document_with_only_empty_paragraphs_is_rejected(docx_factory):
    def build(doc):
        for _ in range(3):
            doc.add_paragraph("")

    with pytest.raises(InputError) as exc:
        parse_docx(docx_factory.bytes(build))
    assert "正文" in str(exc.value)


def test_invalid_docx_is_input_error(docx_factory):
    with pytest.raises(InputError):
        parse_docx(b"not a docx")


def test_paragraph_limit_is_enforced(docx_factory, monkeypatch):
    def build(doc):
        for index in range(3):
            doc.add_paragraph(f"第{index}段")

    monkeypatch.setattr(security, "MAX_PARAGRAPHS", 2)
    with pytest.raises(InputError) as exc:
        parse_docx(docx_factory.bytes(build))
    assert "段落" in str(exc.value)


def test_char_limit_is_enforced(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_TOTAL_CHARS", 5)
    with pytest.raises(InputError) as exc:
        parse_docx(docx_factory.bytes(lambda doc: doc.add_paragraph("超过五个字符")))
    assert "字符" in str(exc.value)


def test_parse_accepts_path_and_keeps_filename(docx_factory):
    path = docx_factory.path(lambda doc: doc.add_paragraph("正文"), name="合同.docx")
    document = parse_docx(path)
    assert document.source_name == "合同.docx"
    assert document.blocks[0].text == "正文"


def test_document_exposes_security_report(docx_factory):
    document = _parse(docx_factory, lambda doc: doc.add_paragraph("正文"))
    assert document.security.entries > 0
    assert document.security.content_type == docx_factory.main_content_type


def test_warning_list_is_bounded(docx_factory):
    def build(doc):
        for _ in range(60):
            paragraph = doc.add_paragraph("无定义编号")
            _set_numbering(paragraph, 999, 0)

    document = _parse(docx_factory, build)
    assert document.stats.numbering_missing == 60
    assert len(document.stats.warnings) <= parser_module.MAX_WARNINGS
    assert any("等" in warning for warning in document.stats.warnings)


def test_re_layout_paragraph_text_handles_soft_returns(docx_factory):
    def build(doc):
        paragraph = doc.add_paragraph("甲")
        _append(paragraph, f'<w:r {nsdecls("w")}><w:cr/></w:r>')

    assert "\n" in _blocks(docx_factory, build)[0].text


def test_docx_stats_to_dict_round_trips_supported_formats(docx_factory):
    def build(doc):
        _add_numbering(doc, num_id=94, levels=_level(0, fmt="decimalEnclosedCircle"))
        paragraph = doc.add_paragraph("条款", style="Heading 2")
        _set_numbering(paragraph, 94, 0)

    payload = _parse(docx_factory, build).stats.to_dict()
    assert payload["numbering"]["unsupported_formats"] == ["decimalEnclosedCircle"]
    assert isinstance(payload["warnings"], list)

