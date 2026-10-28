"""全流程共用的数据模型。

这里只放**数据结构**，不放逻辑。各阶段（detect / render / ocr / chunk / index / qa / eval）
通过这些类型交接，好处是每一阶段的产物都能单独序列化、单独检查——
"每一步都能单独跑、单独看中间产物"是定位问题的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------- 基础枚举

PageKind = Literal["TEXT", "SCANNED", "HYBRID"]
BlockType = Literal[
    "heading", "paragraph", "clause", "table", "figure", "stamp", "header", "footer"
]


class RefusalReason(str, Enum):
    """六类拒答。刻意区分开，因为**它们的用户动作不同**。

    把"文档里没有"和"这页没解析出来"混成一句"无法回答"，用户就不知道该换个问法
    还是该去重扫一遍。拒答必须能解释为什么拒。
    """

    NOT_IN_DOCUMENT = "not_in_document"        # 检索穷尽仍无证据 -> 换个问法
    EXTRACTION_FAILED = "extraction_failed"    # 该页 OCR 失败/未入库 -> 重跑或换更清晰的扫描件
    AMBIGUOUS_QUESTION = "ambiguous_question"  # >=2 个冲突候选 -> 用户消歧
    VERIFICATION_FAILED = "verification_failed"  # 有界修复后仍不过门 -> 查看证据
    OUT_OF_SCOPE = "out_of_scope"              # 问的是别的文档/领域 -> 换路由
    LOW_CONFIDENCE = "low_confidence"          # 最高分低于校准裕度 -> 查看证据


class CheckSeverity(str, Enum):
    """自检项的严重度。

    GATE 能改变控制流（不过则触发修复或拒答）；ADVISORY 只记录与上报。
    为什么需要这个区分：句级支撑度依赖中文分词，阈值在拿到实测精确率之前
    没有资格当门——把它当门只会产生假警报，最终训练使用者忽略它。
    """

    GATE = "gate"
    ADVISORY = "advisory"


# ---------------------------------------------------------------- 计量

@dataclass(frozen=True)
class Usage:
    """一次调用的计量。迁移到金融/合规场景时，成本与延迟是设计约束而非花絮。"""

    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0
    latency_ms: int = 0
    calls: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            est_cost_usd=self.est_cost_usd + other.est_cost_usd,
            latency_ms=self.latency_ms + other.latency_ms,
            calls=self.calls + other.calls,
        )

    def to_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "est_cost_usd": round(self.est_cost_usd, 6),
            "latency_ms": self.latency_ms,
            "calls": self.calls,
        }


# ---------------------------------------------------------------- PDF 类型判定

@dataclass(frozen=True)
class DetectSignal:
    """单条判定信号的取值为证据。

    判定结论必须**连同证据一起留下**，否则日后出现误判时无从复盘是哪个信号出了问题。
    """

    name: str
    value: float
    threshold: float
    passed: bool
    detail: str
    source: str = ""  # 该信号来自哪个引擎/工具，用于交叉验证的溯源


@dataclass
class PageProfile:
    page_no: int  # 1 起
    kind: PageKind
    char_count: int
    font_resource_count: int
    drawn_image_count: int  # 已按最小面积过滤
    excluded_tiny_images: int  # 被过滤掉的数量——单独计数，让"排除"这个行为可审计
    drawn_image_area_ratio: float  # 从显示列表的绘制面积算，不是数对象
    native_dpi: float | None  # 由最大绘制图像的像素尺寸与显示尺寸推得
    signals: list[DetectSignal] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "page_no": self.page_no,
            "kind": self.kind,
            "char_count": self.char_count,
            "font_resource_count": self.font_resource_count,
            "drawn_image_count": self.drawn_image_count,
            "excluded_tiny_images": self.excluded_tiny_images,
            "drawn_image_area_ratio": round(self.drawn_image_area_ratio, 4),
            "native_dpi": round(self.native_dpi, 1) if self.native_dpi else None,
            "reason": self.reason,
            "signals": [
                {
                    "name": s.name,
                    "value": s.value,
                    "threshold": s.threshold,
                    "passed": s.passed,
                    "detail": s.detail,
                    "source": s.source,
                }
                for s in self.signals
            ],
        }


@dataclass
class PdfProfile:
    path: Path
    sha256: str
    page_count: int
    pages: list[PageProfile]
    kind: PageKind
    # 交叉引擎结果，例如 {"pymupdf_chars": 0, "pdftotext_chars": 0, "pdftotext_available": 1}
    # 两个独立抽取器都给出 0 字符，比单个引擎的阈值判定硬得多。
    cross_engine: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "page_count": self.page_count,
            "kind": self.kind,
            "cross_engine": self.cross_engine,
            "pages": [p.to_dict() for p in self.pages],
        }


# ---------------------------------------------------------------- 页图

@dataclass(frozen=True)
class PageImage:
    page_no: int
    path: Path
    sha256: str
    width: int
    height: int
    dpi: float
    native: bool  # True = 位精确取自内嵌图像，未经重采样
    upscale: float = 1.0  # 相对原生尺寸的放大倍数（用于 A/B 实验留痕）


# ---------------------------------------------------------------- OCR 产物

@dataclass
class OcrBlock:
    block_id: str  # p{page}-b{idx}
    page_no: int
    type: BlockType
    order: int
    text: str  # 逐字转录；表格承载 HTML（保留 rowspan/colspan）
    bbox: tuple[float, float, float, float] | None = None
    clause_label: str | None = None  # "3.6" / "三、" / "（二）"，按转录原样
    uncertain: bool = False  # 模型自己标的不确定
    needs_review: bool = False  # 由跨引擎比对置位
    alternatives: list[str] = field(default_factory=list)

    @property
    def is_table(self) -> bool:
        return self.type == "table"

    def to_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "page_no": self.page_no,
            "type": self.type,
            "order": self.order,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "clause_label": self.clause_label,
            "uncertain": self.uncertain,
            "needs_review": self.needs_review,
            "alternatives": self.alternatives,
        }


@dataclass
class CellSpan:
    """合并单元格信息。表格问答要能定位到具体单元格，就得保留合并结构。"""

    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1


@dataclass
class TableGrid:
    """把表格规范化成矩形网格。

    `cells` 是**已展开合并**的矩形：被合并覆盖的位置重复填同一个值，
    这样按 (行, 列) 取值永远不需要额外判断。原始合并信息保留在 `spans` 里。
    """

    table_id: str  # p{page}-t{idx}
    page_no: int
    caption: str | None
    n_rows: int
    n_cols: int
    cells: list[list[str]]
    spans: list[CellSpan] = field(default_factory=list)
    n_header_rows: int = 1
    parse_degraded: bool = False  # 结构解析失败时置位，绝不静默降级

    def to_markdown(self) -> str:
        if not self.cells:
            return ""
        lines: list[str] = []
        if self.caption:
            lines.append(f"**{self.caption}**")
        for i, row in enumerate(self.cells):
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
            if i == self.n_header_rows - 1:
                lines.append("|" + "---|" * len(row))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "page_no": self.page_no,
            "caption": self.caption,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "n_header_rows": self.n_header_rows,
            "cells": self.cells,
            "spans": [
                {"row": s.row, "col": s.col, "rowspan": s.rowspan, "colspan": s.colspan}
                for s in self.spans
            ],
            "parse_degraded": self.parse_degraded,
        }


@dataclass
class OcrPage:
    page_no: int
    blocks: list[OcrBlock]
    tables: list[TableGrid]
    image_sha256: str
    provider: str
    model: str
    prompt_version: str
    usage: Usage = field(default_factory=Usage)
    warnings: list[str] = field(default_factory=list)
    # 逐页失败隔离：这一页失败不影响其它页，但证据触及它的问题必须拒答
    failed: bool = False
    error: str | None = None

    def plain_text(self) -> str:
        """按阅读顺序拼接整页文本，**并把块上的条号补回去**。

        为什么必须补
        ------------
        VLM 通常把条号放进结构化的 `clause_label` 字段，`text` 里只剩正文
        （「范围」而不是「1  范围」）。只拼 `text` 会让这份文本表示**丢掉条号**，
        而它有两个真实后果：

        1. **索引层评估**：参考转写里写着「3.1」，OCR 文本里找不到 →
           条号被报成"没识别出来"，数值召回被无端拉低，
           读起来像识别质量问题，实际是文本表示漏了字段；
        2. **跨引擎比对**：两个引擎对**条号**的分歧恰恰是最该抓的高风险差异之一
           （错一个条号，答案就引到了别的条款上）。丢掉它等于把一路信号静默关掉。

        正文里已经带了条号的（模型有时会原样写进 text）不重复补。
        """
        parts: list[str] = []
        for b in sorted(self.blocks, key=lambda b: b.order):
            label = (b.clause_label or "").strip()
            text = b.text or ""
            if label and not text.lstrip().startswith(label):
                parts.append(f"{label}  {text}" if text.strip() else label)
            else:
                parts.append(text)
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "page_no": self.page_no,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "image_sha256": self.image_sha256,
            "failed": self.failed,
            "error": self.error,
            "warnings": self.warnings,
            "usage": self.usage.to_dict(),
            "blocks": [b.to_dict() for b in self.blocks],
            "tables": [t.to_dict() for t in self.tables],
        }


# ---------------------------------------------------------------- 切分

@dataclass(frozen=True)
class Chunk:
    """检索与引用的基本单位。

    `clause_path` 是元组，从外到内（如 ("3 技术要求", "3.6")），
    它让"答案声称的条号"与"证据的条号"可以做一致性校验。
    """

    chunk_id: str
    doc_id: str
    page_no: int
    ordinal: int
    kind: Literal["clause", "paragraph", "table", "heading"]
    text: str
    clause_path: tuple[str, ...] = ()
    clause_label: str | None = None  # 渲染引用用，如 "3.6"
    table_id: str | None = None
    block_ids: tuple[str, ...] = ()
    char_len: int = 0

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "page_no": self.page_no,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "text": self.text,
            "clause_path": list(self.clause_path),
            "clause_label": self.clause_label,
            "table_id": self.table_id,
            "block_ids": list(self.block_ids),
            "char_len": self.char_len,
        }


# ---------------------------------------------------------------- 检索

@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    score: float
    arm: str  # "bm25_word" / "bm25_char" / "dense"
    rank: int = 0


# ---------------------------------------------------------------- 问答

# 引用渲染的默认模板。`citation_template` 在 domain profile 里可覆盖，
# 由 `Citation.render(template=...)` 使用——模板是**场景配置**的一部分，
# 不是写死的字符串（公文用「第X条」、标准用「§3.6」、合同可能要「附件二」）。
DEFAULT_CITATION_TEMPLATE = "[p.{page} §{clause}]"
# 没有条号时的退化模板（短公文整篇无结构时用段落号）。
FALLBACK_CITATION_TEMPLATE = "[p.{page} ¶{para}]"
BARE_CITATION_TEMPLATE = "[p.{page}]"


@dataclass(frozen=True)
class Citation:
    """引用。渲染要**容错**：有条号时给条号，没有时退化到段落号。

    短公文可能整篇无标题结构，此时条款切分会退化成句子窗口。
    如果引用格式强制要求条号，这类文档的引用就会崩掉。
    """

    page_no: int
    clause_label: str | None
    chunk_id: str
    quote: str
    para_no: int | None = None
    # 来源文档。多文档检索时，两份文件的"第 3 页"是两回事，
    # 引用里必须能区分它们——否则读者无法回到正确的那一份。
    doc_id: str | None = None

    def render(self, template: str | None = None) -> str:
        """渲染成人类可读的引用。

        Args:
            template: 形如 `"[p.{page} §{clause}]"` 的模板，通常来自
                `domain.citation_template`。为 None 时用默认模板。
                **模板里出现 `{doc}` 且本引用带 doc_id 时**会插入来源文档名，
                这是多文档场景下区分引用的唯一方式。
        """
        tpl = template or DEFAULT_CITATION_TEMPLATE
        doc = self.doc_id or ""
        # 模板里带了 {doc} 却没给 doc_id：不能留一个空槽，退回默认形式。
        if "{doc}" in tpl and not doc:
            tpl = tpl.replace("{doc}", "").replace("  ", " ").replace(" §", "§").strip()

        if self.clause_label:
            out = tpl.format(page=self.page_no, clause=self.clause_label,
                             doc=doc, para=self.para_no or "")
            return out
        if self.para_no:
            return FALLBACK_CITATION_TEMPLATE.format(
                page=self.page_no, para=self.para_no, doc=doc, clause=""
            )
        return BARE_CITATION_TEMPLATE.format(page=self.page_no, doc=doc, clause="",
                                             para="")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: CheckSeverity
    detail: str
    offenders: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity.value,
            "detail": self.detail,
            "offenders": list(self.offenders),
        }


@dataclass
class VerificationReport:
    checks: list[CheckResult] = field(default_factory=list)
    support_score: float = 0.0  # 咨询性指标；实测精确率前不当门

    @property
    def gates_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity is CheckSeverity.GATE)

    @property
    def failed_gates(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity is CheckSeverity.GATE and not c.passed]

    def to_dict(self) -> dict:
        return {
            "gates_passed": self.gates_passed,
            "support_score": round(self.support_score, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class ClarificationOption:
    """歧义问题的候选解释。给用户的是"选哪个"，不是一句"无法回答"。"""

    interpretation: str
    value: str
    citation: Citation


@dataclass
class TraceStep:
    """Agent 轨迹的一步。

    自检一旦能改变控制流（不过门 -> 修复 -> 复检），这条轨迹就是"自检不是走过场"的证据：
    评审能看到 `verify FAIL(numeric_grounding) -> repair -> verify PASS`。
    """

    step: str
    detail: str
    ms: int = 0
    usage: Usage | None = None


@dataclass
class AnswerBundle:
    question: str
    answer: str | None
    citations: list[Citation] = field(default_factory=list)
    refused: bool = False
    refusal_reason: RefusalReason | None = None
    refusal_detail: str | None = None
    candidates: list[ClarificationOption] = field(default_factory=list)
    verification: VerificationReport = field(default_factory=VerificationReport)
    hits: list[RetrievalHit] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1
    # 渲染引用用的模板（来自领域配置）。对外序列化时一并给出"渲染后"的字符串，
    # 前端与报告直接显示它，不需要自己再去拼一遍格式。
    citation_template: str | None = None

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason.value if self.refusal_reason else None,
            "refusal_detail": self.refusal_detail,
            "attempts": self.attempts,
            "citations": [
                {
                    "page_no": c.page_no,
                    "clause_label": c.clause_label,
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "quote": c.quote,
                    "rendered": c.render(self.citation_template),
                }
                for c in self.citations
            ],
            "candidates": [
                {"interpretation": c.interpretation, "value": c.value,
                 "citation": c.citation.render(self.citation_template)}
                for c in self.candidates
            ],
            "verification": self.verification.to_dict(),
            "hits": [
                {"chunk_id": h.chunk_id, "score": round(h.score, 4), "arm": h.arm, "rank": h.rank}
                for h in self.hits
            ],
            "trace": [
                {"step": t.step, "detail": t.detail, "ms": t.ms,
                 "usage": t.usage.to_dict() if t.usage else None}
                for t in self.trace
            ],
            "usage": self.usage.to_dict(),
        }
