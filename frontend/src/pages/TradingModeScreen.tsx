import { useEffect, useState } from "react";
import {
  api,
  type ModeStatus,
  type ReadinessReport,
  type WalletHealth,
} from "../api/client";
import { ModeSwitch } from "../components/ModeSwitch";
import { Badge, PageHeader, Panel, Row } from "../ui";

/** The trading wallet's identity and funding — never its key.
 *
 *  The private key is deliberately absent from this screen and from the API
 *  behind it. It lives in a file mounted into the Worker alone, so a compromised
 *  dashboard or API cannot read it and it never crosses the network. There is no
 *  form here to paste it into, and adding one would undo that.
 */
function WalletPanel({ wallet }: { wallet: WalletHealth | null }) {
  const configured = wallet?.configured ?? false;
  return (
    <Panel title="Trading wallet">
      <Row
        label="Configured"
        value={
          <Badge tone={configured ? "success" : "neutral"}>
            {configured ? "yes" : "no"}
          </Badge>
        }
      />
      <Row
        label="Public key"
        value={
          wallet?.public_key ? (
            <span className="font-mono text-xs">{wallet.public_key}</span>
          ) : (
            "—"
          )
        }
      />
      <Row label="Balance" value={`${(wallet?.balance_sol ?? 0).toFixed(4)} SOL`} />
      <Row label="Status" value={wallet?.detail ?? "…"} danger={configured && !wallet?.healthy} />

      {!configured && (
        <div className="mt-4 rounded-lg border border-white/5 bg-black/20 p-3">
          <p className="text-xs text-hades-muted">
            No wallet is configured, which is fine for paper trading. To provision one,
            copy the keypair to the server and start with the live overlay — the key is
            mounted read-only into the worker and is never sent to this dashboard:
          </p>
          <pre className="mt-2 overflow-x-auto rounded bg-black/40 p-2 text-[11px] leading-relaxed text-gray-300">
{`mkdir -p secrets && chmod 700 secrets
cp your-keypair.json secrets/hades_wallet.json
chmod 600 secrets/hades_wallet.json

# then set WALLET_PUBLIC_KEY in .env and start with:
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d`}
          </pre>
        </div>
      )}
    </Panel>
  );
}

export function TradingModeScreen() {
  const [mode, setMode] = useState<ModeStatus | null>(null);
  const [report, setReport] = useState<ReadinessReport | null>(null);
  const [wallet, setWallet] = useState<WalletHealth | null>(null);

  useEffect(() => {
    const load = () => {
      api.tradingMode().then(setMode).catch(() => undefined);
      api.executionWallet().then(setWallet).catch(() => undefined);
    };
    load();
    const timer = setInterval(load, 15_000);
    return () => clearInterval(timer);
  }, []);

  const runVerify = () => api.verifyLive().then(setReport).catch(() => undefined);

  return (
    <div>
      <PageHeader
        title="Trading mode"
        subtitle="Paper is always safe. Live is hard-gated: env flag + verification + confirmation."
      />
      <div className="grid gap-6 md:grid-cols-2">
        <Panel title="Current mode" actions={<ModeSwitch />}>
          <Row label="Effective mode" value={mode?.mode ?? "…"} />
          <Row label="Is live" value={String(mode?.is_live)} danger={mode?.is_live} />
          <Row label="Live env gate" value={String(mode?.live_gate_enabled)} />
        </Panel>

        <Panel
          title="Live readiness"
          actions={
            <button
              onClick={runVerify}
              className="rounded-md border border-white/10 px-3 py-1 text-xs text-gray-200 hover:bg-white/5"
            >
              Run verification
            </button>
          }
        >
          {!report && <p className="text-sm text-hades-muted">Run verification to see checks.</p>}
          {report && (
            <>
              <div className="mb-3">
                <Badge tone={report.ready ? "success" : "danger"}>
                  {report.ready ? "READY" : "NOT READY"}
                </Badge>
              </div>
              {report.checks.map((c) => (
                <Row
                  key={c.name}
                  label={`${c.ok ? "✓" : "✕"} ${c.name}${c.required ? "" : " (optional)"}`}
                  value={c.detail}
                  danger={c.required && !c.ok}
                />
              ))}
            </>
          )}
        </Panel>

        <WalletPanel wallet={wallet} />
      </div>
    </div>
  );
}
