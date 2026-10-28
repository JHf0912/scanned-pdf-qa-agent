"""条款感知切分。

为什么不能按固定字数硬切
------------------------
固定窗口会把一个条款从中间截断，于是"3.6 楔键斜度 1:100 的角度公差……"
被切成两半，检索时两半的分数都不高，答案就漏了。更糟的是引用——
如果 chunk 没有条号，答案就没法说清"依据是 3.6 条"。

所以按**文档自身的结构**切：条号是天然的分隔符，也是天然的引用锚点。

三条硬规则
----------
1. **表格原子化**：一张表永远是一个 chunk，绝不跨 chunk 切断，
   也绝不和相邻正文合并。表格是数值的载体，切断了数值就会落到错误的上下文里。
2. **条号随行**：chunk 记录 `clause_path`（从外到内，如 `("3 技术要求", "3.6")`），
   它是"答案声称的条号"与"证据的条号"做一致性校验的依据。
3. **必须有退化路径**：短公文可能整篇是一段连续文字，没有任何标题结构。
   这时条款切分会产出唯一一个 chunk，引用与检索双双退化。
   必须能识别这种情况并退回**句子窗口**切分——否则检索单位就变成了整页。

退化不是缺陷，是必须被设计出来的路径：真实客户文档里，
"没有结构"比"结构规整"要常见得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import DomainProfile
from .logging import get_logger
from .models import Chunk, OcrBlock, OcrPage

_log = get_logger("chunker")

# 句子边界：中文句号/问号/叹号/分号 + 英文对应符号。
# 分号也算边界，是因为公文与标准里分号常用来分隔并列的独立要求。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")


@dataclass(frozen=True)
class _Section:
    """一个结构单元：条号 + 标题 + 正文。"""

    label: str | None
    title: str
    text: str
    block_ids: tuple[str, ...]
    page_no: int


def _compile_patterns(profile: DomainProfile) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for raw in profile.clause_patterns:
        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            _log.warning(
                "场景 %s 的条款正则无法编译，已跳过",
                profile.name,
                extra={"detail": f"{raw!r}: {exc}"},
            )
    return patterns


def _match_label(text: str, patterns: list[re.Pattern[str]]) -> tuple[str, str] | None:
    """尝试把一行识别为条款起点。

    Returns:
        (label, title) 或 None。
    """
    head = text.strip()[:120]
    for pattern in patterns:
        m = pattern.match(head)
        if m and m.groupdict().get("label"):
            return m.group("label").strip(), (m.groupdict().get("title") or "").strip()
    return None


def split_sentences(text: str) -> list[str]:
    """按中文标点切句。保留标点，因为它们承载语气与逻辑关系。"""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p and p.strip()]
    return parts


def _running_furniture(pages: list[OcrPage]) -> set[str]:
    """找出**跨页重复出现**的页眉/页脚文本（归一化后）。

    为什么不能一律丢掉
    ------------------
    "页眉页脚是版面噪声"这个判断，对**重复出现**的页眉页脚成立
    （书名、页码、文件编号）。但对**封面**不成立：封面底部恰恰是
    发布/实施日期、发布机构这类正式元数据——而它们正是会被问到的东西。

    实测踩到过：GB/T 的封面把 `2008-09-22 发布` / `2009-05-01 实施`
    判成了 `footer`，切分时被整块丢掉。于是"发布日期是哪天"这个问题
    在检索里**永远找不到证据**，只能拒答成"文档里没有"——
    识别明明是对的，是切分把证据丢了，而表面上看起来像"文档没写"。

    判据因此改成**是否重复**：只有跨页重复出现的才算版面噪声。
    这也正是 "running header/footer" 本来的定义。
    """
    seen: dict[str, set[int]] = {}
    for page in pages:
        for b in page.blocks:
            if b.type in ("header", "footer"):
                seen.setdefault(_norm_furniture(b.text), set()).add(page.page_no)
    return {text for text, pageset in seen.items() if len(pageset) >= 2}


def _norm_furniture(text: str) -> str:
    """归一化以判断"是不是同一段页眉页脚"。

    只压空白、去首尾——**故意不做繁简/全角归一**：这里判的是"两页上是不是
    同一段文字"，宽松到把不同内容算成相同，会让本该保留的内容被丢掉。
    宁可少丢，不可错丢。
    """
    return "".join((text or "").split())


def _is_running(text: str, running: set[str]) -> bool:
    return _norm_furniture(text) in running


def _blocks_to_sections(
    blocks: list[OcrBlock], patterns: list[re.Pattern[str]],
    running: set[str] | None = None,
) -> tuple[list[_Section], bool]:
    """把块序列归并成结构单元。

    Args:
        running: **跨页重复出现**的页眉/页脚文本（见 `_running_furniture`）。
            只有落在里面的才丢弃；一次性的页眉页脚是内容，要留着。

    Returns:
        (单元列表, 是否真的识别到了结构)。第二个返回值驱动退化决策。
    """
    sections: list[_Section] = []
    current_label: str | None = None
    current_title = ""
    current_parts: list[str] = []
    current_blocks: list[str] = []
    current_page = 1
    matched_any = False

    def flush() -> None:
        nonlocal current_label, current_title, current_parts, current_blocks
        text = "\n".join(p for p in current_parts if p.strip())
        if text.strip() or current_label:
            sections.append(
                _Section(
                    label=current_label,
                    title=current_title,
                    text=text,
                    block_ids=tuple(current_blocks),
                    page_no=current_page,
                )
            )
        current_label, current_title, current_parts, current_blocks = None, "", [], []

    for block in sorted(blocks, key=lambda b: (b.page_no, b.order)):
        # 表格自成一体，先冲刷已有正文，再单独入列
        if block.is_table:
            flush()
            sections.append(
                _Section(
                    label=block.clause_label,
                    title=block.clause_label or "表格",
                    text=block.text,
                    block_ids=(block.block_id,),
                    page_no=block.page_no,
                )
            )
            continue

        if block.type in ("header", "footer") and _is_running(block.text, running):
            continue  # 跨页重复出现的才是版面噪声，不进检索

        # 条号有两个来源，**两个都要认**：
        #
        #   1. OCR 提示词明确要求把条号写进 `clause_label` 字段
        #      （见 prompts/ocr_page.v1.md），所以识别结果里条号在字段上、
        #      正文只剩标题——`text` 是「范围」而不是「1 范围」。
        #   2. 从文本层直取的文档（或模型把条号写进了正文）里，
        #      条号就在 `text` 开头，需要正则去认。
        #
        # 只认第 2 种会有一个很隐蔽的后果：**结构明明识别出来了，
        # 切分器却当没有**，于是整个文档退回句子窗口，条款引用与表格原子性
        # 一起丢掉——而日志只轻描淡写地说"未识别到条款结构"。
        # 实测就是这么踩的：4 页国标 66 个块，切出来只有 14 个片段、0 张表。
        hit: tuple[str, str] | None = None
        label = (block.clause_label or "").strip()
        if label:
            head = block.text.strip().splitlines()[0] if block.text.strip() else ""
            # 条号已经单独给出来了，标题取正文首行；若首行本身就是条号则去掉它
            title = head[len(label):].strip() if head.startswith(label) else head
            hit = (label, title)
        else:
            hit = _match_label(block.text, patterns)

        if hit is not None:
            flush()
            matched_any = True
            current_label, current_title = hit
            current_page = block.page_no
            current_parts.append(block.text)
            current_blocks.append(block.block_id)
            continue

        if block.type == "heading" and not current_parts:
            current_title = block.text.strip()
            current_page = block.page_no

        if not current_parts:
            current_page = block.page_no
        current_parts.append(block.text)
        current_blocks.append(block.block_id)

    flush()
    return sections, matched_any


def _window_split(text: str, *, window: int, overlap_sentences: int) -> list[str]:
    """句子窗口切分。用于没有结构的连续正文。"""
    sentences = split_sentences(text)
    if not sentences:
        return []
    windows: list[str] = []
    buf: list[str] = []
    size = 0
    for sent in sentences:
        buf.append(sent)
        size += len(sent)
        if size >= window:
            windows.append("".join(buf))
            keep = buf[-overlap_sentences:] if overlap_sentences > 0 else []
            buf = list(keep)
            size = sum(len(s) for s in buf)
    if buf:
        tail = "".join(buf)
        if not windows or tail != windows[-1]:
            windows.append(tail)
    return windows


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长条款的二次切分，按句子边界尽量切齐。"""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for sent in split_sentences(text):
        if size + len(sent) > max_chars and buf:
            out.append("".join(buf))
            tail = "".join(buf)[-overlap:] if overlap else ""
            buf = [tail] if tail else []
            size = len(tail)
        buf.append(sent)
        size += len(sent)
    if buf:
        out.append("".join(buf))
    return [s for s in out if s.strip()]


