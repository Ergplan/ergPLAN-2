"use client";

import { useState } from "react";
import { ProfileChart } from "@/components/intake/ProfileChart";
import { clientFetch, DAYS, type ProfileOut } from "@/lib/intake";
import { type AnswerResult, dateOf, type Draft, draftOf, mmddOf, profileWords, type Question, type QuestionsOut, standing, statedOn, toValue } from "@/lib/questions";

type Props = { connectionId: string; siteId: string; initial: QuestionsOut; initialProfile: ProfileOut | null };

// What the bills do not say: each question in a plain form. An answer is saved as "you said"
// (client-stated), and the load profile beside the questions is redrawn from it.
export function Questions({ connectionId, siteId, initial, initialProfile }: Props) {
  const [data, setData] = useState(initial);
  const [profile, setProfile] = useState(initialProfile);
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => Object.fromEntries(initial.questions.map((q) => [q.key, draftOf(q)])));
  const [busy, setBusy] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string[]>>({});

  const upd = (key: string, patch: Partial<Draft>) => setDrafts((d) => ({ ...d, [key]: { ...d[key], ...patch } }));

  async function after(key: string, r: AnswerResult) {
    setData((d) => ({ ...d, outstanding: r.outstanding, questions: d.questions.map((q) => (q.key === key ? r.question : q)) }));
    setDrafts((d) => ({ ...d, [key]: draftOf(r.question) }));
    setNotes((n) => ({ ...n, [key]: r.warnings }));
    if (r.changed) setProfile(await clientFetch<ProfileOut>(`/connections/${connectionId}/profile`));
  }

  async function save(q: Question) {
    const got = toValue(q.form, drafts[q.key]);
    if ("error" in got) return setErrors((e) => ({ ...e, [q.key]: got.error }));
    setBusy(q.key);
    setErrors((e) => ({ ...e, [q.key]: "" }));
    try {
      await after(q.key, await clientFetch<AnswerResult>(`/sites/${siteId}/questions/${q.key}`, { method: "PUT", body: JSON.stringify({ value: got.value }) }));
    } catch (e) {
      setErrors((x) => ({ ...x, [q.key]: (e as Error).message }));
    } finally {
      setBusy("");
    }
  }

  async function withdraw(q: Question) {
    setBusy(q.key);
    setErrors((e) => ({ ...e, [q.key]: "" }));
    try {
      await after(q.key, await clientFetch<AnswerResult>(`/sites/${siteId}/questions/${q.key}`, { method: "DELETE" }));
    } catch (e) {
      setErrors((x) => ({ ...x, [q.key]: (e as Error).message }));
    } finally {
      setBusy("");
    }
  }

  const left = data.outstanding.length;
  const peak = profile ? Math.max(...profile.months.map((m) => Number(m.max_kw.value))) : 0;
  return (
    <div className="ep-questions">
      <section className="eg-card ep-panel" data-testid="profile-now">
        <h2 className="eg-card__title">Your load profile now</h2>
        {profile ? (
          <>
            <ProfileChart kw={profile.week_kw.map(Number)} maxKw={peak} />
            <p className="ep-small eg-muted">
              One week of the busiest month, in 15-minute blocks; the dashed line is the highest demand of the year. Version {profile.version}.{" "}
              <span className={`eg-chip${profile.provenance === "measured" ? " eg-chip--accent" : ""}`}>{profile.provenance === "measured" ? "meter data" : "estimated"}</span>{" "}
              {profileWords(profile.provenance, data.measured)}
            </p>
          </>
        ) : (
          <p className="eg-muted">No load profile yet: save your inputs first.</p>
        )}
      </section>

      <p className="eg-muted" data-testid="questions-left">
        {left === 0 ? "Every question is answered. You can change an answer at any time." : `${left} question${left === 1 ? "" : "s"} still open. A plan can be sized without them; each answer sharpens it.`}
      </p>

      {data.questions.map((q) => {
        const d = drafts[q.key];
        return (
          <section key={q.key} id={`q-${q.key}`} className="eg-card ep-panel ep-question" data-testid={`question-${q.key}`}>
            <div className="ep-row" style={{ justifyContent: "space-between" }}>
              <h2 className="eg-card__title">{q.label}</h2>
              {q.answer ? <span className="eg-chip" title={statedOn(q.answer)}>you said</span> : !q.asked ? <span className="eg-chip">not needed</span> : q.known_from_inputs ? <span className="eg-chip">already known</span> : <span className="eg-chip eg-chip--warning">open</span>}
            </div>
            <p>{q.question}</p>
            <p className="ep-small eg-muted">{q.why} <b>It changes:</b> {q.used_for}</p>
            <p className="ep-small" data-testid={`standing-${q.key}`}>{standing(q)}{q.answer ? ` (${statedOn(q.answer)})` : ""}</p>
            {q.asked && (
              <>
                <Form q={q} d={d} upd={(p) => upd(q.key, p)} />
                <div className="ep-row" style={{ marginTop: 12 }}>
                  <button className="eg-btn eg-btn--primary eg-btn--sm" disabled={busy === q.key} onClick={() => void save(q)}>{busy === q.key ? "Saving…" : q.answer ? "Change my answer" : "Save my answer"}</button>
                  {q.answer && <button className="eg-btn eg-btn--ghost eg-btn--sm" disabled={busy === q.key} onClick={() => void withdraw(q)}>Withdraw my answer</button>}
                  {q.form !== "number" && <span className="ep-small eg-muted">Unit: {q.unit}</span>}
                </div>
              </>
            )}
            {errors[q.key] && <p className="ep-error">{errors[q.key]}</p>}
            {(notes[q.key] ?? []).map((w) => <p key={w} className="ep-small"><span className="eg-chip eg-chip--warning">note</span> {w}</p>)}
          </section>
        );
      })}
    </div>
  );
}

