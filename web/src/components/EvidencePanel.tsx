import { useEffect, useMemo, useRef } from "react";
import type { BBox, Chunk, Citation, EvidenceText, RetrievalHit } from "../types";
import { formatLocator, formatScore, highlightQuote } from "../format";
import { EmptyState } from "./States";

/** 引用在页图上的高亮覆盖层。viewBox 用 0~1，配合 preserveAspectRatio="none"
 *  让比例坐标直接铺满图片，与缩放/响应式无关。 */
function BBoxLayer({ bboxes }: { bboxes: BBox[] }) {
  return (
    <svg
      className="evidence-bbox-layer"
      viewBox="0 0 1 1"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {bboxes.map((b, i) => (
        <rect
          key={i}
          x={b.x0}
          y={b.y0}
          width={Math.max(0.002, b.x1 - b.x0)}
          height={Math.max(0.002, b.y1 - b.y0)}
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  );
}

function PageSnapshot({
  src,
  alt,
  bboxes,
  onOpen,
}: {
  src: string;
  alt: string;
  bboxes: BBox[];
  onOpen?: () => void;
}) {
  return (
    <span
      className={`evidence-page${onOpen ? " is-openable" : ""}`}
      onClick={
        onOpen
          ? (event) => {
              event.stopPropagation();
              onOpen();
            }
          : undefined
      }
    >
      <span className="evidence-page-inner">
        <img src={src} alt={alt} loading="lazy" decoding="async" />
        <BBoxLayer bboxes={bboxes} />
      </span>
      <span className="evidence-page-cap">
        页图 · 引用位置已高亮{onOpen ? " · 点击查看原文档" : ""}
      </span>
    </span>
  );
}

interface EvidencePanelProps {
  chunks: Chunk[];
  hits: RetrievalHit[];
  citations: Citation[];
  evidence: Record<string, EvidenceText>;
  activeChunkId: string | null;
  onSelect: (chunkId: string) => void;
  /** 打开原文并定位到某页（可选）。 */
  onOpenPage?: (pageNo: number) => void;
}

interface EvidenceItem {
  chunk: Chunk;
  hit?: RetrievalHit;
  citation?: Citation;
}

function buildItems(
  chunks: Chunk[],
  hits: RetrievalHit[],
  citations: Citation[],
  evidence: Record<string, EvidenceText>,
): EvidenceItem[] {
  const byId = new Map(chunks.map((chunk) => [chunk.chunk_id, chunk]));
  const seen = new Set<string>();
  const items: EvidenceItem[] = [];

  const ensureChunk = (chunkId: string): Chunk | null => {
    const existing = byId.get(chunkId);
    if (existing) return existing;
    const fallback = evidence[chunkId];
    if (!fallback) return null;
    return {
      chunk_id: chunkId,
      page_no: fallback.page_no,
      kind: "",
      clause_label: fallback.clause_label,
      clause_path: [],
      text: fallback.text,
      locator_kind: fallback.locator_kind ?? "page",
      section: fallback.section ?? fallback.clause_label ?? null,
      para_no: fallback.para_no ?? null,
      para_end: fallback.para_end ?? null,
    };
  };

  for (const hit of hits) {
    if (seen.has(hit.chunk_id)) continue;
    const chunk = ensureChunk(hit.chunk_id);
    if (!chunk) continue;
    seen.add(hit.chunk_id);
    items.push({
      chunk,
      hit,
      citation: citations.find((item) => item.chunk_id === hit.chunk_id),
    });
  }

  for (const citation of citations) {
    if (seen.has(citation.chunk_id)) continue;
    const chunk = ensureChunk(citation.chunk_id);
    if (!chunk) continue;
    seen.add(citation.chunk_id);
    items.push({ chunk, citation });
  }

  return items;
}

export function EvidencePanel({
  chunks,
  hits,
  citations,
  evidence,
  activeChunkId,
  onSelect,
  onOpenPage,
}: EvidencePanelProps) {
  const items = useMemo(
    () => buildItems(chunks, hits, citations, evidence),
    [chunks, hits, citations, evidence],
  );
  const refs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    if (!activeChunkId) return;
    const node = refs.current.get(activeChunkId);
    if (!node) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  }, [activeChunkId]);

  if (items.length === 0) {
    return (
      <EmptyState
        title="还没有命中的证据"
        body="提问后，检索到的片段、位置（页码或段落号）、条款号与融合分都会显示在这里。"
      />
    );
  }

  return (
    <div className="evidence-list">
      {items.map(({ chunk, hit, citation }) => {
        const active = activeChunkId === chunk.chunk_id;
        const ev = evidence[chunk.chunk_id];
        const pageImage = ev?.page_image ?? null;
        const bboxes = ev?.bboxes ?? [];
        const segments = citation
          ? highlightQuote(chunk.text, citation.quote)
          : [{ text: chunk.text, match: false }];
        const locator = formatLocator(
          {
            page_no: chunk.page_no,
            locator_kind: chunk.locator_kind ?? citation?.locator_kind,
            section: chunk.section ?? citation?.section,
            clause_label: chunk.clause_label,
            para_no: chunk.para_no ?? citation?.para_no,
            para_end: chunk.para_end ?? citation?.para_end,
          },
          chunk.chunk_id,
        );
        return (
          <button
            key={chunk.chunk_id}
            type="button"
            ref={(node) => {
              if (node) refs.current.set(chunk.chunk_id, node);
              else refs.current.delete(chunk.chunk_id);
            }}
            className={`evidence-item${active ? " is-active" : ""}`}
            aria-pressed={active}
            onClick={() => onSelect(chunk.chunk_id)}
          >
            <span className="evidence-meta">
              <span className="mono">{locator}</span>
              {hit ? (
                <span className="evidence-score">
                  {hit.arm} · {formatScore(hit.score)}
                </span>
              ) : (
                <span className="evidence-score">被引用</span>
              )}
            </span>
            {pageImage && bboxes.length > 0 ? (
              <PageSnapshot
                src={pageImage.url}
                alt={`第 ${chunk.page_no ?? ""} 页`}
                bboxes={bboxes}
                onOpen={
                  onOpenPage && chunk.page_no != null
                    ? () => onOpenPage(chunk.page_no as number)
                    : undefined
                }
              />
            ) : null}
            <span className="evidence-text">
              {segments.map((segment, index) =>
                segment.match ? (
                  <mark key={index}>{segment.text}</mark>
                ) : (
                  <span key={index}>{segment.text}</span>
                ),
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}
