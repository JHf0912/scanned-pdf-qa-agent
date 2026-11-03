export type LocatorKind = "page" | "paragraph";

export interface DocInfo {
  doc_id: string;
  dir: string;
  engine: string | null;
  kind: string | null;
  pages: number | null;
  chunks: number | null;
  mtime: number;
  failed_pages: number[];
  source_type?: string | null;
  unit_count?: number | null;
}

export interface Status {
  ready: boolean;
  error?: string;
  doc_id?: string;
  kind?: string;
  pages?: number;
  chunks?: number;
  tables?: number;
  failed_pages?: number[];
  citation_mode?: string | null;
  engine?: string | null;
  source_type?: string | null;
  unit_count?: number | null;
  /** DOCX 中未解析的构造计数（脚注、文本框、公式等），有值才展示。 */
  unsupported?: Record<string, number> | null;
  domain?: string;
  has_llm?: boolean;
  docs: DocInfo[];
  selected: string[] | null;
  max_concurrent?: number;
}

export interface Chunk {
  chunk_id: string;
  page_no: number | null;
  kind: string;
  clause_label: string | null;
  clause_path: string[];
  text: string;
  locator_kind: LocatorKind;
  section: string | null;
  para_no?: number | null;
  para_end?: number | null;
}

export interface Citation {
  page_no: number | null;
  clause_label: string | null;
  chunk_id: string;
  doc_id: string | null;
  quote: string;
  rendered: string;
  locator_kind: LocatorKind;
  section: string | null;
  para_no?: number | null;
  para_end?: number | null;
}

export type CheckSeverity = "gate" | "advisory";

export interface CheckResult {
  name: string;
  passed: boolean;
  severity: CheckSeverity;
  detail: string;
  offenders: string[];
}

export interface Verification {
  gates_passed: boolean;
  support_score: number;
  checks: CheckResult[];
}

export interface RetrievalHit {
  chunk_id: string;
  score: number;
  arm: string;
  rank: number;
}

export interface ClarificationCandidate {
  interpretation: string;
  value: string;
  citation: string;
}

export interface TraceStep {
  step: string;
  detail: string;
  ms: number;
}

export interface Usage {
  tokens_in: number;
  tokens_out: number;
  est_cost_usd: number;
  latency_ms: number;
  calls: number;
}

export interface EvidenceText {
  text: string;
  page_no: number | null;
  clause_label: string | null;
  locator_kind?: LocatorKind | null;
  section?: string | null;
  para_no?: number | null;
  para_end?: number | null;
  /** 引用在页图上的位置（相对页面的比例坐标，0~1）。 */
  bboxes?: BBox[];
  /** 页图信息；没有页图（DOCX / 文本层页）时为 null，前端退回纯文本高亮。 */
  page_image?: PageImageInfo | null;
}

export interface BBox {
  page_no: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface PageImageInfo {
  url: string;
  width: number | null;
  height: number | null;
}

export type AnswerMode = "strict" | "hybrid";

export interface AskResponse {
  question: string;
  answer: string | null;
  refused: boolean;
  refusal_reason: string | null;
  refusal_detail: string | null;
  attempts: number;
  citations: Citation[];
  candidates: ClarificationCandidate[];
  verification: Verification;
  hits: RetrievalHit[];
  trace: TraceStep[];
  usage: Usage;
  evidence: Record<string, EvidenceText>;
  server_elapsed_ms: number;
  /** 回答模式；旧响应缺字段时由 api 层归一为 strict。 */
  answer_mode: AnswerMode;
  /** hybrid 的模型知识补充段；strict 恒为 null。 */
  model_knowledge: string | null;
  /** 实际出现了非空补充段。 */
  knowledge_used: boolean;
}

export interface UploadResult {
  ready: boolean;
  doc_id: string;
  chunks: number;
  failed_pages: number[];
}

export type WorkbenchPanel = "chat" | "evidence" | "docs";
export type BottomSheetKind = "evidence" | "checks";
