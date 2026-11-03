import type { Status } from "../types";
import { docTitle, isDocxSource } from "../format";

interface OverviewProps {
  status: Status | null;
}

const UNSUPPORTED_LABELS: Record<string, string> = {
  footnotes: "脚注",
  endnotes: "尾注",
  text_boxes: "文本框",
  equations: "公式",
  comments: "批注",
  headers_footers: "页眉页脚",
  hidden_text: "隐藏文字",
  tracked_deletions: "修订删除",
  embedded_objects: "嵌入对象",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="overview-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function unsupportedChips(status: Status): string | null {
  const entries = Object.entries(status.unsupported ?? {}).filter(
    ([, count]) => typeof count === "number" && count > 0,
  );
  if (entries.length === 0) return null;
  return entries
    .map(([key, count]) => `${UNSUPPORTED_LABELS[key] ?? key} ${count}`)
    .join(" · ");
}

export function Overview({ status }: OverviewProps) {
  if (!status || !status.ready) {
    return (
      <div className="overview">
        <p className="overview-empty">尚未就绪。</p>
      </div>
    );
  }

  const failed = status.failed_pages ?? [];
  const isDocx = isDocxSource(status.source_type, status.unit_count);
  const source = isDocx ? "DOCX" : status.source_type === "pdf" ? "PDF" : null;
  const unsupported = unsupportedChips(status);
  const unitCount =
    status.unit_count !== null && status.unit_count !== undefined
      ? `${status.unit_count} 段`
      : "—";

  return (
    <div className="overview">
      <dl>
        <Row label="文档" value={status.doc_id ? docTitle(status.doc_id) : "—"} />
        <Row label="来源" value={source ?? "—"} />
        <Row label="类型" value={status.kind ?? "—"} />
        {isDocx ? (
          <Row label="正文块" value={unitCount} />
        ) : (
          <Row label="页数" value={String(status.pages ?? "—")} />
        )}
        <Row
          label="片段"
          value={`${status.chunks ?? "—"}${status.tables ? `（表格 ${status.tables}）` : ""}`}
        />
        <Row label="引用模式" value={status.citation_mode ?? "—"} />
        <Row
          label="OCR 引擎"
          value={isDocx ? "不适用（原生文本）" : (status.engine ?? "—")}
        />
        <Row
          label="生成模型"
          value={status.has_llm ? "已配置" : "未配置（抽取式）"}
        />
      </dl>
      <div className="overview-chips">
        {isDocx ? (
          <span className="chip chip-ok">原生文本 · 未调用 OCR</span>
        ) : failed.length > 0 ? (
          <span className="chip chip-bad">识别失败页：{failed.join(", ")}</span>
        ) : (
          <span className="chip chip-ok">全部页面识别成功</span>
        )}
        {unsupported ? (
          <span className="chip chip-warn chip-wrap">未支持构造：{unsupported}</span>
        ) : null}
        {status.selected && status.selected.length > 1 ? (
          <span className="chip chip-cyan">跨 {status.selected.length} 份文档</span>
        ) : null}
      </div>
    </div>
  );
}
