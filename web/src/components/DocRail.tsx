import type { DocInfo } from "../types";
import { docTitle, isDocxSource } from "../format";
import { DocIcon, RefreshIcon, UploadIcon } from "./Icons";
import { EmptyState } from "./States";

interface DocRailProps {
  docs: DocInfo[];
  selected: string[];
  currentDocIds: string[];
  busy: boolean;
  uploading: boolean;
  uploadError: string | null;
  onToggle: (docId: string) => void;
  onClear: () => void;
  onRefresh: () => void;
  onUpload: (file: File) => void;
}

function docMeta(doc: DocInfo): string {
  const isDocx = isDocxSource(doc.source_type, doc.unit_count);
  const source = isDocx ? "DOCX" : doc.source_type === "pdf" ? "PDF" : null;
  const type = isDocx ? "原生文本" : doc.kind;

  let size: string | null = null;
  if (isDocx) {
    if (doc.unit_count !== null && doc.unit_count !== undefined) {
      size = `${doc.unit_count} 段`;
    }
  } else if (doc.pages) {
    size = `${doc.pages} 页`;
  }

  return [
    source,
    type,
    isDocx ? null : doc.engine,
    size,
    doc.chunks ? `${doc.chunks} 片段` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function DocRail({
  docs,
  selected,
  currentDocIds,
  busy,
  uploading,
  uploadError,
  onToggle,
  onClear,
  onRefresh,
  onUpload,
}: DocRailProps) {
  const handleFile = (file: File | undefined) => {
    if (file) onUpload(file);
  };

  return (
    <div className="doc-rail">
      <div className="doc-rail-head">
        <div>
          <span className="rail-kicker">文档</span>
          <span className="rail-count">{docs.length}</span>
        </div>
        <button
          type="button"
          className="icon-btn"
          aria-label="重新扫描产物"
          title="重新扫描 data/derived 下的产物"
          onClick={onRefresh}
        >
          <RefreshIcon size={15} />
        </button>
      </div>

      {docs.length === 0 ? (
        <EmptyState
          title="还没有可用产物"
          body="先上传一份 PDF / DOCX，或在终端运行 docqa ingest。"
          icon={<DocIcon size={18} />}
        />
      ) : (
        <>
          <p className="doc-rail-hint">点选切换；多选即跨文档检索。</p>
          <ul className="doc-list">
            {docs.map((doc) => {
              const isOn = currentDocIds.includes(doc.doc_id);
              const hasFailed =
                !isDocxSource(doc.source_type, doc.unit_count) &&
                (doc.failed_pages ?? []).length > 0;
              return (
                <li key={doc.doc_id}>
                  <button
                    type="button"
                    className={`doc-item${isOn ? " is-on" : ""}`}
                    aria-pressed={isOn}
                    disabled={busy}
                    onClick={() => onToggle(doc.doc_id)}
                  >
                    <span className="doc-check" aria-hidden="true">
                      {isOn ? "✓" : ""}
                    </span>
                    <span className="doc-main">
                      <span className="doc-id">{docTitle(doc.doc_id)}</span>
                      <span className="doc-meta">{docMeta(doc) || "—"}</span>
                      {hasFailed ? (
                        <span className="doc-badge">
                          失败页 {doc.failed_pages.join(", ")}
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          {selected.length > 0 ? (
            <button type="button" className="text-btn" onClick={onClear} disabled={busy}>
              清除多选，回到最新产物
            </button>
          ) : null}
        </>
      )}

      <label
        className={`dropzone${uploading ? " is-busy" : ""}`}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          handleFile(event.dataTransfer.files[0]);
        }}
      >
        <input
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          hidden
          disabled={uploading}
          onChange={(event) => handleFile(event.target.files?.[0])}
        />
        <UploadIcon size={18} />
        <span>{uploading ? "上传并解析中…" : "拖入或点击上传 PDF / DOCX"}</span>
        <small>{uploading ? "解析完成前请勿关闭页面" : "上传后会当场解析"}</small>
        {uploading ? <span className="upload-bar" aria-hidden="true" /> : null}
      </label>
      {uploadError ? <p className="rail-error">{uploadError}</p> : null}
    </div>
  );
}
