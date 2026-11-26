"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { RefChips } from "@/components/cost/RefChips";
import { clientFetch } from "@/lib/intake";
import {
  ACCEPTED, EMPTY_FORM, againstLibrary, basisWords, formProblem, monthName, pct, provenanceWords, resolutionWords, runWords,
  technologyWords, toFormData, useWords, type GenerationProfile, type UploadForm,
} from "@/lib/generation";

// A site's own generation profiles: upload a year of solar or wind, see the CUF it implies (annual
// and by month) beside the library curve it replaces, and which plans were worked with it.
export function GenerationProfiles({ siteId, connectionId }: { siteId: string; connectionId: string }) {
  const file = useRef<HTMLInputElement>(null);
  const [form, setForm] = useState<UploadForm>(EMPTY_FORM);
  const [rows, setRows] = useState<GenerationProfile[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setRows(await clientFetch<GenerationProfile[]>(`/sites/${siteId}/generation-profiles`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [siteId]);
  useEffect(() => { void load(); }, [load]);

  const set = <K extends keyof UploadForm>(k: K, v: UploadForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function upload() {
    const f = file.current?.files?.[0];
    const problem = formProblem(form, Boolean(f));
    setMsg("");
    if (problem || !f) return setError(problem ?? "Choose the CSV file.");
    setBusy(true);
    setError("");
    try {
      const p = await clientFetch<GenerationProfile>(`/sites/${siteId}/generation-profiles`, { method: "POST", body: toFormData(form, f) });
      setMsg(`${p.filename} is accepted: an annual CUF of ${pct(p.annual_cuf)}, ${againstLibrary(p) ?? "no library curve to compare"}. New plans for this site generate with it.`);
      if (file.current) file.current.value = "";
      await load();
    } catch (e) {
      // a refused file: the API's reason, in its own words
      setError(`The file is refused: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function withdraw(p: GenerationProfile) {
    setError("");
    setMsg("");
    try {
      await clientFetch<GenerationProfile>(`/generation-profiles/${p.id}/withdraw`, { method: "POST" });
      setMsg(`${p.filename} is withdrawn: new plans no longer use it. Plans already worked with it keep it.`);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <section className="eg-card ep-panel">
        <div className="eg-overline" style={{ marginBottom: 12 }}>Upload a generation profile</div>
        <div className="ep-form">
          <label className="eg-field"><span className="eg-field__label">File (CSV)</span>
            <span className="eg-input"><input ref={file} type="file" accept=".csv,text/csv" /></span></label>
          <label className="eg-field"><span className="eg-field__label">Plant</span>
            <span className="eg-input"><select value={`${form.technology}:${form.appliesTo}`} onChange={(e) => {
              const [t, a] = e.target.value.split(":") as [UploadForm["technology"], UploadForm["appliesTo"]];
              setForm((f) => ({ ...f, technology: t, appliesTo: a }));
            }}>
              <option value="solar:open_access">Open-access solar plant</option>
              <option value="wind:open_access">Open-access wind plant</option>
              <option value="solar:rooftop">Rooftop solar at this site</option>
            </select></span></label>
          <label className="eg-field"><span className="eg-field__label">Where the profile is from</span>
            <span className="eg-input"><select value={form.provenance} onChange={(e) => set("provenance", e.target.value as UploadForm["provenance"])}>
              <option value="stated">The developer's estimate (a simulation or a quote)</option>
              <option value="measured">The meter of a working plant</option>
            </select></span></label>
          <label className="eg-field"><span className="eg-field__label">The values are for</span>
            <span className="eg-input"><select value={form.basis} onChange={(e) => set("basis", e.target.value as UploadForm["basis"])}>
              <option value="">A fraction or percent of capacity (CUF)</option>
              <option value="per_mw">1 MW of capacity</option>
              <option value="capacity">A plant of the capacity I state</option>
            </select></span></label>
          {form.basis === "capacity" && (
            <label className="eg-field"><span className="eg-field__label">Plant capacity, MW (AC)</span>
              <span className="eg-input"><input inputMode="decimal" value={form.capacityMw} onChange={(e) => set("capacityMw", e.target.value)} placeholder="e.g. 12.5" /></span></label>
          )}
          <label className="eg-field"><span className="eg-field__label">First day (only when the file has no timestamps)</span>
            <span className="eg-input"><input type="date" value={form.firstDay} onChange={(e) => set("firstDay", e.target.value)} /></span></label>
        </div>
        <div className="ep-row" style={{ marginTop: "var(--space-4)" }}>
          <button className="eg-btn eg-btn--primary" disabled={busy} onClick={upload}>{busy ? "Checking the file…" : "Upload"}</button>
        </div>
        {msg && <p className="eg-muted ep-note" role="status">{msg}</p>}
        {error && <p className="ep-error" role="alert">{error}</p>}
        <details style={{ marginTop: "var(--space-4)" }}>
          <summary className="eg-muted" style={{ fontSize: 13, cursor: "pointer" }}>What a file must be</summary>
          <ul className="eg-muted" style={{ fontSize: 13, maxWidth: "80ch" }}>{ACCEPTED.map((a) => <li key={a}>{a}</li>)}</ul>
        </details>
      </section>

      {rows === null ? null : rows.length === 0 ? (
        <section className="eg-card ep-panel">
          <p className="ep-empty">No profile supplied yet. Plans for this site use the library curve for the state (an assumption) until you upload one.</p>
        </section>
      ) : rows.map((p) => {
        const top = Math.max(...p.monthly_cuf.map((m) => Number(m.cuf.value)), 1);
        const versus = againstLibrary(p);
        return (
          <section key={p.id} className="eg-card ep-panel">
            <div className="ep-row" style={{ justifyContent: "space-between" }}>
              <div>
                <div className="eg-overline">{technologyWords(p)}</div>
                <h2 className="eg-h2" style={{ margin: "4px 0" }}>{p.filename}</h2>
              </div>
              <div className="ep-row">
                <span className={`eg-chip ${p.in_use ? "eg-chip--accent" : ""}`}>{useWords(p)}</span>
                {!p.withdrawn_at && <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => withdraw(p)}>Stop using it</button>}
              </div>
            </div>
            <div className="ep-stats">
              <div className="eg-card ep-stat">
                <span className="ep-stat__label">Annual CUF the file implies</span>
                <strong style={{ fontSize: 28 }}>{pct(p.annual_cuf, 2)}</strong>
                <RefChips refs={p.annual_cuf.source_refs} max={1} kinds={1} />
                {versus && <span className="eg-muted" style={{ fontSize: 12 }}>{versus}</span>}
              </div>
              <div className="eg-card ep-stat">
                <span className="ep-stat__label">Highest period, share of capacity</span>
                <strong style={{ fontSize: 28 }}>{pct(p.peak)}</strong>
                <RefChips refs={p.peak.source_refs} max={1} kinds={1} />
              </div>
            </div>
            <div className="ep-months" role="img" aria-label={`CUF by month: ${p.monthly_cuf.map((m) => `${monthName(m.month)} ${pct(m.cuf)}`).join(", ")}`}>
              {p.monthly_cuf.map((m) => (
                <div key={m.month} className="ep-month" title={`${monthName(m.month)}: ${pct(m.cuf, 2)}`}>
                  <div className="ep-month__bar"><span style={{ height: `${(Number(m.cuf.value) / top) * 100}%`, background: "var(--accent)" }} /></div>
                  <small>{monthName(m.month)}<br />{pct(m.cuf)}</small>
                </div>
              ))}
            </div>
            <dl className="ep-kv">
              <dt>Where it is from</dt><dd>{provenanceWords(p)}</dd>
              <dt>What the file holds</dt><dd>{resolutionWords(p)}; {basisWords(p)}</dd>
              <dt>File fingerprint (SHA-256)</dt><dd className="eg-mono" style={{ fontSize: 12 }}>{p.sha256}</dd>
              <dt>Uploaded</dt><dd>{p.created_at.slice(0, 10)} by {p.created_by}</dd>
              <dt>Plans worked with it</dt>
              <dd>
                {p.runs.length === 0 ? "None yet. The next plan you run for this site uses it." : (
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {p.runs.map((r) => (
                      <li key={r.run_id}><Link href={`/results?c=${connectionId}&s=${r.scenario_id}`}>{runWords(r)}</Link></li>
                    ))}
                  </ul>
                )}
              </dd>
            </dl>
          </section>
        );
      })}
    </>
  );
}
