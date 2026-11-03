import { useEffect, useState } from "react";

export interface RouteState {
  path: string;
  navigate: (to: string) => void;
}

export function useRoute(): RouteState {
  const [path, setPath] = useState<string>(() => window.location.pathname);

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = (to: string) => {
    if (window.location.pathname !== to) {
      window.history.pushState({}, "", to);
      setPath(to);
    }
    window.scrollTo({ top: 0, behavior: "auto" });
  };

  return { path, navigate };
}
