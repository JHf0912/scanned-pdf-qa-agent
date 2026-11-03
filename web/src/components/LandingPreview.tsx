import { BrandMark, CheckIcon, DocIcon } from "./Icons";

export function LandingPreview() {
  return (
    <div className="preview" aria-hidden="true">
      <div className="preview-frame">
        <div className="preview-top">
          <span className="preview-brand">
            <BrandMark size={17} />
            docqa
          </span>
          <span className="preview-doc">扫描件 A · 4 页 · fixture</span>
          <span className="preview-ready">
            <span className="preview-dot" />
            就绪
          </span>
        </div>
        <div className="preview-grid">
          <div className="preview-col preview-docs">
            <span className="preview-label">文档</span>
            <div className="preview-doc-row is-on">
              <DocIcon size={14} />
              <span>扫描件 A</span>
            </div>
            <div className="preview-doc-row">
              <DocIcon size={14} />
              <span>规格 B · DOCX</span>
            </div>
            <div className="preview-upload">拖入 PDF / DOCX 上传</div>
          </div>

          <div className="preview-col preview-chat">
            <span className="preview-label">提问</span>
            <div className="preview-q">表 1 中半圆键的键宽 b 的 AQL 是多少？</div>
            <div className="preview-a">
              <p>
                键宽 b 的 AQL 为 <strong>1.0</strong>
                <span className="preview-cite">[p.3 §3.6]</span>。
              </p>
              <div className="preview-cite-row">
                <span className="preview-cite">[p.3 §3.6]</span>
                <span className="preview-cite">[§2.1 ¶4]</span>
              </div>
            </div>
            <div className="preview-composer">
              <span>就这份文档提问…</span>
              <span className="preview-send">↑</span>
            </div>
          </div>

          <div className="preview-col preview-side">
            <span className="preview-label">自检</span>
            <div className="preview-check">
              <CheckIcon size={13} />
              引用有效性
            </div>
            <div className="preview-check">
              <CheckIcon size={13} />
              数值落地
            </div>
            <div className="preview-check">
              <CheckIcon size={13} />
              条号一致性
            </div>
            <span className="preview-label preview-label-gap">证据</span>
            <div className="preview-evidence">
              第 3 页 · §3.6
              <span>键宽 b 的 AQL 按表 1 取值…</span>
            </div>
          </div>
        </div>
      </div>
      <span className="preview-caption">界面示意 · 实际内容来自你的文档</span>
    </div>
  );
}
