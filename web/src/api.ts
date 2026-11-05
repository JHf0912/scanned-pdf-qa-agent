import type {
  AnswerMode,
  AskResponse,
  Chunk,
  DocInfo,
  DocumentInfo,
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
    // 会话过期：除了登录接口本身，其它 401 统一广播，前端据此退回登录页。
    if (
      response.status === 401 &&
      !url.startsWith("/api/auth/login") &&
      !url.startsWith("/api/auth/me")
    ) {
      window.dispatchEvent(new CustomEvent("docqa:unauthorized"));
    }
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

/** 取某份文档的可浏览描述（PDF 地址 / 页图 / DOCX 段落）。 */
export async function getDocument(docId: string): Promise<DocumentInfo> {
  return request<DocumentInfo>(`/api/document?doc_id=${encodeURIComponent(docId)}`);
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
    answer_mode:
      payload.answer_mode === "hybrid"
        ? "hybrid"
        : payload.answer_mode === "free"
          ? "free"
          : "strict",
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

// ---------------------------------------------------------------- 认证与账号
export interface MeResult {
  authenticated: boolean;
  username?: string;
  role?: "user" | "admin";
}

export async function getMe(): Promise<MeResult> {
  return request<MeResult>("/api/auth/me");
}

export async function login(username: string, pw: string): Promise<MeResult> {
  return request<MeResult>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, pw }),
  });
}

/** 开放自助注册：成功后直接登录。 */
export async function register(username: string, pw: string): Promise<MeResult> {
  return request<MeResult>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, pw }),
  });
}

export async function logout(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
}

/** 登录用户自助修改密码（需原密码）。 */
export async function changePassword(oldPw: string, newPw: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/api/auth/change-pw", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_pw: oldPw, new_pw: newPw }),
  });
}

export interface UsersPayload {
  users: import("./types").AdminUser[];
}

export async function adminListUsers(): Promise<UsersPayload> {
  return request<UsersPayload>("/api/admin/users");
}

export async function adminCreateUser(
  username: string,
  pw: string,
  role: "user" | "admin",
): Promise<{ user: import("./types").AdminUser }> {
  return request("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, pw, role }),
  });
}

export async function adminUpdateUser(
  username: string,
  patch: { role?: "user" | "admin"; pw?: string; disabled?: boolean },
): Promise<{ user: import("./types").AdminUser }> {
  return request(`/api/admin/users/${encodeURIComponent(username)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export async function adminDeleteUser(username: string): Promise<{ ok: boolean }> {
  return request(`/api/admin/users/${encodeURIComponent(username)}`, { method: "DELETE" });
}

export async function adminUploads(): Promise<{
  by_owner: Record<string, import("./types").AdminUpload[]>;
}> {
  return request("/api/admin/uploads");
}
