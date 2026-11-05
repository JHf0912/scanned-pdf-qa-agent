"""标题栈切分：把 `DocxBlock` 序列变成可检索、可引用的 `Chunk`。

三条硬规则（与 PDF 侧的 `chunker.py` 一致，但定位方式不同）
--------------------------------------------------------
1. **标题成栈**：`Heading 1 → Heading 2 → Heading 1` 时，第 3 个标题先把
   2 级及更深的条目弹出。每个 chunk 记下从外到内的 `clause_path`
   （`("1 总则", "1.1 范围")`），它是引用一致性校验的依据。
2. **表格原子化**：一张表永远是**一个** chunk，再长也不切。表格是数值的载体，
   切断之后数值会落到错误的上下文，检索能命中但引用指向错的地方。
3. **`¶n` 全覆盖**：每个非空正文块必须落在至少一个 chunk 的
   `[para_no, para_end]` 里。这是"没有内容被静默丢掉"的可执行断言
   （`tests/docx/test_chunks.py::test_every_nonempty_block_is_covered`）。

为什么不按段落一一对应出 chunk
------------------------------
短段落各自成 chunk 会让检索单位碎成一句半句，BM25 的分数失去区分度。
所以同一节内的连续段落会合并到 `max_chars` 为止；超长段落再按句子边界切，
并用字符级 overlap 保留上下文。**合并只发生在段落之间**，表格永远是独立块。

`¶n` 的定义
-----------
文档顺序第 n 个**非空正文块**（段落或线性化表格；标题计入；空段落不计）。
Word 的"段落数"包含空段落，两者对不上是预期行为——契约里写死的是前者。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from docqa.chunker import split_sentences
from docqa.config import DomainProfile
from docqa.docx.parser import DocxBlock, DocxDocument
from docqa.errors import InputError
from docqa.models import Chunk

ChunkKind = Literal["clause", "paragraph", "table", "heading"]


@dataclass(frozen=True)
class PlannedChunk:
    """切分计划的中间形态。

    先规划、再转成 `Chunk` 的好处：切分逻辑（标题栈、合并、切长）可以脱离
    数据模型单独测试，而 `Chunk` 的字段契约由一条薄转换保证。
    """

    ordinal: int
    kind: ChunkKind
    text: str
    para_no: int
    para_end: int
    clause_label: str | None = None
    clause_path: tuple[str, ...] = ()
    section: str | None = None
    table_id: str | None = None
    block_ids: tuple[str, ...] = ()
    char_len: int = 0


@dataclass(frozen=True)
class _Heading:
    level: int
    label: str | None  # 裸条号，进 clause_label
    label_text: str | None  # 展示条号，进 section / clause_path
    title: str

    @property
    def display(self) -> str:
        label = (self.label_text or self.label or "").strip()
        if label and self.title:
            return f"{label} {self.title}"
        return label or self.title


def split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """按句子边界切超长文本。

    `chunker._split_long` 是私有函数且 `chunker.py` 不在本 workstream 的
    文件所有权内，所以这里保留一份等价实现。两处行为必须一致（同样的
    `max_chars`/`overlap` 语义）；将来若把 `chunker._split_long` 提升为公开
    API，这段应当直接复用，不再各写一份。
    """
    if len(text) <= max_chars or max_chars <= 0:
        return [text]
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0
    for sentence in split_sentences(text):
        if size + len(sentence) > max_chars and buffer:
            pieces.append("".join(buffer))
            tail = "".join(buffer)[-overlap:] if overlap else ""
            buffer = [tail] if tail else []
            size = len(tail)
        buffer.append(sentence)
        size += len(sentence)
    if buffer:
        pieces.append("".join(buffer))
    return [piece for piece in pieces if piece.strip()]


def plan_docx_chunks(
    document: DocxDocument,
    profile: DomainProfile,
    *,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> tuple[list[PlannedChunk], dict]:
    """只做切分规划，不构造 `Chunk`。"""
    limit = max_chars if max_chars is not None else profile.chunk.max_chars
    overlap_chars = overlap if overlap is not None else profile.chunk.overlap_chars

    stack: list[_Heading] = []
    planned: list[PlannedChunk] = []
    pending: list[DocxBlock] = []
    structured = any(block.kind == "heading" for block in document.blocks)

    def emit(
        kind: ChunkKind,
        text: str,
        para_no: int,
        para_end: int,
        *,
        table_id: str | None = None,
        block_ids: tuple[str, ...] = (),
    ) -> None:
        top = stack[-1] if stack else None
        # 长章节会被切成多片。每片都必须带上紧凑的节头，否则答案写
        # "见 3.6 条"时，后面几片的证据文本里根本没有 3.6，
        # `numeric_grounding` / 条号一致性会假失败。标题 chunk 本身就以
        # 标题文字开头，不重复加。
        if top is not None and kind != "heading":
            header = top.display.strip()
            if header:
                text = f"{header}\n{text}"
        planned.append(
            PlannedChunk(
                ordinal=len(planned) + 1,
                kind=kind,
                text=text,
                para_no=para_no,
                para_end=para_end,
                clause_label=top.label if top is not None else None,
                clause_path=tuple(item.display for item in stack if item.display),
                section=_clean_section(
                    (top.label_text or top.label or top.title) if top is not None else None
                ),
                table_id=table_id,
                block_ids=block_ids,
                char_len=len(text),
            )
        )

    def emit_group(blocks: list[DocxBlock]) -> None:
        text = "\n".join(block.text for block in blocks)
        emit(
            "paragraph",
            text,
            blocks[0].para_no,
            blocks[-1].para_no,
            block_ids=tuple(f"b{block.para_no}" for block in blocks),
        )

    def emit_clause(block: DocxBlock, display: str) -> None:
        """把"自带条号的正文段落"发成一个带条号锚点的 chunk（超长仍按句子切）。"""
        path = tuple(item.display for item in stack if item.display) + (display,)
        # 预算里先扣掉条号前缀，保证每一片都还能装下正文。
        budget = max(limit - len(display) - 1, 1)
        for piece in split_long(block.text, budget, overlap_chars):
            text = piece if piece.lstrip().startswith(display) else f"{display} {piece}"
            planned.append(
                PlannedChunk(
                    ordinal=len(planned) + 1,
                    kind="paragraph",
                    text=text,
                    para_no=block.para_no,
                    para_end=block.para_no,
                    clause_label=block.clause_label,
                    clause_path=path,
                    section=_clean_section(display),
                    block_ids=(f"b{block.para_no}",),
                    char_len=len(text),
                )
            )

    def flush() -> None:
        nonlocal pending
        blocks, pending = pending, []
        buffer: list[DocxBlock] = []
        size = 0
        for block in blocks:
            if len(block.text) > limit:
                if buffer:
                    emit_group(buffer)
                    buffer, size = [], 0
                for piece in split_long(block.text, limit, overlap_chars):
                    emit(
                        "paragraph",
                        piece,
                        block.para_no,
                        block.para_no,
                        block_ids=(f"b{block.para_no}",),
                    )
                continue
            if buffer and size + len(block.text) > limit:
                emit_group(buffer)
                buffer, size = [], 0
            buffer.append(block)
            size += len(block.text)
        if buffer:
            emit_group(buffer)

    for block in document.blocks:
        if block.kind == "heading":
            flush()
            level = block.level or 1
            while stack and stack[-1].level >= level:
                stack.pop()
            stack.append(
                _Heading(
                    level=level,
                    label=block.clause_label,
                    label_text=block.label_text,
                    title=block.title,
                )
            )
            # 标题自己也是所在章节的开头：此时栈顶就是它，`section` 自然
            # 由它自己给出，而不是沿用上一个标题。
            emit(
                "heading",
                block.text,
                block.para_no,
                block.para_no,
                block_ids=(f"b{block.para_no}",),
            )
            continue
        if block.kind == "table":
            flush()
            emit(
                "table",
                block.text,
                block.para_no,
                block.para_no,
                table_id=block.table_id,
                block_ids=(f"b{block.para_no}",),
            )
            continue
        clause_display = _clause_start_display(block)
        if clause_display is not None:
            # 普通段落自带的条号（自动编号算出来的）是独立条款起点：
            # 让它在自己的 chunk 里带上条号，引用才能落到 `[§3.6.1 ¶7]`，
            # 而不是退化到 `[¶7]`。
            flush()
            emit_clause(block, clause_display)
            continue
        pending.append(block)
    flush()

    stats = {
        "chunks": len(planned),
        "structured": structured,
        "used_sentence_fallback": False,
        "tables": sum(1 for chunk in planned if chunk.kind == "table"),
        "avg_chars": (
            round(sum(chunk.char_len for chunk in planned) / len(planned), 1)
            if planned
            else 0
        ),
        "citation_mode": "clause" if structured else "paragraph",
    }
    return planned, stats


def _clean_section(value: str | None) -> str | None:
    """把 section 压成适合放进 `[...]` 引用的单行文本。

    `section` 会出现在 `[§{section} ¶{para}]` 里：换行会破坏证据头排版，
    `]` 会提前闭合引用。原始标题不做清洗，留在 `clause_path`/block 里。
    """
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.replace("]", " ")).strip()
    return cleaned or None


_CLAUSE_PREFIXES = ("第", "（", "(")
_CLAUSE_SUFFIXES = ("条", "章", "节", "篇", "部分")


def _clause_start_display(block: DocxBlock) -> str | None:
    """普通段落自带的条号能否作为独立条款起点。

    委员会文档里大量条号段落不是 Heading 样式（`第一条`、`3.6.1`），
    条号只存在于自动编号里。只按标题栈给 chunk 打标签，这些段落的条号
    就永远进不了证据文本，引用退化成 `[¶n]`。

    不是所有编号段落都该成为条款：`1.`、`2.` 这类列表项如果也开启新节，
    会让每个列表项都变成伪造的条款引用。所以只有"看起来像条号"的才认：
    多级编号（含 `.`）、`第X条/章/节`、`（一）` 形态。
    """
    if block.kind != "paragraph" or not block.clause_label:
        return None
    display = (block.label_text or block.clause_label).strip()
    if not display:
        return None
    if "." in block.clause_label or "." in display:
        return display
    if display.startswith(_CLAUSE_PREFIXES) or display.endswith(_CLAUSE_SUFFIXES):
        return display
    return None


def _ensure_coverage(document: DocxDocument, planned: list[PlannedChunk]) -> None:
    """硬不变量：每个非空正文块都必须进入至少一个 chunk 的 `block_ids`。

    这条不变量是"没有内容被静默丢掉"的可执行形式。规划逻辑本身保证覆盖，
    但一旦未来改动破坏它，**必须当场失败**——产出一份少了几段的证据库，
    比 ingest 直接报错危险得多（问答会自信地说"文档里没有"）。
    """
    covered = {block_id for chunk in planned for block_id in chunk.block_ids}
    missing = [
        block for block in document.blocks if f"b{block.para_no}" not in covered
    ]
    if not missing:
        return
    offenders = "、".join(f"¶{block.para_no}" for block in missing[:10])
    if len(missing) > 10:
        offenders += f" 等 {len(missing)} 个"
    raise InputError(
        f"DOCX 切分后仍有正文块未进入任何片段：{offenders}。\n"
        "这是内部不变量被破坏（有内容会被静默丢弃），已中止以免产出不完整的证据库。\n"
        "请保留该文件并反馈给维护者。"
    )


def build_docx_chunks(
    document: DocxDocument,
    doc_id: str,
    profile: DomainProfile,
    *,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> tuple[list[Chunk], dict]:
    """切分并构造冻结契约里的 `Chunk`。

    DOCX 没有页：`page_no=None`、`locator_kind="paragraph"`、
    `chunk_id=f"{doc_id}:b{ordinal}"`。页码相关逻辑（`failed_pages` 等）
    对 `page_no=None` 一律跳过，由管线层负责。
    """
    planned, stats = plan_docx_chunks(
        document, profile, max_chars=max_chars, overlap=overlap
    )
    _ensure_coverage(document, planned)
    chunks = [
        Chunk(
            chunk_id=f"{doc_id}:b{item.ordinal}",
            doc_id=doc_id,
            page_no=None,
            ordinal=item.ordinal,
            kind=item.kind,
            text=item.text,
            clause_path=item.clause_path,
            clause_label=item.clause_label,
            table_id=item.table_id,
            block_ids=item.block_ids,
            char_len=item.char_len,
            locator_kind="paragraph",
            section=item.section,
            para_no=item.para_no,
            para_end=item.para_end,
        )
        for item in planned
    ]
    return chunks, {**stats, "doc_id": doc_id}
