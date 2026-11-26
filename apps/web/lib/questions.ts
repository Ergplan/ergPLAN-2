// What the bills do not say (Stage 15): the questions for a site, their answers, and the plain forms
// that state them. Every answer is client-stated: shown as a chip, never as a sourced fact.
import type { Ref, SourcedValue } from "./intake";

export type FormKind = "shifts" | "weekdays" | "shutdowns" | "loads" | "number" | "percent";

export type AnswerOut = { value: unknown; words: string; figures: SourcedValue[]; stated_by: string; stated_at: string; source_ref: Ref };

export type Question = {
  key: string;
  label: string;
  question: string;
  unit: string;
  why: string;
  form: FormKind;
  used_for: string;
  shapes_profile_only: boolean;
  asked: boolean;
  not_asked: string | null;
  until_answered: string;
  known_from_inputs: string | null;
  answer: AnswerOut | null;
};

export type QuestionsOut = { site_id: string; measured: boolean; questions: Question[]; outstanding: string[] };

export type AnswerResult = {
  question: Question;
  changed: boolean;
  profiles: { connection_id: string; load_profile_id: string; version: number; provenance: string }[];
  warnings: string[];
  outstanding: string[];
};

// An answer as the plan page shows it (PlanOut.answers)
export type StatedAnswer = {
  key: string;
  label: string;
  words: string;
  figures: SourcedValue[];
  stated_by: string;
  stated_at: string;
  source_ref: Ref;
  used_for: string;
  in_use: boolean;
  in_this_plan: boolean | null;
};

export type Hours = { start: string; end: string };
export type ShutdownRow = { start: string; end: string; name: string }; // start and end as MM-DD
export type LoadRow = { name: string; kw: string; start: string; end: string };

// What a form holds while it is being filled in
export type Draft = { hours: Hours[]; days: number[]; shutdowns: ShutdownRow[]; loads: LoadRow[]; text: string; none: boolean };

export const EMPTY: Draft = { hours: [{ start: "06:00", end: "14:00" }], days: [], shutdowns: [], loads: [], text: "", none: false };

type Stored = { start?: string; end?: string; name?: string; kw?: string; growth_pct?: string };

// The form as it opens: the answer in force, else an empty form
export function draftOf(q: Question): Draft {
  const v = q.answer?.value;
  if (v === undefined || v === null) return { ...EMPTY, hours: [...EMPTY.hours] };
  const rows = Array.isArray(v) ? (v as Stored[]) : [];
  switch (q.form) {
    case "shifts":
      return { ...EMPTY, hours: rows.map((r) => ({ start: r.start ?? "", end: r.end ?? "" })) };
    case "weekdays":
      return { ...EMPTY, days: (v as number[]).slice(), none: (v as number[]).length === 0 };
    case "shutdowns":
      return { ...EMPTY, shutdowns: rows.map((r) => ({ start: r.start ?? "", end: r.end ?? "", name: r.name ?? "" })), none: rows.length === 0 };
    case "loads":
      return { ...EMPTY, loads: rows.map((r) => ({ name: r.name ?? "", kw: r.kw ?? "", start: r.start ?? "", end: r.end ?? "" })), none: rows.length === 0 };
    case "percent":
      return { ...EMPTY, text: (v as Stored).growth_pct ?? "", none: Number((v as Stored).growth_pct) === 0 };
    default:
      return { ...EMPTY, text: String(v) };
  }
}

const QUARTER = /^([01]\d|2[0-3]):(00|15|30|45)$/;
const NUMBER = /^-?\d+(\.\d+)?$/;

function hoursError(h: Hours, what: string): string | null {
  if (!QUARTER.test(h.start) || !(QUARTER.test(h.end) || h.end === "24:00")) return `Give ${what} a start and an end on a quarter hour, such as 06:00 and 14:15.`;
  if (h.start === h.end) return `${what[0].toUpperCase()}${what.slice(1)} starts and ends at the same time.`;
  return null;
}

// The value the API takes for a filled form, or what is wrong with it in plain words.
// Numbers travel as the text the person typed (never as floats).
export function toValue(form: FormKind, d: Draft): { value: unknown } | { error: string } {
  switch (form) {
    case "shifts": {
      if (d.hours.length === 0) return { error: "Add at least one shift." };
      for (const h of d.hours) {
        const e = hoursError(h, "each shift");
        if (e) return { error: e };
      }
      return { value: d.hours.map((h) => ({ start: h.start, end: h.end })) };
    }
    case "weekdays":
      if (d.none) return { value: [] };
      if (d.days.length === 0) return { error: "Pick the closed days, or say there is no weekly off." };
      if (d.days.length > 6) return { error: "A plant closed all seven days has no working week." };
      return { value: [...d.days].sort((a, b) => a - b) };
    case "shutdowns": {
      if (d.none) return { value: [] };
      if (d.shutdowns.length === 0) return { error: "Add a shutdown, or say there is none." };
      for (const s of d.shutdowns) if (!/^\d\d-\d\d$/.test(s.start) || !/^\d\d-\d\d$/.test(s.end)) return { error: "Give each shutdown its first and last day." };
      return { value: d.shutdowns.map((s) => ({ start: s.start, end: s.end, name: s.name.trim() })) };
    }
    case "loads": {
      if (d.none) return { value: [] };
      if (d.loads.length === 0) return { error: "Add a load, or say there is none." };
      for (const l of d.loads) {
        if (!l.name.trim()) return { error: "Give each load a name." };
        if (!NUMBER.test(l.kw.trim()) || Number(l.kw) <= 0) return { error: `Give ${l.name.trim()} its size in kW.` };
        const e = hoursError(l, l.name.trim());
        if (e) return { error: e };
      }
      return { value: d.loads.map((l) => ({ name: l.name.trim(), kw: l.kw.trim(), start: l.start, end: l.end })) };
    }
    case "percent": {
      const t = d.none ? "0" : d.text.trim();
      if (!NUMBER.test(t)) return { error: "Give the change as a percentage, such as 20 (or -10 for a reduction)." };
      return { value: { growth_pct: t } };
    }
    default: {
      const t = d.text.trim().replaceAll(",", "");
      if (!NUMBER.test(t)) return { error: "Give a number." };
      return { value: t };
    }
  }
}

// "05-15" ↔ the value of a date input (the year is not kept: a seasonal shutdown comes every year)
export const dateOf = (mmdd: string): string => (/^\d\d-\d\d$/.test(mmdd) ? `2026-${mmdd}` : "");
export const mmddOf = (date: string): string => (/^\d{4}-\d\d-\d\d$/.test(date) ? date.slice(5) : "");

// One line under a question saying where it stands
export function standing(q: Question): string {
  if (!q.asked) return q.not_asked ?? "Not asked for this site.";
  if (q.answer) return `You said: ${q.answer.words}`;
  if (q.known_from_inputs) return `Already known: ${q.known_from_inputs}`;
  return `Until you answer: ${q.until_answered}`;
}

export function statedOn(a: { stated_by: string; stated_at: string }): string {
  const day = new Date(a.stated_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  return `stated by ${a.stated_by} on ${day}`;
}

// The words beside the profile chart
export function profileWords(provenance: string, measured: boolean): string {
  if (measured || provenance === "measured") return "Measured: your 15-minute meter file. The answers about shifts, weekly off, shutdowns and large loads are not needed.";
  return "Estimated from your bills and your answers. It is redrawn each time you answer, and always holds each bill's units, time-of-day split and recorded maximum demand.";
}
