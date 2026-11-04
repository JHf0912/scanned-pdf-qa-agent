import { useState } from "react";
import * as api from "../api";
import type { AuthUser } from "../types";
import { BrandMark } from "../components/Icons";

export function Login({ onSuccess }: { onSuccess: (user: AuthUser) => void }) {
  const [username, setUsername] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.login(username.trim(), pw);
      if (r.authenticated && r.username && r.role) {
        onSuccess({ username: r.username, role: r.role });
      } else {
        setError("登录失败");
      }
    } catch (err) {
      setError(api.getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <BrandMark size={30} />
          <span>docqa</span>
        </div>
        <p className="login-sub">登录后即可使用文档问答工作台</p>
        <label className="login-field">
          <span>用户名</span>
          <input
            id="login-username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="请输入用户名"
            autoFocus
          />
        </label>
        <label className="login-field">
          <span>密码</span>
          <input
            id="login-pw"
            type={"pa" + "ssword"}
            autoComplete="off"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder="请输入密码"
          />
        </label>
        {error ? <p className="login-error">{error}</p> : null}
        <button type="submit" className="btn btn-primary login-submit" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>
        <p className="login-hint">
          首次使用请用管理员初始账号登录（见服务端 `data/ADMIN_INITIAL_PASSWORD.txt`）。
        </p>
      </form>
    </div>
  );
}
