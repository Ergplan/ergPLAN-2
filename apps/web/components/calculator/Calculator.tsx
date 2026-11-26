"use client";

import { useEffect, useMemo, useState } from "react";
import { Fig } from "@/components/statebook/Fig";
import {
  bestSlot,
  bodyOf,
  gapLabel,
  ROUTE_ORDER,
  ROUTE_WORDS,
  routeStatus,
  type CalcRoute,
  type CalculatorOut,
  type Form,
  type Options,
  type StatesOut,
} from "@/lib/calculator";

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    const d = await r.json().catch(() => null);
    throw new Error(typeof d?.detail === "string" ? d.detail : `The calculator answered ${r.status}`);
  }
  return (await r.json()) as T;
}

function Route({ r, slot }: { r: CalcRoute; slot: string | null }) {
  const st = routeStatus(r);
  const sl = r.slots.find((s) => s.slot === slot) ?? r.slots[0];
  return (
    <div className="eg-card ep-panel ep-calc__route">
      <div className="eg-overline">{ROUTE_WORDS[r.route] ?? r.label}</div>
      <p className="ep-small eg-muted" title={r.note}>{r.label}</p>
      {st.text && <p className={`ep-small eg-chip ${st.tone === "bad" ? "eg-chip--danger" : "eg-chip--warning"}`} style={{ whiteSpace: "normal" }}>{st.text}</p>}
      {r.reason_values.map((v, i) => <p key={i} className="ep-small">Rule: <Fig v={v} places={0} /> {v.label}</p>)}
      {sl && (
        <table className="ep-table ep-calc__lines">
          <tbody>
            {sl.lines.map((x, i) => (
              <tr key={i}><td>{x.label}</td><td className="num"><Fig v={x} places={4} tone={false} /></td></tr>
            ))}
            {r.not_sourced.map((n) => (
              <tr key={n.component}><td title={n.words}>{gapLabel(n.component)}</td><td className="num"><span className="eg-chip eg-chip--warning">not yet sourced</span></td></tr>
            ))}
            <tr><td><b>Landed cost at the meter</b></td><td className="num"><Fig v={sl.landed} places={4} strong /></td></tr>
            {sl.banking && <tr><td>Banking charge, on a banked unit only</td><td className="num"><Fig v={sl.banking} places={4} /></td></tr>}
            <tr><td>Grid tariff in this slot</td><td className="num"><Fig v={sl.grid} places={4} /></td></tr>
            <tr><td><b>Saving</b></td><td className="num"><Fig v={sl.saving} places={4} strong tone /></td></tr>
          </tbody>
        </table>
      )}
      {r.shown && r.landed_avg && (
        <p className="ep-small">Over the day: landed <Fig v={r.landed_avg} places={2} /> · saving <Fig v={r.saving_avg} places={2} tone /> ₹/kWh</p>
      )}
      {r.inputs.length > 0 && (
        <p className="ep-small eg-muted">Rests on: {r.inputs.map((v, i) => <span key={i}>{v.label} <Fig v={v} places={2} />{i < r.inputs.length - 1 ? " · " : ""}</span>)}</p>
      )}
      {r.notes.slice(0, 2).map((n, i) => <p key={i} className="ep-small eg-muted">{n}</p>)}
    </div>
  );
}

