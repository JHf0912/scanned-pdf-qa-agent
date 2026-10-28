"""ingest 编排：PDF → 类型判定 → 渲染 → OCR → 切分 → 索引 → 落盘。

这个模块把各阶段串起来，同时负责三件容易被忽略、但决定"能不能定位问题"的事：

1. **每一步的产物都单独落盘。** `profile.json` / `pages/` / `ocr.json` /
   `chunks.jsonl` / `manifest.json` 各自独立。出了问题能一眼看出是哪一步的锅，
   而不是面对一个"跑完了但结果不对"的黑盒。
2. **manifest 记录全部证据。** 判定信号、渲染参数与页图哈希、OCR 溯源、
   切分统计、耗时。复现与审计都靠它。
3. **失败隔离。** 单页 OCR 失败只标记该页，流程继续；但凡是证据涉及该页的问题，
   一律以 `EXTRACTION_FAILED` 拒答——**绝不用不完整的证据去回答**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .chunker import chunk_document
from .config import Config, DomainProfile
from .detect import pages_needing_ocr, profile_pdf
from .errors import ConfigError, InputError, PageOcrError
from .index import HybridIndex
from .injection import scan_pages
from .logging import get_logger, stage
from .models import Chunk, OcrPage, PageImage, PdfProfile
from .ocr import OcrEngine
from .ocr.spotcheck import spotcheck_page
from .ocr.textlayer import TextLayerEngine
from .render import describe as describe_images
from .render import render_pages

_log = get_logger("pipeline")


@dataclass
class IngestResult:
    doc_id: str
    out_dir: Path
    profile: PdfProfile
    pages: list[OcrPage]
    chunks: list[Chunk]
    index: HybridIndex
    manifest: dict
    # 单文档时是页码集合；多文档（`load_results`）时升级成 (doc_id, page_no)——
    # 两份文件的"第 3 页"是两回事。`QaAgent` 会把两种形态归一成后者。
    failed_pages: set[int] | set[tuple[str, int]]

    @property
    def succeeded_pages(self) -> int:
        return sum(1 for p in self.pages if not p.failed)


def _doc_id(profile: PdfProfile) -> str:
    return f"{profile.path.stem}-{profile.sha256[:8]}"


# 金标目录。**其中的内容绝不能成为管线输入。**
#
# 为什么需要这条硬约束：金标集是用来看管线表现好不好的标尺。
# 一旦它（或它的任何衍生文件）被 ingest 读进去，评估就变成了用答案考试——
# 而且这种错误是静默的：指标会变得非常漂亮，没有任何迹象表明出了问题。
#
# 所以这道闸门做在代码里，而不是靠流程约束或文档提醒。靠人记得的事情早晚会忘。
GOLDEN_DIR_NAMES = frozenset({"golden", "golden_data"})


def assert_not_golden(path: Path) -> None:
    """拒绝把金标目录下的任何文件当作输入。

    Raises:
        InputError: 路径位于金标目录下。
    """
    resolved = path.resolve()
    for parent in [resolved, *resolved.parents]:
        if parent.name.lower() in GOLDEN_DIR_NAMES:
            raise InputError(
                f"拒绝把金标集内容作为管线输入：{path}\n"
                f"\n"
                f"data/golden/ 下存放的是**衡量系统的标尺**（人工校对的转写与问答对）。\n"
                f"把它们喂回管线，评估就变成了用答案考试——而且指标会变得很好看，\n"
                f"没有任何迹象表明出了问题。\n"
                f"\n"
                f"请把待解析的 PDF 放在 data/raw/ 下。"
            )


def ingest(
    pdf_path: Path,
    *,
    cfg: Config,
    domain: str,
    profile: DomainProfile | None = None,
    engine: OcrEngine | None = None,
    engine2: OcrEngine | None = None,
    out_root: Path | None = None,
    force: bool = False,
) -> IngestResult:
    """跑完一次 ingest。

    Args:
        domain: 业务场景名，用于装载 domain profile。
        engine: OCR 引擎；None 时由调用方（CLI）按配置构造并传入。
        engine2: 可选的第二 OCR 引擎，用于跨引擎一致性校验。
            给了就逐页再识别一遍并比对高风险字段，差异写回块级 `needs_review`。
            **成本翻倍**，所以默认不启用——它防的是"证据本身被识别错"，
            与自检防的"生成阶段幻觉"是两套机制，按风险预算决定是否开启。
        force: 忽略缓存强制重新识别（用于验证缓存之外的实跑结果）。
    """
    from .config import load_domain

    assert_not_golden(pdf_path)  # 金标内容绝不作为管线输入
    started = time.time()
    dom = profile or load_domain(domain, cfg)

    # --- 1. 类型判定 ---
    with stage("detect", _log, doc=pdf_path.name) as info:
        pdf_profile = profile_pdf(pdf_path)
        info["detail"] = f"{pdf_profile.kind} / {pdf_profile.page_count} 页"
    _log.info(
        "PDF 类型判定完成",
        extra={
            "doc": pdf_path.name,
            "detail": f"{pdf_profile.kind}，{pdf_profile.page_count} 页",
        },
    )

    out_dir = (out_root or cfg.cache_dir.parent) / _doc_id(pdf_profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "profile.json").write_text(
        json.dumps(pdf_profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- 2. 渲染 ---
    #
    # 注意这里传的是 `targets` 本身，**不能写 `targets or None`**。
    # `pages_needing_ocr()` 对纯文本 PDF 返回 `[]`，而 `[] or None` 得到 `None`，
    # `render_pages` 又把 `None` 解释为"渲染全部页"——于是整本 born-digital PDF
    # 会被渲染并**整本送去 OCR**。那既违反 detect.py 写明的硬不变量
    # （"有真实文本层的页永远不送去 OCR"），也白白烧掉识别费用。
    # 空列表必须保持为空列表：**"这一页都不需要识别"是一个有效结论，不是缺省值。**
    targets = pages_needing_ocr(pdf_profile)
    with stage("render", _log, doc=pdf_path.name) as info:
        images = render_pages(
            pdf_path,
            out_dir / "pages",
            page_count=pdf_profile.page_count,
            config=cfg.render,
            pages=targets,
        )
        info["detail"] = f"{len(images)} 页（共 {pdf_profile.page_count} 页）"
    if not targets:
        _log.info(
            "所有页面都有可读文本层，无需 OCR",
            extra={"doc": pdf_path.name, "detail": f"{pdf_profile.page_count} 页全部判为 TEXT"},
        )
    (out_dir / "pages.json").write_text(
        json.dumps(describe_images(images), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- 3. OCR（逐页失败隔离）---
    pages: list[OcrPage] = []
    failed: set[int] = set()
    profiles_by_page = {p.page_no: p for p in pdf_profile.pages}

    # 原实现这里只打了一条"跳过识别"的日志，然后照样调用 `engine.ocr_page()`，
    # 于是 `engine=None` 时必然 `AttributeError`——**日志承诺的行为和代码做的事相反**。
    #
    # 现在明确区分两种情况：
    # - 没有待识别的页（纯文本 PDF）：引擎无关紧要，正常继续；
    # - 有待识别的页却没有引擎：这是调用方的错误，直接报出来，
    #   而不是让它以 AttributeError 的形式在几十行之后炸掉。
    if engine is None and targets:
        raise ConfigError(
            f"有 {len(targets)} 页需要 OCR（{targets}），但没有提供 OCR 引擎。\n"
            f"请通过 CLI 运行 `docqa ingest`，或由调用方显式传入引擎。"
        )

    # --- 3. 逐页取内容（按页类型选来源，失败隔离）---
    #
    # 两条来源，**同一套产物格式**：
    #   - 有文本层的页 → `TextLayerEngine`（零模型调用，取 PDF 原生文本层）；
    #   - 没有文本层的页 → 传入的 OCR 引擎（VLM / fixture）。
    #
    # 这一步是"支持真实客户文档"的前提。在此之前，管线**只会走 OCR 路径**：
    # TEXT 页既不渲染也不识别，于是纯 born-digital PDF 切出 0 个片段，
    # 混合件里文本层那几页整页丢失——而 detect.py 明明把它们判成了 TEXT。
    # "不送 OCR"这个正确的决定，当时等于"直接丢掉"。
    text_engine = TextLayerEngine(pdf_path)
    pages: list[OcrPage] = []
    failed: set[int] = set()
    by_text_layer: list[int] = []
    profiles_by_page = {p.page_no: p for p in pdf_profile.pages}
    images_by_page = {img.page_no: img for img in images}

    # 需要 OCR 却没有引擎：这是调用方的错误，直接报出来，
    # 而不是让它以 AttributeError 的形式在几十行之后炸掉。
    if engine is None and targets:
        raise ConfigError(
            f"有 {len(targets)} 页需要 OCR（{targets}），但没有提供 OCR 引擎。\n"
            f"请通过 CLI 运行 `docqa ingest`，或由调用方显式传入引擎。"
        )

    def _failed_page(page_no: int, sha: str, exc: Exception) -> OcrPage:
        """单页失败的统一产物：记录、继续，但不让下游拿到不完整的证据。"""
        failed.add(page_no)
        _log.error(
            "该页内容获取失败，已隔离",
            extra={"page": page_no, "detail": str(exc)[:300]},
        )
        return OcrPage(
            page_no=page_no,
            blocks=[],
            tables=[],
            image_sha256=sha,
            provider="textlayer" if page_no not in images_by_page else getattr(engine, "name", ""),
            model="",
            prompt_version="",
            failed=True,
            error=str(exc)[:500],
        )

    for prof_page in sorted(pdf_profile.pages, key=lambda p: p.page_no):
        page_no = prof_page.page_no
        image = images_by_page.get(page_no)

        if prof_page.kind == "TEXT":
            # 有真实文本层：直接取字，**不送 OCR**（硬不变量）。
            try:
                with stage("textlayer", _log, page=page_no):
                    page = text_engine.ocr_page(
                        # TEXT 页没有被渲染，所以没有页图；这里造一个只带页码与
                        # 哈希占位的 PageImage —— 引擎只用它做溯源标记。
                        PageImage(
                            page_no=page_no, path=Path(), sha256=pdf_profile.sha256,
                            width=0, height=0, dpi=0.0, native=True,
                        ),
                        profile=prof_page,
                    )
                pages.append(page)
                by_text_layer.append(page_no)
            except PageOcrError as exc:
                pages.append(_failed_page(page_no, pdf_profile.sha256, exc))
            continue

        # 其余（SCANNED / HYBRID 页）走 OCR 引擎
        if image is None:
            # 判为需要识别却没渲染出图：这不该发生，但要如实记下来而不是跳过
            pages.append(_failed_page(
                page_no, "", RuntimeError("该页判定需要识别，但没有渲染出页图")
            ))
            continue
        try:
            with stage("ocr", _log, page=page_no):
                page = engine.ocr_page(image, profile=profiles_by_page.get(page_no))
            pages.append(page)
            if page.failed:
                failed.add(page_no)
        except PageOcrError as exc:
            pages.append(_failed_page(page_no, image.sha256, exc))

    text_engine.close()
    if by_text_layer:
        _log.info(
            "部分页面取自文本层，未调用识别模型",
            extra={"doc": pdf_path.name, "detail": f"第 {by_text_layer} 页"},
        )

    # --- 3b. 跨引擎一致性校验（可选）---
    #
    # 威胁模型与自检不是一回事：自检防的是**生成阶段的幻觉**，
    # 而这里防的是**证据本身被识别错**——后者更隐蔽，因为答案里引用的
    # 每一个字都"有出处"，只是那个出处是错的。
    #
    # 代价要说清楚：这一遍会把**每页再识别一次**，API 成本翻倍。
    # 所以它默认关闭，只在配了 `DOCQA_OCR2_*` 时启用。
    cross_engine: dict = {"enabled": False, "reason": "未配置第二 OCR 引擎"}
    if engine2 is not None:
        by_page = {img.page_no: img for img in images}
        compared = 0
        flagged = 0
        skipped: list[int] = []
        for page in pages:
            image = by_page.get(page.page_no)
            # 主引擎已经失败的页没有可比对象，跳过（它已经进了 failed 集）
            if page.failed or image is None:
                skipped.append(page.page_no)
                continue
            # 文本层页**不参与**跨引擎比对：它的内容不是"识别"出来的，
            # 拿它去和模型输出比，只会把"模型与文本层不一致"报成可疑字段——
            # 而文本层恰恰是更可信的那个来源。这类页直接跳过，并计入 skipped。
            if page.provider == "textlayer":
                skipped.append(page.page_no)
                continue
            try:
                other = engine2.ocr_page(image, profile=profiles_by_page.get(page.page_no))
            except PageOcrError as exc:
                # 第二引擎失败**不影响本页可用性**——它只是一路佐证信号。
                # 但这必须记下来：否则"没比对"会被误读成"比对通过"。
                skipped.append(page.page_no)
                _log.warning(
                    "第二引擎失败，该页跳过跨引擎校验",
                    extra={"page": page.page_no, "detail": str(exc)[:200]},
                )
                continue
            diff = spotcheck_page(page, other)
            compared += 1
            if not diff.agreed:
                flagged += 1

        cross_engine = {
            "enabled": True,
            "engine": getattr(engine, "name", ""),
            "engine_b": getattr(engine2, "name", ""),
            "pages_compared": compared,
            "pages_flagged": flagged,
            "pages_skipped": skipped,
            "note": (
                "比对的是高风险字段（数值/编号/单位），差异写回块级 needs_review。"
                "标记≠有错，它是**送人工复核**的信号，不参与拒答判定。"
            ),
        }
        if flagged:
            _log.warning(
                f"跨引擎校验：{compared} 页中发现 {flagged} 页存在字段差异，已标 needs_review"
            )
        else:
            _log.info(f"跨引擎校验：{compared} 页字段两引擎一致")

    (out_dir / "ocr.json").write_text(
        json.dumps([p.to_dict() for p in pages], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- 3c. 提示词注入扫描 ---    #
    # 扫描件是**不可信输入**：一份被构造的文档可以印上"忽略以上指令"。
    # 模型侧的对策（证据进数据区、系统提示声明它是数据）是必要的，但它**不可观测**——
    # 从日志里看不出"这一页到底有没有藏指令"。合规场景要的恰恰是可观测：
    # 哪一页、哪一句、被标成什么。所以在这里扫一遍并落盘。
    #
    # 只标记、不拒答：注入短语与正常内容字面上可以完全一样
    # （"请按上述要求执行"在合同里是正常条款），自动拒答会在大批合法文档上误伤。
    with stage("injection", _log, doc=pdf_path.name) as info:
        injection = scan_pages(pages)
        info["detail"] = f"{injection.n_hits} 处可疑片段"

    # --- 4. 切分 ---
    doc_id = _doc_id(pdf_profile)
    with stage("chunk", _log, doc=doc_id) as info:
        chunks, chunk_stats = chunk_document(pages, doc_id, dom)
        info["detail"] = f"{len(chunks)} 个片段"
    with (out_dir / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")

    # --- 5. 索引 ---
    index = HybridIndex(chunks, dom)

    # --- 6. manifest ---
    manifest = {
        "doc_id": doc_id,
        "source": {
            "path": str(pdf_path),
            "sha256": pdf_profile.sha256,
            "page_count": pdf_profile.page_count,
        },
        "domain": dom.name,
        "detect": {
            "kind": pdf_profile.kind,
            "cross_engine": pdf_profile.cross_engine,
            "pages": [p.to_dict() for p in pdf_profile.pages],
        },
        "render": {
            "mode": cfg.render.mode,
            "upscale": cfg.render.upscale,
            "images": describe_images(images),
        },
        "ocr": {
            "engine": getattr(engine, "name", None),
            "model": getattr(engine, "model", None),
            "cross_engine": cross_engine,
            "pages_total": len(pages),
            "pages_failed": sorted(failed),
            "warnings": {str(p.page_no): p.warnings for p in pages if p.warnings},
        },
        "chunk": chunk_stats,
        "injection": injection.to_dict(),
        "index": index.to_dict(),
        "usage_total": _sum_usage(pages),
        "elapsed_s": round(time.time() - started, 2),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _log.info(
        "ingest 完成",
        extra={
            "doc": doc_id,
            "elapsed_ms": int((time.time() - started) * 1000),
            "detail": (
                f"{pdf_profile.kind}，{len(chunks)} 片段，"
                f"{len(failed)} 页失败，产物在 {out_dir}"
            ),
        },
    )

    return IngestResult(
        doc_id=doc_id,
        out_dir=out_dir,
        profile=pdf_profile,
        pages=pages,
        chunks=chunks,
        index=index,
        manifest=manifest,
        failed_pages=failed,
    )


def _sum_usage(pages: list[OcrPage]) -> dict:
    tin = sum(p.usage.tokens_in for p in pages)
    tout = sum(p.usage.tokens_out for p in pages)
    cost = sum(p.usage.est_cost_usd for p in pages)
    ms = sum(p.usage.latency_ms for p in pages)
    return {
        "tokens_in": tin,
        "tokens_out": tout,
        "est_cost_usd": round(cost, 6),
        "latency_ms": ms,
        "pages": len(pages),
    }


def load_result(out_dir: Path, cfg: Config, domain: str) -> IngestResult:
    """从已落盘的产物重建 IngestResult，无需重新识别。

    这是"无密钥也能验证下游"的实现方式，也是 `ask` 命令能独立运行的基础。
    注意：它读取的是 **ingest 自己产出的 ocr.json**，不是外来的文本文件——
    溯源链（`<doc_id>/` 下的页图哈希与 `cache/` 里的 Provenance）让这件事可以被独立检验。
    注意：本项目的真实文档受版权与隐私约束、不入库，所以这条链**只存在于本地**；
    可被评审完整复现的公开样例见 `artifacts/demo/`（合成件）。
    """
    from .config import load_domain
    from .ocr.cache import _page_from_dict

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    pages_raw = json.loads((out_dir / "ocr.json").read_text(encoding="utf-8"))
    pages = [_page_from_dict(d) for d in pages_raw]

    profile_path = out_dir / "profile.json"
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    from .models import PageProfile, PdfProfile

    pdf_profile = PdfProfile(
        path=Path(profile_data["path"]),
        sha256=profile_data["sha256"],
        page_count=profile_data["page_count"],
        pages=[
            PageProfile(
                page_no=p["page_no"],
                kind=p["kind"],
                char_count=p["char_count"],
                font_resource_count=p["font_resource_count"],
                drawn_image_count=p["drawn_image_count"],
                excluded_tiny_images=p["excluded_tiny_images"],
                drawn_image_area_ratio=p["drawn_image_area_ratio"],
                native_dpi=p["native_dpi"],
                reason=p["reason"],
            )
            for p in profile_data["pages"]
        ],
        kind=profile_data["kind"],
        cross_engine=profile_data.get("cross_engine", {}),
    )

    chunks: list[Chunk] = []
    with (out_dir / "chunks.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            chunks.append(
                Chunk(
                    chunk_id=d["chunk_id"],
                    doc_id=d["doc_id"],
                    page_no=d["page_no"],
                    ordinal=d["ordinal"],
                    kind=d["kind"],
                    text=d["text"],
                    clause_path=tuple(d.get("clause_path") or ()),
                    clause_label=d.get("clause_label"),
                    table_id=d.get("table_id"),
                    block_ids=tuple(d.get("block_ids") or ()),
                    char_len=d.get("char_len", len(d["text"])),
                )
            )

    index = HybridIndex(chunks, load_domain(domain, cfg))
    return IngestResult(
        doc_id=manifest["doc_id"],
        out_dir=out_dir,
        profile=pdf_profile,
        pages=pages,
        chunks=chunks,
        index=index,
        manifest=manifest,
        failed_pages=set(manifest.get("ocr", {}).get("pages_failed", [])),
    )


def load_results(
    out_dirs: list[Path], cfg: Config, domain: str
) -> IngestResult:
    """装载**多份** ingest 产物，合并成一个可检索的库。

    为什么需要这个
    --------------
    实际业务里问题很少只落在一份文件上：合规场景要问"这几份制度里
    对留存期限的规定是否一致"，客户交付要问"合同和技术协议对验收标准
    怎么写的"。单文档检索回答不了这类问题。

    合并的三件事
    ------------
    1. **片段**：直接拼接。`chunk_id` 本身带 doc_id 前缀（`<doc_id>:p1:c5`），
       所以不存在 ID 冲突，引用也能看出答案来自哪份文件。
    2. **索引**：在合并后的片段上**重建**一次。各自建好再合并分数是错的——
       BM25 的 IDF 依赖整个语料，分头算出来的分数不可比。
    3. **失败页**：升级成 `(doc_id, page_no)` 元组。
       两份文件的"第 3 页"是两回事，只比页码会让一份的失败页误伤另一份的证据。

    单份产物时行为与 `load_result` 完全一致。
    """
    from .config import load_domain

    if not out_dirs:
        raise InputError("没有可装载的产物目录")
    if len(out_dirs) == 1:
        return load_result(out_dirs[0], cfg, domain)

    results = [load_result(d, cfg, domain) for d in out_dirs]

    chunks: list[Chunk] = []
    failed: set[tuple[str, int]] = set()
    for r in results:
        chunks.extend(r.chunks)
        failed |= {(r.doc_id, p) for p in r.failed_pages}

    merged_doc_id = "+".join(r.doc_id for r in results)
    _log.info(
        "多文档装载完成",
        extra={
            "doc": merged_doc_id,
            "detail": f"{len(results)} 份产物，共 {len(chunks)} 个片段",
        },
    )
    return IngestResult(
        doc_id=merged_doc_id,
        out_dir=out_dirs[0],
        profile=results[0].profile,
        pages=[p for r in results for p in r.pages],
        chunks=chunks,
        index=HybridIndex(chunks, load_domain(domain, cfg)),
        manifest={
            "doc_id": merged_doc_id,
            "documents": [
                {"doc_id": r.doc_id, "chunks": len(r.chunks),
                 "failed_pages": sorted(r.failed_pages)}
                for r in results
            ],
        },
        failed_pages=failed,
    )
