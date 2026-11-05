import type { LocatorKind } from "./types";

export interface AnswerSegment {
  type: "text" | "citation";
  value: string;
}

export interface HighlightSegment {
  text: string;
  match: boolean;
}

export interface RefusalMeta {
  label: string;
  explanation: string;
  action: string;
}

export interface ParsedCitation {
  page: number | null;
  section: string | null;
  para: number | null;
}

/**
 * 引用 token 必须是 `]` 结尾、内部不含 `]`/换行。
 * section 排除 `¶`，避免 `§...` 与可选段落号之间产生回溯。
 */
const SECTION_PATTERN = "[^\\]¶\\n]+?";
const CITATION_BODY =
  `p\\.\\d+(?:\\s*§${SECTION_PATTERN})?(?:\\s*¶\\d+)?` +
  `|§${SECTION_PATTERN}(?:\\s*¶\\d+)?` +
  `|¶\\d+`;

const CITATION_SPLIT = new RegExp(`(\\[(?:${CITATION_BODY})\\])`, "g");
const CITATION_TEST = new RegExp(`^\\[(?:${CITATION_BODY})\\]$`);
const CITATION_PARTS = new RegExp(
  `^\\[(?:p\\.(\\d+)(?:\\s*§(${SECTION_PATTERN}))?(?:\\s*¶(\\d+))?` +
    `|§(${SECTION_PATTERN})(?:\\s*¶(\\d+))?` +
    `|¶(\\d+))\\]$`,
);

function toInt(value: string | undefined): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function cleanSection(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

/** 解析单个引用 token；不匹配时返回 null。 */
export function parseCitationToken(token: string): ParsedCitation | null {
  const match = CITATION_PARTS.exec(token.trim());
  if (!match) return null;
  return {
    page: toInt(match[1]),
    section: cleanSection(match[2] ?? match[4]),
    para: toInt(match[3] ?? match[5] ?? match[6]),
  };
}

export function splitAnswer(answer: string): AnswerSegment[] {
  return answer
    .split(CITATION_SPLIT)
    .filter((part) => part !== "")
    .map((part) => ({
      type: CITATION_TEST.test(part) ? "citation" : "text",
      value: part,
    }));
}

/** 旧产物没有 source_type：有正文块数时按 DOCX 处理，避免显示“0 页”。 */
export function isDocxSource(
  sourceType: string | null | undefined,
  unitCount?: number | null,
): boolean {
  if (sourceType === "docx") return true;
  if (sourceType === "pdf") return false;
  return typeof unitCount === "number" && unitCount > 0;
}

export interface LocatorParts {
  page_no: number | null;
  locator_kind?: LocatorKind | null;
  section?: string | null;
  clause_label?: string | null;
  para_no?: number | null;
  para_end?: number | null;
}

function paragraphRange(
  start: number | null | undefined,
  end: number | null | undefined,
): string | null {
  if (start === null || start === undefined) return null;
  if (end === null || end === undefined || end === start) return String(start);
  return `${start}–${end}`;
}

/**
 * PDF 输出 `p.3 §3.6`（无条号时 `p.3 ¶12`），DOCX 输出 `§3.6 ¶4`（无条号时 `¶4`）。
 * page_no 为空且没有任何段落定位时返回 fallback（调用方传 chunk_id）。
 */
export function formatLocator(parts: LocatorParts, fallback = "—"): string {
  const section = (parts.section ?? parts.clause_label ?? "").trim();
  const para = paragraphRange(parts.para_no, parts.para_end);
  const sectionBit = section ? `§${section}` : "";
  const paraBit = para ? `¶${para}` : "";

  if (parts.locator_kind === "paragraph") {
    const bits = [sectionBit, paraBit].filter(Boolean);
    return bits.length > 0 ? bits.join(" ") : fallback;
  }
  if (parts.page_no !== null && parts.page_no !== undefined) {
    return [`p.${parts.page_no}`, sectionBit, paraBit].filter(Boolean).join(" ");
  }
  const bits = [sectionBit, paraBit].filter(Boolean);
  return bits.length > 0 ? bits.join(" ") : fallback;
}

export function highlightQuote(text: string, quote: string): HighlightSegment[] {
  const candidates = [quote, quote.slice(0, 60), quote.slice(0, 30)]
    .map((candidate) => candidate.trim())
    .filter((candidate) => candidate.length >= 6);

  for (const needle of candidates) {
    const index = text.indexOf(needle);
    if (index >= 0) {
      return [
        { text: text.slice(0, index), match: false },
        { text: needle, match: true },
        { text: text.slice(index + needle.length), match: false },
      ].filter((segment) => segment.text.length > 0);
    }
  }
  return [{ text, match: false }];
}

const CHECK_LABELS: Record<string, string> = {
  citation_validity: "引用有效性",
  numeric_grounding: "数值落地",
  clause_consistency: "条号一致性",
  sentence_support: "句级支撑度",
  extraction_health: "识别完整性",
  injection_scan: "提示注入扫描",
  knowledge_label: "知识标注",
  doc_sentence_support: "句级引用支撑",
  doc_citation_marks: "引用标记绑定",
};

export function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name;
}

const REFUSALS: Record<string, RefusalMeta> = {
  not_in_document: {
    label: "文档里没有找到依据",
    explanation: "检索穷尽后仍没有能支撑答案的片段，系统没有编造。",
    action: "换个问法，或确认这个问题是否属于这份文档。",
  },
  extraction_failed: {
    label: "相关页面没有识别成功",
    explanation: "答案可能就在这一页，但 OCR 没有把它读出来。",
    action: "重扫该页，或换一份更清晰的扫描件再试。",
  },
  ambiguous_question: {
    label: "问题有不止一种理解",
    explanation: "检索到多个互相冲突的解释，系统不愿替你猜。",
    action: "从候选解释里选一个，或补一句限定条件。",
  },
  verification_failed: {
    label: "答案没有通过自检",
    explanation: "生成的内容没有通过引用、数值或条号校验，系统选择拒答。",
    action: "查看证据与自检项，换个问法再试。",
  },
  out_of_scope: {
    label: "问题超出了这份文档的范围",
    explanation: "检索结果与问题主题不匹配，继续回答只会是推测。",
    action: "换一份文档，或把问题缩小到文档覆盖的条款。",
  },
  low_confidence: {
    label: "检索到的证据不够明确",
    explanation: "最高分与次高分咬得太紧，系统没有把握判定哪条适用。",
    action: "补上条款号或关键词，或查看证据后自行判断。",
  },
};

const FALLBACK_REFUSAL: RefusalMeta = {
  label: "无法回答",
  explanation: "系统没有找到足够可靠的依据。",
  action: "换个问法，或查看证据后再试。",
};

export function refusalMeta(reason: string | null): RefusalMeta {
  return (reason && REFUSALS[reason]) || FALLBACK_REFUSAL;
}

export function formatMs(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

export function formatScore(score: number): string {
  return Number.isFinite(score) ? score.toFixed(2) : "—";
}

export function docTitle(docId: string): string {
  return docId.replace(/-\d+$/, "");
}
