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

  if (auth === undefined) {
    return (
      <div className="login-page">
        <div className="login-card">
          <p className="login-sub">正在确认登录状态…</p>
        </div>
      </div>
    );
  }

  if (auth === null) {
    return <Login onSuccess={setAuth} />;
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
            setAuth(null);
            navigate("/");
          }
        }}
      />
    );
  }

  return <Landing onOpenWorkbench={() => navigate("/app")} />;
}
