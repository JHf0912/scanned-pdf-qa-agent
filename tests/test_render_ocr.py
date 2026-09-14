"""渲染与 OCR 层测试。

用**运行时合成的** PDF/图像，不依赖 data/raw 下的真实文档——
那些文件不入库（含敏感信息 / 承诺不再分发），测试必须能独立跑起来。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from docqa.config import RenderConfig
from docqa.errors import PageOcrError, ProviderError
from docqa.llm.base import LLMResponse
from docqa.models import PageImage, PageProfile, Usage
from docqa.ocr import build_engine
from docqa.ocr.cache import OcrCache, Provenance, cache_key, prompt_sha256
from docqa.ocr.fixture import FixtureOcrEngine
from docqa.ocr.spotcheck import diff_claims, evaluate_spotcheck, extract_high_risk_fields
from docqa.ocr.vlm import VlmOcrEngine
from docqa.render import describe, render_pages


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    """合成一份"扫描件式"PDF：整页图像、无文本层、无字体资源。"""
    img_path = tmp_path / "page.png"
    Image.new("RGB", (600, 800), "white").save(img_path)

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_image(pymupdf.Rect(0, 0, 300, 400), filename=str(img_path))
    out = tmp_path / "scan.pdf"
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out


class TestRender:
    def test_extracts_native_image_without_resampling(self, scanned_pdf, tmp_path):
        """默认应**位精确取图**，尺寸与源图一致；重采样只会损失信息。"""
        images = render_pages(scanned_pdf, tmp_path / "out", page_count=1)
        assert len(images) == 1
        assert images[0].native is True
        assert (images[0].width, images[0].height) == (600, 800)

    def test_upscale_changes_pixel_size_but_is_recorded(self, scanned_pdf, tmp_path):
        cfg = RenderConfig(upscale=2.0)
        images = render_pages(scanned_pdf, tmp_path / "out", page_count=1, config=cfg)
        assert images[0].width == 1200
        assert images[0].upscale == 2.0

    def test_page_range_out_of_bounds_rejected(self, scanned_pdf, tmp_path):
        from docqa.errors import InputError

        with pytest.raises(InputError, match="超出范围"):
            render_pages(scanned_pdf, tmp_path / "out", page_count=1, pages=[5])

    def test_describe_is_serialisable(self, scanned_pdf, tmp_path):
        images = render_pages(scanned_pdf, tmp_path / "out", page_count=1)
        assert json.dumps(describe(images), ensure_ascii=False)


class TestOcrCache:
    def test_key_is_deterministic(self):
        a = cache_key("img", provider="p", model="m", prompt_digest="d")
        assert a == cache_key("img", provider="p", model="m", prompt_digest="d")

    @pytest.mark.parametrize("field,value", [
        ("image", "other"), ("provider", "other"), ("model", "other"), ("prompt_digest", "other"),
    ])
    def test_key_changes_with_every_input(self, field, value):
        base = {"image": "img", "provider": "p", "model": "m", "prompt_digest": "d"}
        base[field] = value
        assert cache_key("img", provider="p", model="m", prompt_digest="d") != cache_key(
            base["image"], provider=base["provider"], model=base["model"],
            prompt_digest=base["prompt_digest"],
        )

    def test_prompt_hash_changes_when_prompt_changes(self):
        assert prompt_sha256("a") != prompt_sha256("b")

    def test_roundtrip(self, tmp_path, structured_doc):
        cache = OcrCache(tmp_path)
        key = cache_key("img", provider="p", model="m", prompt_digest="d")
        assert cache.get(key) is None

        prov = Provenance(created_at="t", provider="p", model="m", prompt_version="v1",
                          prompt_sha256="d", image_sha256="img")
        cache.put(key, structured_doc, prov)

        page, got_prov = cache.get(key)
        assert len(page.blocks) == len(structured_doc.blocks)
        assert got_prov.model == "m"
        assert cache.has(key)

    def test_corrupt_entry_treated_as_miss(self, tmp_path):
        cache = OcrCache(tmp_path)
        (tmp_path / "k.json").write_text("{ not json", encoding="utf-8")
        assert cache.get("k") is None  # 不抛异常，当作未命中


class TestFixtureEngine:
    def test_reads_by_page_number(self, tmp_path, structured_doc):
        (tmp_path / "p0001.json").write_text(json.dumps({
            "blocks": [{"type": "clause", "order": 0, "text": "3.1 内容", "clause_label": "3.1"}]
        }, ensure_ascii=False), encoding="utf-8")
        engine = FixtureOcrEngine(tmp_path)
        img = PageImage(page_no=1, path=Path("x.png"), sha256="abc",
                        width=10, height=10, dpi=200, native=True)
        page = engine.ocr_page(img)
        assert page.blocks[0].clause_label == "3.1"
        assert any("fixture" in w for w in page.warnings)

    def test_missing_fixture_raises_page_error(self, tmp_path):
        engine = FixtureOcrEngine(tmp_path)
        img = PageImage(page_no=3, path=Path("x.png"), sha256="zzz",
                        width=10, height=10, dpi=200, native=True)
        with pytest.raises(PageOcrError, match="第 3 页"):
            engine.ocr_page(img)

    def test_table_html_converted_to_grid(self, tmp_path):
        (tmp_path / "p0001.json").write_text(json.dumps({
            "blocks": [{"type": "table", "order": 0, "text": "",
                        "table": {"caption": "表 1",
                                  "html": "<table><tr><th>项目</th><th>AQL</th></tr>"
                                          "<tr><td>键宽 b</td><td>1.0</td></tr></table>"}}]
        }, ensure_ascii=False), encoding="utf-8")
        engine = FixtureOcrEngine(tmp_path)
        img = PageImage(page_no=1, path=Path("x.png"), sha256="abc",
                        width=10, height=10, dpi=200, native=True)
        page = engine.ocr_page(img)
        assert page.tables and page.tables[0].cells[1] == ["键宽 b", "1.0"]


class FakeVisionProvider:
    """假 provider。**不发任何真实请求。**"""

    name = "fake"
    supports_vision = True
    model = "fake"

    def __init__(self, payload: str | list[str]):
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.calls = 0

    def complete(self, system, user, **kw) -> LLMResponse:
        self.calls += 1
        idx = min(self.calls - 1, len(self.payloads) - 1)
        return LLMResponse(self.payloads[idx], "fake", "fake", Usage(calls=1))

    def close(self):
        pass


class TestVlmEngine:
    @pytest.fixture
    def img(self, tmp_path) -> PageImage:
        p = tmp_path / "p.png"
        Image.new("RGB", (40, 40), "white").save(p)
        return PageImage(page_no=1, path=p, sha256="deadbeef", width=40, height=40,
                         dpi=200, native=True)

    def test_parses_structured_blocks(self, tmp_path, cfg, img):
        payload = json.dumps({"blocks": [
            {"type": "clause", "order": 0, "clause_label": "3.1", "text": "抗拉强度 590 MPa。"},
            {"type": "paragraph", "order": 1, "text": "补充说明。"},
        ]}, ensure_ascii=False)
        engine = VlmOcrEngine(FakeVisionProvider(payload), cfg,
                              prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        page = engine.ocr_page(img)
        assert len(page.blocks) == 2
        assert page.blocks[0].clause_label == "3.1"

    def test_second_call_hits_cache(self, tmp_path, cfg, img):
        payload = json.dumps({"blocks": [{"type": "paragraph", "order": 0, "text": "内容"}]})
        provider = FakeVisionProvider(payload)
        engine = VlmOcrEngine(provider, cfg, prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        engine.ocr_page(img)
        engine.ocr_page(img)
        assert provider.calls == 1, "同一页图 + 同一提示词应只花一次调用"

    def test_unknown_block_type_degrades_with_warning(self, tmp_path, cfg, img):
        payload = json.dumps({"blocks": [{"type": "外星类型", "order": 0, "text": "内容"}]})
        engine = VlmOcrEngine(FakeVisionProvider(payload), cfg,
                              prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        page = engine.ocr_page(img)
        assert page.blocks[0].type == "paragraph"
        assert any("不在允许集合" in w for w in page.warnings)

    def test_invalid_json_retried_once_then_isolated(self, tmp_path, cfg, img):
        provider = FakeVisionProvider(["不是 JSON", "仍然不是 JSON"])
        engine = VlmOcrEngine(provider, cfg, prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        with pytest.raises(PageOcrError, match="第 1 页"):
            engine.ocr_page(img)
        assert provider.calls == 2  # 重试了一次

    def test_recovery_on_retry(self, tmp_path, cfg, img):
        payload = json.dumps({"blocks": [{"type": "paragraph", "order": 0, "text": "好"}]})
        engine = VlmOcrEngine(FakeVisionProvider(["坏 JSON", payload]), cfg,
                              prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        assert engine.ocr_page(img).blocks[0].text == "好"

    def test_missing_blocks_key_rejected(self, tmp_path, cfg, img):
        engine = VlmOcrEngine(FakeVisionProvider('{"other": 1}'), cfg,
                              prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        with pytest.raises(PageOcrError, match="blocks"):
            engine.ocr_page(img)

    def test_uncertain_flag_preserved(self, tmp_path, cfg, img):
        payload = json.dumps({"blocks": [
            {"type": "paragraph", "order": 0, "text": "模糊", "uncertain": True,
             "alternatives": ["另一读法"]}
        ]}, ensure_ascii=False)
        engine = VlmOcrEngine(FakeVisionProvider(payload), cfg,
                              prompt_path=Path("prompts/ocr_page.v1.md"),
                              cache=OcrCache(tmp_path / "c"))
        page = engine.ocr_page(img)
        assert page.blocks[0].uncertain is True
        assert page.blocks[0].alternatives == ["另一读法"]


class TestBuildEngine:
    def test_unknown_engine_rejected(self, cfg):
        from docqa.errors import ConfigError

        with pytest.raises(ConfigError, match="未知的 OCR 引擎"):
            build_engine("nope", cfg)

    def test_local_engine_reports_it_is_reserved(self, cfg):
        from docqa.errors import ConfigError

        with pytest.raises(ConfigError, match="预留位"):
            build_engine("local", cfg)

    def test_fixture_requires_dir(self, cfg):
        from docqa.errors import ConfigError

        with pytest.raises(ConfigError, match="fixtures-dir"):
            build_engine("fixture", cfg)


class TestSpotcheck:
    def test_high_risk_fields_extracted(self):
        fields = extract_high_risk_fields("按 GB/T 11334—2005 选取，组长：纪○○，2026-09-04")
        assert any("11334" in f for f in fields)
        assert any("2026" in f for f in fields)

    def test_agreeing_texts(self):
        assert diff_claims("强度 590 MPa", "强度 590 MPa").agreed

    def test_numeric_disagreement_detected(self):
        d = diff_claims("强度 590 MPa", "强度 950 MPa")
        assert not d.agreed
        assert "590MPa" in d.only_in_a or "590" in "".join(d.only_in_a)

    def test_evaluation_of_spotcheck_itself(self):
        """这个标记必须**可被评估**——否则它就是个不可能错的数字。"""
        r = evaluate_spotcheck([(True, True), (True, False), (False, True), (False, False)])
        assert r["precision"] == 0.5
        assert r["recall"] == 0.5
        assert r["f1"] == 0.5
