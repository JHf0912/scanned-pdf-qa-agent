"""标题栈切分的测试。

切分要同时满足三件事，它们互相拉扯：

1. **可定位**：每个非空正文块都必须被至少一个 chunk 覆盖，且 chunk 带
   `para_no..para_end`，引用 `[§3.6 ¶4]` 才能落回证据；
2. **结构随行**：`clause_path` 从外到内（`("1 总则", "1.1 范围")`），
   `section` 优先裸条号、否则标题；
3. **表格原子化**：一张表永远是一个 chunk，再长也不切——切断了数值就会
   落到错误的上下文里。

`¶n` 的定义（冻结契约）：文档顺序第 n 个**非空正文块**，
标题计入、空段落不计、线性化表格占一个号。
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from docx_fixtures import DocxFactory

from docqa.config import ChunkConfig, DomainProfile
from docqa.docx import chunks as chunks_module
from docqa.docx.chunks import PlannedChunk, build_docx_chunks, plan_docx_chunks
from docqa.docx.parser import DocxBlock, DocxDocument, DocxStats, parse_docx
from docqa.docx.security import DocxSecurity
from docqa.errors import InputError


@pytest.fixture
def docx_factory(tmp_path):
    """现场生成 DOCX 的工厂（见 `docx_fixtures.py`）。"""
    return DocxFactory(tmp_path)

SECURITY = DocxSecurity(
    entries=1,
    compressed_bytes=1,
    uncompressed_bytes=1,
    document_xml_bytes=1,
    external_relationships=0,
    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
)


def _profile(max_chars: int = 800, overlap: int = 100) -> DomainProfile:
    return DomainProfile(
        name="docx-test",
        chunk=ChunkConfig(max_chars=max_chars, overlap_chars=overlap),
    )


def _heading(
    para_no: int,
    text: str,
    *,
    label: str | None = None,
    label_text: str | None = None,
    title: str | None = None,
    level: int = 1,
) -> DocxBlock:
    return DocxBlock(
        body_index=para_no - 1,
        para_no=para_no,
        kind="heading",
        text=text,
        title=text if title is None else title,
        level=level,
        clause_label=label,
        label_text=label_text if label_text is not None else label,
    )


def _paragraph(
    para_no: int,
    text: str,
    *,
    label: str | None = None,
    label_text: str | None = None,
) -> DocxBlock:
    return DocxBlock(
        body_index=para_no - 1,
        para_no=para_no,
        kind="paragraph",
        text=text,
        clause_label=label,
        label_text=label_text if label_text is not None else label,
    )


def _table(para_no: int, text: str, table_id: str = "t1") -> DocxBlock:
    return DocxBlock(
        body_index=para_no - 1,
        para_no=para_no,
        kind="table",
        text=text,
        table_id=table_id,
        rows=((text,),),
    )


def _document(*blocks: DocxBlock) -> DocxDocument:
    return DocxDocument(
        blocks=tuple(blocks),
        stats=DocxStats(unsupported={}),
        security=SECURITY,
        source_name="test.docx",
    )


def _parse(docx_factory, builder=None) -> DocxDocument:
    return parse_docx(docx_factory.bytes(builder), filename="test.docx")


# ---------------------------------------------------------------- 标题栈


def test_heading_stack_builds_clause_path(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("总则正文")
        doc.add_paragraph("1.1 范围", style="Heading 2")
        doc.add_paragraph("范围正文")
        doc.add_paragraph("2 术语", style="Heading 1")
        doc.add_paragraph("术语正文")

    chunks, _ = plan_docx_chunks(_parse(docx_factory, build), _profile())
    by_para = {chunk.para_no: chunk for chunk in chunks}

    assert by_para[1].clause_path == ("1 总则",)
    assert by_para[1].clause_label == "1"
    assert by_para[1].section == "1"
    assert by_para[1].kind == "heading"

    assert by_para[4].clause_path == ("1 总则", "1.1 范围")
    assert by_para[4].clause_label == "1.1"
    assert by_para[4].section == "1.1"

    assert by_para[6].clause_path == ("2 术语",)
    assert by_para[6].section == "2"


def test_heading_stack_pops_on_same_or_higher_level():
    document = _document(
        _heading(1, "1 总则", label="1", title="总则", level=1),
        _heading(2, "1.1 范围", label="1.1", title="范围", level=2),
        _heading(3, "1.2 术语", label="1.2", title="术语", level=2),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    assert chunks[1].clause_path == ("1 总则", "1.1 范围")
    assert chunks[2].clause_path == ("1 总则", "1.2 术语")


def test_heading_level_jump_is_tolerated():
    document = _document(
        _heading(1, "1 总则", label="1", title="总则", level=1),
        _heading(2, "1.1.1 细节", label="1.1.1", title="细节", level=3),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    assert chunks[1].clause_path == ("1 总则", "1.1.1 细节")


def test_heading_without_label_uses_title_as_section():
    document = _document(
        _heading(1, "总则", label=None, title="总则"),
        _paragraph(2, "正文"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    assert chunks[0].clause_label is None
    assert chunks[0].section == "总则"
    assert chunks[1].section == "总则"
    assert chunks[1].clause_label is None


def test_no_headings_falls_back_to_paragraph_mode():
    document = _document(_paragraph(1, "第一段"), _paragraph(2, "第二段"))
    chunks, stats = plan_docx_chunks(document, _profile())
    assert stats["structured"] is False
    assert stats["citation_mode"] == "paragraph"
    assert all(chunk.clause_path == () for chunk in chunks)
    assert all(chunk.section is None for chunk in chunks)
    assert all(chunk.clause_label is None for chunk in chunks)


# ---------------------------------------------------------------- ¶n 覆盖


def test_every_nonempty_block_is_covered(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("")
        doc.add_paragraph("正文一段")
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "表格值"
        doc.add_paragraph("正文二段")

    document = _parse(docx_factory, build)
    chunks, _ = plan_docx_chunks(document, _profile())
    for block in document.blocks:
        assert any(
            chunk.para_no <= block.para_no <= chunk.para_end for chunk in chunks
        ), f"块 ¶{block.para_no} 未被任何 chunk 覆盖"


def test_heading_chunks_are_separate_and_not_duplicated(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("正文内容")

    chunks, _ = plan_docx_chunks(_parse(docx_factory, build), _profile())
    heading_chunks = [c for c in chunks if c.kind == "heading"]
    body_chunks = [c for c in chunks if c.kind != "heading"]
    assert len(heading_chunks) == 1
    assert heading_chunks[0].text == "1 总则"
    assert heading_chunks[0].para_no == heading_chunks[0].para_end == 1
    # 正文 chunk 只带一行节头 + 正文，标题文字不会作为正文内容重复出现。
    assert body_chunks[0].text == "1 总则\n正文内容"


def test_paragraphs_are_grouped_up_to_max_chars():
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, "第一段。"),
        _paragraph(3, "第二段。"),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=200))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) == 1
    assert body[0].para_no == 2
    assert body[0].para_end == 3
    assert body[0].text.startswith("1 总则\n")
    assert "第一段。" in body[0].text and "第二段。" in body[0].text
    assert body[0].block_ids == ("b2", "b3")


def test_long_paragraph_is_split_on_sentences():
    text = "".join(f"第{i}句内容。" for i in range(40))
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, text),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=60, overlap=0))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) > 1
    assert all(c.para_no == 2 and c.para_end == 2 for c in body)
    assert "".join(c.text for c in body).count("句内容。") == 40
    header = "1 总则\n"
    assert all(c.text.startswith(header) for c in body)
    assert all(len(c.text) - len(header) <= 60 for c in body)


def test_every_split_piece_carries_the_section_label():
    # 长章节切多片时，每一片都要带节头，否则 "见 3.6 条" 在后几片里没有依据。
    text = "".join(f"第{i}句内容。" for i in range(40))
    document = _document(
        _heading(1, "3.6 楔键", label="3.6", title="楔键", level=2),
        _paragraph(2, text),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=60, overlap=0))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) > 1
    assert all("3.6" in c.text for c in body)
    assert all(c.clause_label == "3.6" for c in body)


def test_split_pieces_share_the_overlap_tail():
    text = "".join(f"第{i}句内容。" for i in range(40))
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, text),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=60, overlap=5))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) > 1
    header = "1 总则\n"
    bodies = [c.text[len(header) :] for c in body]
    for previous, following in pairwise(bodies):
        assert following[:5] == previous[-5:]


def test_table_is_atomic_even_when_long():
    table_text = " | ".join(f"数值{i}" for i in range(50))
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _table(2, table_text, table_id="t7"),
    )
    chunks, stats = plan_docx_chunks(document, _profile(max_chars=20, overlap=0))
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert tables[0].text == f"1 总则\n{table_text}"
    assert tables[0].table_id == "t7"
    assert tables[0].para_no == tables[0].para_end == 2
    assert stats["tables"] == 1


def test_table_inherits_heading_section():
    document = _document(
        _heading(1, "3.6 楔键", label="3.6", title="楔键"),
        _table(2, "抗压强度 | 42"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    table_chunk = chunks[-1]
    assert table_chunk.clause_label == "3.6"
    assert table_chunk.section == "3.6"
    assert table_chunk.clause_path == ("3.6 楔键",)


def test_bare_label_is_used_for_clause_label_and_display_for_section():
    document = _document(
        _heading(1, "第3章 总则", label="3", label_text="第3章", title="总则"),
        _paragraph(2, "正文"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    assert chunks[0].clause_label == "3"
    assert chunks[0].section == "第3章"
    assert chunks[0].clause_path == ("第3章 总则",)
    assert chunks[1].clause_label == "3"
    assert chunks[1].section == "第3章"


def test_section_is_cleaned_for_citation_brackets():
    document = _document(
        _heading(1, "总则]\n范围", title="总则]\n范围"),
        _paragraph(2, "正文"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    assert chunks[0].section == "总则 范围"
    assert chunks[1].section == "总则 范围"
    # 原始标题不做清洗，留在 clause_path 里可回溯。
    assert chunks[0].clause_path == ("总则]\n范围",)
    assert "\n" not in chunks[1].section
    assert "]" not in chunks[1].section


def test_body_clause_label_becomes_its_own_anchor():
    document = _document(
        _heading(1, "3 技术要求", label="3", title="技术要求"),
        _paragraph(2, "楔键的斜度应为 1:100。", label="3.6.1", label_text="3.6.1"),
        _paragraph(3, "后续说明。"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    by_para = {chunk.para_no: chunk for chunk in chunks}

    anchored = by_para[2]
    assert anchored.clause_label == "3.6.1"
    assert anchored.section == "3.6.1"
    assert anchored.clause_path == ("3 技术要求", "3.6.1")
    # 条号必须进入证据文本，否则检索/引用看不到它。
    assert anchored.text.startswith("3.6.1 ")

    # 后续无条号段落仍归在最近的标题下，不被上一条的条号“继承”。
    assert by_para[3].clause_label == "3"
    assert by_para[3].section == "3"


def test_body_clause_label_already_in_text_is_not_duplicated():
    document = _document(
        _heading(1, "3 技术要求", label="3", title="技术要求"),
        _paragraph(2, "3.6.1 楔键的斜度应为 1:100。", label="3.6.1", label_text="3.6.1"),
    )
    chunk = plan_docx_chunks(document, _profile())[0][1]
    assert chunk.text == "3.6.1 楔键的斜度应为 1:100。"


def test_long_body_clause_keeps_label_on_every_piece():
    text = "".join(f"第{i}句。" for i in range(40))
    document = _document(
        _heading(1, "3 技术要求", label="3", title="技术要求"),
        _paragraph(2, text, label="3.6.1", label_text="3.6.1"),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=60, overlap=0))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) > 1
    assert all(c.text.startswith("3.6.1 ") for c in body)
    assert all(c.clause_label == "3.6.1" for c in body)
    assert "".join(c.text for c in body).count("句。") == 40


def test_flat_list_number_is_not_promoted_to_a_clause():
    document = _document(
        _heading(1, "3 技术要求", label="3", title="技术要求"),
        _paragraph(2, "列表项一", label="1", label_text="1"),
        _paragraph(3, "列表项二", label="2", label_text="2"),
    )
    chunks, _ = plan_docx_chunks(document, _profile())
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) == 1
    assert body[0].clause_label == "3"
    assert body[0].section == "3"
    assert "1 列表项一" not in body[0].text


def test_empty_document_yields_no_chunks():
    chunks, stats = plan_docx_chunks(_document(), _profile())
    assert chunks == []
    assert stats["chunks"] == 0
    assert stats["structured"] is False


def test_heading_only_document_yields_one_chunk():
    chunks, _ = plan_docx_chunks(_document(_heading(1, "总则")), _profile())
    assert len(chunks) == 1
    assert chunks[0].kind == "heading"


# ---------------------------------------------------------------- Chunk 契约


def test_build_chunks_fills_the_frozen_contract():
    document = _document(
        _heading(1, "3.6 楔键", label="3.6", title="楔键"),
        _paragraph(2, "楔键的斜度应为 1:100。"),
        _table(3, "项目 | 值", table_id="t1"),
    )
    chunks, stats = build_docx_chunks(document, "doc-1", _profile())

    assert [c.chunk_id for c in chunks] == ["doc-1:b1", "doc-1:b2", "doc-1:b3"]
    assert [c.ordinal for c in chunks] == [1, 2, 3]
    assert all(c.doc_id == "doc-1" for c in chunks)
    assert all(c.locator_kind == "paragraph" for c in chunks)
    assert all(c.page_no is None for c in chunks)
    assert all(c.para_end >= c.para_no for c in chunks)
    assert all(c.char_len == len(c.text) for c in chunks)
    assert stats["doc_id"] == "doc-1"
    assert stats["chunks"] == 3


def test_build_chunks_matches_plan():
    document = _document(_heading(1, "总则"), _paragraph(2, "正文。"))
    planned, _ = plan_docx_chunks(document, _profile())
    chunks, _ = build_docx_chunks(document, "doc-2", _profile())
    assert [(c.ordinal, c.text) for c in chunks] == [(p.ordinal, p.text) for p in planned]
    assert isinstance(planned[0], PlannedChunk)


def test_build_chunks_is_deterministic():
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, "甲。"),
        _paragraph(3, "乙。"),
    )
    first, stats_first = build_docx_chunks(document, "doc", _profile())
    second, stats_second = build_docx_chunks(document, "doc", _profile())
    assert first == second
    assert stats_first == stats_second


def test_build_rejects_blocks_left_uncovered(monkeypatch):
    # 覆盖不变量被破坏时必须当场失败：产出一份少了几段的证据库，
    # 比 ingest 报错危险得多（问答会自信地说"文档里没有"）。
    document = _document(_heading(1, "总则"), _paragraph(2, "正文"))
    planned, stats = plan_docx_chunks(document, _profile())
    kept = [chunk for chunk in planned if chunk.kind == "heading"]
    monkeypatch.setattr(
        chunks_module, "plan_docx_chunks", lambda *args, **kwargs: (kept, stats)
    )
    with pytest.raises(InputError) as exc:
        build_docx_chunks(document, "doc", _profile())
    assert "¶2" in str(exc.value)
    assert "不变量" in str(exc.value)


def test_stats_report_citation_mode_for_structured_documents(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("正文")

    _, stats = plan_docx_chunks(_parse(docx_factory, build), _profile())
    assert stats["structured"] is True
    assert stats["used_sentence_fallback"] is False
    assert stats["citation_mode"] == "clause"


def test_heading_paragraph_range_uses_single_block():
    chunks, _ = plan_docx_chunks(_document(_heading(5, "总则")), _profile())
    assert chunks[0].para_no == 5
    assert chunks[0].para_end == 5


def test_chunk_kinds_are_from_the_contract():
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, "正文"),
        _table(3, "值"),
    )
    chunks, _ = build_docx_chunks(document, "doc", _profile())
    assert {c.kind for c in chunks} == {"heading", "paragraph", "table"}
    for chunk in chunks:
        assert chunk.kind in {"clause", "paragraph", "table", "heading"}


def test_profile_defaults_drive_max_chars(docx_factory):
    def build(doc):
        doc.add_paragraph("1 总则", style="Heading 1")
        doc.add_paragraph("甲" * 50)
        doc.add_paragraph("乙" * 50)

    chunks, _ = plan_docx_chunks(_parse(docx_factory, build), _profile(max_chars=60))
    body = [c for c in chunks if c.kind != "heading"]
    assert len(body) == 2
    assert all(len(c.text) <= 60 for c in body)


@pytest.mark.parametrize("max_chars", [10, 50, 800])
def test_coverage_holds_for_any_max_chars(max_chars):
    document = _document(
        _heading(1, "1 总则", label="1", title="总则"),
        _paragraph(2, "甲。" * 30),
        _table(3, "值 | 1"),
        _paragraph(4, "乙。" * 30),
    )
    chunks, _ = plan_docx_chunks(document, _profile(max_chars=max_chars, overlap=0))
    for block in document.blocks:
        assert any(chunk.para_no <= block.para_no <= chunk.para_end for chunk in chunks)
