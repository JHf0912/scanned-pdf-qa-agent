import { useEffect, useState } from "react";
import * as api from "../api";
import type { DocumentInfo } from "../types";
import { CloseIcon, DocIcon } from "./Icons";

interface DocViewerProps {
  open: boolean;
  docId: string;
  docTitle: string;
  /** 打开时定位到的页码（PDF 阅读器用 `#page=`）。 */
  page?: number | null;
  onClose: () => void;
}

/**
 * 原文浏览：PDF 用内嵌原生阅读器（可滚动/缩放/跳页），
 * DOCX 无 PDF 就按段落列出解析文本，其余情况退化为逐页页图。
 */
export function DocViewer({ open, docId, docTitle, page, onClose }: DocViewerProps) {
  const [doc, setDoc] = useState<DocumentInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDoc(null);
    setError(null);
    void api
      .getDocument(docId)
      .then((next) => {
        if (!cancelled) setDoc(next);
      })
      .catch((err) => {
        if (!cancelled) setError(api.getErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [open, docId]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const pdfUrl = doc?.pdf_url ?? null;
  const frameSrc = pdfUrl
    ? `${pdfUrl}${page ? `#page=${page}&zoom=page-width` : "#zoom=page-width"}`
    : null;

  return (
    <div
      className="docviewer-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="浏览原文"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="docviewer">
        <header className="docviewer-top">
          <span className="docviewer-title">
            <DocIcon size={16} />
            <span className="docviewer-name">{docTitle}</span>
            {doc ? (
              <span className="docviewer-sub">
                {doc.source_type === "docx" ? "DOCX" : `${doc.page_count} 页`}
              </span>
            ) : null}
          </span>
          <div className="docviewer-actions">
            {pdfUrl ? (
              <a className="btn docviewer-open" href={pdfUrl} target="_blank" rel="noreferrer">
                新标签打开
              </a>
            ) : null}
            <button type="button" className="icon-btn" aria-label="关闭原文" onClick={onClose}>
              <CloseIcon size={18} />
            </button>
          </div>
        </header>
        <div className="docviewer-body">
          {error ? (
            <p className="docviewer-msg">{error}</p>
          ) : !doc ? (
            <p className="docviewer-msg">加载中…</p>
          ) : frameSrc ? (
            <iframe className="docviewer-frame" src={frameSrc} title={`${docTitle} 原文`} />
          ) : doc.units.length > 0 ? (
            <div className="docviewer-text">
              <p className="docviewer-hint">该文档为原生 DOCX，无 PDF 页；以下为解析出的段落文本。</p>
              {doc.units.map((unit, index) => (
                <div className="docviewer-unit" key={index}>
                  <span className="docviewer-loc mono">{unit.locator}</span>
                  <p>{unit.text}</p>
                </div>
              ))}
            </div>
          ) : doc.pages.length > 0 ? (
            <div className="docviewer-text">
              <p className="docviewer-hint">该文档没有可浏览的 PDF；下面按页显示页图。</p>
              <div className="docviewer-pages">
                {doc.pages.map((item) => (
                  <figure key={item.page_no}>
                    <img src={item.url} alt={`第 ${item.page_no} 页`} loading="lazy" />
                    <figcaption>第 {item.page_no} 页</figcaption>
                  </figure>
                ))}
              </div>
            </div>
          ) : (
            <p className="docviewer-msg">该文档没有可浏览的内容。</p>
          )}
        </div>
      </div>
    </div>
  );
}
