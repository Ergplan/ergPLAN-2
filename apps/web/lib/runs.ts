import type { Ref, SourcedValue } from "./intake";

export type SizeOut = {
  id: string; kind: string; label: string; kw: SourcedValue; kwh: SourcedValue | null; delivered_kwh: SourcedValue | null;
  setoff_kwh: SourcedValue | null; energy_cost: SourcedValue | null; banked_kwh: SourcedValue | null; unbanked_kwh: SourcedValue | null;
  waterfall: Record<string, SourcedValue[]>; gaps: string[]; ownership?: string | null;
  exchange?: { segment: string; blocks_bought: number; hours_bought: string; avg_landed: SourcedValue; share_of_load: SourcedValue } | null;
};
export type MonthMix = { month: string; load_kwh: SourcedValue; grid_kwh: SourcedValue; withdrawn_kwh: SourcedValue; sources: Record<string, SourcedValue> };
export type SourceOption = {
  kind: string; label: string; group: string; connection: string; price_kind: string; default_price_inr_per_kwh: string | null;
  price_note: string | null; ownerships: string[]; available: boolean; reason: string | null;
  inputs?: Record<string, string>;
};
// FPPCA or electricity duty at the rate the client's own bills print (never a regulation's), with the
// treatment on units not bought from the DISCOM: an assumption, named in words.
export type LevyOut = {
  code: string; label: string; bills: number; rates: SourcedValue[]; on_discom_kwh: SourcedValue; on_other_kwh: SourcedValue | null;
  charged_on_other_units: boolean; treatment: SourcedValue; treatment_words: string; sourced_to_regulation: boolean;
  discom_kwh_before: SourcedValue; discom_kwh_after: SourcedValue; other_kwh: SourcedValue;
  before: SourcedValue; after_discom: SourcedValue; after_other: SourcedValue;
};
// One row per levy for the results and plan pages: the rate as the bills print it and, in words, what
// is charged on open-access, captive and rooftop units ("not charged" is said, never left blank).
export function levyRows(levies: LevyOut[] | undefined): { code: string; label: string; rate: string; before: string; afterDiscom: string; afterOther: string; words: string; value: LevyOut }[] {
  return (levies ?? []).map((l) => {
    const r = l.rates[0];
    return {
      code: l.code, label: l.label, value: l,
      rate: r ? `₹${Number(r.value).toFixed(2)}/${r.unit.replace("INR/", "")} on your ${l.bills} bill${l.bills === 1 ? "" : "s"}` : "—",
      before: l.before.value, afterDiscom: l.after_discom.value,
      afterOther: l.charged_on_other_units ? l.after_other.value : "not charged (assumed)",
      words: l.treatment_words,
    };
  });
}
export type SummaryOut = {
  sizes: SizeOut[];
  annual: { before: SourcedValue; after: SourcedValue; saving: SourcedValue; capex: SourcedValue };
  // on the capital of the client's own (owned_capital says against which saving); null when the plan has none
  payback_years: SourcedValue | null; irr_pct: SourcedValue | null;
  owned_capital?: OwnedCapital | null; // absent on runs made before 25 Nov 2026
  emissions: { factor: SourcedValue; before_t: SourcedValue; after_t: SourcedValue };
  settlement: { grid_kwh: SourcedValue; lapsed_kwh: SourcedValue; withdrawn_kwh: SourcedValue; rule: string | null };
  contract_demand_kva: SourcedValue;
  binding: { name: string; words: string; refs: SourcedValue[] }[];
  provenance: { load_profile: string; prices: string[]; profile_ref: SourcedValue };
  sensitivities: { label: string; saving: SourcedValue }[];
  notes: string[];
  monthly?: MonthMix[];
  lifecycle?: LifecycleOut | null;
  levies?: LevyOut[];
  lines_before?: Record<string, string>;
  lines_after?: Record<string, string>;
  settlement_rule?: string | null;
};
export type RunOut = {
  id: string; scenario_id: string; status: "queued" | "running" | "done" | "failed" | "failed_resettle"; resolution_min: number;
  created_at: string; finished_at: string | null; error: string | null; replay_of: string | null;
  pins: Record<string, unknown>; solver: { status?: string; gap?: number | null; seconds?: number; blocks?: number; objective_inr?: string };
  summary: SummaryOut | null; analyst: { resettle: { agrees: boolean; max_diff_kwh: string; tolerance_kwh: string; solver_objective_inr: string; resettled_cost_inr: string } } | null;
  identical_to_original: boolean | null;
};
export type CandidateSpec = {
  kind: string; ownership?: string; price_inr_per_kwh?: string; max_mw?: string; min_mw?: string; commissioned?: string;
  fix_at_max?: boolean; capex_inr_per_kw?: string; capex_inr_per_kwh?: string; om_pct_of_capex?: string; cuf_pct?: string;
  life_years?: number; degradation_pct?: string; round_trip_pct?: string; min_hours?: string; max_hours?: string;
  equity_share_pct?: string;
};
// The capital of the client's own in a plan: a plant (rooftop, a captive open-access plant), a battery,
// or the equity outlay for a plant taken with 26% equity; and how payback and IRR are measured on it.
export type OwnedAsset = { id: string; label: string; kind: "plant" | "battery" | "equity" | string; capex: SourcedValue; words: string };
export type OwnedCapital = {
  total: SourcedValue; assets: OwnedAsset[]; saving: SourcedValue; bought_saving: SourcedValue | null; bought: string[];
  basis: string; payback_absent: string | null; irr_absent: string | null;
};
export const CAPITAL_KIND: Record<string, string> = { plant: "your own plant", battery: "your own battery", equity: "equity outlay" };

