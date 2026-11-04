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

import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config, load_domain
from ..docx import is_docx
from ..errors import DocqaError, InputError
from ..llm import build_llm_provider
from ..logging import get_logger
from ..models import sanitize_display_text
from ..pipeline import load_result, load_results
from ..qa import QaAgent
from ..qa.answer import HYBRID_REQUIRES_LLM_MESSAGE

_log = get_logger("api")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# 上传接受的两种来源。DOCX 是 Word 原生文本，不需要 OCR/渲染；
# `.doc`/`.docm`/`.dotx` 明确拒绝并给出"另存为"的出路，而不是让它在
# ZIP 校验里报一句用户看不懂的话。
REJECTED_WORD_SUFFIXES = (".doc", ".docm", ".dotm", ".dotx")

# Windows 的 `mimetypes` 会读注册表，`.js` 有时被映射成 `text/plain`。
# 浏览器对 `<script type="module">` 做严格 MIME 检查，`text/plain` 会被拒绝执行；
# 同理样式表需要 `text/css`。显式钉住这几类静态资源的类型。
for _extension, _media_type in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".svg", "image/svg+xml"),
):
    mimetypes.add_type(_media_type, _extension, strict=True)

# 同时在跑的问答上限。压到 1 是因为 `QaAgent` 不是线程安全的
# （它有索引与 provider，但修复状态已经改成局部变量了；保险起见仍串行）。
MAX_CONCURRENT_ASKS = 2
# 单个问题的长度上限。检索与生成都是 O(问题长度) 的：一个几 MB 的"问题"
# 只会占 CPU/内存与模型配额。4000 字符远超正常提问（含粘贴长条款）。
MAX_QUESTION_CHARS = 4000
# 同时在跑的上传上限。上传会把整个 body 读进内存并落盘，并发跑会同时推高
# 内存与磁盘占用；压到 1 是**资源保护**，不是吞吐设计。
MAX_CONCURRENT_UPLOADS = 1
# 上传大小上限。扫描件 50 页大约 20–60 MB，这里给足余量，
# 但不留无限——一个 500 MB 的请求足以把内存打满。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Windows 保留设备名（含带扩展名形式：`CON.docx` 同样被系统当成设备）。
# 存储名虽然会加内容哈希后缀（`CON-abcd1234.docx` 已不保留），仍显式拒绝：
# 用户上传这类名字几乎一定是误操作，静默改名比报错更难排查。
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# Windows 禁止出现在文件名里的字符。去掉它们同时消除 ADS（`foo:bar`）、
# 路径分隔与通配符问题；POSIX 上也一并遵守，避免"本机通过、部署机 500"。
_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')
# 保留可读的 stem，但不允许无限长：`<stem>-<sha8>.docx` 要留在常见 255 字节
# 文件名上限内（UTF-8 中文占 3 字节，60 字符留足余量）。
_MAX_STEM_CHARS = 60


def _too_large_detail(size: int | None = None) -> str:
    limit_mb = MAX_UPLOAD_BYTES / 1e6
    if size is None or size <= 0:
        return f"文件过大：超过上限 {limit_mb:.0f} MB"
    return f"文件过大：{size / 1e6:.1f} MB，上限 {limit_mb:.0f} MB"


