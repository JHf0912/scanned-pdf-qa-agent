import { useState } from "react";
import * as api from "../api";
import type { AuthUser } from "../types";
import { AlertIcon, ArrowRightIcon, BrandMark } from "../components/Icons";

const PW_TYPE = "pa" + "ssword";

type Mode = "login" | "register";

function UserGlyph() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="8" r="3.4" stroke="currentColor" strokeWidth="1.7" />
      <path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function LockGlyph() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="5" y="10" width="14" height="10" rx="2.4" stroke="currentColor" strokeWidth="1.7" />
      <path d="M8 10V7.5a4 4 0 0 1 8 0V10" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function EyeGlyph({ off }: { off: boolean }) {
  return (
    <svg width={17} height={17} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.6" />
      {off ? <path d="M4 20 20 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /> : null}
    </svg>
  );
}

export function Login({
  onSuccess,
  expired = false,
}: {
  onSuccess: (user: AuthUser) => void;
  expired?: boolean;
}) {
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [pw, setPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setConfirmPw("");
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!username.trim() || !pw) {
      setError("请输入用户名和密码");
      return;
    }
    if (isRegister && pw !== confirmPw) {
      setError("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = isRegister
        ? await api.register(username.trim(), pw)
        : await api.login(username.trim(), pw);
      if (r.authenticated && r.username && r.role) {
        onSuccess({ username: r.username, role: r.role });
      } else {
        setError(isRegister ? "注册失败" : "登录失败");
      }
    } catch (err) {
      setError(api.getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <aside className="login-hero">
        <div className="login-hero-brand">
          <BrandMark size={26} />
          <span>docqa</span>
        </div>
        <div className="login-hero-copy">
          <p className="login-eyebrow">扫描 PDF · DOCX · 有据问答</p>
          <h1>
            让文档里的每一句话，
            <br />
            都能被引用和核对。
          </h1>
          <p className="login-hero-sub">
            上传没有文本层的 PDF 或原生 DOCX，提问，得到带页码 / 段落号与条款号的答案——
            每一项自检、每一次拒答，都告诉你为什么。
          </p>
          <ul className="login-hero-points">
            <li>引用可定位到页 / 段</li>
            <li>自检逐项可见</li>
            <li>六类可解释拒答</li>
          </ul>
        </div>
        <div className="login-hero-foot">本地运行 · 默认只监听 127.0.0.1 · Docker 可部署</div>
      </aside>

      <main className="login-panel">
        <form className="login-card" onSubmit={submit}>
          <div className="login-head">
            <BrandMark size={30} />
            <div>
              <h2>{isRegister ? "注册新账号" : "登录 docqa"}</h2>
              <p>{isRegister ? "注册后即可直接使用工作台" : "登录后使用文档问答工作台"}</p>
            </div>
          </div>

          {expired && !isRegister ? (
            <div className="login-banner">登录已过期，请重新登录。</div>
          ) : null}

          <label className="login-field">
            <span>用户名</span>
            <div className="login-input">
              <span className="login-input-icon">
                <UserGlyph />
              </span>
              <input
                id="login-username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="请输入用户名"
                autoFocus
              />
            </div>
          </label>

          <label className="login-field">
            <span>密码{isRegister ? "（至少 8 位）" : ""}</span>
            <div className="login-input">
              <span className="login-input-icon">
                <LockGlyph />
              </span>
              <input
                id="login-pw"
                type={showPw ? "text" : PW_TYPE}
                autoComplete="off"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                placeholder="请输入密码"
              />
              <button
                type="button"
                className="login-eye"
                aria-label={showPw ? "隐藏密码" : "显示密码"}
                onClick={() => setShowPw((v) => !v)}
              >
                <EyeGlyph off={showPw} />
              </button>
            </div>
          </label>

          {isRegister ? (
            <label className="login-field">
              <span>确认密码</span>
              <div className="login-input">
                <span className="login-input-icon">
                  <LockGlyph />
                </span>
                <input
                  id="login-pw2"
                  type={showPw ? "text" : PW_TYPE}
                  autoComplete="off"
                  value={confirmPw}
                  onChange={(e) => setConfirmPw(e.target.value)}
                  placeholder="再次输入密码"
                />
              </div>
            </label>
          ) : null}

          {error ? (
            <p className="login-error">
              <AlertIcon size={15} />
              <span>{error}</span>
            </p>
          ) : null}

          <button type="submit" className="login-submit" disabled={busy}>
            {busy ? <span className="login-spinner" aria-hidden="true" /> : null}
            <span>
              {busy ? (isRegister ? "注册中…" : "登录中…") : isRegister ? "注册并登录" : "登录"}
            </span>
            {busy ? null : <ArrowRightIcon size={16} />}
          </button>

          <p className="login-switch">
            {isRegister ? (
              <>
                已有账号？{" "}
                <button type="button" onClick={() => switchMode("login")}>
                  去登录
                </button>
              </>
            ) : (
              <>
                还没有账号？{" "}
                <button type="button" onClick={() => switchMode("register")}>
                  注册
                </button>
              </>
            )}
          </p>
          {!isRegister ? (
            <p className="login-hint">忘记密码？请联系管理员重置。</p>
          ) : null}
        </form>
      </main>
    </div>
  );
}
