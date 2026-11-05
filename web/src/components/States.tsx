import type { ReactNode } from "react";
import { AlertIcon } from "./Icons";

interface EmptyStateProps {
  title: string;
  body?: string;
  icon?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ title, body, icon, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <p className="empty-title">{title}</p>
      {body ? <p className="empty-body">{body}</p> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error-banner" role="alert">
      <AlertIcon size={16} />
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="btn btn-ghost error-retry" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="notice" role="status">
      {children}
    </div>
  );
}

export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <span
          key={index}
          className="skeleton-line"
          style={{ width: `${88 - index * 12}%` }}
        />
      ))}
    </div>
  );
}

export function WorkbenchSkeleton() {
  return (
    <div className="wb-skeleton" aria-label="正在加载工作台">
      <div className="wb-skeleton-top" />
      <div className="wb-skeleton-body">
        <div className="wb-skeleton-col" />
        <div className="wb-skeleton-col wb-skeleton-main">
          <SkeletonLines lines={5} />
          <SkeletonLines lines={3} />
        </div>
        <div className="wb-skeleton-col" />
      </div>
    </div>
  );
}
