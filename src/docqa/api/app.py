"""FastAPI 应用：把 AnswerBundle 原样暴露给前端。

CLI 与 Web 共用同一个 `QaAgent` 与同一个 `AnswerBundle`，
所以两条路径的结果**必然一致**——不会出现"命令行答对了、网页答错了"这种事。

三个曾经让 Web 不能用的设计问题，在这里一并修掉
------------------------------------------------
1. **错误状态会粘住。** 原先 `_load()` 一旦失败就把 `state["error"]` 写死，
   之后无论用户做了什么（`ingest` 完、重新上传），页面永远显示
   "尚未 ingest 任何文档"，必须重启进程。现在把"扫盘"与"装载"分开：
   每次请求都重新扫一次候选产物，只有**装载**失败才缓存（并带失效条件）。

2. **只能看"最新一份"。** 多文档检索在 CLI 是 `--doc A --doc B`，Web 却写死
   `max(mtime)`。现在有 `/api/docs` 列出全部可用产物、`/api/select` 切换，
   也支持一次选多份（与 CLI 的语义一致）。

3. **没有并发控制。** 一个请求打一次模型调用，页面连点几下就是并发 N 次，
   既费钱也容易把端点打到限流。这里用一个信号量限流，超限直接 429
   并**说清楚原因**——排队等 30 秒再返回，用户只会以为页面卡了。

上传用**裸 body + 文件名头**，不用 multipart
---------------------------------------------
multipart 需要额外依赖（`python-multipart`），而本项目的一条硬要求是
"三条命令可复现、依赖越少越好"。上传一个 PDF 只是"把这个字节流写进
`data/raw/`"，用 `Content-Type: application/pdf` + `X-Filename` 就够了。
代价是浏览器端要用 `fetch(body: file)` 而不是 `<form>`，前端已经这么做了。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..config import Config, load_domain
from ..errors import DocqaError
from ..llm import build_llm_provider
from ..logging import get_logger
from ..pipeline import load_result, load_results
from ..qa import QaAgent

_log = get_logger("api")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# 同时在跑的问答上限。压到 1 是因为 `QaAgent` 不是线程安全的
# （它有索引与 provider，但修复状态已经改成局部变量了；保险起见仍串行）。
MAX_CONCURRENT_ASKS = 2
# 上传大小上限。扫描件 50 页大约 20–60 MB，这里给足余量，
# 但不留无限——一个 500 MB 的请求足以把内存打满。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class AskRequest(BaseModel):
    question: str


class SelectRequest(BaseModel):
    doc_ids: list[str]


def create_app(cfg: Config, *, domain: str = "standard") -> FastAPI:
    app = FastAPI(title="docqa", docs_url="/docs")
    state: dict = {
        "result": None,
        "agent": None,
        "selected": None,   # list[str] | None；None = 自动取最新
        "load_error": None,  # 只缓存**装载**错误，且带它对应的选择
        "error_for": None,
    }
    ask_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ASKS)

    # ---------------------------------------------------------------- 产物发现

    def _base() -> Path:
        # 与 pipeline.ingest() 的写入位置**同源**（cfg.cache_dir.parent）。
        # 曾经这里硬编码 data/derived，改了 cache_dir 就会"写一处、找另一处"。
        return cfg.cache_dir.parent

    def _discover() -> list[dict]:
        """每次请求都重新扫盘。

        这是修"错误状态粘住"的关键：产物是**外部**产生的（用户可能在另一个
        终端里刚 `ingest` 完），所以"有哪些产物"不是一个可以缓存的判断。
        """
        base = _base()
        if not base.exists():
            return []
        out: list[dict] = []
        for p in base.iterdir():
            manifest = p / "manifest.json"
            if not (p.is_dir() and manifest.exists()):
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({
                "doc_id": data.get("doc_id") or p.name,
                "dir": p.name,
                "engine": (data.get("ocr") or {}).get("engine"),
                "kind": (data.get("detect") or {}).get("kind"),
                "pages": (data.get("source") or {}).get("page_count"),
                "chunks": (data.get("chunk") or {}).get("chunks"),
                "mtime": p.stat().st_mtime,
                "failed_pages": (data.get("ocr") or {}).get("pages_failed") or [],
            })
        out.sort(key=lambda d: d["mtime"], reverse=True)
        return out

    def _dir_for(doc_id: str) -> Path | None:
        for item in _discover():
            if item["doc_id"] == doc_id or item["dir"] == doc_id:
                return _base() / item["dir"]
        return None

    def _load() -> None:
        """惰性装载。选择没变且已装载成功时直接返回。

        **错误不再永久缓存**：只有当"选择"没变时复用上一次的装载错误，
        否则重新尝试——这样用户 ingest 完刷新页面就能用了。
        """
        wanted = state["selected"]
        key = tuple(wanted) if wanted else ("__latest__",)
        if state["agent"] is not None and state["error_for"] == key:
            return
        if state["load_error"] is not None and state["error_for"] == key:
            return

        found = _discover()
        if not found:
            state["load_error"] = (
                "尚未 ingest 任何文档。请在终端运行：docqa ingest <pdf>，"
                "或直接在上方上传一份 PDF。"
            )
            state["error_for"] = key
            return

        if wanted:
            dirs = [_dir_for(d) for d in wanted]
            missing = [d for d, p in zip(wanted, dirs, strict=True) if p is None]
            if missing:
                state["load_error"] = f"找不到这些产物：{missing}。可用：{[f['doc_id'] for f in found]}"
                state["error_for"] = key
                return
            dirs = [p for p in dirs if p is not None]
        else:
            dirs = [_base() / found[0]["dir"]]

        try:
            result = load_results(dirs, cfg, domain) if len(dirs) > 1 else \
                load_result(dirs[0], cfg, domain)
        except Exception as exc:  # noqa: BLE001 - 装载失败要变成可读的错误
            state["load_error"] = f"读取产物失败：{exc}"
            state["error_for"] = key
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
        state["load_error"] = None
        state["error_for"] = key

    # ---------------------------------------------------------------- 页面

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = WEB_DIR / "index.html"
        if not page.exists():
            raise HTTPException(500, "缺少 web/index.html")
        return page.read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> JSONResponse:
        _load()
        docs = _discover()
        if state["load_error"]:
            return JSONResponse({
                "ready": False, "error": state["load_error"], "docs": docs,
                "selected": state["selected"], "domain": domain,
            })
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
            "docs": docs,
            "selected": state["selected"],
            "max_concurrent": MAX_CONCURRENT_ASKS,
        })

    @app.get("/api/docs")
    def docs() -> JSONResponse:
        """列出全部可用产物（供前端切换/多选）。"""
        return JSONResponse({"docs": _discover(), "selected": state["selected"]})

    @app.post("/api/select")
    def select(req: SelectRequest) -> JSONResponse:
        """切换要提问的产物。传空列表 = 回到"自动取最新"。"""
        wanted = [d for d in (req.doc_ids or []) if d.strip()]
        state["selected"] = wanted or None
        # 清空装载缓存，强制下一次请求按新选择重装
        state["agent"] = None
        state["result"] = None
        state["load_error"] = None
        state["error_for"] = None
        _load()
        if state["load_error"]:
            return JSONResponse({"ready": False, "error": state["load_error"]},
                                status_code=400)
        return JSONResponse({"ready": True, "doc_id": state["result"].doc_id})

    @app.get("/api/chunks")
    def chunks() -> JSONResponse:
        _load()
        if state["load_error"]:
            raise HTTPException(503, state["load_error"])
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

    # ---------------------------------------------------------------- 上传

    @app.post("/api/upload")
    async def upload(request: Request) -> JSONResponse:
        """接收一份 PDF 并跑 ingest。

        用裸 body 而不是 multipart：省掉一个运行必需依赖，
        而"上传一个字节流"本来就不需要 multipart 的分段协议。
        文件名从 `X-Filename` 头取（会用 `Path.name` 去掉任何目录部分，
        防止 `../` 逃逸）。
        """
        raw_name = request.headers.get("x-filename", "")
        name = Path(raw_name).name  # 去掉路径部分：防目录穿越
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "只接受 PDF 文件（X-Filename 需以 .pdf 结尾）")

        body = await request.body()
        if not body:
            raise HTTPException(400, "请求体为空")
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"文件过大：{len(body) / 1e6:.1f} MB，上限 {MAX_UPLOAD_BYTES / 1e6:.0f} MB"
            )
        if not body.startswith(b"%PDF-"):
            raise HTTPException(400, "内容不是 PDF（缺少 %PDF- 文件头）")

        raw_dir = _base().parent / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / name
        target.write_bytes(body)

        # 就地跑 ingest。这里要**同步**返回结果：异步跑的话前端只能轮询，
        # 而一份 4 页扫描件的识别通常在几十秒内，等它比轮询体验更好。
        from ..ocr import build_engine
        from ..pipeline import ingest as run_ingest

        try:
            engine = build_engine("vlm", cfg) if cfg.has_ocr else None
            result = run_ingest(target, cfg=cfg, domain=domain, engine=engine)
        except DocqaError as exc:
            raise HTTPException(502, f"解析失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            _log.error("上传后解析失败", extra={"detail": str(exc)[:300]})
            raise HTTPException(500, f"解析失败：{exc}") from exc

        state["selected"] = None
        state["agent"] = None
        state["result"] = None
        state["load_error"] = None
        state["error_for"] = None
        _load()
        return JSONResponse({
            "ready": True,
            "doc_id": result.doc_id,
            "chunks": len(result.chunks),
            "failed_pages": sorted(result.failed_pages),
        })

    # ---------------------------------------------------------------- 问答

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        _load()
        if state["load_error"]:
            raise HTTPException(503, state["load_error"])
        question = (req.question or "").strip()
        if not question:
            raise HTTPException(400, "问题不能为空")

        # 限流：超限立刻拒绝并说明原因，**不排队**——
        # 让用户等 30 秒再返回，他只会以为页面卡住了。
        if not ask_slots.acquire(blocking=False):
            raise HTTPException(
                429,
                f"当前已有 {MAX_CONCURRENT_ASKS} 个请求在处理中。"
                f"一次问答要调用模型，并发跑会同时推高成本与超时概率，"
                f"所以这里直接拒绝而不是排队。请稍后重试。",
            )
        try:
            t0 = time.perf_counter()
            try:
                bundle = state["agent"].answer(question)
            except DocqaError as exc:
                # 不接住的话，生成端点 401 / 超时这类**用户能自己修**的问题
                # 会被 FastAPI 兜底成 `{"detail":"Internal Server Error"}`，
                # 前端只能显示这句话，而"缺哪个环境变量"这种关键信息全丢了。
                raise HTTPException(502, str(exc)) from exc
            payload = bundle.to_dict()
            payload["question"] = question
            payload["server_elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
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
        finally:
            ask_slots.release()

    return app
