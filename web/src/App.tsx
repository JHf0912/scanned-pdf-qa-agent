import { Landing } from "./pages/Landing";
import { Workbench } from "./pages/Workbench";
import { useRoute } from "./router";

export function App() {
  const { path, navigate } = useRoute();
  const isWorkbench = path === "/app" || path.startsWith("/app/");

  if (isWorkbench) {
    return <Workbench onNavigateHome={() => navigate("/")} />;
  }

  return <Landing onOpenWorkbench={() => navigate("/app")} />;
}