// The headline for payback and IRR: always "on the owned capital", with the capital asset by asset, the
// saving it is measured against, and the reason in words when there is none to state (never a zero).
// A run made before the owned-capital treatment has no `owned_capital`: its payback was the plan's as
// a whole, and is labelled so.
export function paybackHeadline(s: Pick<SummaryOut, "payback_years" | "irr_pct" | "owned_capital" | "annual">): {
  label: string; capital: string | null; payback: string; irr: string; assets: { label: string; kind: string; capex: string }[];
  saving: string | null; basis: string; absent: string | null;
} {
  const oc = s.owned_capital;
  const years = (v: SourcedValue | null) => (v ? `${Number(v.value).toFixed(1)} years` : null);
  const pct = (v: SourcedValue | null) => (v ? `${Number(v.value).toFixed(1)}%` : null);
  if (!oc) {
    return {
      label: "Payback on the plan as a whole", capital: Number(s.annual.capex.value) > 0 ? crore(s.annual.capex) : null,
      payback: years(s.payback_years) ?? "—", irr: pct(s.irr_pct) ?? "—", assets: [], saving: null,
      basis: "This run was made before payback was worked on the owned capital: it sets the capital of the owned plant against the whole plan's saving. Run the plan again for payback on the owned capital.",
      absent: null,
    };
  }
  const none = oc.assets.length === 0;
  return {
    label: "Payback on the owned capital",
    capital: none ? null : crore(oc.total),
    payback: years(s.payback_years) ?? (none ? "none to state" : "not within the period"),
    irr: pct(s.irr_pct) ?? (none ? "none to state" : "none"),
    assets: oc.assets.map((a) => ({ label: a.label, kind: CAPITAL_KIND[a.kind] ?? a.kind, capex: crore(a.capex) })),
    saving: none ? null : crore(oc.saving),
    basis: oc.basis,
    absent: none ? oc.payback_absent : [oc.payback_absent, oc.irr_absent].filter(Boolean).join(" ") || null,
  };
}
export type LifecycleYear = {
  year: number; bau: SourcedValue; grid: SourcedValue; contracted: SourcedValue; om: SourcedValue; capex: SourcedValue;
  saving: SourcedValue; cash: SourcedValue; cumulative: SourcedValue; pv_cash: SourcedValue; discount: string;
  owned_saving?: SourcedValue | null; // the part of the saving payback and IRR are measured on
};
export type LifecycleOut = {
  analysis_years: SourcedValue; assumptions: Record<string, SourcedValue>; capex: SourcedValue | null; pv_bau: SourcedValue | null; pv_plan: SourcedValue | null;
  npv: SourcedValue | null; nominal_saving: SourcedValue | null; simple_payback_years: SourcedValue | null; discounted_payback_years: SourcedValue | null;
  irr_pct: SourcedValue | null; years: LifecycleYear[]; notes: string[];
};
export type ScenarioSpec = { candidates: CandidateSpec[]; as_of?: string | null; resolution_min: 15 | 60; sensitivities?: { kind: string; candidate?: string; pct: string[] }[] };
export type ScenarioOut = { id: string; site_id: string; connection_id: string; name: string; spec: ScenarioSpec; created_at: string; latest_run: RunOut | null };
export type DispatchOut = { run_id: string; start: string; minutes: number; times: string[]; slots: string[]; series: Record<string, SourcedValue[]> };

export const refsOf = (...v: (SourcedValue | null | undefined)[]): Ref[] => v.flatMap((x) => x?.source_refs ?? []);

export function mw(kw: SourcedValue | null | undefined): string {
  if (!kw) return "—";
  return (Number(kw.value) / 1000).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function mwh(v: SourcedValue | null | undefined): string {
  if (!v) return "—";
  return (Number(v.value) / 1000).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export function crore(v: SourcedValue | null | undefined): string {
  if (!v) return "—";
  return `₹${(Number(v.value) / 1e7).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} cr`;
}

// Three starting scenarios for a site: captive solar; third-party hybrid with a battery; rooftop, solar and a battery.
export function presets(asOf: string, minutes: 15 | 60): { name: string; spec: ScenarioSpec }[] {
  const base = { as_of: asOf, resolution_min: minutes };
  return [
    { name: "Captive solar", spec: { ...base, candidates: [{ kind: "oa_solar_intra", ownership: "captive" }], sensitivities: [{ kind: "price", candidate: "oa_solar_intra", pct: ["-10", "10"] }] } },
    { name: "Hybrid + battery", spec: { ...base, candidates: [{ kind: "oa_hybrid_intra", ownership: "third_party" }, { kind: "btm_bess" }], sensitivities: [{ kind: "price", candidate: "oa_hybrid_intra", pct: ["-10", "10"] }] } },
    { name: "Rooftop + solar + battery", spec: { ...base, candidates: [{ kind: "rooftop_pv" }, { kind: "oa_solar_intra", ownership: "captive" }, { kind: "btm_bess" }], sensitivities: [{ kind: "grid_rate", pct: ["-5", "5"] }] } },
  ];
}
