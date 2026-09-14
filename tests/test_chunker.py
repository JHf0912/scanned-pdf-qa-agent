"""切分测试：条款感知、表格原子化、句子窗口退化。"""

from __future__ import annotations

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


class TestSentenceSplit:
    def test_splits_on_chinese_punctuation(self):
        assert len(split_sentences("第一句。第二句！第三句？")) == 3

    def test_splits_on_semicolon(self):
        assert len(split_sentences("条件一；条件二；条件三。")) >= 2

    def test_empty_input(self):
        assert split_sentences("") == []
