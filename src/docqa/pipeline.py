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
from .logging import get_logger, stage
from .models import Chunk, OcrPage, PdfProfile
from .ocr import OcrEngine
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
    failed_pages: set[int]

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
    out_root: Path | None = None,
    force: bool = False,
) -> IngestResult:
    """跑完一次 ingest。

    Args:
        domain: 业务场景名，用于装载 domain profile。
        engine: OCR 引擎；None 时由调用方（CLI）按配置构造并传入。
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

    for image in images:
        try:
            with stage("ocr", _log, page=image.page_no):
                page = engine.ocr_page(image, profile=profiles_by_page.get(image.page_no))
            pages.append(page)
            if page.failed:
                failed.add(page.page_no)
        except PageOcrError as exc:
            # 单页失败被隔离：记录、继续，但不让下游拿到不完整的证据
            failed.add(image.page_no)
            _log.error(
                "该页识别失败，已隔离",
                extra={"page": image.page_no, "detail": str(exc)[:300]},
            )
            pages.append(
                OcrPage(
                    page_no=image.page_no,
                    blocks=[],
                    tables=[],
                    image_sha256=image.sha256,
                    provider=getattr(engine, "name", ""),
                    model=getattr(engine, "model", ""),
                    prompt_version="",
                    failed=True,
                    error=str(exc)[:500],
                )
            )

    (out_dir / "ocr.json").write_text(
        json.dumps([p.to_dict() for p in pages], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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
            "pages_total": len(pages),
            "pages_failed": sorted(failed),
            "warnings": {str(p.page_no): p.warnings for p in pages if p.warnings},
        },
        "chunk": chunk_stats,
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
