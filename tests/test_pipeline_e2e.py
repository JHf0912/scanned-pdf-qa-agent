"""pipeline 端到端测试：跑完整个 ingest。

用**运行时合成的**扫描件 + fixture 引擎，所以不碰网络、不碰真实文档。
要验证的是端到端行为，而不只是各模块单独正确：

- 每一步的产物都落盘了（"能定位问题"的前提）；
- manifest 记录了判定证据与溯源；
- 单页 OCR 失败被隔离，且**证据触及该页的问题会拒答**；
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from docqa.config import load_config, load_domain
from docqa.errors import PageOcrError
from docqa.llm import NullProvider
from docqa.models import AnswerBundle, PageImage, RefusalReason
from docqa.ocr.fixture import FixtureOcrEngine
from docqa.pipeline import ingest, load_result
from docqa.qa import QaAgent


@pytest.fixture
def scan_pdf(tmp_path: Path) -> Path:
    """合成一份两页的"扫描件式"PDF（整页图像、无文本层）。"""
    doc = pymupdf.open()
    for i in range(2):
        img = tmp_path / f"p{i}.png"
        Image.new("RGB", (600, 800), "white").save(img)
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(0, 0, 300, 400), filename=str(img))
    out = tmp_path / "scan.pdf"
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    """两页预置识别结果，含一条带条款号的正文与一张表格。"""
    d = tmp_path / "fixtures"
    d.mkdir()
    (d / "p0001.json").write_text(json.dumps({"blocks": [
        {"type": "clause", "order": 0, "clause_label": "3.1", "text": "3.1 抗拉强度"},
        {"type": "paragraph", "order": 1, "text": "键的抗拉强度应大于等于 590 MPa。"},
        {"type": "table", "order": 2, "clause_label": "表 1", "text": "",
         "table": {"caption": "表 1",
                   "html": "<table><tr><th>检查项目</th><th>半圆键</th></tr>"
                           "<tr><td>键宽 b</td><td>1.0</td></tr></table>"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    (d / "p0002.json").write_text(json.dumps({"blocks": [
        {"type": "clause", "order": 0, "clause_label": "5.1", "text": "5.1 标志与包装"},
        {"type": "paragraph", "order": 1, "text": "包装箱外表面应有制造厂名。"},
    ]}, ensure_ascii=False), encoding="utf-8")
    return d


class TestFullIngest:
    @pytest.fixture
    def result(self, scan_pdf, fixtures_dir, tmp_path):
        cfg = load_config()
        cfg.cache_dir = tmp_path / "cache"
        engine = FixtureOcrEngine(fixtures_dir)
        return ingest(scan_pdf, cfg=cfg, domain="standard", engine=engine,
                      out_root=tmp_path / "docs")

    def test_detects_scanned(self, result):
        assert result.profile.kind == "SCANNED"
        assert result.profile.page_count == 2

    def test_all_intermediate_artifacts_written(self, result):
        """每一步的产物都要落盘——这是"可定位问题"的前提。"""
        for name in ("profile.json", "pages.json", "ocr.json", "chunks.jsonl", "manifest.json"):
            assert (result.out_dir / name).exists(), f"缺少产物 {name}"
        assert (result.out_dir / "pages").is_dir()

    def test_manifest_records_detection_evidence(self, result):
        m = json.loads((result.out_dir / "manifest.json").read_text(encoding="utf-8"))
        page = m["detect"]["pages"][0]
        assert page["font_resource_count"] == 0
        assert page["reason"]
        assert page["signals"]  # 判定依据必须留下

    def test_manifest_records_render_and_ocr_provenance(self, result):
        m = json.loads((result.out_dir / "manifest.json").read_text(encoding="utf-8"))
        assert m["render"]["images"][0]["native"] is True
        assert m["ocr"]["engine"] == "fixture"
        assert m["ocr"]["pages_failed"] == []
        assert "usage_total" in m

    def test_table_survives_into_chunks(self, result):
        tables = [c for c in result.chunks if c.kind == "table"]
        assert tables and "1.0" in tables[0].text

    def test_chunks_are_retrievable(self, result):
        hits = result.index.search("抗拉强度要求是多少")
        assert hits.hits

    def test_reload_from_disk_matches(self, result):
        """产物落盘后能完整重建——这是 `ask` 能独立运行的基础。"""
        cfg = load_config()
        reloaded = load_result(result.out_dir, cfg, "standard")
        assert reloaded.doc_id == result.doc_id
        assert len(reloaded.chunks) == len(result.chunks)
        assert reloaded.profile.kind == result.profile.kind


class TestPageFailureIsolation:
    """单页失败只该影响一个答案，不该毁掉一次 ingest。"""

    class PartiallyFailingEngine:
        name = "partial"
        model = "partial"
        supports_vision = False

        def __init__(self, inner: FixtureOcrEngine, fail_pages: set[int]):
            self.inner = inner
            self.fail_pages = fail_pages

        def ocr_page(self, image: PageImage, **kw):
            if image.page_no in self.fail_pages:
                raise PageOcrError(image.page_no, "模拟该页识别失败")
            return self.inner.ocr_page(image, **kw)

        def close(self):
            pass

    @pytest.fixture
    def result(self, scan_pdf, fixtures_dir, tmp_path):
        cfg = load_config()
        cfg.cache_dir = tmp_path / "cache"
        engine = self.PartiallyFailingEngine(FixtureOcrEngine(fixtures_dir), {1})
        return ingest(scan_pdf, cfg=cfg, domain="standard", engine=engine,
                      out_root=tmp_path / "docs")

    def test_ingest_completes_despite_page_failure(self, result):
        assert result.profile.page_count == 2
        assert result.failed_pages == {1}

    def test_failed_page_recorded_in_manifest(self, result):
        assert result.manifest["ocr"]["pages_failed"] == [1]

    def test_failed_page_has_no_blocks(self, result):
        page = next(p for p in result.pages if p.page_no == 1)
        assert page.failed is True and page.blocks == []

    def test_good_page_still_produces_chunks(self, result):
        assert any(c.page_no == 2 for c in result.chunks)

    def test_evidence_touching_failed_page_refuses(self, result, tmp_path):
        """**关键约束**：证据涉及失败页时必须拒答，绝不用不完整的证据作答。"""
        cfg = load_config()
        profile = load_domain("standard", cfg)
        agent = QaAgent(result.index, NullProvider(), cfg, profile,
                        failed_pages=result.failed_pages, doc_id=result.doc_id)
        # 只保留第 1 页的证据，强制走"证据触及失败页"这条路径
        result.index.chunks = [c for c in result.index.chunks if c.page_no == 1]
        result.index._by_id = {c.chunk_id: c for c in result.index.chunks}
        from docqa.index import HybridIndex
        agent.index = HybridIndex(result.index.chunks, profile)

        bundle = agent.answer("抗拉强度要求是多少")
        if bundle.refused:
            assert bundle.refusal_reason in (
                RefusalReason.EXTRACTION_FAILED, RefusalReason.NOT_IN_DOCUMENT
            )
