"""跨引擎一致性校验在 **ingest 管线里**的行为。

这套机制此前只做到"函数实现 + 单元测试"，**从未接进 ingest**——
于是 README 把它当头等能力写着，实际执行路径里一次也不会跑。
一个声称存在却空转的安全机制，比没有这个机制更伤。

这些用例守住的就是"它真的会跑、而且跑的方式是对的"。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from docqa.config import load_config
from docqa.errors import PageOcrError
from docqa.models import OcrBlock, OcrPage, Usage
from docqa.pipeline import ingest


def _scan_pdf(tmp_path: Path, pages: int = 2) -> Path:
    """合成一份"扫描件式"PDF（整页图像、无文本层）。"""
    doc = pymupdf.open()
    for i in range(pages):
        img = tmp_path / f"p{i}.png"
        Image.new("RGB", (600, 800), "white").save(img)
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(0, 0, 300, 400), filename=str(img))
    out = tmp_path / "scan.pdf"
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out


class FakeEngine:
    """可编排的假引擎：指定每页返回什么，或让某页抛错。"""

    supports_vision = True

    def __init__(self, name: str, texts: dict[int, str],
                 *, fail_pages: set[int] | None = None):
        self.name = name
        self.model = name
        self._texts = texts
        self._fail = fail_pages or set()

    def ocr_page(self, image, *, profile=None) -> OcrPage:
        if image.page_no in self._fail:
            raise PageOcrError(image.page_no, f"{self.name} 第 {image.page_no} 页失败")
        text = self._texts.get(image.page_no, "")
        return OcrPage(
            page_no=image.page_no,
            blocks=[OcrBlock(block_id=f"p{image.page_no}-b0", page_no=image.page_no,
                             type="paragraph", order=0, text=text)],
            tables=[], image_sha256=image.sha256,
            provider=self.name, model=self.model, prompt_version="v1",
            usage=Usage(),
        )

    def close(self) -> None:
        pass


BASE = {1: "3.1  抗拉强度应大于等于 590 MPa。", 2: "3.2  表面不允许有裂纹。"}


class TestCrossEngineWiring:
    def test_not_enabled_is_recorded_honestly(self, tmp_path: Path):
        """没配第二引擎要**明确写进 manifest**。

        否则"没比对"在下游读起来和"比对通过"没有区别。
        """
        pdf = _scan_pdf(tmp_path)
        r = ingest(pdf, cfg=load_config(), domain="standard",
                   engine=FakeEngine("primary", BASE), out_root=tmp_path / "out")
        ce = r.manifest["ocr"]["cross_engine"]
        assert ce["enabled"] is False
        assert "未配置" in ce["reason"]

    def test_disagreeing_engines_flag_blocks_and_record(self, tmp_path: Path):
        """两引擎在高风险字段上不一致 → 块级 needs_review + manifest 计数。"""
        pdf = _scan_pdf(tmp_path)
        # 第二引擎把 590 读成了 950 —— 正是跨引擎校验要抓的那类错误
        other = {1: "3.1  抗拉强度应大于等于 950 MPa。", 2: BASE[2]}
        r = ingest(
            pdf, cfg=load_config(), domain="standard",
            engine=FakeEngine("primary", BASE),
            engine2=FakeEngine("second", other),
            out_root=tmp_path / "out",
        )
        ce = r.manifest["ocr"]["cross_engine"]
        assert ce["enabled"] is True
        assert ce["pages_compared"] == 2
        assert ce["pages_flagged"] == 1, "590 vs 950 应当被标记"

        page1 = next(p for p in r.pages if p.page_no == 1)
        assert any(b.needs_review for b in page1.blocks), "差异应落到块级标记"
        assert any("跨引擎" in w for w in page1.warnings)

    def test_agreeing_engines_flag_nothing(self, tmp_path: Path):
        """一致时不应误报——误报会让这个信号迅速失去可信度。"""
        pdf = _scan_pdf(tmp_path)
        r = ingest(
            pdf, cfg=load_config(), domain="standard",
            engine=FakeEngine("primary", BASE),
            engine2=FakeEngine("second", dict(BASE)),
            out_root=tmp_path / "out",
        )
        ce = r.manifest["ocr"]["cross_engine"]
        assert ce["pages_compared"] == 2
        assert ce["pages_flagged"] == 0
        assert not any(b.needs_review for p in r.pages for b in p.blocks)

    def test_second_engine_failure_does_not_fail_the_page(self, tmp_path: Path):
        """**关键隔离**：第二引擎只是佐证信号，它失败不能拖垮本页可用性。"""
        pdf = _scan_pdf(tmp_path)
        r = ingest(
            pdf, cfg=load_config(), domain="standard",
            engine=FakeEngine("primary", BASE),
            engine2=FakeEngine("second", BASE, fail_pages={1}),
            out_root=tmp_path / "out",
        )
        ce = r.manifest["ocr"]["cross_engine"]
        assert ce["pages_compared"] == 1          # 只剩第 2 页比成了
        assert 1 in ce["pages_skipped"], "跳过必须被记下，否则会读成'比对通过'"
        assert r.failed_pages == set(), "第二引擎失败不该让本页变成失败页"
        assert next(p for p in r.pages if p.page_no == 1).failed is False

    def test_primary_failed_page_is_skipped_not_compared(self, tmp_path: Path):
        """主引擎已失败的页没有可比对象，跳过即可（它已进 failed 集）。"""
        pdf = _scan_pdf(tmp_path)
        r = ingest(
            pdf, cfg=load_config(), domain="standard",
            engine=FakeEngine("primary", BASE, fail_pages={1}),
            engine2=FakeEngine("second", BASE),
            out_root=tmp_path / "out",
        )
        ce = r.manifest["ocr"]["cross_engine"]
        assert r.failed_pages == {1}
        assert ce["pages_compared"] == 1
        assert 1 in ce["pages_skipped"]

    def test_needs_review_does_not_by_itself_cause_refusal(self, tmp_path: Path):
        """标记 ≠ 有错。它是**送人工复核**的信号，不参与拒答判定。

        若把标记直接当拒答依据，一次误报就会让整个系统变得不可用——
        用户会很快学会忽略拒答。
        """
        pdf = _scan_pdf(tmp_path)
        r = ingest(
            pdf, cfg=load_config(), domain="standard",
            engine=FakeEngine("primary", BASE),
            engine2=FakeEngine("second", {1: "3.1  抗拉强度应大于等于 950 MPa。", 2: BASE[2]}),
            out_root=tmp_path / "out",
        )
        assert r.manifest["ocr"]["cross_engine"]["pages_flagged"] == 1
        # 页面仍然可用：证据在，只是带了复核标记
        page1 = next(p for p in r.pages if p.page_no == 1)
        assert page1.failed is False
        assert page1.plain_text().strip(), "标记不该清空证据"


class TestCacheIsolation:
    def test_second_engine_does_not_share_the_primary_cache(self, tmp_path: Path):
        """**第二引擎绝不能共用 OCR 缓存。**

        共用的话它会直接命中主引擎写下的结果并原样返回，
        比对必然一致——又一个"永远不会失败的检查"。
        这里通过 `build_engine` 的接线来验证：传 provider_cfg 时 cache 必须为 None。
        """
        import inspect

        from docqa.ocr import build_engine

        src = inspect.getsource(build_engine)
        assert "cache=None if provider_cfg is not None" in src, (
            "第二引擎若共用缓存，跨引擎比对会退化成恒真检查"
        )