def chunk_document(
    pages: list[OcrPage],
    doc_id: str,
    profile: DomainProfile,
    *,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> tuple[list[Chunk], dict]:
    """把 OCR 产物切成可检索的 chunk。

    Returns:
        (chunks, 统计信息)。统计信息进 manifest —— 包括**是否走了退化路径**，
        因为那会直接影响引用格式与检索质量，必须可追溯。
    """
    cfg = profile.chunk
    max_chars = max_chars or cfg.max_chars
    overlap = overlap if overlap is not None else cfg.overlap_chars
    patterns = _compile_patterns(profile)

    all_blocks: list[OcrBlock] = [b for p in pages for b in p.blocks]
    sections, structured = _blocks_to_sections(
        all_blocks, patterns, _running_furniture(pages)
    )

    chunks: list[Chunk] = []
    ordinal = 0
    used_fallback = False

    for section in sections:
        if not section.text.strip():
            continue

        is_table = section.block_ids and any(
            b.is_table for b in all_blocks if b.block_id in section.block_ids
        )
        kind = "table" if is_table else ("clause" if section.label else "paragraph")

        # 表格永远不切；其余按长度决定是否二次切分
        pieces = [section.text] if is_table else _split_long(section.text, max_chars, overlap)

        for piece in pieces:
            ordinal += 1
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:p{section.page_no}:c{ordinal}",
                    doc_id=doc_id,
                    page_no=section.page_no,
                    ordinal=ordinal,
                    kind=kind,  # type: ignore[arg-type]
                    text=piece,
                    clause_path=tuple(x for x in (section.title, section.label) if x),
                    clause_label=section.label,
                    table_id=None,
                    block_ids=section.block_ids,
                    char_len=len(piece),
                )
            )

    # --- 退化路径 ---
    # 结构没识别出来，或者切完只剩一个 chunk（说明整篇是一段连续正文）。
    # 后者同样要退化：单 chunk 的检索等于没有检索。
    if not structured or len(chunks) <= 1:
        used_fallback = True
        ordinal = 0
        chunks = []
        # 退化路径同样只丢**跨页重复**的页眉页脚——理由与主路径一致
        # （见 `_running_furniture`）。两处判据必须用同一个 `running` 集，
        # 否则同一份文档走哪条路径会得到不同的正文，而这件事完全不可见。
        running = _running_furniture(pages)
        for page in sorted(pages, key=lambda p: p.page_no):
            body = "\n".join(
                b.text
                for b in sorted(page.blocks, key=lambda b: b.order)
                if not (b.type in ("header", "footer") and _is_running(b.text, running))
            )
            if not body.strip():
                continue
            windows = _window_split(
                body,
                window=cfg.sentence_window_chars,
                overlap_sentences=cfg.sentence_overlap,
            )
            for window in windows:
                ordinal += 1
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}:p{page.page_no}:c{ordinal}",
                        doc_id=doc_id,
                        page_no=page.page_no,
                        ordinal=ordinal,
                        kind="paragraph",
                        text=window,
                        clause_path=(),
                        clause_label=None,
                        block_ids=tuple(b.block_id for b in page.blocks),
                        char_len=len(window),
                    )
                )
        _log.info(
            "切分走退化路径（句子窗口）",
            extra={"detail": f"structured={structured} chunks={len(chunks)} doc={doc_id}"},
        )

    stats = {
        "doc_id": doc_id,
        "chunks": len(chunks),
        "structured": structured,
        "used_sentence_fallback": used_fallback,
        "tables": sum(1 for c in chunks if c.kind == "table"),
        "avg_chars": round(sum(c.char_len for c in chunks) / len(chunks), 1) if chunks else 0,
        "citation_mode": "clause" if structured and not used_fallback else "paragraph",
    }
    return chunks, stats
