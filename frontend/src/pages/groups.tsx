// Grouped screens — the navigation used to carry twelve flat entries, several of
// which were two views of the same subject (System/Health, Logs/Terminal) or
// steps of one pipeline (Scanner -> Wallet Intel -> AI). Grouping them collapses
// the sidebar to six entries without removing a single view: every original
// screen is still rendered, now as a tab.
//
// The screen components themselves are untouched. Composition happens here, so
// regrouping later is an edit to this file alone.

import { Tabs } from "../components/Tabs";
import { AIScreen } from "./AIScreen";
import { ConfigScreen } from "./ConfigScreen";
import { HealthScreen } from "./HealthScreen";
import { IntelligenceScreen } from "./IntelligenceScreen";
import { LogsScreen } from "./LogsScreen";
import { RiskScreen } from "./RiskScreen";
import { ScannerScreen } from "./ScannerScreen";
import { SystemScreen } from "./SystemScreen";
import { TerminalScreen } from "./TerminalScreen";
import { TradingModeScreen } from "./TradingModeScreen";

// System + Health: both answer "is the platform alive", one from the platform's
// side and one from the dependency probes'.
export function SystemGroup() {
  return (
    <Tabs
      tabs={[
        { id: "overview", label: "Overview", element: <SystemScreen /> },
        { id: "health", label: "Health", element: <HealthScreen /> },
      ]}
    />
  );
}

// The analytical pipeline in the order it actually runs: a token is discovered,
// its wallets are profiled, then the committee forms an opinion.
export function IntelligenceGroup() {
  return (
    <Tabs
      tabs={[
        { id: "scanner", label: "Scanner", element: <ScannerScreen /> },
        { id: "wallets", label: "Wallet Intel", element: <IntelligenceScreen /> },
        { id: "ai", label: "AI Committee", element: <AIScreen /> },
      ]}
    />
  );
}

// Everything that changes how the platform behaves, in one place: the general
// configuration, the guarded Paper/Live switch and the risk limits + defence
// layer. These are the three screens an operator uses to *change* posture.
export function ConfigurationGroup() {
  return (
    <Tabs
      tabs={[
        { id: "general", label: "General", element: <ConfigScreen /> },
        { id: "trading", label: "Trading Mode", element: <TradingModeScreen /> },
        { id: "risk", label: "Risk", element: <RiskScreen /> },
      ]}
    />
  );
}

// Logs and Terminal are the same thing at two grains: the stored, filterable
// record and the live stream.
export function LogsGroup() {
  return (
    <Tabs
      tabs={[
        { id: "terminal", label: "Live Terminal", element: <TerminalScreen /> },
        { id: "logs", label: "Log History", element: <LogsScreen /> },
      ]}
    />
  );
}
