"""引用 bbox + 页图高亮：后端数据链路与页图路由的回归测试。

覆盖：
- `BBox` / `Chunk.bboxes` / `Citation.bboxes` 的序列化；
- `pipeline._page_bbox_normalizer` 的两条单位分支（VLM 像素 / 文本层点）；
- `load_result` 从 `ocr.json` 现算 bbox（旧产物无需重建即有高亮）；
- `parse_citations` 把 chunk 的 bbox 透传到 Citation（并按页过滤）；
- `/api/page-image` 的正常返回与各种非法输入。

这些用例都断言具体数值/状态码，删掉实现会立刻失败（不是只断言 200）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from docqa.api.app import create_app
from docqa.config import load_config
from docqa.models import BBox, Chunk
from docqa.pipeline import _page_bbox_normalizer, load_result
from docqa.qa.answer import parse_citations

# 1x1 透明 PNG，够真实文件即可（路由只校验存在与扩展名）。
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mQAAANUAQ8Zl1YAAAAASUVORK5CYII="
)


def _write_artifact(
    base: Path,
    doc_id: str,
    *,
    pages: list[dict],
    images: list[dict],
    chunks: list[dict],
    png: bool = False,
) -> Path:
    out = base / doc_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "doc_id": doc_id,
                "render": {"images": images},
                "ocr": {"pages_failed": []},
                "chunk": {"citation_mode": "clause"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out / "profile.json").write_text(
        json.dumps(
            {"path": "x.pdf", "sha256": "abc", "page_count": len(pages),
             "pages": [], "kind": "SCANNED"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out / "ocr.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    with (out / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out / "pages").mkdir(exist_ok=True)
    if png:
        (out / "pages" / images[0]["file"]).write_bytes(_PNG)
    return out


def _block(block_id: str, page_no: int, text: str, bbox) -> dict:
    return {
        "block_id": block_id,
        "page_no": page_no,
        "type": "paragraph",
        "order": 0,
        "text": text,
        "bbox": bbox,
    }


def _chunk(chunk_id: str, page_no: int, text: str, block_ids: list[str], label="1") -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": "d",
        "page_no": page_no,
        "ordinal": 1,
        "kind": "clause",
        "text": text,
        "clause_path": [label],
        "clause_label": label,
        "block_ids": block_ids,
    }


# ---------------------------------------------------------------- 模型序列化


class TestBBoxModel:
    def test_bbox_rounds_and_serializes(self):
        d = BBox(1, 0.1, 0.2, 0.3, 0.400004).to_dict()
        assert d == {"page_no": 1, "x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}

    def test_chunk_to_dict_includes_bboxes(self):
        c = Chunk("d:p1:c1", "d", 1, 1, "clause", "范围",
                  bboxes=(BBox(1, 0.1, 0.2, 0.3, 0.4),))
        assert c.to_dict()["bboxes"] == [
            {"page_no": 1, "x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}
        ]

    def test_chunk_without_bboxes_serializes_empty_list(self):
        c = Chunk("d:p1:c1", "d", 1, 1, "clause", "范围")
        assert c.to_dict()["bboxes"] == []

    def test_answer_bundle_citation_includes_bboxes(self):
        from docqa.models import AnswerBundle, Citation

        cit = Citation(page_no=3, clause_label="1", chunk_id="c", quote="q",
                       bboxes=(BBox(3, 0.1, 0.1, 0.5, 0.2),))
        bundle = AnswerBundle(question="q", answer="a", citations=[cit])
        assert bundle.to_dict()["citations"][0]["bboxes"] == [
            {"page_no": 3, "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2}
        ]


# ---------------------------------------------------------------- 归一化


class TestNormalizer:
    def test_vlm_pixel_bbox_is_divided_by_image_size(self):
        from docqa.models import OcrBlock

        norm = _page_bbox_normalizer(
            {1: {"width": 1000, "height": 2000, "dpi": 200}}, {1: "openai"}
        )
        b = OcrBlock("p1-b0", 1, "paragraph", 0, "x", bbox=(100, 200, 300, 260))
        got = norm(b)
        assert got == BBox(1, 0.1, 0.1, 0.3, 0.13)

    def test_textlayer_point_bbox_is_divided_by_page_points(self):
        from docqa.models import OcrBlock

        # 1000x2000 px @200dpi -> 360 x 720 pt
        norm = _page_bbox_normalizer(
            {1: {"width": 1000, "height": 2000, "dpi": 200}}, {1: "textlayer"}
        )
        b = OcrBlock("p1-b0", 1, "paragraph", 0, "x", bbox=(36, 72, 180, 108))
        got = norm(b)
        assert got == BBox(1, 0.1, 0.1, 0.5, 0.15)

    def test_missing_image_or_bbox_returns_none(self):
        from docqa.models import OcrBlock

        norm = _page_bbox_normalizer({}, {1: "openai"})
        assert norm(OcrBlock("p1-b0", 1, "paragraph", 0, "x", bbox=(1, 2, 3, 4))) is None
        norm2 = _page_bbox_normalizer({1: {"width": 100, "height": 100, "dpi": 72}}, {1: "openai"})
        assert norm2(OcrBlock("p1-b0", 1, "paragraph", 0, "x", bbox=None)) is None

    def test_degenerate_bbox_is_dropped(self):
        from docqa.models import OcrBlock

        norm = _page_bbox_normalizer(
            {1: {"width": 1000, "height": 1000, "dpi": 72}}, {1: "openai"}
        )
        assert norm(OcrBlock("p1-b0", 1, "paragraph", 0, "x", bbox=(10, 10, 10, 10))) is None


# ---------------------------------------------------------------- load_result


class TestLoadResultBboxes:
    def _cfg(self, tmp_path: Path):
        import dataclasses

        return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")

    def test_bboxes_are_derived_from_ocr_blocks(self, tmp_path):
        base = tmp_path / "derived"
        _write_artifact(
            base,
            "d",
            pages=[
                {"page_no": 1, "provider": "openai", "image_sha256": "",
                 "blocks": [_block("p1-b0", 1, "范围 本标准规定了", [100, 200, 300, 260])],
                 "tables": []},
            ],
            images=[{"page_no": 1, "file": "p0001_native.png", "width": 1000,
                     "height": 2000, "dpi": 200}],
            chunks=[_chunk("d:p1:c1", 1, "范围 本标准规定了", ["p1-b0"])],
        )
        result = load_result(base / "d", self._cfg(tmp_path), "standard")
        assert result.chunks[0].bboxes == (BBox(1, 0.1, 0.1, 0.3, 0.13),)

    def test_chunk_without_page_images_has_no_bboxes(self, tmp_path):
        """DOCX / 无页图的产物 block_ids 对不上 OCR 块 → 空 bboxes（退回文本高亮）。"""
        base = tmp_path / "derived"
        _write_artifact(
            base,
            "d",
            pages=[],
            images=[],
            chunks=[_chunk("d:p1:c1", None, "段落文字", ["b3"])],
        )
        result = load_result(base / "d", self._cfg(tmp_path), "standard")
        assert result.chunks[0].bboxes == ()


# ---------------------------------------------------------------- parse_citations


class TestParseCitationsBboxes:
    def test_citation_carries_chunk_bboxes(self):
        c = Chunk("d:p1:c1", "d", 1, 1, "clause", "范围 本标准规定了",
                  clause_label="1", bboxes=(BBox(1, 0.1, 0.2, 0.3, 0.4),))
        cits = parse_citations("范围 [p.1 §1]", [c])
        assert len(cits) == 1
        assert cits[0].bboxes == (BBox(1, 0.1, 0.2, 0.3, 0.4),)

    def test_bboxes_are_filtered_to_the_cited_page(self):
        c = Chunk("d:p3:c1", "d", 3, 1, "clause", "范围 本标准规定了",
                  clause_label="1",
                  bboxes=(BBox(2, 0.9, 0.9, 1.0, 1.0), BBox(3, 0.1, 0.2, 0.3, 0.4)))
        cits = parse_citations("范围 [p.3 §1]", [c])
        # 只保留被引页（第 3 页）的框，别的页不参与高亮。
        assert cits[0].bboxes == (BBox(3, 0.1, 0.2, 0.3, 0.4),)


# ---------------------------------------------------------------- 页图 API


class TestPageImageApi:
    def _client(self, tmp_path: Path):
        base = tmp_path / "derived"
        _write_artifact(
            base,
            "gbtX",
            pages=[
                {"page_no": 1, "provider": "openai", "image_sha256": "",
                 "blocks": [_block("p1-b0", 1, "范围", [10, 10, 100, 40])], "tables": []},
            ],
            images=[{"page_no": 1, "file": "p0001_native.png", "width": 1000,
                     "height": 2000, "dpi": 200}],
            chunks=[_chunk("gbtX:p1:c1", 1, "范围", ["p1-b0"])],
            png=True,
        )
        import dataclasses

        cfg = dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")
        from fastapi.testclient import TestClient

        return TestClient(create_app(cfg, domain="standard"))

    def test_serves_existing_page(self, tmp_path):
        client = self._client(tmp_path)
        r = client.get("/api/page-image", params={"doc_id": "gbtX", "page_no": 1})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert r.content[:8] == _PNG[:8]

    def test_unknown_page_is_404(self, tmp_path):
        client = self._client(tmp_path)
        r = client.get("/api/page-image", params={"doc_id": "gbtX", "page_no": 9})
        assert r.status_code == 404

    def test_unknown_doc_is_404(self, tmp_path):
        client = self._client(tmp_path)
        r = client.get("/api/page-image", params={"doc_id": "nope", "page_no": 1})
        assert r.status_code == 404

    def test_path_traversal_doc_id_is_404(self, tmp_path):
        client = self._client(tmp_path)
        r = client.get("/api/page-image", params={"doc_id": "../../etc", "page_no": 1})
        assert r.status_code == 404
