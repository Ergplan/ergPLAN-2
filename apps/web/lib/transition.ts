import type { Ref, SourcedValue } from "./intake";

// The transition plan (Stage 15): from today's bill to a target, in phases the client can act on.
// Every figure is a SourcedValue read from a settled, pinned run; the shapes mirror the API's TransitionOut.
export type Binding = { name: string; words: string; refs: SourcedValue[] };
export type PhaseSource = {
  id: string; kind: string; label: string; ownership: string | null;
  capacity: SourcedValue; added: SourcedValue; energy: SourcedValue | null; added_energy: SourcedValue | null;
  delivered_kwh: SourcedValue | null; yearly_cost: SourcedValue | null;
  capital: SourcedValue | null; capital_words: string | null; tariff: SourcedValue | null; gaps: string[];
};
export type RuleLine = { text: string; ref: Ref; value?: SourcedValue | null };
// basis "rule": an approved rule row states the step and `ref` cites it. "assumption": general practice
// that no approved rule in the state's rule book states; it is shown as assumed, never as a rule.
export type Action = { what: string; with_whom: string; basis: "rule" | "assumption"; ref: Ref };
export type PhaseEnergy = {
  load_kwh: SourcedValue; grid_kwh: SourcedValue; delivered_kwh: SourcedValue; setoff_kwh: SourcedValue;
  banked_kwh: SourcedValue; unbanked_kwh: SourcedValue; withdrawn_kwh: SourcedValue; lapsed_kwh: SourcedValue;
};
export type Phase = {
  key: "rooftop" | "open_access" | "storage" | "target" | "most" | string; order: number; title: string; what: string;
  status: "taken" | "adds_nothing"; status_words: string | null; route: string | null; route_words: string | null;
  run_id: string | null; scenario_id: string | null; resolution_min: number; proven: boolean; proven_words: string | null;
  sources: PhaseSource[];
  annual_cost: SourcedValue; annual_saving: SourcedValue; added_saving: SourcedValue;
  capital: SourcedValue; added_capital: SourcedValue; share: SourcedValue; added_share: SourcedValue;
  payback_years: SourcedValue | null; irr_pct: SourcedValue | null;
  energy: PhaseEnergy; binding: Binding[]; rules: RuleLine[]; actions: Action[]; notes: string[];
};
export type CurvePoint = {
  role: "phase" | "target" | "curve" | "most"; label: string; share: SourcedValue; annual_cost: SourcedValue;
  annual_saving: SourcedValue; capital: SourcedValue;
  sizes: { label: string; kind: string; capacity: SourcedValue; energy: SourcedValue | null }[];
  run_id: string | null; proven: boolean; proven_words: string | null; cheapest: boolean;
};
export type RouteAlternative = {
  route: string; label: string; chosen: boolean; settlement_rule_approved: boolean; run_id: string | null;
  annual_cost: SourcedValue | null; annual_saving: SourcedValue | null; capital: SourcedValue | null; words: string;
};
export type TransitionTarget = { kind: "lowest_cost" | "share"; share_pct?: string | null; ownership?: string | null; resolution_min?: 15 | 60; curve_points?: number };
export type TransitionOut = {
  id: string; connection_id: string; created_at: string; finished_at: string | null;
  status: "running" | "done" | "failed"; pending_runs: number; runs: number; resolution_min: number; state: string; discom: string;
  asked: TransitionTarget;
  today: { annual_cost: SourcedValue; load_kwh: SourcedValue; share: SourcedValue } | null;
  target: {
    kind: "lowest_cost" | "share"; asked: SourcedValue | null; reached: SourcedValue | null; met: boolean | null;
    reached_in_phase: string | null; most: SourcedValue | null; most_proven?: boolean | null; most_binding: Binding[]; most_run_id: string | null; words: string;
  };
  phases: Phase[]; curve: CurvePoint[]; alternatives: RouteAlternative[]; not_worked_out: { key: string; words: string }[];
};

const BATTERY = new Set(["btm_bess", "oa_bess"]);
const n = (v: SourcedValue | null | undefined) => (v ? Number(v.value) : 0);
const fmt = (x: number, places = 2) => x.toLocaleString("en-IN", { maximumFractionDigits: places });

// "32.1%": a share of the year's consumption, to one decimal.
export function pct(v: SourcedValue | null | undefined): string {
  return v ? `${fmt(Number(v.value), 1)}%` : "—";
}

// A capacity as the client reads it: "4.61 MW", a battery as power and energy "2.5 MW / 10 MWh".
export function capacityText(kind: string, kw: SourcedValue | null | undefined, kwh?: SourcedValue | null): string {
  if (!kw) return "—";
  const power = `${fmt(n(kw) / 1000)} MW`;
  return BATTERY.has(kind) ? `${power} / ${fmt(n(kwh) / 1000)} MWh` : power;
}

// What a phase adds to a source: "new", "+1.2 MW" or "as before"; a source not taken says so.
export function addedText(s: Pick<PhaseSource, "kind" | "capacity" | "added" | "added_energy">): string {
  const cap = n(s.capacity), add = n(s.added), addKwh = n(s.added_energy);
  if (cap < 0.5 && n(s.added_energy) < 0.5 && add < 0.5) return "not taken";
  if (add < 0.5 && addKwh < 0.5) return "as before";
  if (Math.abs(cap - add) < 0.5) return "new in this phase";
  return BATTERY.has(s.kind) ? `+${fmt(add / 1000)} MW / +${fmt(addKwh / 1000)} MWh` : `+${fmt(add / 1000)} MW`;
}

// Capital of the client's own, a tariff per kWh, or both, in words beside the figures.
export function payBasis(s: Pick<PhaseSource, "capital" | "tariff">): "capital" | "tariff" | "capital and tariff" | "none" {
  if (s.capital && s.tariff) return "capital and tariff";
  return s.capital ? "capital" : s.tariff ? "tariff" : "none";
}

// A step is shown as required by a rule only when a rule row states it; anything else is general practice.
export function basisLabel(a: Pick<Action, "basis">): string {
  return a.basis === "rule" ? "the rule requires it" : "general practice (assumed, not a rule)";
}

// The body for a new plan from the form: a share target needs a share between 0 and 100.
export function targetBody(kind: "lowest_cost" | "share", sharePct: string): { body: TransitionTarget } | { error: string } {
  if (kind === "lowest_cost") return { body: { kind } };
  const v = Number(sharePct);
  if (!sharePct.trim() || !Number.isFinite(v) || v <= 0 || v > 100) return { error: "Give the share of renewable energy you want, between 1 and 100%." };
  return { body: { kind, share_pct: String(v) } };
}

// "3 of 5 runs finished": progress while the worker solves the plan's runs one after another.
export function progress(p: Pick<TransitionOut, "runs" | "pending_runs" | "status">): string {
  if (p.status !== "running") return "";
  const done = p.runs - p.pending_runs;
  return `Working out your transition plan: ${done} of ${p.runs} run${p.runs === 1 ? "" : "s"} settled so far. Each phase is sized on the one before it, so more runs follow.`;
}
