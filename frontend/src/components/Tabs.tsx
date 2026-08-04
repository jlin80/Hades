// Tab strip used by the grouped screens. The active tab lives in the URL as a
// `?tab=` search param rather than in component state, so a tab is linkable,
// survives a refresh, and the browser's back button steps through tabs the way
// a user expects. An unknown or absent param falls back to the first tab.

import { useSearchParams } from "react-router-dom";

export interface TabDef {
  id: string;
  label: string;
  element: React.ReactNode;
}

export function Tabs({ tabs }: { tabs: TabDef[] }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const active = tabs.find((t) => t.id === requested) ?? tabs[0];

  return (
    <div>
      <div className="mb-6 flex gap-1 border-b border-white/5">
        {tabs.map((tab) => {
          const isActive = tab.id === active.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setParams({ tab: tab.id }, { replace: true })}
              className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
                isActive
                  ? "border-hades-accent text-hades-accent"
                  : "border-transparent text-gray-400 hover:border-white/20 hover:text-gray-200"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {active.element}
    </div>
  );
}
