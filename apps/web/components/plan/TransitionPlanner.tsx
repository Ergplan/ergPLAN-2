"use client";

import { useCallback, useEffect, useState } from "react";
import { clientFetch } from "@/lib/intake";
import { progress, targetBody, type TransitionOut } from "@/lib/transition";
import { TransitionView } from "./TransitionView";

// The client states the target (the lowest cost, or a share of renewable energy), the plan is worked
// out phase by phase on the worker, and the page fills in as each phase settles.
export function TransitionPlanner({ connectionId, initial }: { connectionId: string; initial: TransitionOut | null }) {
  const [plan, setPlan] = useState<TransitionOut | null>(initial);
  const [kind, setKind] = useState<"lowest_cost" | "share">(initial?.asked.kind ?? "lowest_cost");
  const [share, setShare] = useState(initial?.asked.share_pct ? String(Number(initial.asked.share_pct)) : "50");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setPlan(await clientFetch<TransitionOut>(`/connections/${connectionId}/transition-plan`));
  }, [connectionId]);

  useEffect(() => {
    if (plan?.status !== "running") return;
    const t = setInterval(() => void refresh().catch(() => undefined), 5000);
    return () => clearInterval(t);
  }, [plan?.status, refresh]);

  async function start() {
    const made = targetBody(kind, share);
    if ("error" in made) {
      setError(made.error);
      return;
    }
    setBusy(true);
    setError("");
    try {
      setPlan(await clientFetch<TransitionOut>(`/connections/${connectionId}/transition-plan`, { method: "POST", body: JSON.stringify(made.body) }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="eg-card ep-panel">
      <h2 className="eg-card__title">Your transition plan</h2>
      <p className="eg-card__sub">From today&apos;s bill to your target in steps you can act on: rooftop first, then open access, then storage. Each step is sized at least cost under your state&apos;s rules and settled again before it is shown.</p>
      <div className="ep-row">
        <label><input type="radio" name="tp-target" checked={kind === "lowest_cost"} onChange={() => setKind("lowest_cost")} /> The lowest cost</label>
        <label><input type="radio" name="tp-target" checked={kind === "share"} onChange={() => setKind("share")} /> A share of renewable energy</label>
        {kind === "share" && (
          <label>
            <input className="eg-input" style={{ width: 90 }} inputMode="decimal" value={share} onChange={(e) => setShare(e.target.value)} aria-label="Target share of renewable energy, percent" /> %
          </label>
        )}
        <button className="eg-btn eg-btn--primary" disabled={busy || plan?.status === "running"} onClick={start}>
          {busy ? "Starting…" : plan ? "Work it out for this target" : "Work out my transition plan"}
        </button>
      </div>
      {error && <p className="ep-error">{error}</p>}
      {plan?.status === "running" && <p className="ep-plan-status">{progress(plan)}</p>}
      {plan && <TransitionView plan={plan} />}
    </section>
  );
}
