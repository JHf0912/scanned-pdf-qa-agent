import type { AnswerMode, AskResponse, Chunk, Citation } from "../types";
import {
  checkLabel,
  formatMs,
  parseCitationToken,
  refusalMeta,
  splitAnswer,
} from "../format";
import { AlertIcon, ArrowRightIcon, CheckIcon, InfoIcon } from "./Icons";
import { CheckList } from "./CheckList";

interface AnswerCardProps {
  response: AskResponse;
  activeCitation: string | null;
  chunks?: Chunk[];
  onCitation: (chunkId: string) => void;
  onOpenChecks: () => void;
  onCandidate: (value: string) => void;
}

interface LocatorSource {
  pageNo: number | null;
  sections: string[];
  paraNo: number | null;
  paraEnd: number | null;
}

function ModeChip({ mode }: { mode: AnswerMode }) {
  const cls = mode === "hybrid" ? " is-hybrid" : mode === "free" ? " is-free" : "";
  const label =
    mode === "hybrid" ? "含模型知识" : mode === "free" ? "自由回答" : "仅文档";
  return <span className={`answer-mode-chip${cls}`}>{label}</span>;
}

/**
 * 后端字段可能缺失（旧产物/旧 JSON）：citation 没有时退到同 chunk 的定位信息。
 * `section` 与 `clause_label` 都参与匹配，避免标题/条号谁优先造成漏配。
 */
function locatorFor(citation: Citation, chunks: Chunk[]): LocatorSource {
  const chunk = chunks.find((item) => item.chunk_id === citation.chunk_id);
  const pageNo = citation.page_no ?? chunk?.page_no ?? null;
  const sections = [
    citation.section,
    citation.clause_label,
    chunk?.section,
    chunk?.clause_label,
  ]
    .map((value) => (value ?? "").trim())
    .filter((value) => value.length > 0);
  const paraNo = citation.para_no ?? chunk?.para_no ?? null;
  return {
    pageNo,
    sections: [...new Set(sections)],
    paraNo,
    paraEnd: citation.para_end ?? chunk?.para_end ?? paraNo,
  };
}

function findCitation(
  token: string,
  citations: Citation[],
  chunks: Chunk[] = [],
): Citation | undefined {
  const normalized = token.trim();
  const exact = citations.find((citation) => citation.rendered.trim() === normalized);
  if (exact) return exact;

  const parsed = parseCitationToken(normalized);
  if (!parsed) return undefined;

  const locatorMatches = (citation: Citation): boolean => {
    const locator = locatorFor(citation, chunks);
    if (parsed.page !== null && locator.pageNo !== parsed.page) return false;
    if (parsed.section !== null && !locator.sections.includes(parsed.section)) {
      return false;
    }
    if (parsed.para === null) return true;
    if (locator.paraNo === null) return false;
    // `[§3.6 ¶5]` 可能落在覆盖 ¶4–¶6 的同一个 chunk 里。
    return parsed.para >= locator.paraNo && parsed.para <= (locator.paraEnd ?? locator.paraNo);
  };

  return citations.find(locatorMatches);
}

