"""切分测试：条款感知、表格原子化、句子窗口退化。"""

from __future__ import annotations

from conftest import make_block, make_page
from docqa.chunker import chunk_document, split_sentences


class TestStructuredChunking:
    def test_clause_labels_are_captured(self, structured_doc, standard_profile):
        chunks, stats = chunk_document([structured_doc], "d", standard_profile)
        labels = {c.clause_label for c in chunks}
        assert {"3.1", "3.6"} <= labels
        assert stats["structured"] is True
        assert stats["used_sentence_fallback"] is False
        assert stats["citation_mode"] == "clause"

    def test_clause_path_records_nesting(self, structured_doc, standard_profile):
        chunks, _ = chunk_document([structured_doc], "d", standard_profile)
        c36 = next(c for c in chunks if c.clause_label == "3.6")
        assert "3.6" in c36.clause_path

    def test_table_is_atomic_and_never_split(self, structured_doc, standard_profile):
        """表格必须恰好是一个 chunk，绝不与相邻正文合并、也绝不被切断。"""
        chunks, stats = chunk_document([structured_doc], "d", standard_profile)
        tables = [c for c in chunks if c.kind == "table"]
        assert len(tables) == 1
        assert stats["tables"] == 1
        assert "AQL" in tables[0].text

    def test_chunk_ids_are_unique(self, structured_doc, standard_profile):
        chunks, _ = chunk_document([structured_doc], "d", standard_profile)
        assert len({c.chunk_id for c in chunks}) == len(chunks)


class TestDegradation:
    def test_flat_document_falls_back_to_sentence_windows(self, flat_doc, notice_profile):
        """无结构连续正文必须触发退化路径，否则检索单位会退化成整页。"""
        chunks, stats = chunk_document([flat_doc], "d", notice_profile)
        assert stats["used_sentence_fallback"] is True
        assert stats["citation_mode"] == "paragraph"
        assert all(c.clause_label is None for c in chunks)

    def test_fallback_chunks_reference_paragraph_numbers(self, flat_doc, notice_profile):
        chunks, _ = chunk_document([flat_doc], "d", notice_profile)
        assert all(c.ordinal >= 1 for c in chunks)

    def test_long_clause_is_split_with_label_preserved(self, standard_profile):
        from conftest import make_block, make_page
        long_body = "这是很长的条款内容。" * 120
        doc = make_page([
            make_block(0, "clause", "5.1 长条款", "5.1"),
            make_block(1, "paragraph", long_body),
        ])
        chunks, _ = chunk_document([doc], "d", standard_profile)
        clause_chunks = [c for c in chunks if c.kind == "clause"]
        assert len(clause_chunks) > 1, "超长条款应被二次切分"
        assert all(c.clause_label == "5.1" for c in clause_chunks)


class TestPageBoundarySplit:
    """一个 section 不跨页：跨页会同时破坏引用页码、页图 bbox 与检索。

    回归本源：GB/T 1568 的前言页（page 2）正文被并进了封面所在的 section，
    导致 page 2 一个 chunk 都没有，"历次版本 1979/1997" 检索不到、被误拒。
    """

    def test_unlabeled_section_does_not_swallow_next_page(self, standard_profile):
        """封面（无条号）+ 前言（无条号）必须各自成片、各自落在自己的页。"""
        page1 = make_page(
            [make_block(0, "paragraph", "某某标准封面 2008-09-22 发布", page=1)], page_no=1
        )
        page2 = make_page(
            [make_block(0, "paragraph", "本标准所代替标准的历次版本为 GB 1568—1979。", page=2)],
            page_no=2,
        )
        chunks, _ = chunk_document([page1, page2], "d", standard_profile)
        assert any(c.page_no == 2 for c in chunks), (
            "前言页没有独立 chunk——它被并进了上一页的 section"
        )
        assert any("1979" in c.text for c in chunks if c.page_no == 2)

    def test_clause_spanning_pages_split_but_keeps_label(self, standard_profile):
        """跨页的同一 clause 拆成两条、各自定位到自己的页，但条号都保留。"""
        page1 = make_page(
            [make_block(0, "clause", "3.6 斜度公差", "3.6", page=1),
             make_block(1, "paragraph", "第一页的正文，未写完……", page=1)],
            page_no=1,
        )
        page2 = make_page(
            [make_block(0, "paragraph", "第二页的续写：角度公差按 AT8 级选取。", page=2)],
            page_no=2,
        )
        chunks, _ = chunk_document([page1, page2], "d", standard_profile)
        p1 = [c for c in chunks if c.page_no == 1 and c.clause_label == "3.6"]
        p2 = [c for c in chunks if c.page_no == 2 and c.clause_label == "3.6"]
        assert p1 and p2, "跨页条款应拆成两条、且都保留 3.6 条号"
        assert "AT8" in p2[0].text


