// Left navigation. Six grouped entries rather than twelve flat ones — screens
// that answer the same question (System/Health, Logs/Terminal) or that are
// consecutive stages of one pipeline (Scanner -> Wallet Intel -> AI) are tabs
// within an entry, not siblings in the sidebar. No screen was removed.
//
// Kept text-only and minimal for speed and clarity (no icon library dependency).

import { NavLink } from "react-router-dom";

export interface NavItem {
  to: string;
  label: string;
  /** The tabs reachable inside this entry — shown as a hint under the label. */
  tabs?: string[];
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "System", tabs: ["Overview", "Health"] },
  {
    to: "/intelligence",
    label: "Intelligence",
    tabs: ["Scanner", "Wallet Intel", "AI"],
  },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/research", label: "Research" },
  { to: "/logs", label: "Logs", tabs: ["Terminal", "History"] },
  {
    to: "/config",
    label: "Configuration",
    tabs: ["General", "Trading Mode", "Risk"],
  },
];

export function Sidebar() {
  return (
    <aside className="flex w-52 shrink-0 flex-col border-r border-white/5 bg-black/30">
      <div className="px-5 py-6">
        <span className="text-xl font-bold tracking-tight text-hades-accent">HADES</span>
        <p className="mt-1 text-[10px] uppercase tracking-widest text-hades-muted">
          Control Center
        </p>
      </div>
      <nav className="flex-1 space-y-1 px-3">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              `block rounded-md px-3 py-2 transition-colors ${
                isActive
                  ? "bg-hades-accent/10 text-hades-accent"
                  : "text-gray-400 hover:bg-white/5 hover:text-gray-200"
              }`
            }
          >
            <span className="block text-sm">{item.label}</span>
            {item.tabs && (
              <span className="mt-0.5 block text-[10px] text-hades-muted/70">
                {item.tabs.join(" · ")}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
