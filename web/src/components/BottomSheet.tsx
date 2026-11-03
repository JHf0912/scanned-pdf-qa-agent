import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { CloseIcon } from "./Icons";

interface BottomSheetProps {
  open: boolean;
  title: string;
  onClose: () => void;
  variant?: "sheet" | "drawer";
  children: ReactNode;
}

export function BottomSheet({
  open,
  title,
  onClose,
  variant = "sheet",
  children,
}: BottomSheetProps) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className={`sheet-backdrop sheet-backdrop--${variant}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className={`sheet sheet--${variant}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header className="sheet-head">
          <h2 id={titleId}>{title}</h2>
          <button
            ref={closeRef}
            type="button"
            className="icon-btn"
            aria-label="关闭"
            onClick={onClose}
          >
            <CloseIcon size={16} />
          </button>
        </header>
        <div className="sheet-body">{children}</div>
      </section>
    </div>
  );
}
