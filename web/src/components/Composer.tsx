import type { RefObject } from "react";
import type { AnswerMode } from "../types";
import { ModeSwitch } from "./ModeSwitch";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  disabled: boolean;
  busy: boolean;
  mode: AnswerMode;
  onModeChange: (mode: AnswerMode) => void;
  hasLlm: boolean;
  inputRef: RefObject<HTMLInputElement>;
}

export function Composer({
  value,
  onChange,
  onSubmit,
  disabled,
  busy,
  mode,
  onModeChange,
  hasLlm,
  inputRef,
}: ComposerProps) {
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="composer-modes">
        <ModeSwitch
          mode={mode}
          onChange={onModeChange}
          hasLlm={hasLlm}
          busy={busy}
        />
      </div>
      <label className="sr-only" htmlFor="wb-question">
        向当前文档提问
      </label>
      <div className="composer-inner">
        <input
          id="wb-question"
          ref={inputRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={disabled ? "先上传或选择一份文档…" : "就这份文档提问…"}
          autoComplete="off"
          disabled={disabled || busy}
        />
        <button
          type="submit"
          className="composer-send"
          disabled={disabled || busy || !value.trim()}
          aria-label="发送问题"
        >
          {busy ? <span className="spinner" aria-hidden="true" /> : "↑"}
        </button>
      </div>
      <p className="composer-hint">
        <span className="mono">⌘K</span> / <span className="mono">Ctrl K</span> 聚焦 ·
        Enter 发送
      </p>
    </form>
  );
}
