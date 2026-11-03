"""DOCX 原生支持（Phase 1：纯文本）。

分层与职责：

- `security`：打开之前先校验（ZIP 结构、大小、编码变体、DTD、路径穿越）；
- `parser`：按文档顺序抽正文（run walker、标题、表格线性化、不支持构造计数）；
- `numbering`：把 Word 自动编号解析成条号标签（有界、可退化为无标签）；
- `chunks`：标题栈切分成 `Chunk`，带 `para_no`/`section` 定位。

Phase 1 明确不做：表格合并结构、图片/扫描页 OCR、脚注/尾注/文本框/批注/
页眉页脚（这些只计数上报，绝不静默丢弃）。
"""

from __future__ import annotations

from docqa.docx.chunks import PlannedChunk, build_docx_chunks, plan_docx_chunks
from docqa.docx.numbering import NumberingLabel, NumberingResolver
from docqa.docx.parser import DocxBlock, DocxDocument, DocxStats, parse_docx
from docqa.docx.security import DocxSecurity, is_docx, validate_docx

__all__ = [
    "DocxBlock",
    "DocxDocument",
    "DocxSecurity",
    "DocxStats",
    "NumberingLabel",
    "NumberingResolver",
    "PlannedChunk",
    "build_docx_chunks",
    "is_docx",
    "parse_docx",
    "plan_docx_chunks",
    "validate_docx",
]
