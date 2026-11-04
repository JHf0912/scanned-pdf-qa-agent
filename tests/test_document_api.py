"""原文浏览接口：/api/document 与 /api/pdf 的回归测试。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from fastapi.testclient import TestClient

from docqa.api.app import create_app
from docqa.config import load_config

_PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _cfg(tmp_path: Path):
    return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")


def _write_artifact(base: Path, doc_id: str, *, source: dict, images: list[dict],
                    chunks: list[dict]) -> Path:
    out = base / doc_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(
            {"doc_id": doc_id, "source": source, "render": {"images": images},
             "ocr": {"pages_failed": []}, "chunk": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out / "profile.json").write_text(
        json.dumps({"path": source.get("path", ""), "sha256": "x", "page_count": len(images),
                    "pages": [], "kind": "SCANNED"}), encoding="utf-8",
    )
    (out / "ocr.json").write_text("[]", encoding="utf-8")
    with (out / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out / "pages").mkdir(exist_ok=True)
    (out / "pages" / "p0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return out


def _pdf_artifact(tmp_path: Path) -> Path:
    base = tmp_path / "derived"
    pdf = base / "doc" / "src.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(_PDF_BYTES)
    return _write_artifact(
        base, "doc",
        source={"path": str(pdf), "sha256": "s", "page_count": 1},
        images=[{"page_no": 1, "file": "p0001.png", "width": 100, "height": 200}],
        chunks=[],
    )


def _docx_artifact(tmp_path: Path) -> Path:
    base = tmp_path / "derived"
    docx = base / "dx" / "a.docx"
    docx.parent.mkdir(parents=True, exist_ok=True)
    docx.write_bytes(b"PK\x03\x04")
    return _write_artifact(
        base, "dx",
        source={"path": str(docx), "sha256": "s", "source_type": "docx", "page_count": 0},
        images=[],
        chunks=[{"chunk_id": "dx:b1", "doc_id": "dx", "page_no": None, "ordinal": 1,
                 "kind": "clause", "text": "第一条 标准正文", "section": "1", "para_no": 1}],
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path), domain="standard"))


class TestDocumentApi:
    def test_pdf_document_has_pdf_url_and_pages(self, tmp_path):
        _pdf_artifact(tmp_path)
        j = _client(tmp_path).get("/api/document", params={"doc_id": "doc"}).json()
        assert j["source_type"] == "pdf"
        assert j["pdf_url"] and j["pdf_url"].endswith("/api/pdf?doc_id=doc")
        assert len(j["pages"]) == 1
        assert j["pages"][0]["url"].startswith("/api/page-image")

    def test_docx_document_lists_units_without_pdf(self, tmp_path):
        _docx_artifact(tmp_path)
        j = _client(tmp_path).get("/api/document", params={"doc_id": "dx"}).json()
        assert j["source_type"] == "docx"
        assert j["pdf_url"] is None
        assert j["units"] and "标准正文" in j["units"][0]["text"]
        assert j["units"][0]["locator"] == "§1 ¶1"

    def test_unknown_doc_is_404(self, tmp_path):
        _pdf_artifact(tmp_path)
        assert _client(tmp_path).get("/api/document", params={"doc_id": "nope"}).status_code == 404


class TestSourcePdfApi:
    def test_serves_pdf(self, tmp_path):
        _pdf_artifact(tmp_path)
        r = _client(tmp_path).get("/api/pdf", params={"doc_id": "doc"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")

    def test_docx_has_no_pdf(self, tmp_path):
        _docx_artifact(tmp_path)
        assert _client(tmp_path).get("/api/pdf", params={"doc_id": "dx"}).status_code == 404

    def test_unknown_doc_is_404(self, tmp_path):
        _pdf_artifact(tmp_path)
        assert _client(tmp_path).get("/api/pdf", params={"doc_id": "nope"}).status_code == 404