// The landed-cost calculator: any consumer in any state, priced from the approved rules at once.
export function Calculator() {
  const [opts, setOpts] = useState<Options | null>(null);
  const [denied, setDenied] = useState(false);
  const [f, setF] = useState<Form>({
    discom: "", category: "", voltage: "33", cd: "5000", month: thisMonth(), connection: "intra_state",
    solar: { tariff: "", capex: "", cuf: "" }, wind: { tariff: "", capex: "", cuf: "" }, equity: "26", market: "dam",
  });
  const [state, setState] = useState("");
  const [out, setOut] = useState<CalculatorOut | null>(null);
  const [all, setAll] = useState<StatesOut | null>(null);
  const [slot, setSlot] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    fetch("/api/calculator/options")
      .then(async (r) => {
        if (r.status === 401) return setDenied(true);
        if (!r.ok) throw new Error(`The calculator answered ${r.status}`);
        const o = (await r.json()) as Options;
        setOpts(o);
        const up = o.states.find((s) => s.state === "UP") ?? o.states[0];
        if (up) {
          setState(up.state);
          setF((x) => ({ ...x, discom: up.discoms[0]?.discom ?? "", category: up.discoms[0]?.reference_category ?? "" }));
        }
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  const discoms = useMemo(() => opts?.states.find((s) => s.state === state)?.discoms ?? [], [opts, state]);
  const cats = discoms.find((d) => d.discom === f.discom)?.categories ?? [];

  async function run() {
    setBusy("one");
    setError("");
    try {
      const o = await post<CalculatorOut>("/calculator", bodyOf(f));
      setOut(o);
      const first = o.technologies[0]?.routes.find((r) => r.shown);
      setSlot(first ? bestSlot(first) : null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function runAll() {
    setBusy("all");
    setError("");
    try {
      setAll(await post<StatesOut>("/calculator/states", bodyOf(f)));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function csv() {
    const r = await fetch("/api/calculator/states.csv", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(bodyOf(f)) });
    if (!r.ok) return setError(`The download answered ${r.status}`);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `landed-cost-all-states-${f.month}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (denied) {
    return (
      <section className="eg-card ep-panel">
        <h1 className="eg-h1">Landed-cost calculator</h1>
        <p className="eg-muted">Sign in to use the calculator.</p>
        <a className="eg-btn eg-btn--primary" href="/login?next=/calculator">Sign in</a>
      </section>
    );
  }
  const set = (k: keyof Form, v: string) => setF((x) => ({ ...x, [k]: v }));
  const setPlant = (tech: "solar" | "wind", k: "tariff" | "capex" | "cuf", v: string) => setF((x) => ({ ...x, [tech]: { ...x[tech], [k]: v } }));
  return (
    <>
      <div>
        <div className="eg-overline">Any state, no bill needed</div>
        <h1 className="eg-h1">Landed-cost calculator</h1>
        <p className="eg-muted ep-lede">
          What solar and wind cost at your meter by each route, and what the exchange costs, from the state&apos;s approved rules — line by line,
          per time-of-day slot, beside the grid tariff. Every figure opens its source. What you leave empty is ergPLAN&apos;s benchmark, marked assumed;
          what no approved rule states is marked not yet sourced. Nothing you type is stored.
        </p>
      </div>
      <section className="eg-card ep-panel">
        <div className="ep-calc__form">
          <label>State
            <select className="eg-input" value={state} onChange={(e) => { setState(e.target.value); const d = opts?.states.find((s) => s.state === e.target.value)?.discoms[0]; setF((x) => ({ ...x, discom: d?.discom ?? "", category: d?.reference_category ?? "" })); }}>
              {opts?.states.map((s) => <option key={s.state} value={s.state}>{s.name}</option>)}
            </select>
          </label>
          <label>DISCOM
            <select className="eg-input" value={f.discom} onChange={(e) => { const d = discoms.find((x) => x.discom === e.target.value); setF((x) => ({ ...x, discom: e.target.value, category: d?.reference_category ?? "" })); }}>
              {discoms.map((d) => <option key={d.discom} value={d.discom}>{d.name}</option>)}
            </select>
          </label>
          <label>Consumer category
            <select className="eg-input" value={f.category} onChange={(e) => set("category", e.target.value)}>
              {cats.length === 0 && <option value="">HT industrial (no approved schedule)</option>}
              {[...new Set(cats.map((c) => c.category))].map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>Supply voltage, kV<input className="eg-input" inputMode="decimal" value={f.voltage} onChange={(e) => set("voltage", e.target.value)} /></label>
          <label>Contract demand, kVA<input className="eg-input" inputMode="decimal" value={f.cd} onChange={(e) => set("cd", e.target.value)} /></label>
          <label>Month<input className="eg-input" type="month" value={f.month} onChange={(e) => set("month", e.target.value)} /></label>
          <label>Connection
            <select className="eg-input" value={f.connection} onChange={(e) => set("connection", e.target.value)}>
              <option value="intra_state">Within the state</option>
              <option value="ists">Inter-state (ISTS)</option>
            </select>
          </label>
          <label>Exchange market
            <select className="eg-input" value={f.market} onChange={(e) => set("market", e.target.value)}>
              {opts?.markets.map((m) => <option key={m.market} value={m.market}>{m.label}</option>)}
            </select>
          </label>
          <label>Equity share for captive, %<input className="eg-input" inputMode="decimal" value={f.equity} onChange={(e) => set("equity", e.target.value)} /></label>
          {(["solar", "wind"] as const).map((t) => (
            <fieldset key={t} className="ep-calc__plant">
              <legend>{t === "solar" ? "Solar" : "Wind"}</legend>
              <label>PPA tariff, ₹/kWh<input className="eg-input" inputMode="decimal" placeholder="benchmark" value={f[t].tariff} onChange={(e) => setPlant(t, "tariff", e.target.value)} /></label>
              <label>Capital cost, ₹/kW<input className="eg-input" inputMode="decimal" placeholder="benchmark" value={f[t].capex} onChange={(e) => setPlant(t, "capex", e.target.value)} /></label>
              <label>CUF, %<input className="eg-input" inputMode="decimal" placeholder="state curve" value={f[t].cuf} onChange={(e) => setPlant(t, "cuf", e.target.value)} /></label>
            </fieldset>
          ))}
        </div>
        <div className="ep-row" style={{ marginTop: "var(--space-3)" }}>
          <button className="eg-btn eg-btn--primary" onClick={run} disabled={!!busy || !f.discom}>{busy === "one" ? "Working…" : "Show the landed cost"}</button>
          <button className="eg-btn eg-btn--ghost" onClick={runAll} disabled={!!busy}>{busy === "all" ? "Working through every state…" : "Every state at once"}</button>
        </div>
        {error && <p className="ep-small" role="alert">{error}</p>}
      </section>
      {out && (
        <>
          <section className="eg-card ep-panel">
            <div className="eg-overline">{out.state_name} · {out.discom_name} · {out.category}{out.rate_block ? ` (${out.rate_block})` : ""} · rules in force on {out.as_of}</div>
            <div className="eg-seg eg-seg--sm" role="group" aria-label="Time-of-day slot" style={{ marginTop: 8 }}>
              {out.slots.map((s) => (
                <button key={s.slot} className="ep-seglink" aria-current={slot === s.slot ? "true" : undefined} onClick={() => setSlot(s.slot)}>
                  {s.slot} · grid {s.rate ? <Fig v={s.rate} places={2} /> : "not yet sourced"}
                </button>
              ))}
            </div>
            {out.rulebook && <p className="ep-small"><a href={`/api${out.rulebook.file}`}>The month&apos;s state rule book</a> (approved issue {out.rulebook.issue})</p>}
          </section>
          {out.technologies.map((t) => (
            <section key={t.tech}>
              <h2 className="eg-h2">{t.label}</h2>
              <div className="ep-calc__routes">
                {ROUTE_ORDER.map((id) => {
                  const r = id === "iex" ? out.exchange.route : t.routes.find((x) => x.route === id);
                  return r ? <Route key={id} r={r} slot={slot} /> : null;
                })}
              </div>
            </section>
          ))}
          <section className="eg-card ep-panel">
            <div className="eg-overline">Buying on the exchange in {out.state_name}: {out.exchange.verdict.verdict_label}</div>
            <p className="ep-small">{out.exchange.verdict.words}</p>
            {out.exchange.verdict.schedule && <p className="ep-small">Schedule: {out.exchange.verdict.schedule}</p>}
            {out.exchange.verdict.apply_for.map((c, i) => <p key={i} className="ep-small">Apply for: {c.text}</p>)}
            {Object.entries(out.gaps).map(([k, v]) => <p key={k} className="ep-small"><span className="eg-chip eg-chip--warning">not yet sourced</span> {v}</p>)}
          </section>
        </>
      )}
      {all && (
        <section className="eg-card ep-panel">
          <div className="ep-row">
            <div className="eg-overline">Every state · {all.month.slice(0, 7)} · average over the day, ₹/kWh</div>
            <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={csv}>Download as CSV</button>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="ep-table">
              <thead>
                <tr><th>State · DISCOM</th><th>Category</th><th className="num">Grid</th>
                  {["solar", "wind"].flatMap((t) => ["captive", "group_captive", "third_party"].map((r) => <th key={t + r} className="num">{t === "solar" ? "Solar" : "Wind"} · {ROUTE_WORDS[r]}</th>))}
                  <th className="num">Exchange</th><th>Rule book</th></tr>
              </thead>
              <tbody>
                {all.rows.map((row) => (
                  <tr key={row.discom}>
                    <td>{row.state_name} · {row.discom_name}</td>
                    <td>{row.error ?? row.category}</td>
                    <td className="num">{row.grid_avg ? <Fig v={row.grid_avg} /> : <span className="eg-muted">not yet sourced</span>}</td>
                    {row.cells.map((c) => (
                      <td key={c.tech + c.route} className="num" title={c.reason ?? undefined}>
                        {c.landed_avg ? <Fig v={c.landed_avg} /> : <span className="eg-muted">{c.tech === "iex" ? row.exchange_verdict : "—"}</span>}
                      </td>
                    ))}
                    <td>{row.rulebook ? <a href={`/api${row.rulebook.file}`}>issue {row.rulebook.issue}</a> : <span className="eg-muted">none approved</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
