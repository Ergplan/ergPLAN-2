"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { analystFetch, getAnalyst, setAnalyst } from "@/lib/analyst";
import type { Book } from "@/lib/statebook";

type D = { id: string; name: string; state_name: string };

const SOURCES: [string, string][] = [
  ["oa_solar_intra", "Solar, in-state"],
  ["oa_wind_intra", "Wind, in-state"],
  ["oa_solar_ists", "Solar, inter-state"],
  ["oa_wind_ists", "Wind, inter-state"],
];

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

// Generate the month's books for every state and client, or one book with your own reference consumer
// and quoted PPA tariffs. Generating never approves: every book lands as a draft for review.
export function GeneratePanel({ discoms, onChange }: { discoms: D[]; onChange: () => void }) {
  const router = useRouter();
  const [analyst, setAnalystState] = useState("");
  const [month, setMonth] = useState(thisMonth());
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [discom, setDiscom] = useState("");
  useEffect(() => { if (!discom && discoms[0]) setDiscom(discoms[0].id); }, [discom, discoms]);
  const [kv, setKv] = useState("");
  const [cd, setCd] = useState("");
  const [prices, setPrices] = useState<Record<string, string>>({});
  useEffect(() => setAnalystState(getAnalyst()), []);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    setError("");
    setMsg("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  const all = () =>
    run("month", async () => {
      const made = await analystFetch<Book[]>("/statebooks/generate-month", {
        method: "POST",
        body: JSON.stringify({ month: `${month}-01`, clients: true }),
      });
      setMsg(
        made.length
          ? `${made.length} rule ${made.length === 1 ? "book" : "books"} drafted for review.`
          : "Nothing new: every book for this month is already awaiting review or unchanged since it was approved.",
      );
      onChange();
    });

  const one = () =>
    run("one", async () => {
      const p = Object.fromEntries(Object.entries(prices).filter(([, v]) => v.trim() !== ""));
      const b = await analystFetch<Book>("/statebooks/generate", {
        method: "POST",
        body: JSON.stringify({
          discom,
          month: `${month}-01`,
          voltage_kv: kv.trim() || null,
          contract_demand_kva: cd.trim() || null,
          prices: p,
        }),
      });
      router.push(`/statebooks/${b.id}`);
    });

  return (
    <section className="eg-card ep-panel">
      <div className="ep-row" style={{ alignItems: "flex-end" }}>
        <label className="ep-field" style={{ minWidth: 220 }}>
          <span className="eg-overline">Reviewing as</span>
          <span className="eg-input">
            <input value={analyst} placeholder="name@joulewise.in" onChange={(e) => { setAnalystState(e.target.value); setAnalyst(e.target.value); }} onBlur={onChange} />
          </span>
        </label>
        <label className="ep-field">
          <span className="eg-overline">Month</span>
          <span className="eg-input"><input type="month" value={month} onChange={(e) => setMonth(e.target.value)} /></span>
        </label>
        <button className="eg-btn eg-btn--primary" disabled={!!busy || !month} onClick={() => void all()}>
          {busy === "month" ? "Generating…" : "Generate this month's rule books"}
        </button>
        <button className="eg-btn eg-btn--ghost" disabled={!!busy} onClick={() => setOpen(!open)}>
          {open ? "Close" : "One book with my own inputs"}
        </button>
      </div>
      {open && (
        <div style={{ marginTop: "var(--space-4)" }}>
          <div className="ep-grid3">
            <label className="ep-field">
              <span className="eg-overline">DISCOM</span>
              <span className="eg-input">
                <select value={discom} onChange={(e) => setDiscom(e.target.value)}>
                  {discoms.map((d) => <option key={d.id} value={d.id}>{d.state_name} · {d.name}</option>)}
                </select>
              </span>
            </label>
            <label className="ep-field">
              <span className="eg-overline">Supply voltage (kV)</span>
              <span className="eg-input"><input inputMode="decimal" value={kv} placeholder="33" onChange={(e) => setKv(e.target.value)} /></span>
            </label>
            <label className="ep-field">
              <span className="eg-overline">Contract demand (kVA)</span>
              <span className="eg-input"><input inputMode="decimal" value={cd} placeholder="5000" onChange={(e) => setCd(e.target.value)} /></span>
            </label>
            {SOURCES.map(([id, label]) => (
              <label key={id} className="ep-field">
                <span className="eg-overline">{label} PPA (₹/kWh)</span>
                <span className="eg-input">
                  <input inputMode="decimal" value={prices[id] ?? ""} placeholder="benchmark" onChange={(e) => setPrices({ ...prices, [id]: e.target.value })} />
                </span>
              </label>
            ))}
          </div>
          <div className="ep-row" style={{ marginTop: "var(--space-3)" }}>
            <button className="eg-btn eg-btn--primary" disabled={!!busy || !discom} onClick={() => void one()}>
              {busy === "one" ? "Generating…" : "Generate this book"}
            </button>
            <span className="eg-muted ep-small">A PPA tariff left empty takes ergPLAN&apos;s benchmark, shown as assumed.</span>
          </div>
        </div>
      )}
      {msg && <p className="eg-muted" style={{ margin: "var(--space-3) 0 0" }}>{msg}</p>}
      {error && <p role="alert" style={{ color: "var(--negative, #b42318)", margin: "var(--space-3) 0 0" }}>{error}</p>}
    </section>
  );
}
