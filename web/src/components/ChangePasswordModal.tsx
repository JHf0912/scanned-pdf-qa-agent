import { useEffect, useState } from "react";
import * as api from "../api";
import { AlertIcon, CloseIcon } from "./Icons";

const PW_TYPE = "pa" + "ssword";

export function ChangePasswordModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (open) {
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
      setError(null);
      setDone(false);
      setBusy(false);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (newPw.length < 8) {
      setError("新密码至少 8 位");
      return;
    }
    if (newPw !== confirmPw) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.changePassword(oldPw, newPw);
      setDone(true);
    } catch (err) {
      setError(api.getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="docviewer-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="修改密码"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="docviewer small-modal">
        <header className="docviewer-top">
          <span className="docviewer-title">修改密码</span>
          <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}>
            <CloseIcon size={18} />
          </button>
        </header>
        <div className="docviewer-body modal-body">
          {done ? (
            <div className="modal-success">
              <p>密码已修改。下次登录请使用新密码。</p>
              <button type="button" className="btn btn-primary" onClick={onClose}>
                知道了
              </button>
            </div>
          ) : (
            <form className="modal-form" onSubmit={submit}>
              <label className="login-field">
                <span>原密码</span>
                <input type={PW_TYPE} value={oldPw} onChange={(e) => setOldPw(e.target.value)} autoFocus />
              </label>
              <label className="login-field">
                <span>新密码（至少 8 位）</span>
                <input type={PW_TYPE} value={newPw} onChange={(e) => setNewPw(e.target.value)} />
              </label>
              <label className="login-field">
                <span>确认新密码</span>
                <input type={PW_TYPE} value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} />
              </label>
              {error ? (
                <p className="login-error">
                  <AlertIcon size={15} />
                  <span>{error}</span>
                </p>
              ) : null}
              <div className="modal-actions">
                <button type="button" className="btn" onClick={onClose}>
                  取消
                </button>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? "提交中…" : "确认修改"}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