export function AnswerCard({
  response,
  activeCitation,
  chunks = [],
  onCitation,
  onOpenChecks,
  onCandidate,
}: AnswerCardProps) {
  const mode: AnswerMode =
    response.answer_mode === "hybrid"
      ? "hybrid"
      : response.answer_mode === "free"
        ? "free"
        : "strict";
  // free 不受文档约束：不展示引用与模型知识区块，只呈现答案正文。
  const isFree = mode === "free";
  const modelKnowledge =
    typeof response.model_knowledge === "string" && response.model_knowledge.trim()
      ? response.model_knowledge
      : null;

  if (response.refused) {
    const meta = refusalMeta(response.refusal_reason);
    return (
      <article className="answer-card card refusal-card">
        <header className="answer-head">
          <span className="answer-kicker answer-kicker--refused">拒答</span>
          <ModeChip mode={mode} />
          <span className="chip chip-warn">{meta.label}</span>
          {response.refusal_reason ? (
            <span className="answer-reason mono">{response.refusal_reason}</span>
          ) : null}
        </header>
        <p className="refusal-explanation">{meta.explanation}</p>
        {response.refusal_detail ? (
          <p className="refusal-detail">{response.refusal_detail}</p>
        ) : null}
        <div className="refusal-action">
          <span className="refusal-action-label">下一步</span>
          <span>{meta.action}</span>
        </div>
        {mode === "hybrid" ? (
          <p className="refusal-hybrid-note">
            hybrid 仍可能因 extraction_failed、问题歧义或自检失败拒答；模型知识不会替代文档证据。
          </p>
        ) : null}
        {response.refusal_reason === "ambiguous_question" &&
        response.candidates.length > 0 ? (
          <div className="candidates">
            <p className="candidates-title">可能的意思</p>
            {response.candidates.map((candidate) => (
              <button
                key={candidate.value}
                type="button"
                className="candidate"
                onClick={() => onCandidate(candidate.value)}
              >
                <span className="candidate-interpretation">
                  {candidate.interpretation}
                </span>
                <span className="candidate-value mono">{candidate.value}</span>
                <span className="candidate-cite mono">{candidate.citation}</span>
              </button>
            ))}
          </div>
        ) : null}
        <div className="answer-checks">
          <CheckList verification={response.verification} compact />
        </div>
      </article>
    );
  }

  const answer = response.answer ?? "";
  const hasAnswer = answer.trim().length > 0;
  const segments = hasAnswer ? splitAnswer(answer) : [];
  const failedGates = response.verification.checks.filter(
    (check) => check.severity === "gate" && !check.passed,
  );

  return (
    <article className="answer-card card">
      <header className="answer-head">
        <span className="answer-kicker">答案</span>
        <ModeChip mode={mode} />
        {mode === "hybrid" && response.knowledge_used ? (
          <span className="knowledge-badge">已用模型知识</span>
        ) : null}
        {!isFree ? (
          <span
            className={`chip ${response.verification.gates_passed ? "chip-ok" : "chip-bad"}`}
          >
            {response.verification.gates_passed ? (
              <>
                <CheckIcon size={12} />
                门禁通过
              </>
            ) : (
              <>
                <AlertIcon size={12} />
                {failedGates.length} 项未通过
              </>
            )}
          </span>
        ) : null}
        <span className="answer-meta">
          {formatMs(response.server_elapsed_ms)} · {response.attempts} 次尝试
        </span>
      </header>

      {hasAnswer && isFree ? (
        <div className="answer-text">{answer}</div>
      ) : hasAnswer ? (
        <div className="answer-text">
          {segments.map((segment, index) => {
            if (segment.type !== "citation") {
              return <span key={`text-${index}`}>{segment.value}</span>;
            }
            const citation = findCitation(
              segment.value,
              response.citations,
              chunks,
            );
            return (
              <button
                key={`${segment.value}-${index}`}
                type="button"
                className={`cite-chip${
                  citation?.chunk_id === activeCitation ? " is-active" : ""
                }`}
                onClick={() => {
                  if (citation) onCitation(citation.chunk_id);
                }}
              >
                {segment.value}
              </button>
            );
          })}
        </div>
      ) : modelKnowledge && !isFree ? (
        <div className="answer-doc-empty">
          <InfoIcon size={16} />
          文档中未找到可引用的依据
        </div>
      ) : (
        <div className="answer-empty">
          <AlertIcon size={16} />
          模型没有返回正文。请查看自检与证据，或换个问法重试。
        </div>
      )}

      {!isFree ? (
        response.citations.length > 0 ? (
          <div className="answer-cites" aria-label="引用来源">
            {response.citations.map((citation) => (
              <button
                key={`${citation.chunk_id}-${citation.rendered}`}
                type="button"
                className={`cite-chip${
                  citation.chunk_id === activeCitation ? " is-active" : ""
                }`}
                onClick={() => onCitation(citation.chunk_id)}
                aria-label={`查看引用 ${citation.rendered}`}
              >
                {citation.rendered}
              </button>
            ))}
          </div>
        ) : (
          <p className="answer-no-cites">这次回答没有引用任何片段。</p>
        )
      ) : null}

      {modelKnowledge && !isFree ? (
        <section className="knowledge-block" aria-label="模型知识补充">
          <div className="knowledge-block-head">
            <InfoIcon size={14} />
            <span>模型知识补充</span>
          </div>
          {/* 纯文本：补充段不是证据，不解析引用 token，也不生成可点击 chip。 */}
          <p className="knowledge-text">{modelKnowledge}</p>
        </section>
      ) : null}

      {failedGates.length > 0 ? (
        <div className="answer-failed">
          {failedGates.map((check) => (
            <span key={check.name} className="chip chip-bad">
              {checkLabel(check.name)}
            </span>
          ))}
        </div>
      ) : null}

      {!isFree ? (
        <footer className="answer-foot">
          <span className="chip chip-muted">
            句级支撑度 {response.verification.support_score.toFixed(2)}（咨询性）
          </span>
          <button type="button" className="text-btn answer-checks-btn" onClick={onOpenChecks}>
            查看逐项自检
            <ArrowRightIcon size={13} />
          </button>
        </footer>
      ) : null}
    </article>
  );
}
