import { useId, useRef } from "react";
import type { KeyboardEvent } from "react";
import type { AnswerMode } from "../types";

interface ModeSwitchProps {
  mode: AnswerMode;
  onChange: (mode: AnswerMode) => void;
  hasLlm: boolean;
  busy: boolean;
}

interface ModeOption {
  value: AnswerMode;
  label: string;
  hint: string;
}

const OPTIONS: ModeOption[] = [
  {
    value: "strict",
    label: "仅文档",
    hint: "答案只来自检索到的证据",
  },
  {
    value: "hybrid",
    label: "文档 + 模型知识",
    hint: "文档内容仍带引用；模型知识会单独标注",
  },
  {
    value: "free",
    label: "自由回答",
    hint: "不受文档限制，随意提问",
  },
];

export function ModeSwitch({ mode, onChange, hasLlm, busy }: ModeSwitchProps) {
  const noteId = useId();
  const optionRefs = useRef<Record<AnswerMode, HTMLButtonElement | null>>({
    strict: null,
    hybrid: null,
    free: null,
  });

  const isDisabled = (value: AnswerMode): boolean =>
    busy || (value !== "strict" && !hasLlm);

  const select = (next: AnswerMode) => {
    if (next === mode || isDisabled(next)) return;
    onChange(next);
  };

  // 三个选项的 ARIA radiogroup 用左右方向键循环切换并移动焦点；disabled 的选项跳过。
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const current = OPTIONS.findIndex((option) => option.value === mode);
    for (let step = 1; step <= OPTIONS.length; step += 1) {
      const index =
        (((current + direction * step) % OPTIONS.length) + OPTIONS.length) %
        OPTIONS.length;
      const next = OPTIONS[index].value;
      if (isDisabled(next)) continue;
      onChange(next);
      optionRefs.current[next]?.focus();
      return;
    }
  };

  return (
    <div
      className={`mode-switch${busy ? " is-busy" : ""}`}
      data-mode={mode}
      aria-busy={busy || undefined}
    >
      <div
        className="mode-segment"
        role="radiogroup"
        aria-label="回答范围模式"
        onKeyDown={handleKeyDown}
      >
        <span className="mode-indicator" aria-hidden="true">
          <span key={mode} className="mode-sheen" />
        </span>
        {OPTIONS.map((option) => {
          const selected = mode === option.value;
          const disabled = isDisabled(option.value);
          return (
            <button
              key={option.value}
              ref={(node) => {
                optionRefs.current[option.value] = node;
              }}
              type="button"
              role="radio"
              className="mode-option"
              aria-checked={selected}
              aria-disabled={disabled || undefined}
              aria-describedby={
                option.value !== "strict" && !hasLlm ? noteId : undefined
              }
              tabIndex={selected ? 0 : -1}
              disabled={disabled}
              onClick={() => select(option.value)}
            >
              {option.label}
            </button>
          );
        })}
      </div>
      <p className="mode-hint" aria-live="polite">
        {OPTIONS.map((option) => (
          <span
            key={option.value}
            className={`mode-hint-text${mode === option.value ? " is-active" : ""}`}
            aria-hidden={mode !== option.value}
          >
            {option.hint}
          </span>
        ))}
      </p>
      {!hasLlm ? (
        <p
          className="mode-llm-note"
          id={noteId}
          title="hybrid / free 需要配置 DOCQA_LLM_*"
        >
          未配置生成模型
        </p>
      ) : null}
    </div>
  );
}
