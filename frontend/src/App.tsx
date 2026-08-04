import { Navigate, Route, Routes } from "react-router-dom";
import { ModeSwitch } from "./components/ModeSwitch";
import { Sidebar } from "./components/Sidebar";
import { useLiveStatus } from "./hooks";
import {
  ConfigurationGroup,
  IntelligenceGroup,
  LogsGroup,
  SystemGroup,
} from "./pages/groups";
import { PortfolioScreen } from "./pages/PortfolioScreen";
import { ResearchScreen } from "./pages/ResearchScreen";

// Control-center shell: fixed sidebar, a header with the guarded Paper/Live
// switch and live status, and the routed screen area.
//
// Navigation is grouped: six entries, each hosting the related screens as tabs
// (see `pages/groups.tsx`). The twelve original paths are kept as redirects into
// the tab that now holds them, so existing links and bookmarks still resolve.
export default function App() {
  const status = useLiveStatus();

  return (
    <div className="flex h-screen overflow-hidden bg-hades-bg text-gray-100">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex items-center justify-between border-b border-white/5 px-6 py-3">
          <div className="text-sm text-hades-muted">
            {status ? (
              <span>
                v{status.version} · {status.environment} ·{" "}
                {status.event_bus_transport} · up {Math.floor(status.uptime_seconds)}s
              </span>
            ) : (
              <span className="text-red-400">API unreachable</span>
            )}
          </div>
          <ModeSwitch />
        </header>

        {status?.is_live && (
          <div className="bg-red-950/60 px-6 py-1.5 text-center text-xs font-semibold text-red-300">
            LIVE TRADING ENABLED — real orders are being placed
          </div>
        )}

        <main className="flex-1 overflow-y-auto p-6">
          <Routes>
            {/* Grouped screens. */}
            <Route path="/" element={<SystemGroup />} />
            <Route path="/intelligence" element={<IntelligenceGroup />} />
            <Route path="/portfolio" element={<PortfolioScreen />} />
            <Route path="/research" element={<ResearchScreen />} />
            <Route path="/logs" element={<LogsGroup />} />
            <Route path="/config" element={<ConfigurationGroup />} />

            {/* Legacy paths — preserved so old links keep working. */}
            <Route path="/health" element={<Navigate to="/?tab=health" replace />} />
            <Route
              path="/scanner"
              element={<Navigate to="/intelligence?tab=scanner" replace />}
            />
            <Route path="/ai" element={<Navigate to="/intelligence?tab=ai" replace />} />
            <Route
              path="/terminal"
              element={<Navigate to="/logs?tab=terminal" replace />}
            />
            <Route path="/trading" element={<Navigate to="/config?tab=trading" replace />} />
            <Route path="/risk" element={<Navigate to="/config?tab=risk" replace />} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
