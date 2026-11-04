import { useEffect, useState } from "react";
import * as api from "./api";
import { Landing } from "./pages/Landing";
import { Login } from "./pages/Login";
import { Workbench } from "./pages/Workbench";
import { useRoute } from "./router";
import type { AuthUser } from "./types";

export function App() {
  const { path, navigate } = useRoute();
  // undefined = 正在确认登录态；null = 未登录；AuthUser = 已登录
  const [auth, setAuth] = useState<AuthUser | null | undefined>(undefined);
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getMe()
      .then((r) => {
        if (cancelled) return;
        setAuth(
          r.authenticated && r.username && r.role
            ? { username: r.username, role: r.role }
            : null,
        );
      })
      .catch(() => {
        if (!cancelled) setAuth(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 任何接口返回 401（会话过期）都退回登录页并提示。
  useEffect(() => {
    const onUnauthorized = () => {
      setExpired(true);
      setAuth(null);
    };
    window.addEventListener("docqa:unauthorized", onUnauthorized);
    return () => window.removeEventListener("docqa:unauthorized", onUnauthorized);
  }, []);

  if (auth === undefined) {
    return (
      <div className="login-shell login-shell-loading">
        <div className="login-spinner" aria-hidden="true" />
        <p>正在确认登录状态…</p>
      </div>
    );
  }

  if (auth === null) {
    return (
      <Login
        expired={expired}
        onSuccess={(u) => {
          setExpired(false);
          setAuth(u);
        }}
      />
    );
  }

  const isWorkbench = path === "/app" || path.startsWith("/app/");
  if (isWorkbench) {
    return (
      <Workbench
        user={auth}
        onNavigateHome={() => navigate("/")}
        onLogout={async () => {
          try {
            await api.logout();
          } finally {
            setExpired(false);
            setAuth(null);
            navigate("/");
          }
        }}
      />
    );
  }

  return <Landing onOpenWorkbench={() => navigate("/app")} />;
}
