import type { CheckResult, Verification } from "../types";
import { checkLabel } from "../format";
import { AlertIcon, CheckIcon } from "./Icons";
import { EmptyState } from "./States";

interface CheckListProps {
  verification?: Verification | null;
  compact?: boolean;
}

function CheckRow({ check }: { check: CheckResult }) {
  const Icon = check.passed ? CheckIcon : AlertIcon;
  const state = check.passed ? "通过" : "未通过";
  return (
    <li className={`check-row ${check.passed ? "is-ok" : "is-bad"}`}>
      <span className="check-icon" aria-hidden="true">
        <Icon size={14} />
      </span>
      <div className="check-main">
        <div className="check-head">
          <span className="check-name">{checkLabel(check.name)}</span>
          <span className={`chip ${check.passed ? "chip-ok" : "chip-bad"}`}>
            {state}
          </span>
          {check.severity === "advisory" ? (
            <span className="chip chip-muted">咨询性</span>
          ) : null}
        </div>
        <p className="check-detail">{check.detail}</p>
        {check.offenders.length > 0 ? (
          <details className="check-offenders">
            <summary>查看 {check.offenders.length} 个问题项</summary>
            <ul>
              {check.offenders.map((offender) => (
                <li key={offender} className="mono">
                  {offender}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </li>
  );
}

export function CheckList({ verification, compact = false }: CheckListProps) {
  if (!verification || verification.checks.length === 0) {
    return (
      <EmptyState
        title="还没有自检结果"
        body="提问后，这里会逐项显示引用、数值与条号的检查结果。"
      />
    );
  }

  const gates = verification.checks.filter((check) => check.severity === "gate");
  const failed = gates.filter((check) => !check.passed);
  const shown = compact ? gates : verification.checks;

  return (
    <div className="check-list">
      <div className="check-overall">
        <span className={`chip ${verification.gates_passed ? "chip-ok" : "chip-bad"}`}>
          {verification.gates_passed
            ? "全部门禁通过"
            : `${failed.length} 项门禁未通过`}
        </span>
        <span className="chip chip-muted">
          支撑度 {verification.support_score.toFixed(2)}（咨询）
        </span>
      </div>
      <ul className="check-rows">
        {shown.map((check) => (
          <CheckRow key={`${check.name}-${check.passed}`} check={check} />
        ))}
      </ul>
    </div>
  );
}