async def _read_upload_body(request: Request) -> bytes:
    """在**读取过程中**执行大小上限，而不是读完再检查。

    为什么必须这样（SEC-DOCX-03）
    -----------------------------
    `await request.body()` 会把所有 chunk 收进 list 再 `join`，峰值约为 body 的
    2 倍；而且大小检查发生在**完整读取之后**——攻击者用一个 chunked/撒谎
    Content-Length 的请求就能让进程先分配任意内存再被拒绝。上传是唯一接收
    不可信字节的入口，这条必须在上限处**停下**，而不是"读完再判断"。

    顺序：
    1. `Content-Length` 声明超限 → 立刻 413，**一个字节都不读**；
    2. 无长度/chunked（或声明撒谎）→ 边读边累计，超过上限立即 413；
    3. 返回实际字节（长度必然 ≤ 上限）。
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > MAX_UPLOAD_BYTES:
            raise HTTPException(413, _too_large_detail(declared_bytes))

    buffer = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        # **先判断再扩展**：buffer 本身永远不超过上限，峰值不随"声明长度"膨胀。
        if len(buffer) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, _too_large_detail(len(buffer) + len(chunk)))
        buffer.extend(chunk)
    return bytes(buffer)


def _safe_stem(raw_stem: str) -> str:
    """把用户文件名净化成可安全落盘的 stem（不含扩展名、不含目录）。

    返回的 stem 只保留可读性；**存储名还会再拼内容哈希**，所以它永远不是
    用户可控的最终路径——ADS、设备名、同名覆盖都被这一层与哈希一起挡掉。
    """
    stem = unicodedata.normalize("NFC", raw_stem)
    cleaned = "".join(
        ch for ch in stem
        if ch.isprintable() and ch not in _INVALID_FILENAME_CHARS
    )
    cleaned = " ".join(cleaned.split()).strip(" .")
    if len(cleaned) > _MAX_STEM_CHARS:
        cleaned = cleaned[:_MAX_STEM_CHARS].rstrip(" .")
    return cleaned or "upload"


def _is_reserved_windows_name(stem: str) -> bool:
    return stem.rstrip(" .").upper() in _WINDOWS_RESERVED_NAMES


def _public_error_detail(message: str, target: Path, original_name: str) -> str:
    """把可能带绝对路径的错误信息换成用户能看懂的名字。

    `InputError` 的消息是给人看的、通常不含主机路径；但少数分支（"无法读取
    \\<path>"）会带上。400 响应不回显服务器目录结构——这既是信息泄露，
    也是把内部路径写进浏览器控件的坏事。
    """
    return (
        message.replace(str(target), original_name)
        .replace(target.as_posix(), original_name)
    )


def _regular_file_matches(path: Path, digest: str, size: int) -> bool:
    """目标是否是**普通文件**，且完整 sha256 与本次内容一致。

    必须用 `lstat`：符号链接要在这一步被判为"不匹配"。跟随链接去 `open()`
    正是 RECHECK-01 要把住的那一步——共享 raw 目录里预置
    `raw/<name> -> victim` 时，绝不能让上传内容写穿到 victim。
    """
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != size:
        return False
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return False
    return hasher.hexdigest() == digest


def _store_upload(
    raw_dir: Path, stored_name: str, body: bytes, digest: str
) -> tuple[Path, bool]:
    """把 body 原子地落到 `raw_dir/stored_name`，返回 `(target, preexisting)`。

    三条安全属性（RECHECK-01/02）：
    1. **不跟随 symlink**：目标已存在时先 `lstat`，必须是普通文件且**完整**
       sha256 与本次一致；否则拒绝（409）。预置 `raw/<name> -> victim`
       时 victim 不会被写入。
    2. **不写半截文件**：先写同目录随机临时文件，再 `os.replace` 原子替换；
       任何失败都清理临时文件。
    3. **幂等**：同内容重复上传命中已存在的普通文件时不重写
       （`preexisting=True`），因此后续失败清理也不会误删一份已验证的源文件。
    """
    target = raw_dir / stored_name
    if target.exists() or target.is_symlink():
        if _regular_file_matches(target, digest, len(body)):
            return target, True
        raise HTTPException(
            409,
            "目标存储位置已存在同名但内容不一致的文件"
            "（可能是本地符号链接，或文件被篡改），已拒绝写入。\n"
            "请清理 data/raw 下的该文件后重试。",
        )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=raw_dir, prefix=".upload-", suffix=".tmp", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(body)
        os.replace(tmp_path, target)
        tmp_path = None
        return target, False
    except OSError as exc:
        _log.error(
            "保存上传文件失败",
            extra={"detail": f"{type(exc).__name__}: {exc}"[:300]},
        )
        raise HTTPException(
            500, "无法保存上传文件（磁盘空间或文件系统错误），详情见服务端日志"
        ) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _source_info(manifest: dict) -> dict:
    """从 manifest 里取"这份语料是什么来源"的几个事实。

    升级前的 PDF 产物没有 `source_type`/`unit_count`，缺字段时按 PDF 处理——
    与 `load_result()` 的默认值保持同一套语义。
    """
    source = manifest.get("source") or {}
    source_type = source.get("source_type") or "pdf"
    return {
        "source_type": source_type,
        "unit_count": int(source.get("unit_count") or 0),
        # 页数只有 PDF 才有意义。DOCX 没有页，混合语料里报 PDF 的页数
        # 会让人误以为另一半也有那么多页。
        "pages": int(source.get("page_count") or 0) if source_type == "pdf" else 0,
        "unsupported": (manifest.get("docx") or {}).get("unsupported") or {},
        "engine": (manifest.get("ocr") or {}).get("engine"),
    }


def _failed_page_list(failed_pages) -> list[int]:
    """把失败页集合变成 JSON 友好的页码列表。

    单文档时是 `{2}`；多文档时是 `{("A", 2)}`。前端只展示"哪些页有问题"，
    这里统一成页码；`None`（DOCX 没有页）永远不进列表。
    """
    pages: set[int] = set()
    for item in failed_pages or ():
        page = item[1] if isinstance(item, tuple) else item
        if page is not None:
            pages.add(page)
    return sorted(pages)


# 展示层控制字符净化：C0/C1 的单一实现在 `models.sanitize_display_text()`；
# bidi 控制字符（U+202A–202E、U+2066–2069）目前 models 尚未覆盖，出口这一层
# 先补上——它们能把文本视觉顺序翻转，是"看起来像另一句话"的欺骗手段。
# 只影响展示，不改动任何存储/索引内容。
_BIDI_CONTROL_RE = re.compile(r"[‪-‮⁦-⁩]")


def _sanitize_outlet(text: str | None) -> str | None:
    """API 出口的统一净化：C0/C1（models）+ bidi 控制字符（本地补充）。"""
    cleaned = sanitize_display_text(text)
    if cleaned is None:
        return None
    return _BIDI_CONTROL_RE.sub("", cleaned)


def _sanitize_payload(payload: dict) -> dict:
    """净化 `AnswerBundle.to_dict()` 里会直接展示的文本字段。

    - `answer`/`refusal_detail`/`model_knowledge`、`citations[*].quote` 是
      模型或文档原文的直接去向；
    - `citations[*].rendered`/`clause_label` 与 `candidates[*].citation` 是
      由 `clause_label` 渲染出的引用字符串——`clause_label` 本身来自文档，
      同样可能夹带控制字符，渲染后照样会进前端；
    - 自检的 `detail`/`offenders` 也会引用答案或证据原句。
    其余字段（chunk_id、locator、数值/布尔）由结构生成，不参与净化。
    """
    for field in ("answer", "refusal_detail", "model_knowledge"):
        payload[field] = _sanitize_outlet(payload.get(field))
    for citation in payload.get("citations") or []:
        for field in ("quote", "clause_label", "rendered"):
            citation[field] = _sanitize_outlet(citation.get(field))
    for candidate in payload.get("candidates") or []:
        candidate["citation"] = _sanitize_outlet(candidate.get("citation"))
    for check in (payload.get("verification") or {}).get("checks") or []:
        check["detail"] = _sanitize_outlet(check.get("detail"))
        check["offenders"] = [
            _sanitize_outlet(item) for item in check.get("offenders") or []
        ]
    return payload


class AskRequest(BaseModel):
    question: str
    # 回答范围模式。默认 strict（只依据文档证据）；hybrid 是显式可选路径，
    # 不是"没模型时的降级"。非法值由 Pydantic 直接 422。
    mode: Literal["strict", "hybrid"] = "strict"


class SelectRequest(BaseModel):
    doc_ids: list[str]


def create_app(cfg: Config, *, domain: str = "standard") -> FastAPI:
    app = FastAPI(title="docqa", docs_url="/docs")

    # 前端构建产物（React + Vite）。目录由 `npm run build` 生成；
    # 源码没构建时跳过挂载，避免 `docqa serve` 在开发机上直接起不来。
    assets_dir = WEB_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    state: dict = {
        "result": None,
        "agent": None,
        "selected": None,   # list[str] | None；None = 自动取最新
        "load_error": None,  # 只缓存**装载**错误，且带它对应的选择
        "error_for": None,
    }
    ask_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ASKS)
    # 上传的并发槽位。**在读取 body 之前**获取：否则一个慢速大文件上传
    # 会先把内存占住，限流也就失去了意义。
    upload_slots = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)

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
            info = _source_info(data)
            out.append({
                "doc_id": data.get("doc_id") or p.name,
                "dir": p.name,
                "engine": info["engine"],
                "kind": (data.get("detect") or {}).get("kind"),
                "pages": info["pages"],
                "chunks": (data.get("chunk") or {}).get("chunks"),
                "mtime": p.stat().st_mtime,
                "failed_pages": (data.get("ocr") or {}).get("pages_failed") or [],
                "source_type": info["source_type"],
                "unit_count": info["unit_count"],
                "unsupported": info["unsupported"],
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
                "尚未 ingest 任何文档。请在终端运行：docqa ingest <pdf|docx>，"
                "或直接在上方上传一份 PDF / DOCX。"
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

    def _index_html() -> str:
        page = WEB_DIR / "index.html"
        if not page.exists():
            raise HTTPException(
                500,
                "缺少 web/index.html。前端源码在 web/，先运行 `npm run build`。",
            )
        return page.read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    @app.get("/app", response_class=HTMLResponse)
    def workbench() -> str:
        """工作台入口。

        单页应用的两个路由共用同一份 HTML；`/app` 只是让浏览器地址栏
        与产品结构一致，真正的路由切换由前端完成。
        """
        return _index_html()

    @app.get("/api/status")
    def status() -> JSONResponse:
        _load()
        docs = _discover()
        if state["load_error"]:
            return JSONResponse({
                "ready": False, "error": state["load_error"], "docs": docs,
                "selected": state["selected"], "domain": domain,
                # 早退响应也必须带 `has_llm`：前端用 `has_llm !== false`
                # 判断能不能选 hybrid，缺字段会被误当成"已配置"。
                "has_llm": cfg.has_llm,
            })
        result = state["result"]
        info = _source_info(result.manifest)
        return JSONResponse({
            "ready": True,
            "doc_id": result.doc_id,
            "kind": result.profile.kind,
            "pages": info["pages"],
            "chunks": len(result.chunks),
            "tables": sum(1 for c in result.chunks if c.kind == "table"),
            "failed_pages": _failed_page_list(result.failed_pages),
            # 来源与规模：DOCX 没有页，前端据此显示「DOCX · 原生文本 · N 段」，
            # 而不是一个误导性的「0 页」。混合语料是 `mixed`。
            "source_type": info["source_type"],
            "unit_count": info["unit_count"],
            "unsupported": info["unsupported"],
            "citation_mode": result.manifest.get("chunk", {}).get("citation_mode"),
            "engine": info["engine"],
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
                # 定位字段：前端按 locator_kind 分支显示「第 N 页」或「§3.6 ¶4」。
                # DOCX 的 page_no 是 null，不是 0——0 会被渲染成「第 0 页」。
                "locator_kind": c.locator_kind,
                "section": c.section,
                "para_no": c.para_no,
                "para_end": c.para_end,
            }
            for c in state["result"].chunks
        ])

    # ---------------------------------------------------------------- 页图

    @app.get("/api/page-image")
    def page_image(doc_id: str, page_no: int) -> FileResponse:
        """返回某份产物某一页的页图（引用 bbox 高亮的底图）。

        只服务**本工具自己 ingest 出来的页图**：`doc_id` 必须能在
        `_discover()` 里找到（未知 doc_id 直接 404，天然挡住目录穿越），
        文件名取自 manifest 的 `render.images`，再用 `Path(...).name` 兜底。
        DOCX 与纯文本层页没有页图 → 404，前端据此退回纯文本高亮。
        """
        d = _dir_for(doc_id)
        if d is None:
            raise HTTPException(404, "找不到该文档产物")
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "产物清单不可读") from None
        images = {
            int(im["page_no"]): im
            for im in (data.get("render") or {}).get("images") or []
            if im.get("page_no") is not None
        }
        im = images.get(page_no)
        if im is None:
            raise HTTPException(404, "该页没有页图")
        fname = Path(str(im.get("file") or "")).name
        if not fname:
            raise HTTPException(404, "该页没有页图文件")
        pages_dir = (d / "pages").resolve()
        path = pages_dir / fname
        # 兜底：解析后的真实路径必须仍落在 pages/ 之下。
        try:
            path.resolve().relative_to(pages_dir)
        except ValueError:
            raise HTTPException(404, "页图路径非法") from None
        if not path.is_file():
            raise HTTPException(404, "页图文件缺失")
        media = mimetypes.guess_type(fname)[0] or "image/png"
        return FileResponse(
            path, media_type=media, headers={"Cache-Control": "public, max-age=3600"}
        )

    # ---------------------------------------------------------------- 原文浏览

    def _source_pdf(d: Path) -> Path | None:
        """从 manifest 的 `source.path` 取原始 PDF（若存在且确为 PDF）。"""
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw = (data.get("source") or {}).get("path")
        if not raw:
            return None
        p = Path(str(raw))
        if p.suffix.lower() != ".pdf" or not p.is_file():
            return None
        return p

    @app.get("/api/pdf")
    def source_pdf(doc_id: str) -> FileResponse:
        """返回原始 PDF，供前端内嵌阅读器整份浏览（可滚动/缩放/跳页）。

        只服务**本工具自己 ingest 的产物对应的源文件**：`doc_id` 必须能在
        `_discover()` 里找到，路径取自该产物的 manifest。DOCX 没有 PDF → 404。
        """
        d = _dir_for(doc_id)
        if d is None:
            raise HTTPException(404, "找不到该文档产物")
        path = _source_pdf(d)
        if path is None:
            raise HTTPException(404, "该文档没有可浏览的 PDF 源文件")
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "public, max-age=3600",
            },
        )

    @app.get("/api/document")
    def document(doc_id: str) -> JSONResponse:
        """一份文档的"可浏览"描述：PDF 地址 / 页图列表 / DOCX 段落文本。

        前端用它决定怎么展示原文：有 PDF 用内嵌阅读器；没有 PDF（DOCX）就
        按段落列出解析文本；扫描页还可逐页看页图。
        """
        d = _dir_for(doc_id)
        if d is None:
            raise HTTPException(404, "找不到该文档产物")
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "产物清单不可读") from None
        real_id = data.get("doc_id") or doc_id
        src = data.get("source") or {}
        src_path = str(src.get("path") or "")
        source_type = src.get("source_type") or (
            "docx" if src_path.lower().endswith(".docx") else "pdf"
        )
        images = (data.get("render") or {}).get("images") or []
        pages = [
            {
                "page_no": im["page_no"],
                "url": f"/api/page-image?doc_id={quote(real_id)}&page_no={im['page_no']}",
                "width": im.get("width"),
                "height": im.get("height"),
            }
            for im in images
            if im.get("page_no") is not None
        ]
        pdf_available = source_type != "docx" and _source_pdf(d) is not None
        units: list[dict] = []
        if source_type == "docx":
            cj = d / "chunks.jsonl"
            if cj.exists():
                for line in cj.open(encoding="utf-8"):
                    if not line.strip():
                        continue
                    c = json.loads(line)
                    sec = c.get("section")
                    para = c.get("para_no")
                    if sec and para is not None:
                        loc = f"§{sec} ¶{para}"
                    elif para is not None:
                        loc = f"¶{para}"
                    elif sec:
                        loc = f"§{sec}"
                    else:
                        loc = f"片段 {c.get('ordinal')}"
                    units.append({"locator": loc, "text": c.get("text") or ""})
        return JSONResponse(
            {
                "doc_id": real_id,
                "source_type": source_type,
                "page_count": len(pages) or int(src.get("page_count") or 0),
                "pdf_url": f"/api/pdf?doc_id={quote(real_id)}" if pdf_available else None,
                "pages": pages,
                "units": units,
            }
        )

    # ---------------------------------------------------------------- 上传

    async def _upload_impl(request: Request) -> JSONResponse:
        raw_name = request.headers.get("x-filename", "")
        # 控制字符（含 NUL）一律拒绝：它们会让文件名在日志/HTTP 头/文件系统
        # 之间出现三种不同的样子，是路径与日志注入的经典入口。
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_name):
            raise HTTPException(400, "文件名含控制字符（如 NUL），已拒绝")
        # 反斜杠先归一成 `/`：`Path.name` 在 POSIX 上不把 `\` 当分隔符，
        # 而 Windows 客户端会送 `..\\..\\x.pdf`。
        name = Path(raw_name.replace("\\", "/")).name
        if not name or name in {".", ".."}:
            raise HTTPException(400, "缺少文件名：X-Filename 头需带文件名")
        lowered = name.lower()
        if lowered.endswith(REJECTED_WORD_SUFFIXES):
            raise HTTPException(
                400,
                f"不支持的 Word 格式：{name}\n"
                "本工具只处理纯文本 .docx。请用 Word/WPS「另存为 → .docx」后重试。",
            )
        if lowered.endswith(".docx"):
            source_type, suffix = "docx", ".docx"
        elif lowered.endswith(".pdf"):
            source_type, suffix = "pdf", ".pdf"
        else:
            raise HTTPException(
                400,
                f"只接受 PDF 或 DOCX 文件（X-Filename 需以 .pdf 或 .docx 结尾），收到：{name}",
            )

        safe_stem = _safe_stem(name[: -len(suffix)])
        if _is_reserved_windows_name(safe_stem):
            raise HTTPException(
                400,
                f"文件名是 Windows 保留设备名：{name}\n"
                "这类名字在 Windows 上指向设备而不是文件。请重命名后再上传"
                "（例如 contract.docx）。",
            )

        body = await _read_upload_body(request)
        if not body:
            raise HTTPException(400, "请求体为空")
        if source_type == "pdf" and not body.startswith(b"%PDF-"):
            raise HTTPException(400, "内容不是 PDF（缺少 %PDF- 文件头）")
        if source_type == "docx" and not is_docx(body):
            raise HTTPException(
                400,
                "内容不是 DOCX（ZIP 包里找不到 word/document.xml）。\n"
                "请用 Word/WPS「另存为 → .docx」后重试。",
            )

        # 存储名 = 净化后的 stem + **内容哈希** + 扩展名。
        #
        # 为什么不用用户给的名字做路径（SEC-DOCX-04/06）：
        # - 同名二次上传会覆盖源文件，旧 manifest 的 `source.path`/`sha256`
        #   就会指向新字节，证据链断裂；
        # - `foo:bar.docx` 在 Windows 上会被写成基文件 `foo` 的备用数据流；
        # - 设备名（`CON`）、通配符/分隔符与超长名会让 `mkdir/write` 直接 500。
        # 原始文件名只进日志与响应元数据，不参与任何路径拼接。
        #
        # 用 sha256[:32]（128 bit）而不是 [:8]（32 bit）：这个哈希还会进
        # `doc_id` / 产物目录名，8 位在共享 raw 目录里碰撞会直接串产物。
        digest = hashlib.sha256(body).hexdigest()
        stored_name = f"{safe_stem}-{digest[:32]}{suffix}"
        raw_dir = _base().parent / "raw"
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.error(
                "创建 raw 目录失败",
                extra={"detail": f"{type(exc).__name__}: {exc}"[:300]},
            )
            raise HTTPException(
                500, "无法保存上传文件（磁盘空间或文件系统错误），详情见服务端日志"
            ) from exc
        target, preexisting = _store_upload(raw_dir, stored_name, body, digest)
        _log.info("收到上传", extra={"detail": f"{raw_name[:120]!r} → {stored_name}"})

        def _discard_unverified() -> None:
            """删除**本次新建**的未验证文件。

            同内容重复上传时目标可能早已存在且验证过；这次失败不该顺手删掉
            一份有效源文件（存储名带内容哈希，同名即同内容）。
            """
            if not preexisting:
                target.unlink(missing_ok=True)

        # 就地跑 ingest。这里要**同步**返回结果：异步跑的话前端只能轮询，
        # 而一份 4 页扫描件的识别通常在几十秒内，等它比轮询体验更好。
        from ..ocr import build_engine
        from ..pipeline import ingest as run_ingest

        try:
            # DOCX 不需要 OCR 引擎：它在管线里走原生文本分支，构造引擎纯属浪费，
            # 而没有 OCR 配置时构造还会失败——那会把"能解析的原生文本"挡在门外。
            engine = build_engine("vlm", cfg) if (source_type == "pdf" and cfg.has_ocr) else None
            result = run_ingest(target, cfg=cfg, domain=domain, engine=engine)
        except InputError as exc:
            # 输入层面的问题（坏包、加密、超限、不支持）是**用户可修**的，
            # 必须是 400；502 会让前端以为服务端坏了。
            _discard_unverified()
            raise HTTPException(
                400, _public_error_detail(f"解析失败：{exc}", target, name)
            ) from exc
        except DocqaError as exc:
            _discard_unverified()
            raise HTTPException(
                502, _public_error_detail(f"解析失败：{exc}", target, name)
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # 兜底 500 **不回显异常文本**：那可能带绝对路径、类型名或内部状态。
            # 细节只进服务端日志（审计 SEC-DOCX-05）。
            _discard_unverified()
            _log.error(
                "上传后解析失败",
                extra={"detail": f"{type(exc).__name__}: {exc}"[:300]},
            )
            raise HTTPException(
                500, "解析失败：服务端内部错误，详情见服务端日志"
            ) from exc

        # 上传成功后**明确选中刚上传的文档**，不要把 selected 置回 None。
        # `_discover()` 按目录 mtime 排序；幂等上传不会重建产物目录，若回到
        # "自动最新"，另一份更新的文档（例如 E2E 里的 gbt48351.pdf）会胜出，
        # 用户刚上传的 DOCX 反而看不到。
        out_dir = getattr(result, "out_dir", None)
        if out_dir is not None:
            # 同时刷新产物目录 mtime：服务重启后 selected=None 时按 mtime 选
            # 最新，刚上传的文档必须排在最前。只动时间戳，不改任何文件内容。
            try:
                os.utime(out_dir, None)
            except OSError as exc:
                _log.warning(
                    "无法刷新产物目录时间戳",
                    extra={"detail": f"{type(exc).__name__}: {exc}"[:200]},
                )
        state["selected"] = [result.doc_id]
        state["agent"] = None
        state["result"] = None
        state["load_error"] = None
        state["error_for"] = None
        _load()
        profile = getattr(result, "profile", None)
        return JSONResponse({
            "ready": True,
            "doc_id": result.doc_id,
            "chunks": len(result.chunks),
            "failed_pages": _failed_page_list(getattr(result, "failed_pages", [])),
            "source_type": getattr(profile, "source_type", None) or source_type,
            "unit_count": getattr(profile, "unit_count", 0),
            # 原始文件名只作为元数据返回；磁盘上的名字是 `<stem>-<sha8><ext>`。
            "original_filename": name,
            "stored_filename": stored_name,
        })

    @app.post("/api/upload")
    async def upload(request: Request) -> JSONResponse:
        """接收一份 PDF 或 DOCX 并跑 ingest。

        用裸 body 而不是 multipart：省掉一个运行必需依赖，
        而"上传一个字节流"本来就不需要 multipart 的分段协议。
        文件名从 `X-Filename` 头取（只作元数据；落盘名带内容哈希）。

        分派只看**扩展名 + 文件头**：
        - PDF 校验 `%PDF-`；
        - DOCX 用 `is_docx()` 做便宜的 ZIP 结构检查，**深度校验交给
          `run_ingest` → `validate_docx`**（大小/条目/加密/DTD/路径穿越）。
          DOCX 是原生文本，不构造 VLM 引擎——没配 OCR 的机器也能上传它。

        **先限流、再读 body、再落盘**：上传是唯一接收不可信字节的入口，
        顺序错了就等于给了"在读之前先分配任意内存"的口子（SEC-DOCX-03/04）。
        """
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(
                429,
                f"当前已有 {MAX_CONCURRENT_UPLOADS} 个上传在处理中。"
                f"一次上传会把整份文档读进内存、落盘并解析，并发跑会同时推高"
                f"内存与磁盘占用，所以这里直接拒绝而不是排队。请稍后重试。",
            )
        try:
            return await _upload_impl(request)
        finally:
            upload_slots.release()

    # ---------------------------------------------------------------- 问答

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        _load()
        if state["load_error"]:
            raise HTTPException(503, state["load_error"])
        question = (req.question or "").strip()
        if not question:
            raise HTTPException(400, "问题不能为空")
        if len(question) > MAX_QUESTION_CHARS:
            raise HTTPException(
                400,
                f"问题过长：{len(question)} 字符，上限 {MAX_QUESTION_CHARS}。"
                f"请缩短问题后重试（把大段原文当作问题既检索不准，也会浪费模型配额）。",
            )

        # hybrid 必须真的由生成模型作答。没有模型时明确 400，**绝不静默降级**
        # 成 strict——否则前端显示"文档 + 模型知识"，拿到的却是仅文档的答案。
        if req.mode == "hybrid" and not cfg.has_llm:
            raise HTTPException(400, HYBRID_REQUIRES_LLM_MESSAGE)

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
                bundle = state["agent"].answer(question, mode=req.mode)
            except DocqaError as exc:
                # 不接住的话，生成端点 401 / 超时这类**用户能自己修**的问题
                # 会被 FastAPI 兜底成 `{"detail":"Internal Server Error"}`，
                # 前端只能显示这句话，而"缺哪个环境变量"这种关键信息全丢了。
                raise HTTPException(502, str(exc)) from exc
            payload = _sanitize_payload(bundle.to_dict())
            payload["question"] = question
            payload["server_elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            # 附上被引证据的全文，供前端高亮
            idx = state["result"].index
            doc_id = state["result"].doc_id
            render_imgs = {
                int(im["page_no"]): im
                for im in (state["result"].manifest.get("render") or {}).get("images") or []
                if im.get("page_no") is not None
            }
            payload["evidence"] = {}
            for citation in bundle.citations:
                # 定位字段以**索引里的 chunk** 为准：Citation 上的 para_no
                # 可能被模型写成引用里的那个数，而前端要高亮的是整段证据。
                chunk = idx.chunk(citation.chunk_id)
                # 页图 bbox：优先用 chunk 上归一化好的比例坐标，且只保留被引页的框。
                raw_boxes = chunk.bboxes if chunk else citation.bboxes
                if citation.page_no is not None:
                    boxes = [b.to_dict() for b in raw_boxes if b.page_no == citation.page_no]
                else:
                    boxes = [b.to_dict() for b in raw_boxes]
                im = render_imgs.get(citation.page_no) if citation.page_no is not None else None
                evidence_entry = {
                    # 证据原文同样来自文档，可能夹带控制字符：净化后再出 API。
                    "text": _sanitize_outlet(idx.evidence_text(citation.chunk_id)),
                    "page_no": citation.page_no,
                    "clause_label": citation.clause_label,
                    "locator_kind": chunk.locator_kind if chunk else citation.locator_kind,
                    "section": chunk.section if chunk else citation.section,
                    "para_no": chunk.para_no if chunk else citation.para_no,
                    "para_end": chunk.para_end if chunk else citation.para_end,
                    "bboxes": boxes,
                    # 只有"有页图且确有 bbox"时才给 page_image；否则前端退回文本高亮。
                    "page_image": (
                        {
                            "url": f"/api/page-image?doc_id={quote(doc_id)}&page_no={citation.page_no}",
                            "width": im.get("width"),
                            "height": im.get("height"),
                        }
                        if (im is not None and boxes)
                        else None
                    ),
                }
                payload["evidence"][citation.chunk_id] = evidence_entry
            return JSONResponse(json.loads(json.dumps(payload, ensure_ascii=False)))
        finally:
            ask_slots.release()

    return app
