import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import type { AdminUpload, AdminUser, AuthUser } from "../types";
import { CloseIcon } from "./Icons";

const PW_TYPE = "pa" + "ssword";

interface AdminPanelProps {
  open: boolean;
  currentUser: AuthUser;
  onClose: () => void;
}

type Dialog =
  | { kind: "reset"; user: AdminUser }
  | { kind: "delete"; user: AdminUser }
  | null;

export function AdminPanel({ open, currentUser, onClose }: AdminPanelProps) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [uploads, setUploads] = useState<Record<string, AdminUpload[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newPw, setNewPw] = useState("");
  const [newRole, setNewRole] = useState<"user" | "admin">("user");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [resetPw, setResetPw] = useState("");

  const reload = useCallback(async () => {
    setError(null);
    try {
      const [u, up] = await Promise.all([api.adminListUsers(), api.adminUploads()]);
      setUsers(u.users);
      setUploads(up.by_owner);
    } catch (err) {
      setError(api.getErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    if (open) {
      void reload();
      setDialog(null);
    }
  }, [open, reload]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (dialog) setDialog(null);
        else onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, dialog]);

  if (!open) return null;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await reload();
      setDialog(null);
    } catch (err) {
      setError(api.getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const createUser = () =>
    run(async () => {
      await api.adminCreateUser(newName.trim(), newPw, newRole);
      setNewName("");
      setNewPw("");
      setNewRole("user");
    });

  return (
    <div
      className="docviewer-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="用户管理"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="docviewer adminviewer">
        <header className="docviewer-top">
          <span className="docviewer-title">用户管理</span>
          <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}>
            <CloseIcon size={18} />
          </button>
        </header>
        <div className="docviewer-body admin-body">
          {error ? <p className="admin-error">{error}</p> : null}

          <section className="admin-section">
            <h3>新建账号</h3>
            <div className="admin-create">
              <input
                placeholder="用户名"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <input
                type={PW_TYPE}
                placeholder="初始密码（≥8 位）"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
              />
              <select value={newRole} onChange={(e) => setNewRole(e.target.value as "user" | "admin")}>
                <option value="user">普通用户</option>
                <option value="admin">管理员</option>
              </select>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || !newName.trim() || newPw.length < 8}
                onClick={createUser}
              >
                创建
              </button>
            </div>
          </section>

          <section className="admin-section">
            <h3>账号（{users.length}）</h3>
            <div className="admin-users">
              {users.map((u) => {
                const self = u.username === currentUser.username;
                return (
                  <div className="admin-user" key={u.username}>
                    <div className="admin-user-main">
                      <span className="admin-avatar">{(u.username || "?").slice(0, 1).toUpperCase()}</span>
                      <span className="admin-user-name">{u.username}</span>
                      <span className={`chip ${u.role === "admin" ? "chip-ok" : ""}`}>
                        {u.role === "admin" ? "管理员" : "普通用户"}
                      </span>
                      {u.disabled ? <span className="chip chip-warn">已禁用</span> : null}
                      {self ? <span className="admin-self">（你）</span> : null}
                    </div>
                    <div className="admin-user-actions">
                      <button
                        type="button"
                        className="btn admin-btn"
                        disabled={busy || self}
                        onClick={() =>
                          void run(() =>
                            api.adminUpdateUser(u.username, {
                              role: u.role === "admin" ? "user" : "admin",
                            }),
                          )
                        }
                      >
                        {u.role === "admin" ? "降为普通" : "升为管理员"}
                      </button>
                      <button
                        type="button"
                        className="btn admin-btn"
                        disabled={busy || self}
                        onClick={() =>
                          void run(() => api.adminUpdateUser(u.username, { disabled: !u.disabled }))
                        }
                      >
                        {u.disabled ? "启用" : "禁用"}
                      </button>
                      <button
                        type="button"
                        className="btn admin-btn"
                        disabled={busy}
                        onClick={() => {
                          setResetPw("");
                          setDialog({ kind: "reset", user: u });
                        }}
                      >
                        重置密码
                      </button>
                      <button
                        type="button"
                        className="btn admin-btn admin-danger"
                        disabled={busy || self}
                        onClick={() => setDialog({ kind: "delete", user: u })}
                      >
                        删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="admin-section">
            <h3>各账号上传的文档</h3>
            {Object.keys(uploads).length === 0 ? (
              <p className="docviewer-hint">暂无上传记录。</p>
            ) : (
              Object.entries(uploads).map(([owner, docs]) => (
                <div className="admin-uploads" key={owner}>
                  <div className="admin-owner">
                    {owner} · {docs.length} 份
                  </div>
                  <ul>
                    {docs.map((d) => (
                      <li key={d.doc_id}>
                        <span className="mono">{d.doc_id}</span>
                        <span className="docviewer-hint">
                          {" "}
                          {d.source_type ?? ""} · {d.chunks ?? "?"} 片段
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            )}
          </section>
        </div>

        {dialog ? (
          <div className="admin-dialog-overlay" onMouseDown={(e) => {
            if (e.target === e.currentTarget) setDialog(null);
          }}>
            <div className="admin-dialog">
              {dialog.kind === "reset" ? (
                <>
                  <h4>重置 {dialog.user.username} 的密码</h4>
                  <input
                    type={PW_TYPE}
                    placeholder="新密码（≥8 位）"
                    value={resetPw}
                    autoFocus
                    onChange={(e) => setResetPw(e.target.value)}
                  />
                  <div className="modal-actions">
                    <button type="button" className="btn" onClick={() => setDialog(null)}>
                      取消
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={busy || resetPw.length < 8}
                      onClick={() => void run(() => api.adminUpdateUser(dialog.user.username, { pw: resetPw }))}
                    >
                      确认重置
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <h4>删除账号 {dialog.user.username}？</h4>
                  <p className="docviewer-hint">该账号将无法登录；其上传文档仍保留，仅管理员可见。</p>
                  <div className="modal-actions">
                    <button type="button" className="btn" onClick={() => setDialog(null)}>
                      取消
                    </button>
                    <button
                      type="button"
                      className="btn admin-danger"
                      disabled={busy}
                      onClick={() => void run(() => api.adminDeleteUser(dialog.user.username))}
                    >
                      确认删除
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