class TestSentenceSplit:
    def test_splits_on_chinese_punctuation(self):
        assert len(split_sentences("第一句。第二句！第三句？")) == 3

    def test_splits_on_semicolon(self):
        assert len(split_sentences("条件一；条件二；条件三。")) >= 2

    def test_empty_input(self):
        assert split_sentences("") == []


class TestHeaderFooterFurniture:
    """**回归**：只有**跨页重复**的页眉页脚才算版面噪声。

    原先一律丢弃。那对重复出现的书名/页码是对的，对**封面**是错的——
    封面底部恰恰是发布/实施日期、发布机构这类正式元数据。

    实测踩到过：GB/T 封面把 `2008-09-22 发布` / `2009-05-01 实施` 判成
    `footer`，切分时整块丢掉。于是"发布日期是哪天"这个问题在检索里
    **永远找不到证据**，只能拒答成"文档里没有"——识别明明是对的，
    是切分把证据丢了，而表面上看起来像"文档没写"。
    """

    def test_one_off_footer_is_kept(self, standard_profile):
        """只在一页上出现的 footer 是**内容**，必须留下。"""
        page = make_page([
            make_block(0, "clause", "1 范围", "1"),
            make_block(1, "paragraph", "本文件规定了示例件的要求。"),
            make_block(2, "footer", "2008-09-22 发布"),
        ])
        chunks, _ = chunk_document([page], "d", standard_profile)
        assert any("2008-09-22" in c.text for c in chunks), (
            "一次性的 footer 被当成版面噪声丢掉了——封面的发布/实施日期就是这样丢的"
        )

    def test_repeated_footer_is_dropped(self, standard_profile):
        """跨页重复出现的才是版面噪声，仍然要丢。"""
        pages = [
            make_page([make_block(0, "clause", "1 范围", "1"),
                       make_block(1, "paragraph", "正文甲。"),
                       make_block(2, "footer", "GB/T 1568—2008")], page_no=1),
            make_page([make_block(0, "clause", "2 引用", "2"),
                       make_block(1, "paragraph", "正文乙。"),
                       make_block(2, "footer", "GB/T 1568—2008")], page_no=2),
        ]
        chunks, _ = chunk_document(pages, "d", standard_profile)
        # 页码式的页眉页脚不该进检索；它出现在每一页，没有检索价值
        assert not any(c.text.strip() == "GB/T 1568—2008" for c in chunks)

    def test_repeated_header_dropped_but_distinct_one_kept(self, standard_profile):
        """同一页里：重复的丢、独有的留。判据是**是否重复**，不是类型。"""
        pages = [
            make_page([make_block(0, "header", "某某标准"),
                       make_block(1, "clause", "1 范围", "1"),
                       make_block(2, "paragraph", "正文甲。"),
                       make_block(3, "footer", "2008-09-22 发布")], page_no=1),
            make_page([make_block(0, "header", "某某标准"),
                       make_block(1, "clause", "2 引用", "2"),
                       make_block(2, "paragraph", "正文乙。"),
                       make_block(3, "footer", "1997-05-01 实施")], page_no=2),
        ]
        chunks, _ = chunk_document(pages, "d", standard_profile)
        joined = "\n".join(c.text for c in chunks)
        assert "某某标准" not in joined, "跨页重复的页眉应当被丢弃"
        assert "2008-09-22 发布" in joined, "只在第 1 页出现的页脚应当保留"
        assert "1997-05-01 实施" in joined, "只在第 2 页出现的页脚应当保留"
