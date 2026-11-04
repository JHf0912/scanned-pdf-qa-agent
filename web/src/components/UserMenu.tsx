import { useEffect, useRef, useState } from "react";
import type { AuthUser } from "../types";

interface UserMenuProps {
  user: AuthUser;
  onChangePassword: () => void;
  onLogout: () => void;
}

export function UserMenu({ user, onChangePassword, onLogout }: UserMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = (user.username || "?").slice(0, 1).toUpperCase();

  return (
    <div className="usermenu" ref={ref}>
      <button
        type="button"
        className="usermenu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="usermenu-avatar">{initial}</span>
        <span className="usermenu-name">{user.username}</span>
        <span className="usermenu-caret" aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="usermenu-pop" role="menu">
          <div className="usermenu-head">
            <span className="usermenu-avatar lg">{initial}</span>
            <div>
              <div className="usermenu-head-name">{user.username}</div>
              <div className="usermenu-head-role">
                {user.role === "admin" ? "管理员" : "普通用户"}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="usermenu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onChangePassword();
            }}
          >
            修改密码
          </button>
          <button
            type="button"
            className="usermenu-item danger"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
          >
            退出登录
          </button>
        </div>
      ) : null}
    </div>
  );
}
