"use client";

import { useEffect, useMemo, useState } from "react";
import { Fig } from "@/components/statebook/Fig";
import type { Ref } from "@/lib/intake";
import type { Me } from "@/lib/signin";
import {
  basisWords,
  dateWords,
  differences,
  discomsOf,
  monthsWords,
  nameDocuments,
  rowsOf,
  rowTitle,
  scopeWords,
  SECTIONS,
  SOURCE_LABEL,
  sourceWords,
  statesWithDiscoms,
  today,
  twinOf,
  versionWords,
  windowWords,
  type Jurisdiction,
  type TariffBook,
  type TariffFigure,
  type TariffRow,
} from "@/lib/tariffs";

const UNIT_WORDS: Record<string, string> = {
  "INR/kWh": "₹/kWh",
  "INR/kVAh": "₹/kVAh",
  "INR/kVA/month": "₹/kVA/month",
  "INR/kW/month": "₹/kW/month",
  "INR/MW/month": "₹/MW/month",
  "INR/MW/day": "₹/MW/day",
  "INR/MWh": "₹/MWh",
  "INR/year": "₹/year",
  percent: "%",
  ratio: "×",
};

function places(v: string): number {
  const d = v.split(".")[1];
  return d ? Math.min(d.length, 4) : 0;
}

