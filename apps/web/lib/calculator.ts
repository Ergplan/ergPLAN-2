// The landed-cost calculator (Stage 19): the shapes POST /calculator and /calculator/states answer
// (report/calculator.py), and the pure helpers the screen uses.
import type { Sourced } from "@/lib/statebook";

export type NotSourced = { component: string; words: string };

export type CalcSlot = {
  slot: string;
  lines: Sourced[];
  landed: Sourced;
  banking: Sourced | null;
  grid: Sourced | null;
  saving: Sourced | null;
};

export type CalcRoute = {
  route: string;
  label: string;
  note: string;
  source: string;
  allowed: boolean;
  shown: boolean;
  reason: string | null;
  reason_values: Sourced[];
  withheld: string | null;
  slots: CalcSlot[];
  landed_avg: Sourced | null;
  saving_avg: Sourced | null;
  not_sourced: NotSourced[];
  notes: string[];
  inputs: Sourced[];
};

export type Condition = { field: string; text: string; refs: { document_id?: string | null; page?: number | null }[] };

export type Verdict = {
  verdict: string;
  verdict_label: string;
  offered: boolean;
  words: string;
  conditions: Condition[];
  apply_for: Condition[];
  not_stated: Condition[];
  schedule: string | null;
};

export type BookLink = { book_id: string; issue: number; month: string; title: string; file: string };

export type CalculatorOut = {
  state: string;
  state_name: string;
  discom: string;
  discom_name: string;
  category: string;
  rate_block: string | null;
  month: string;
  as_of: string;
  slots: { slot: string; start: string; end: string; hours: string; rate: Sourced | null }[];
  technologies: { tech: string; label: string; routes: CalcRoute[] }[];
  exchange: { market: string; market_label: string; verdict: Verdict; route: CalcRoute };
  gaps: Record<string, string>;
  rulebook: BookLink | null;
};

export type StateCell = {
  route: string;
  tech: string;
  shown: boolean;
  reason: string | null;
  landed_avg: Sourced | null;
  saving_avg: Sourced | null;
  not_sourced: string[];
};

export type StatesOut = {
  month: string;
  iex_market: string;
  rows: {
    state: string;
    state_name: string;
    discom: string;
    discom_name: string;
    category: string | null;
    error: string | null;
    grid_avg: Sourced | null;
    cells: StateCell[];
    exchange_verdict: string | null;
    rulebook: BookLink | null;
  }[];
};

export type Options = {
  as_of: string;
  states: {
    state: string;
    name: string;
    discoms: { discom: string; name: string; categories: { category: string; rate_block: string | null; label: string | null }[]; reference_category: string | null }[];
  }[];
  markets: { market: string; label: string }[];
};

// What the visitor types. Every field is text, as typed; an empty one is left to ergPLAN's benchmark.
export type Plant = { tariff: string; capex: string; cuf: string };
export type Form = {
  discom: string;
  category: string;
  voltage: string;
  cd: string;
  month: string; // YYYY-MM
  connection: "intra_state" | "ists";
  solar: Plant;
  wind: Plant;
  equity: string;
  market: "dam" | "gdam" | "rtm";
};

const num = (s: string): string | null => {
  const t = s.trim().replace(/,/g, "");
  return t !== "" && Number.isFinite(Number(t)) ? t : null;
};

function plant(p: Plant): Record<string, string> {
  const out: Record<string, string> = {};
  const t = num(p.tariff), c = num(p.capex), u = num(p.cuf);
  if (t) out.ppa_tariff_inr_per_kwh = t;
  if (c) out.capex_inr_per_kw = c;
  if (u) out.cuf_pct = u;
  return out;
}

// The request body for POST /calculator (and /calculator/states) from the form: numbers stay strings
// (money is never a float), and what is left empty is not sent, so the API's benchmark applies.
export function bodyOf(f: Form): Record<string, unknown> {
  const body: Record<string, unknown> = {
    discom: f.discom,
    connection: f.connection,
    iex_market: f.market,
    solar: plant(f.solar),
    wind: plant(f.wind),
  };
  if (f.category) body.category = f.category;
  if (num(f.voltage)) body.voltage_kv = num(f.voltage);
  if (num(f.cd)) body.contract_demand_kva = num(f.cd);
  if (/^\d{4}-\d{2}$/.test(f.month)) body.month = `${f.month}-01`;
  if (num(f.equity)) body.equity_share_pct = num(f.equity);
  return body;
}

// The order of the columns: own plant, 26% equity, third party, the exchange.
export const ROUTE_ORDER = ["captive", "group_captive", "third_party", "iex"];

// What a route's column says above its figures.
export function routeStatus(r: CalcRoute): { tone: "ok" | "warn" | "bad"; text: string } {
  if (!r.allowed) return { tone: "bad", text: r.reason ?? "Not allowed by the state's rules" };
  if (!r.shown) return { tone: "warn", text: r.reason ?? "Not yet sourced" };
  if (r.withheld) return { tone: "warn", text: r.withheld };
  return { tone: "ok", text: "" };
}

// The slot a column shows by default: the one with the largest saving, else the first.
export function bestSlot(r: CalcRoute): string | null {
  if (!r.slots.length) return null;
  const withSaving = r.slots.filter((s) => s.saving);
  if (!withSaving.length) return r.slots[0].slot;
  return withSaving.reduce((a, b) => (Number(b.saving!.value) > Number(a.saving!.value) ? b : a)).slot;
}

// A line no approved rule states, in plain words (the API's component names are not for the screen).
const GAP_WORDS: Record<string, string> = {
  electricity_duty: "Electricity duty",
  oa_additional_surcharge: "Additional surcharge",
  oa_css: "Cross-subsidy surcharge",
  oa_wheeling: "Wheeling charge",
  oa_losses: "Distribution loss",
  ists_charges: "Inter-state transmission charge",
  ists_waiver: "Inter-state charge waiver",
  iex_fees: "Exchange fees and trading margin",
  iex_prices: "Exchange prices",
  scheduled_energy: "Capacity charge per kWh (needs the plant's size and CUF)",
  settlement_rule: "Settlement and banking",
  banking: "Banking charge",
  fppca_monthly: "Fuel and power purchase adjustment",
};

export function gapLabel(component: string): string {
  const w = GAP_WORDS[component];
  if (w) return w;
  const s = component.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export const ROUTE_WORDS: Record<string, string> = {
  captive: "Own plant",
  group_captive: "26% equity",
  third_party: "Third party",
  iex: "Exchange",
};
