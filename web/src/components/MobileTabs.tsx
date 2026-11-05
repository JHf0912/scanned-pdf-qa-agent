import type { WorkbenchPanel } from "../types";
import { ChatIcon, DocIcon, SearchIcon } from "./Icons";

interface MobileTabsProps {
  panel: WorkbenchPanel;
  onChange: (panel: WorkbenchPanel) => void;
}

const TABS: Array<{
  id: WorkbenchPanel;
  label: string;
  icon: typeof ChatIcon;
}> = [
  { id: "chat", label: "对话", icon: ChatIcon },
  { id: "evidence", label: "证据", icon: SearchIcon },
  { id: "docs", label: "文档", icon: DocIcon },
];

export function MobileTabs({ panel, onChange }: MobileTabsProps) {
  return (
    <nav className="mobile-tabs" aria-label="工作台面板">
      {TABS.map(({ id, label, icon: Icon }) => {
        const active = panel === id;
        return (
          <button
            key={id}
            type="button"
            className={`mobile-tab${active ? " is-on" : ""}`}
            aria-current={active ? "page" : undefined}
            onClick={() => onChange(id)}
          >
            <Icon size={19} />
            <span>{label}</span>
          </button>
        );
      })}
    </nav>
  );
}
