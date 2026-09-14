"""FastAPI 应用：把 AnswerBundle 原样暴露给前端。

CLI 与 Web 共用同一个 `QaAgent` 与同一个 `AnswerBundle`，
所以两条路径的结果**必然一致**——不会出现"命令行答对了、网页答错了"这种事。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..config import Config, load_domain
from ..errors import DocqaError
from ..llm import build_llm_provider
from ..logging import get_logger
from ..pipeline import load_result
from ..qa import QaAgent

_log = get_logger("api")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class AskRequest(BaseModel):
    question: str


def create_app(cfg: Config, *, domain: str = "standard") -> FastAPI:
    app = FastAPI(title="docqa", docs_url="/docs")
    state: dict = {"result": None, "agent": None, "error": None}

    def _load() -> None:
        """惰性装载产物。启服务时不阻塞，首次提问才读盘。"""
        if state["agent"] is not None or state["error"]:
            return
        # 与 pipeline.ingest() 的写入位置**同源**（cfg.cache_dir.parent）。
        # 曾经这里硬编码 data/derived，改了 cache_dir 就会"写一处、找另一处"。
        base = cfg.cache_dir.parent
        candidates = [
            p for p in base.iterdir()
            if p.is_dir() and (p / "manifest.json").exists()
        ] if base.exists() else []
        if not candidates:
            state["error"] = "尚未 ingest 任何文档。请先运行：docqa ingest <pdf>"
            return
        doc_dir = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            result = load_result(doc_dir, cfg, domain)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"读取产物失败：{exc}"
            return
        state["result"] = result
        state["agent"] = QaAgent(
            result.index,
            build_llm_provider(cfg),
            cfg,
            load_domain(domain, cfg),
            failed_pages=result.failed_pages,
            doc_id=result.doc_id,
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = WEB_DIR / "index.html"
        if not page.exists():
            raise HTTPException(500, "缺少 web/index.html")
        return page.read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> JSONResponse:
        _load()
        if state["error"]:
            return JSONResponse({"ready": False, "error": state["error"]})
        result = state["result"]
        return JSONResponse({
            "ready": True,
            "doc_id": result.doc_id,
            "kind": result.profile.kind,
            "pages": result.profile.page_count,
            "chunks": len(result.chunks),
            "tables": sum(1 for c in result.chunks if c.kind == "table"),
            "failed_pages": sorted(result.failed_pages),
            "citation_mode": result.manifest.get("chunk", {}).get("citation_mode"),
            "engine": result.manifest.get("ocr", {}).get("engine"),
            "domain": domain,
            "has_llm": cfg.has_llm,
        })

    @app.get("/api/chunks")
    def chunks() -> JSONResponse:
        _load()
        if state["error"]:
            raise HTTPException(503, state["error"])
        return JSONResponse([
            {
                "chunk_id": c.chunk_id,
                "page_no": c.page_no,
                "kind": c.kind,
                "clause_label": c.clause_label,
                "clause_path": list(c.clause_path),
                "text": c.text,
            }
            for c in state["result"].chunks
        ])

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        _load()
        if state["error"]:
            raise HTTPException(503, state["error"])
        question = (req.question or "").strip()
        if not question:
            raise HTTPException(400, "问题不能为空")
        try:
            bundle = state["agent"].answer(question)
        except DocqaError as exc:
            # 不接住的话，生成端点 401 / 超时这类**用户能自己修**的问题
            # 会被 FastAPI 兜底成 `{"detail":"Internal Server Error"}`，
            # 前端只能显示这句话，而"缺哪个环境变量"这种关键信息全丢了。
            raise HTTPException(502, str(exc)) from exc
        payload = bundle.to_dict()
        payload["question"] = question
        # 附上被引证据的全文，供前端高亮
        idx = state["result"].index
        payload["evidence"] = {
            c.chunk_id: {
                "text": idx.evidence_text(c.chunk_id),
                "page_no": c.page_no,
                "clause_label": c.clause_label,
            }
            for c in bundle.citations
        }
        return JSONResponse(json.loads(json.dumps(payload, ensure_ascii=False)))

    return app
