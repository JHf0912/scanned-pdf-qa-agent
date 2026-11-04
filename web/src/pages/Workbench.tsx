import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import type {
  AnswerMode,
  AskResponse,
  AuthUser,
  BottomSheetKind,
  Chunk,
  DocInfo,
  Status,
  WorkbenchPanel,
} from "../types";
import { AdminPanel } from "../components/AdminPanel";
import { AnswerCard } from "../components/AnswerCard";
import { BottomSheet } from "../components/BottomSheet";
import { ChangePasswordModal } from "../components/ChangePasswordModal";
import { CheckList } from "../components/CheckList";
import { Composer } from "../components/Composer";
import { DocRail } from "../components/DocRail";
import { DocViewer } from "../components/DocViewer";
import { EvidencePanel } from "../components/EvidencePanel";
import { UserMenu } from "../components/UserMenu";
import {
  BrandMark,
  ChatIcon,
  LayersIcon,
  RefreshIcon,
  UploadIcon,
} from "../components/Icons";
import { MobileTabs } from "../components/MobileTabs";
import { Overview } from "../components/Overview";
import {
  EmptyState,
  ErrorBanner,
  Notice,
  SkeletonLines,
  WorkbenchSkeleton,
} from "../components/States";

interface WorkbenchProps {
  user: AuthUser;
  onNavigateHome: () => void;
  onLogout: () => void;
}

interface Message {
  id: string;
  question: string;
  pending: boolean;
  response?: AskResponse;
  error?: string;
}

const SUGGESTIONS = [
  "这份文档的适用范围是什么？",
  "有哪些关键的数值或限值？",
  "文档是否规定了材料牌号？",
  "这份文档代替了哪些历次版本？",
];

const ANSWER_MODE_STORAGE_KEY = "docqa.answerMode";

/** 同步读取，保证首屏直接渲染记住的模式，不出现 strict → hybrid 闪烁。 */
function readStoredAnswerMode(): AnswerMode {
  try {
    const stored = window.localStorage.getItem(ANSWER_MODE_STORAGE_KEY);
    return stored === "hybrid" || stored === "free" ? stored : "strict";
  } catch {
    return "strict";
  }
}

function storeAnswerMode(mode: AnswerMode): void {
  try {
    window.localStorage.setItem(ANSWER_MODE_STORAGE_KEY, mode);
  } catch {
    // localStorage 不可用（隐私模式/被禁用）时只在当前会话生效。
  }
}

function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function isMobile(): boolean {
  return window.matchMedia("(max-width: 767px)").matches;
}

function isTablet(): boolean {
  return window.matchMedia("(min-width: 768px) and (max-width: 1023px)").matches;
}