function Time({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return <span className="eg-input ep-q__time"><input type="time" step={900} aria-label={label} value={value === "24:00" ? "00:00" : value} onChange={(e) => onChange(e.target.value)} /></span>;
}

function NoneBox({ d, upd, label }: { d: Draft; upd: (p: Partial<Draft>) => void; label: string }) {
  return <label className="ep-row ep-small"><input type="checkbox" checked={d.none} onChange={(e) => upd({ none: e.target.checked })} /> {label}</label>;
}

function Form({ q, d, upd }: { q: Question; d: Draft; upd: (p: Partial<Draft>) => void }) {
  if (q.form === "shifts") {
    const set = (i: number, p: Partial<Draft["hours"][number]>) => upd({ hours: d.hours.map((h, j) => (j === i ? { ...h, ...p } : h)) });
    return (
      <div className="ep-q__rows">
        {d.hours.map((h, i) => (
          <div key={i} className="ep-row">
            <span className="ep-small eg-muted">Shift {i + 1}</span>
            <Time label={`Shift ${i + 1} starts`} value={h.start} onChange={(v) => set(i, { start: v })} />
            <span>to</span>
            <Time label={`Shift ${i + 1} ends`} value={h.end} onChange={(v) => set(i, { end: v })} />
            {d.hours.length > 1 && <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ hours: d.hours.filter((_, j) => j !== i) })}>Remove</button>}
          </div>
        ))}
        {d.hours.length < 4 && <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ hours: [...d.hours, { start: "14:00", end: "22:00" }] })}>Add a shift</button>}
      </div>
    );
  }
  if (q.form === "weekdays") {
    return (
      <div className="ep-q__rows">
        <div className="ep-row">
          {DAYS.map((name, i) => (
            <label key={name} className={`eg-chip ep-day${d.days.includes(i) && !d.none ? " ep-day--off" : ""}`}>
              <input type="checkbox" disabled={d.none} checked={d.days.includes(i) && !d.none} onChange={(e) => upd({ days: e.target.checked ? [...d.days, i] : d.days.filter((x) => x !== i) })} /> {name}
            </label>
          ))}
        </div>
        <NoneBox d={d} upd={upd} label="There is no weekly off: the plant works all seven days" />
      </div>
    );
  }
  if (q.form === "shutdowns") {
    const set = (i: number, p: Partial<Draft["shutdowns"][number]>) => upd({ shutdowns: d.shutdowns.map((s, j) => (j === i ? { ...s, ...p } : s)) });
    return (
      <div className="ep-q__rows">
        {!d.none && d.shutdowns.map((s, i) => (
          <div key={i} className="ep-row">
            <span className="eg-input"><input type="date" aria-label="First day of the shutdown" value={dateOf(s.start)} onChange={(e) => set(i, { start: mmddOf(e.target.value) })} /></span>
            <span>to</span>
            <span className="eg-input"><input type="date" aria-label="Last day of the shutdown" value={dateOf(s.end)} onChange={(e) => set(i, { end: mmddOf(e.target.value) })} /></span>
            <span className="eg-input"><input aria-label="What the shutdown is for" placeholder="e.g. annual overhaul" value={s.name} onChange={(e) => set(i, { name: e.target.value })} /></span>
            <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ shutdowns: d.shutdowns.filter((_, j) => j !== i) })}>Remove</button>
          </div>
        ))}
        {!d.none && <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ shutdowns: [...d.shutdowns, { start: "", end: "", name: "" }] })}>Add a shutdown</button>}
        {!d.none && d.shutdowns.length > 0 && <span className="ep-small eg-muted">Only the day and the month are kept: the shutdown is taken to come every year.</span>}
        <NoneBox d={d} upd={upd} label="The plant has no seasonal shutdown" />
      </div>
    );
  }
  if (q.form === "loads") {
    const set = (i: number, p: Partial<Draft["loads"][number]>) => upd({ loads: d.loads.map((l, j) => (j === i ? { ...l, ...p } : l)) });
    return (
      <div className="ep-q__rows">
        {!d.none && d.loads.map((l, i) => (
          <div key={i} className="ep-row">
            <span className="eg-input"><input aria-label="Name of the load" placeholder="e.g. induction furnace" value={l.name} onChange={(e) => set(i, { name: e.target.value })} /></span>
            <span className="eg-input ep-q__num"><input inputMode="decimal" aria-label="Size in kW" placeholder="kW" value={l.kw} onChange={(e) => set(i, { kw: e.target.value })} /></span>
            <span className="eg-muted">kW, from</span>
            <Time label="The load starts" value={l.start} onChange={(v) => set(i, { start: v })} />
            <span>to</span>
            <Time label="The load stops" value={l.end} onChange={(v) => set(i, { end: v })} />
            <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ loads: d.loads.filter((_, j) => j !== i) })}>Remove</button>
          </div>
        ))}
        {!d.none && <button className="eg-btn eg-btn--ghost eg-btn--sm" onClick={() => upd({ loads: [...d.loads, { name: "", kw: "", start: "09:00", end: "17:00" }] })}>Add a load</button>}
        <NoneBox d={d} upd={upd} label="No large load runs at set hours" />
      </div>
    );
  }
  if (q.form === "percent") {
    return (
      <div className="ep-q__rows">
        {!d.none && (
          <div className="ep-row">
            <span className="eg-input ep-q__num"><input inputMode="decimal" aria-label={q.label} placeholder="e.g. 20" value={d.text} onChange={(e) => upd({ text: e.target.value })} /></span>
            <span className="eg-muted">% more than today (a minus sign for less)</span>
          </div>
        )}
        <NoneBox d={d} upd={upd} label="No change is planned" />
      </div>
    );
  }
  return (
    <div className="ep-row">
      <span className="eg-input ep-q__num"><input inputMode="decimal" aria-label={q.label} value={d.text} onChange={(e) => upd({ text: e.target.value })} /></span>
      <span className="eg-muted">{q.unit}</span>
    </div>
  );
}
