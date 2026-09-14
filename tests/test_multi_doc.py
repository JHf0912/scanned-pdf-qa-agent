"""多文档检索：合并装载、索引重建、以及**文档作用域的失败页判断**。

最后那一条是重点。`failed_pages` 原先是**按页码裸比**的
（`{c.page_no} & failed_pages`），单文档时没问题，多文档时就是个安全漏洞的镜像：

- 漏报方向：不会发生（页码只会多匹配，不会少匹配）；
- **误报方向**：A 文档第 3 页识别失败、B 文档第 3 页好好的，
  B 的证据会被判成"触及失败页"而**误拒答**。

误拒答不危险，但**它会毁掉拒答的可信度**——用户被无缘无故拒几次之后，
就会学会忽略所有拒答。所以这条也要防。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from docqa.config import load_config
from docqa.errors import InputError
from docqa.index import HybridIndex
from docqa.models import Chunk
from docqa.pipeline import ingest, load_result, load_results
from docqa.qa import QaAgent
from docqa.qa.policy import decide


def mk(cid: str, text: str, *, doc: str, page: int = 1,
       label: str = "3.1") -> Chunk:
    return Chunk(
        chunk_id=f"{doc}:{cid}", doc_id=doc, page_no=page, ordinal=1,
        kind="clause", text=text, clause_path=(label,), clause_label=label,
    )


class _FixedEngine:
    """每页返回固定文本的假引擎。"""

    supports_vision = True

    def __init__(self, name: str, texts: dict[int, str],
                 *, fail_pages: set[int] | None = None):
        self.name = name
        self.model = name
        self._texts = texts
        self._fail = fail_pages or set()

    def ocr_page(self, image, *, profile=None):
        from docqa.errors import PageOcrError
        from docqa.models import OcrBlock, OcrPage, Usage

        if image.page_no in self._fail:
            raise PageOcrError(image.page_no, "假引擎的预定失败")
        return OcrPage(
            page_no=image.page_no,
            blocks=[OcrBlock(block_id=f"p{image.page_no}-b0", page_no=image.page_no,
                             type="clause", order=0, clause_label="3.1",
                             text=self._texts.get(image.page_no, ""))],
            tables=[], image_sha256=image.sha256,
            provider=self.name, model=self.model, prompt_version="v1",
            usage=Usage(),
        )

    def close(self) -> None:
        pass


def _scan_pdf(tmp_path: Path, name: str, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        img = tmp_path / f"{name}{i}.png"
        Image.new("RGB", (600, 800), "white").save(img)
        p = doc.new_page(width=300, height=400)
        p.insert_image(pymupdf.Rect(0, 0, 300, 400), filename=str(img))
    out = tmp_path / f"{name}.pdf"
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out


class TestFailedPageScoping:
    """**安全不变量**：失败页必须按 (doc_id, page_no) 比对。"""

    def test_failed_page_blocks_its_own_document(self):
        """本份文档的失败页确实要挡住——这是这条判据存在的理由。"""
        chunks = [mk("c1", "额定载荷应大于等于 12.5 kN。", doc="A", page=1)]
        d = decide(
            cfg=load_config().qa,
            top_score=9.0, second_score=1.0, gates_passed=True,
            failed_gate_names=[], evidence_chunks=chunks,
            failed_pages={("A", 1)}, candidates=[],
        )
        assert d.refused and d.reason.value == "extraction_failed"

    def test_other_documents_failed_page_does_not_block(self):
        """**回归**：A 文档第 1 页失败，不该让 B 文档第 1 页的证据被拒。

        原先的实现是 `{c.page_no for c in evidence_chunks} & failed_pages`——
        只比页码。多文档下这会把 B 的证据误判成"触及失败页"，
        用户被无缘无故拒答之后，就会学会忽略所有拒答。
        """
        chunks = [mk("c1", "额定载荷应大于等于 12.5 kN。", doc="B", page=1)]
        d = decide(
            cfg=load_config().qa,
            top_score=9.0, second_score=1.0, gates_passed=True,
            failed_gate_names=[], evidence_chunks=chunks,
            failed_pages={("A", 1)}, candidates=[],   # 失败的是 A，证据来自 B
        )
        assert not d.refused, f"B 的证据被 A 的失败页误伤了：{d.reason}"

    def test_same_page_number_scopes_both_ways(self):
        """同一个页码（都是第 2 页）在两份文档里，判定要**分别**看。

        这是最能说明"按页码裸比"错在哪的一组：
        同样的 `failed_pages={("A", 2)}`，同样的页码 2，
        证据来自 A 就该拒，来自 B 就不该拒。
        """
        cfg = load_config().qa
        kwargs = dict(
            cfg=cfg, top_score=9.0, second_score=1.0, gates_passed=True,
            failed_gate_names=[], failed_pages={("A", 2)}, candidates=[],
        )

        from_a = decide(evidence_chunks=[mk("ca", "A 文档的内容。", doc="A", page=2)],
                        **kwargs)
        assert from_a.refused, "证据来自失败的那一份，必须拒答"

        from_b = decide(
            evidence_chunks=[mk("cb", "B 文档：额定载荷 12.5 kN。", doc="B", page=2)],
            **kwargs,
        )
        assert not from_b.refused, "同样是第 2 页，但来自另一份文档，不该被牵连"


class TestAgentNormalization:
    def test_bare_page_numbers_are_scoped_to_the_doc(self):
        """调用方给裸页码（单文档的自然写法），Agent 内部补上 doc_id。"""
        idx = HybridIndex([mk("c1", "x", doc="A")], load_config_domain())
        agent = QaAgent(idx, None, load_config(), load_config_domain(),
                        failed_pages={1, 2}, doc_id="A")
        assert agent.failed_pages == {("A", 1), ("A", 2)}

    def test_tuples_pass_through_unchanged(self):
        idx = HybridIndex([mk("c1", "x", doc="A")], load_config_domain())
        agent = QaAgent(idx, None, load_config(), load_config_domain(),
                        failed_pages={("A", 1), ("B", 9)}, doc_id="A+B")
        assert agent.failed_pages == {("A", 1), ("B", 9)}


def load_config_domain():
    from docqa.config import load_domain

    cfg = load_config()
    return load_domain("standard", cfg)


class TestLoadResults:
    def _ingest(self, tmp_path: Path, name: str, text: str,
                *, fail: set[int] | None = None) -> Path:
        pdf = _scan_pdf(tmp_path, name, pages=2)
        out = tmp_path / f"out_{name}"
        r = ingest(pdf, cfg=load_config(), domain="standard",
                   engine=_FixedEngine(name, {1: text, 2: text}, fail_pages=fail),
                   out_root=out)
        return r.out_dir

    def test_single_dir_matches_load_result(self, tmp_path: Path):
        """只有一份时行为必须与 `load_result` 完全一致——不留第二种语义。"""
        d = self._ingest(tmp_path, "solo", "额定载荷 12.5 kN。")
        a = load_result(d, load_config(), "standard")
        b = load_results([d], load_config(), "standard")
        assert a.doc_id == b.doc_id
        assert a.failed_pages == b.failed_pages
        assert [c.chunk_id for c in a.chunks] == [c.chunk_id for c in b.chunks]

    def test_merges_chunks_from_both_documents(self, tmp_path: Path):
        d1 = self._ingest(tmp_path, "alpha", "甲文档：额定载荷 12.5 kN。")
        d2 = self._ingest(tmp_path, "beta", "乙文档：工作温度 -20 ℃。")
        m = load_results([d1, d2], load_config(), "standard")

        docs = {c.doc_id for c in m.chunks}
        assert len(docs) == 2, f"没有合并到两份文档：{docs}"
        assert "+" in m.doc_id
        # 引用要能看出答案来自哪份文件
        assert all(c.doc_id in c.chunk_id for c in m.chunks)

    def test_index_is_rebuilt_over_the_merged_corpus(self, tmp_path: Path):
        """**不能在各自索引上合并分数**：BM25 的 IDF 依赖整个语料，分头算不可比。"""
        d1 = self._ingest(tmp_path, "one", "甲文档：额定载荷 12.5 kN。")
        d2 = self._ingest(tmp_path, "two", "乙文档：工作温度 -20 ℃。")
        m = load_results([d1, d2], load_config(), "standard")
        assert len(m.index.chunks) == len(m.chunks)

        # 检索应能同时命中两份文档的内容
        q = m.index.search("额定载荷")
        assert q, "合并后的索引检索不到甲文档的内容"

    def test_failed_pages_become_doc_scoped(self, tmp_path: Path):
        d1 = self._ingest(tmp_path, "bad", "第一份", fail={1})
        d2 = self._ingest(tmp_path, "good", "第二份")
        m = load_results([d1, d2], load_config(), "standard")
        assert all(isinstance(k, tuple) and len(k) == 2 for k in m.failed_pages), (
            "多文档下失败页必须升级成 (doc_id, page_no)，否则页码会互相误伤"
        )

    def test_empty_list_is_an_error_not_a_silent_empty_index(self):
        """没有产物要**报错**，不能悄悄返回一个空索引——
        那会让"没有文档"伪装成"文档里没有这项内容"。"""
        with pytest.raises(InputError):
            load_results([], load_config(), "standard")

    def test_manifest_lists_each_document(self, tmp_path: Path):
        d1 = self._ingest(tmp_path, "m1", "第一份")
        d2 = self._ingest(tmp_path, "m2", "第二份")
        m = load_results([d1, d2], load_config(), "standard")
        assert len(m.manifest["documents"]) == 2
        assert {doc["chunks"] for doc in m.manifest["documents"]} == {len(m.chunks) // 2}