function uid(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

interface ChatIntroProps {
  ready: boolean;
  hasDocs: boolean;
  onAsk: (question: string) => void;
  onUpload: () => void;
}

function ChatIntro({ ready, hasDocs, onAsk, onUpload }: ChatIntroProps) {
  if (!hasDocs) {
    return (
      <EmptyState
        title="先放一份文档"
        body="上传 PDF 或 DOCX，或在终端运行 docqa ingest。PDF 会逐页识别，DOCX 直接解析正文。"
        icon={<UploadIcon size={20} />}
        action={
          <button type="button" className="btn btn-primary" onClick={onUpload}>
            上传文档
          </button>
        }
      />
    );
  }

  if (!ready) {
    return (
      <EmptyState
        title="文档还没有就绪"
        body="看右侧或顶部的错误提示；也可以点重新扫描，或换一份产物。"
        icon={<DocIconFallback />}
      />
    );
  }

  return (
    <div className="chat-intro">
      <div className="chat-intro-head">
        <span className="chat-intro-icon" aria-hidden="true">
          <ChatIcon size={20} />
        </span>
        <div>
          <h2>就这份文档提问</h2>
          <p>答案只来自检索到的证据；证据不足时，系统会拒答并说明原因。</p>
        </div>
      </div>
      <div className="suggestions" aria-label="推荐问题">
        {SUGGESTIONS.map((question) => (
          <button
            key={question}
            type="button"
            className="suggestion"
            onClick={() => onAsk(question)}
          >
            <span>{question}</span>
            <span aria-hidden="true">→</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function DocIconFallback() {
  return (
    <svg width={20} height={20} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 3h7l5 5v13H6V3Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M13 3v5h5" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

export function Workbench({ user, onNavigateHome, onLogout }: WorkbenchProps) {
  const [status, setStatus] = useState<Status | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [chunksError, setChunksError] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [asking, setAsking] = useState(false);
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [selecting, setSelecting] = useState(false);
  const [selectError, setSelectError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [panel, setPanel] = useState<WorkbenchPanel>("chat");
  const [sheet, setSheet] = useState<BottomSheetKind | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [activeCitation, setActiveCitation] = useState<string | null>(null);
  const [answerMode, setAnswerMode] = useState<AnswerMode>(readStoredAnswerMode);
  // 原文浏览：{docId, title, page}；null = 关闭。
  const [viewer, setViewer] = useState<{ docId: string; title: string; page: number | null } | null>(
    null,
  );
  const [adminOpen, setAdminOpen] = useState(false);
  const [changePwOpen, setChangePwOpen] = useState(false);

  const composerRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    setStatusLoading(true);
    setStatusError(null);
    try {
      const next = await api.getStatus();
      setStatus(next);
      setSelectedDocs(next.selected ?? (next.doc_id ? [next.doc_id] : []));
    } catch (error) {
      setStatusError(api.getErrorMessage(error));
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selectedKey = status?.selected?.join("|") ?? "";

  useEffect(() => {
    if (!status?.ready) {
      setChunks([]);
      setChunksError(null);
      return;
    }
    let cancelled = false;
    setChunksError(null);
    void api
      .getChunks()
      .then((next) => {
        if (!cancelled) setChunks(next);
      })
      .catch((error) => {
        if (!cancelled) setChunksError(api.getErrorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [status?.ready, status?.doc_id, selectedKey]);

  useEffect(() => {
    const node = chatRef.current;
    if (!node) return;
    node.scrollTo({
      top: node.scrollHeight,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  }, [messages]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!status?.doc_id) return;
    setMessages([]);
    setActiveCitation(null);
  }, [status?.doc_id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        composerRef.current?.focus();
      }
      if (event.key === "Escape") {
        setSheet(null);
        setDrawerOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const docs = status?.docs ?? [];
  const currentDocIds = useMemo(
    () => (selectedDocs.length > 0 ? selectedDocs : status?.doc_id ? [status.doc_id] : []),
    [selectedDocs, status?.doc_id],
  );
  const lastResponse = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const response = messages[index].response;
      if (response) return response;
    }
    return null;
  }, [messages]);
  const citations = lastResponse?.citations ?? [];
  const hits = lastResponse?.hits ?? [];
  const evidence = lastResponse?.evidence ?? {};
  const evidenceCount = useMemo(
    () =>
      new Set([
        ...hits.map((hit) => hit.chunk_id),
        ...citations.map((citation) => citation.chunk_id),
      ]).size,
    [hits, citations],
  );
  const ready = Boolean(status?.ready);
  // 只有状态明确报告 has_llm === true 才允许 hybrid；未加载、错误或旧响应缺字段时禁用。
  const hasLlm = status?.has_llm === true;
  // 未配置生成模型时强制走 strict，但不覆写 localStorage，等用户主动切换。
  const effectiveMode: AnswerMode = hasLlm ? answerMode : "strict";

  const handleModeChange = useCallback(
    (next: AnswerMode) => {
      if (next !== "strict" && !hasLlm) return;
      setAnswerMode(next);
      storeAnswerMode(next);
    },
    [hasLlm],
  );

  const handleAsk = useCallback(
    async (raw: string) => {
      const question = raw.trim();
      if (!question || asking) return;
      // 在发请求的一刻捕获模式；切换只影响下一问，进行中开关被禁用。
      const mode = effectiveMode;
      const id = uid();
      setMessages((previous) => [...previous, { id, question, pending: true }]);
      setDraft("");
      setAsking(true);
      setPanel("chat");
      try {
        const response = await api.askQuestion(question, mode);
        setMessages((previous) =>
          previous.map((message) =>
            message.id === id ? { ...message, pending: false, response } : message,
          ),
        );
        setActiveCitation(response.citations[0]?.chunk_id ?? null);
      } catch (error) {
        setMessages((previous) =>
          previous.map((message) =>
            message.id === id
              ? { ...message, pending: false, error: api.getErrorMessage(error) }
              : message,
          ),
        );
      } finally {
        setAsking(false);
      }
    },
    [asking, effectiveMode],
  );

  const changeSelection = useCallback(
    async (next: string[]) => {
      setSelecting(true);
      setSelectError(null);
      try {
        await api.selectDocs(next);
        setMessages([]);
        setActiveCitation(null);
        setPanel("chat");
        setNotice(next.length === 0 ? "已回到最新产物" : `已选择 ${next.length} 份文档`);
        await refresh();
      } catch (error) {
        setSelectError(api.getErrorMessage(error));
      } finally {
        setSelecting(false);
      }
    },
    [refresh],
  );

  const handleToggleDoc = (docId: string) => {
    const next = currentDocIds.includes(docId)
      ? currentDocIds.filter((id) => id !== docId)
      : [...currentDocIds, docId];
    void changeSelection(next);
  };

  const handleUpload = useCallback(
    async (file: File) => {
      const lower = file.name.toLowerCase();
      const isPdf = lower.endsWith(".pdf");
      if (!isPdf && !lower.endsWith(".docx")) {
        setUploadError("只接受 PDF 或 DOCX 文件（.pdf、.docx）。");
        return;
      }
      setUploading(true);
      setUploadError(null);
      setNotice(null);
      try {
        const result = await api.uploadDocument(file);
        setMessages([]);
        setActiveCitation(null);
        setPanel("chat");
        // DOCX 没有页概念，failed_pages 恒为空，不展示“识别失败页”。
        const failed =
          isPdf && result.failed_pages.length > 0
            ? `，${result.failed_pages.length} 页识别失败`
            : "";
        setNotice(`已上传并解析 ${file.name}：${result.chunks} 个片段${failed}`);
        await refresh();
      } catch (error) {
        setUploadError(api.getErrorMessage(error));
      } finally {
        setUploading(false);
      }
    },
    [refresh],
  );

  const handleCitation = (chunkId: string) => {
    setActiveCitation(chunkId);
    if (isMobile()) setSheet("evidence");
    else if (isTablet()) setDrawerOpen(true);
  };

  const handleOpenChecks = () => {
    if (isMobile()) setSheet("checks");
    else if (isTablet()) setDrawerOpen(true);
    else {
      document.getElementById("side-checks")?.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
    }
  };

  const handleCandidate = (value: string) => {
    setDraft(value);
    composerRef.current?.focus();
  };

  // 文档行「预览」：直接打开该文档原文，不改变当前选中/提问。
  const handlePreview = useCallback((doc: DocInfo) => {
    setViewer({ docId: doc.doc_id, title: doc.doc_id, page: null });
  }, []);

  if (statusLoading && !status) return <WorkbenchSkeleton />;

  if (statusError && !status) {
    return (
      <div className="wb-fatal">
        <BrandMark size={34} />
        <h1>无法连接后端</h1>
        <p>{statusError}</p>
        <div className="wb-fatal-actions">
          <button type="button" className="btn btn-primary" onClick={() => void refresh()}>
            重试
          </button>
          <button type="button" className="btn" onClick={onNavigateHome}>
            回到首页
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="wb">
      <header className="wb-top">
        <a
          className="wb-brand"
          href="/"
          onClick={(event) => {
            event.preventDefault();
            onNavigateHome();
          }}
        >
          <BrandMark size={24} />
          <span>docqa</span>
        </a>
        <button
          type="button"
          className="wb-doc-switch mobile-only"
          onClick={() => setPanel("docs")}
        >
          {currentDocIds.length > 0 ? `${currentDocIds.length} 份文档` : "选择文档"}
          <span aria-hidden="true">▾</span>
        </button>
        <div className="wb-top-meta">
          {statusLoading ? <span className="wb-refreshing">刷新中…</span> : null}
          <UserMenu
            user={user}
            onChangePassword={() => setChangePwOpen(true)}
            onLogout={onLogout}
            onOpenAdmin={() => setAdminOpen(true)}
          />
        </div>
        <div className="wb-top-actions">
          {user.role === "admin" ? (
            <button
              type="button"
              className="btn wb-open-doc"
              title="管理账号与各账号上传"
              onClick={() => setAdminOpen(true)}
            >
              用户管理
            </button>
          ) : null}
          <button
            type="button"
            className="icon-btn tablet-only"
            aria-label="打开证据与自检"
            onClick={() => setDrawerOpen(true)}
          >
            <LayersIcon size={17} />
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="重新扫描产物"
            title="重新扫描 data/derived 下的产物"
            onClick={() => void refresh()}
          >
            <RefreshIcon size={17} />
          </button>
          <button
            type="button"
            className="btn btn-primary wb-upload-btn"
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
          >
            <UploadIcon size={15} />
            <span>{uploading ? "解析中…" : "上传"}</span>
          </button>
        </div>
      </header>

      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleUpload(file);
          event.currentTarget.value = "";
        }}
      />

      <div className="wb-body" data-panel={panel}>
        <aside className="wb-docs" aria-label="文档列表">
          <DocRail
            docs={docs}
            selected={status?.selected ?? []}
            currentDocIds={currentDocIds}
            busy={selecting}
            uploading={uploading}
            uploadError={uploadError}
            onToggle={handleToggleDoc}
            onPreview={handlePreview}
            onClear={() => void changeSelection([])}
            onRefresh={() => void refresh()}
            onUpload={(file) => void handleUpload(file)}
          />
        </aside>

        <main className="wb-chat" aria-label="问答">
          <div className="chat-scroll" ref={chatRef}>
            <div className="chat-inner" aria-live="polite">
              {!ready && status?.error ? (
                <ErrorBanner message={status.error} onRetry={() => void refresh()} />
              ) : null}
              {statusError && status ? (
                <ErrorBanner message={statusError} onRetry={() => void refresh()} />
              ) : null}
              {selectError ? (
                <ErrorBanner message={selectError} onRetry={() => void refresh()} />
              ) : null}
              {notice ? <Notice>{notice}</Notice> : null}

              {messages.length === 0 ? (
                <ChatIntro
                  ready={ready}
                  hasDocs={docs.length > 0}
                  onAsk={(question) => void handleAsk(question)}
                  onUpload={() => fileRef.current?.click()}
                />
              ) : (
                messages.map((message) => (
                  <section className="msg" key={message.id}>
                    <div className="msg-q">
                      <span className="msg-label">你</span>
                      <p>{message.question}</p>
                    </div>
                    {message.pending ? (
                      <div className="answer-card card answer-pending">
                        <SkeletonLines lines={4} />
                        <p className="pending-note">正在检索证据并自检…</p>
                      </div>
                    ) : message.error ? (
                      <ErrorBanner
                        message={message.error}
                        onRetry={() => void handleAsk(message.question)}
                      />
                    ) : message.response ? (
                      <AnswerCard
                        response={message.response}
                        activeCitation={activeCitation}
                        chunks={chunks}
                        onCitation={handleCitation}
                        onOpenChecks={handleOpenChecks}
                        onCandidate={handleCandidate}
                      />
                    ) : null}
                  </section>
                ))
              )}
            </div>
          </div>
          <Composer
            value={draft}
            onChange={setDraft}
            onSubmit={() => void handleAsk(draft)}
            disabled={!ready}
            busy={asking}
            mode={effectiveMode}
            onModeChange={handleModeChange}
            hasLlm={hasLlm}
            inputRef={composerRef}
          />
        </main>

        <aside className="wb-side" aria-label="证据与自检">
          <section className="side-section" id="side-checks">
            <h2 className="side-title">自检</h2>
            <CheckList verification={lastResponse?.verification} />
          </section>
          <section className="side-section">
            <h2 className="side-title">
              证据
              {evidenceCount > 0 ? <span className="side-count">· {evidenceCount}</span> : null}
            </h2>
            {chunksError ? <ErrorBanner message={chunksError} /> : null}
            <EvidencePanel
              chunks={chunks}
              hits={hits}
              citations={citations}
              evidence={evidence}
              activeChunkId={activeCitation}
              onSelect={handleCitation}
              onOpenPage={(pageNo) =>
                status?.doc_id &&
                setViewer({ docId: status.doc_id, title: status.doc_id, page: pageNo })
              }
            />
          </section>
          <section className="side-section">
            <h2 className="side-title">文档概览</h2>
            <Overview status={status} />
          </section>
        </aside>
      </div>

      <MobileTabs panel={panel} onChange={setPanel} />

      <BottomSheet
        open={sheet !== null}
        title={sheet === "checks" ? "逐项自检" : "证据"}
        onClose={() => setSheet(null)}
      >
        {sheet === "checks" ? (
          <CheckList verification={lastResponse?.verification} />
        ) : (
          <EvidencePanel
            chunks={chunks}
            hits={hits}
            citations={citations}
            evidence={evidence}
            activeChunkId={activeCitation}
            onSelect={handleCitation}
          />
        )}
      </BottomSheet>

      <BottomSheet
        open={drawerOpen}
        variant="drawer"
        title="证据与自检"
        onClose={() => setDrawerOpen(false)}
      >
        <div className="drawer-stack">
          <section>
            <h3 className="side-title">自检</h3>
            <CheckList verification={lastResponse?.verification} />
          </section>
          <section>
            <h3 className="side-title">证据</h3>
            <EvidencePanel
              chunks={chunks}
              hits={hits}
              citations={citations}
              evidence={evidence}
              activeChunkId={activeCitation}
              onSelect={handleCitation}
            />
          </section>
          <section>
            <h3 className="side-title">文档概览</h3>
            <Overview status={status} />
          </section>
        </div>
      </BottomSheet>

      <DocViewer
        open={viewer !== null}
        docId={viewer?.docId ?? ""}
        docTitle={viewer?.title ?? ""}
        page={viewer?.page ?? null}
        onClose={() => setViewer(null)}
      />

      <AdminPanel
        open={adminOpen}
        currentUser={user}
        onClose={() => setAdminOpen(false)}
      />

      <ChangePasswordModal open={changePwOpen} onClose={() => setChangePwOpen(false)} />
    </div>
  );
}
