import type {
  AnswerMode,
  AskResponse,
  Chunk,
  DocInfo,
  Status,
  UploadResult,
} from "./types";

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function messageFromPayload(payload: unknown, fallback: string): string {
  const record = asRecord(payload);
  if (!record) return fallback;
  const detail = record.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  const error = record.error;
  if (typeof error === "string" && error.trim()) return error;
  return fallback;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    throw new Error(
      messageFromPayload(payload, text || `请求失败（HTTP ${response.status}）`),
    );
  }
  return payload as T;
}

export async function getStatus(): Promise<Status> {
  return request<Status>("/api/status");
}

export async function getDocs(): Promise<{
  docs: DocInfo[];
  selected: string[] | null;
}> {
  return request<{ docs: DocInfo[]; selected: string[] | null }>("/api/docs");
}

export async function selectDocs(
  docIds: string[],
): Promise<{ ready: boolean; doc_id?: string; error?: string }> {
  return request("/api/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_ids: docIds }),
  });
}

export async function getChunks(): Promise<Chunk[]> {
  return request<Chunk[]>("/api/chunks");
}

export async function uploadDocument(file: File): Promise<UploadResult> {
  return request<UploadResult>("/api/upload", {
    method: "POST",
    headers: {
      // 后端按 Content-Type + 扩展名分派 PDF/DOCX；浏览器拿不到类型时退回字节流。
      "Content-Type": file.type || "application/octet-stream",
      "X-Filename": file.name,
    },
    body: file,
  });
}

/** @deprecated 使用 uploadDocument；保留别名以免外部调用漏改。 */
export const uploadPdf = uploadDocument;

/**
 * 旧后端 / 旧产物没有 hybrid 字段：缺省时按 strict / null / false 归一，
 * 保证 AnswerCard 永远拿到确定的模式与补充段。
 */
function normalizeAskResponse(payload: AskResponse): AskResponse {
  const knowledge =
    typeof payload.model_knowledge === "string" && payload.model_knowledge.trim()
      ? payload.model_knowledge
      : null;
  return {
    ...payload,
    answer_mode: payload.answer_mode === "hybrid" ? "hybrid" : "strict",
    model_knowledge: knowledge,
    knowledge_used: payload.knowledge_used === true && knowledge !== null,
  };
}

export async function askQuestion(
  question: string,
  mode: AnswerMode = "strict",
): Promise<AskResponse> {
  const payload = await request<AskResponse>("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, mode }),
  });
  return normalizeAskResponse(payload);
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return "发生未知错误，请稍后重试。";
}