function Figures({ figs, book }: { figs: TariffFigure[]; book: TariffBook }) {
  if (figs.length === 0) return null;
  return (
    <table className="ep-table ep-tariff__figs">
      <tbody>
        {figs.map((f) => {
          const src = sourceWords(f.value.source_refs, book.documents);
          return (
            <tr key={f.key}>
              <td>{f.label}</td>
              <td className="num"><Fig v={f.value} places={places(f.value.value)} /> {UNIT_WORDS[f.value.unit] ?? f.value.unit}</td>
              <td className="ep-small">{src === "not yet sourced" ? <span className="eg-chip eg-chip--warning">not yet sourced</span> : <span className="eg-muted">{SOURCE_LABEL[src]}</span>}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PointSource({ r, book }: { r: Ref; book: TariffBook }) {
  const src = sourceWords([r], book.documents);
  if (src === "page") {
    return <a className="ep-small" href={`/api/documents/${r.document_id}#page=${r.page}`} target="_blank" rel="noreferrer">page {r.page}</a>;
  }
  if (src === "unstored") return <span className="ep-small eg-muted">page {r.page} cited, PDF not on this server</span>;
  if (src === "register") return <span className="ep-small eg-muted">register, no stored page</span>;
  return <span className="ep-small eg-muted">ergPLAN&apos;s own statement, not yet sourced</span>;
}

function RowCard({ r, book, compare }: { r: TariffRow; book: TariffBook; compare: boolean }) {
  const v = book.versions.find((x) => x.id === r.version_id);
  const where = book.chain.find((c) => c.id === r.jurisdiction_id);
  const draft = r.status === "draft";
  const diffs = draft && compare ? differences(twinOf(book, r), r) : [];
  const twin = draft && compare ? twinOf(book, r) : null;
  return (
    <div className={`eg-card ep-panel ep-tariff__row${draft ? " ep-tariff__row--draft" : ""}`}>
      <div className="ep-row" style={{ flexWrap: "wrap" }}>
        <b>{rowTitle(r)}</b>
        {draft ? <span className="eg-chip eg-chip--warning">Draft, not approved</span> : <span className="eg-chip eg-chip--accent">Approved</span>}
        {basisWords(r) && <span className="eg-chip">{basisWords(r)}</span>}
      </div>
      {r.family === "oa_charge" && r.label && <p className="ep-small eg-muted">{r.label}</p>}
      {scopeWords(r.scope).length > 0 && <p className="ep-small">Applies to: {scopeWords(r.scope).join(" · ")}</p>}
      <p className="ep-small eg-muted">
        Set for {where?.name ?? r.jurisdiction_id} · {versionWords(v)} · {v ? windowWords(v.effective_from, v.effective_to) : ""}
        {v?.title ? ` · ${v.title}` : ""}
      </p>
      {r.nearer && <p className="ep-small"><span className="eg-chip">Replaced here</span> {r.nearer} states its own row for the same consumers; that one applies.</p>}
      <Figures figs={r.figures} book={book} />
      {r.slots.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="ep-table ep-tariff__figs">
            <thead><tr><th>Slot</th><th>Hours</th><th>Months</th><th>Surcharge or rebate</th></tr></thead>
            <tbody>
              {r.slots.map((s, i) => (
                <tr key={i}>
                  <td>{s.name.replaceAll("_", " ")}{s.category && s.category !== s.name ? <span className="eg-muted"> ({s.category.replaceAll("_", "-")})</span> : null}</td>
                  <td>{s.start} to {s.end}</td>
                  <td>{monthsWords(s.months.length ? s.months : (r.scope.months as number[] | undefined))}</td>
                  <td>
                    {s.figures.length === 0 ? <span className="eg-muted">none stated</span> : s.figures.map((f) => (
                      <span key={f.key} className="ep-small" style={{ display: "block" }}>
                        {f.label}: <Fig v={f.value} places={places(f.value.value)} /> {UNIT_WORDS[f.value.unit] ?? f.value.unit}
                        <span className="eg-muted"> · {SOURCE_LABEL[sourceWords(f.value.source_refs, book.documents)]}</span>
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {r.points.length > 0 && (
        <ul className="ep-small ep-tariff__points">
          {r.points.map((p, i) => <li key={i}>{p.text} <PointSource r={p.ref} book={book} /></li>)}
        </ul>
      )}
      {r.notes.map((n, i) => <p key={i} className="ep-small eg-muted">{n}</p>)}
      {draft && compare && (
        <div className="ep-tariff__diff">
          <div className="eg-overline">Against the approved row</div>
          {!twin ? (
            <p className="ep-small">No approved row covers the same consumers: this draft adds a new one.</p>
          ) : diffs.length === 0 ? (
            <p className="ep-small">Same figures as the approved row.</p>
          ) : (
            <table className="ep-table ep-tariff__figs">
              <thead><tr><th>Figure</th><th className="num">Approved</th><th className="num">Draft</th></tr></thead>
              <tbody>
                {diffs.map((d, i) => (
                  <tr key={i}>
                    <td>{d.label}</td>
                    <td className="num">{d.approved ? <><Fig v={d.approved} places={places(d.approved.value)} /> {UNIT_WORDS[d.approved.unit] ?? d.approved.unit}</> : <span className="eg-muted">not stated</span>}</td>
                    <td className="num">{d.draft ? <><Fig v={d.draft} places={places(d.draft.value)} /> {UNIT_WORDS[d.draft.unit] ?? d.draft.unit}</> : <span className="eg-muted">not stated</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

// The "DISCOM tariffs" tab: pick a state, a DISCOM and a date; see the tariff the approved rule books
// hold for it, every figure opening its source. An analyst may add the latest drafts, each labelled.
export function TariffScreen({ initial }: { initial: { discom?: string; on?: string; drafts?: boolean } }) {
  const [me, setMe] = useState<Me | null>(null);
  const [all, setAll] = useState<Jurisdiction[]>([]);
  const [state, setState] = useState("");
  const [discom, setDiscom] = useState(initial.discom ?? "");
  const [on, setOn] = useState(initial.on ?? today());
  const [drafts, setDrafts] = useState(!!initial.drafts);
  const [compare, setCompare] = useState(true);
  const [book, setBook] = useState<TariffBook | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const analyst = !!me?.signed_in && me.role === "analyst";

  useEffect(() => {
    fetch("/api/auth/me").then((r) => (r.ok ? r.json() : null)).then((m) => setMe(m)).catch(() => undefined);
    fetch("/api/rulebooks/jurisdictions")
      .then(async (r) => {
        if (!r.ok) throw new Error(r.status === 401 ? "Sign in to see the tariffs." : `The server answered ${r.status}`);
        const js = (await r.json()) as Jurisdiction[];
        setAll(js);
        const start = js.find((j) => j.id === initial.discom && j.kind === "discom");
        const st = start?.state_code ?? statesWithDiscoms(js).find((s) => s.state_code === "MH")?.state_code ?? statesWithDiscoms(js)[0]?.state_code ?? "";
        setState(st ?? "");
        if (!start) setDiscom(discomsOf(js, st ?? "")[0]?.id ?? "");
      })
      .catch((e) => setError((e as Error).message));
  }, [initial.discom]);

  const states = useMemo(() => statesWithDiscoms(all), [all]);
  const discoms = useMemo(() => discomsOf(all, state), [all, state]);
  const showDrafts = drafts && analyst;

  useEffect(() => {
    if (!discom) return;
    setBusy(true);
    setError("");
    const q = new URLSearchParams({ on });
    if (showDrafts) q.set("drafts", "true");
    const page = new URLSearchParams({ d: discom, on });
    if (showDrafts) page.set("drafts", "1");
    window.history.replaceState(null, "", `/tariffs?${page}`);
    fetch(`/api/tariffs/${encodeURIComponent(discom)}?${q}`)
      .then(async (r) => {
        if (!r.ok) {
          const d = await r.json().catch(() => null);
          throw new Error(typeof d?.detail === "string" ? d.detail : `The server answered ${r.status}`);
        }
        setBook(nameDocuments((await r.json()) as TariffBook));
      })
      .catch((e) => {
        setBook(null);
        setError((e as Error).message);
      })
      .finally(() => setBusy(false));
  }, [discom, on, showDrafts]);

  const draftCount = book?.rows.filter((r) => r.status === "draft").length ?? 0;
  return (
    <>
      <div>
        <div className="eg-overline">As the approved rule books hold it</div>
        <h1 className="eg-h1">DISCOM tariffs</h1>
        <p className="eg-muted ep-lede">
          Pick a state, a DISCOM and a date to see its retail tariff schedule, its time-of-day slots, and the open-access and banking charges
          that apply there. Every figure opens the order page it comes from; a figure from the State Regulation Master says it has no stored
          page; what no approved rule states is marked not yet sourced. Only approved rules are shown{analyst ? " unless you include drafts" : ""}.
        </p>
      </div>
      <section className="eg-card ep-panel">
        <div className="ep-calc__form">
          <label>State
            <select className="eg-input" value={state} onChange={(e) => { setState(e.target.value); setDiscom(discomsOf(all, e.target.value)[0]?.id ?? ""); }}>
              {states.map((s) => <option key={s.id} value={s.state_code ?? s.id}>{s.name}</option>)}
            </select>
          </label>
          <label>DISCOM
            <select className="eg-input" value={discom} onChange={(e) => setDiscom(e.target.value)}>
              {discoms.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </label>
          <label>Rules in force on
            <input className="eg-input" type="date" value={on} onChange={(e) => e.target.value && setOn(e.target.value)} />
          </label>
          {analyst && (
            <fieldset className="ep-calc__plant">
              <legend>jouleWise</legend>
              <label className="ep-tariff__check"><input type="checkbox" checked={drafts} onChange={(e) => setDrafts(e.target.checked)} /> Include drafts</label>
              {drafts && <label className="ep-tariff__check"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} /> Show where a draft differs</label>}
            </fieldset>
          )}
        </div>
        {busy && <p className="ep-small eg-muted">Reading the rule books…</p>}
        {error && <p className="ep-small" role="alert">{error}</p>}
      </section>
      {book && (
        <>
          <section className="eg-card ep-panel">
            <div className="eg-overline">{book.discom_name} · rules in force on {dateWords(book.on)}</div>
            <p className="ep-small">
              Rules are read for {book.chain.map((c) => c.name).join(", then ")}: the nearest one that states a row applies.
              {showDrafts && ` ${draftCount} draft row${draftCount === 1 ? "" : "s"} shown beside the approved ones; a draft is not used for any plan until it is approved.`}
            </p>
            {book.versions.length > 0 && (
              <div style={{ overflowX: "auto" }}>
                <table className="ep-table ep-small">
                  <thead><tr><th>Rule book</th><th>Set for</th><th>Version</th><th>In force</th></tr></thead>
                  <tbody>
                    {book.versions.map((v) => (
                      <tr key={v.id}>
                        <td>{SECTIONS.find((s) => s.family === v.family)?.title ?? v.family}{v.title ? <span className="eg-muted"> · {v.title}</span> : null}</td>
                        <td>{v.jurisdiction_name}</td>
                        <td>{v.status === "draft" ? <span className="eg-chip eg-chip--warning">draft</span> : null} {versionWords(v)}</td>
                        <td>{windowWords(v.effective_from, v.effective_to)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          {SECTIONS.map((sec) => {
            const rows = rowsOf(book, sec.family, showDrafts);
            const missing = book.not_sourced.includes(sec.family);
            return (
              <section key={sec.family} className="ep-tariff__section">
                <h2 className="eg-h2">{sec.title}</h2>
                <p className="ep-small eg-muted">{sec.lede}</p>
                {missing && <p className="ep-small"><span className="eg-chip eg-chip--warning">not yet sourced</span> No approved rule states this for {book.discom_name} on {dateWords(book.on)}.</p>}
                <div className="ep-tariff__rows">
                  {rows.map((r) => <RowCard key={`${r.status}-${r.rule_id}`} r={r} book={book} compare={compare} />)}
                </div>
              </section>
            );
          })}
        </>
      )}
    </>
  );
}
