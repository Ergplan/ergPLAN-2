// The "DISCOM tariffs" screen: a DISCOM's tariff as the rule books hold it (GET /tariffs/{discom}).
// Pure helpers only: words for what a row covers, the version a figure comes from, and where an
// approved figure and the latest draft differ. Nothing here computes a number.
import type { Ref } from "./intake";
import type { Sourced } from "./statebook";

export type TariffFigure = { key: string; label: string; value: Sourced };
export type TariffSlot = { name: string; start: string; end: string; months: number[]; category: string | null; ref: Ref; figures: TariffFigure[] };
export type TariffRow = {
  family: string;
  rule_id: string;
  version_id: string;
  status: "approved" | "draft";
  jurisdiction_id: string;
  label: string | null;
  scope: Record<string, unknown>;
  heading: string | null;
  basis: string | null;
  match_key: string;
  figures: TariffFigure[];
  slots: TariffSlot[];
  points: { text: string; ref: Ref }[];
  notes: string[];
  nearer: string | null;
};
export type TariffVersion = {
  id: string;
  family: string;
  jurisdiction_id: string;
  jurisdiction_name: string;
  version: number;
  status: string;
  origin: string;
  title: string | null;
  effective_from: string;
  effective_to: string | null;
  approved_by: string | null;
  golden_passed: number;
  golden_total: number;
};
export type TariffBook = {
  discom_id: string;
  discom_name: string;
  state_code: string | null;
  chain: { id: string; kind: string; name: string }[];
  on: string;
  drafts: boolean;
  rows: TariffRow[];
  versions: TariffVersion[];
  documents: Record<string, { title: string | null; url_public: string | null; stored: boolean }>;
  not_sourced: string[];
};
export type Jurisdiction = { id: string; kind: string; name: string; state_code: string | null; parent_id: string | null };

export const SECTIONS: { family: string; title: string; lede: string }[] = [
  { family: "tariff_schedule", title: "Retail tariff schedule", lede: "Each consumer category and rate block: its supply voltage, the demand or fixed charge, the energy charge, and the billing terms the order states." },
  { family: "tod_slots", title: "Time-of-day slots", lede: "The hours of each slot, the months it applies in, and the surcharge or rebate on the energy charge." },
  { family: "oa_charge", title: "Open-access charges", lede: "Wheeling, transmission, losses, cross-subsidy and additional surcharges, and other charges on open-access energy." },
  { family: "settlement", title: "Banking and settlement", lede: "How open-access energy is set off and banked, and what banking costs." },
];

export const ORIGIN_WORDS: Record<string, string> = {
  manual: "entered by an analyst",
  yaml: "imported from a file",
  duplicate: "copied from an earlier version",
  regreader_direct: "read from the order by the regulation reader",
  tariff_oder: "imported from the tariff-order pipeline",
  register: "from the State Regulation Master",
  arr_network: "from the ARR Network",
};

export const COMPONENT_WORDS: Record<string, string> = {
  ists_transmission: "Inter-state transmission charge",
  ists_losses: "Inter-state transmission loss",
  stu_transmission: "Intra-state transmission charge",
  stu_losses: "Intra-state transmission loss",
  wheeling: "Wheeling charge",
  wheeling_losses: "Wheeling (distribution) loss",
  cross_subsidy_surcharge: "Cross-subsidy surcharge",
  additional_surcharge: "Additional surcharge",
  sldc_operating: "SLDC operating charge",
  rldc_operating: "RLDC operating charge",
  sldc_scheduling: "SLDC scheduling charge",
  banking_charge: "Banking charge",
  electricity_duty_oa: "Electricity duty on open access",
  dsm_passthrough: "Deviation settlement pass-through",
  standby_charge: "Standby charge",
  grid_support_charge: "Grid support charge",
  exchange_fee: "Exchange fee",
  trading_margin: "Trading margin",
  green_tariff_premium: "Green tariff premium",
  reactive_energy_charge: "Reactive energy charge",
  other: "Other charge",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

export function dateWords(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return y && m && d ? `${d} ${FULL_MONTHS[m - 1]} ${y}` : iso;
}

// "in force from 1 April 2026" or "1 April 2026 to 31 March 2027"
export function windowWords(from: string, to: string | null): string {
  return to ? `${dateWords(from)} to ${dateWords(to)}` : `in force from ${dateWords(from)}`;
}

export function monthsWords(months: number[] | undefined | null): string {
  if (!months || months.length === 0 || months.length === 12) return "all year";
  return months.map((m) => MONTHS[m - 1] ?? String(m)).join(", ");
}

function band(b: { min?: number; max?: number; min_exclusive?: boolean; max_exclusive?: boolean }, unit: string): string {
  if (b.min !== undefined && b.max !== undefined) return b.min === b.max ? `${b.min} ${unit}` : `${b.min_exclusive ? "above" : "from"} ${b.min} ${b.max_exclusive ? "to below" : "up to"} ${b.max} ${unit}`;
  if (b.min !== undefined) return `${b.min_exclusive ? "above" : "at or above"} ${b.min} ${unit}`;
  if (b.max !== undefined) return `${b.max_exclusive ? "below" : "up to"} ${b.max} ${unit}`;
  return `any ${unit}`;
}

const SCOPE_WORDS: Record<string, string> = {
  ists: "inter-state", intra_state: "within the state", behind_meter: "behind the meter",
  captive: "captive", group_captive: "group captive", third_party: "third party", discom: "DISCOM",
  ht: "HT", eht: "EHV", lt: "LT", any_re: "any renewable", any: "any source", small_hydro: "small hydro",
  waste_to_energy: "waste to energy", bess: "battery storage", pumped_storage: "pumped storage",
};
const word = (v: unknown) => SCOPE_WORDS[String(v)] ?? String(v).replaceAll("_", " ");

// What a row covers, in words: one phrase per scope field the row states.
export function scopeWords(scope: Record<string, unknown>): string[] {
  const out: string[] = [];
  const s = scope as Record<string, never>;
  if (s.category) out.push(`category ${(s.category as string[]).join(" or ")}`);
  if (s.rate_block) out.push(`rate block ${String(s.rate_block)}`);
  if (s.voltage_kv) out.push(`supply ${band(s.voltage_kv, "kV")}`);
  if (s.contract_demand_kva) out.push(`contract demand ${band(s.contract_demand_kva, "kVA")}`);
  if (s.load_kw) out.push(`load ${band(s.load_kw, "kW")}`);
  if (s.capacity_mw) out.push(`plant ${band(s.capacity_mw, "MW")}`);
  if (s.consumer_kind) out.push(`${(s.consumer_kind as string[]).map(word).join(" or ")} consumers`);
  if (s.connection) out.push(`connected ${(s.connection as string[]).map(word).join(" or ")}`);
  if (s.source) out.push(`from ${(s.source as string[]).map(word).join(", ")}`);
  if (s.ownership) out.push(`${(s.ownership as string[]).map(word).join(" or ")} supply`);
  if (s.iex_segment) out.push(`exchange market ${(s.iex_segment as string[]).map((x) => String(x).toUpperCase()).join(", ")}`);
  if (s.season) out.push(`season ${String(s.season)}`);
  if (s.months) out.push(monthsWords(s.months as number[]));
  if (s.commissioned) {
    const c = s.commissioned as { from?: string; to?: string };
    out.push(`plants commissioned ${c.from ? `from ${dateWords(c.from)}` : ""}${c.from && c.to ? " " : ""}${c.to ? `to ${dateWords(c.to)}` : ""}`.trim());
  }
  return out;
}

export function rowTitle(r: TariffRow): string {
  if (r.family === "oa_charge" && r.heading) return COMPONENT_WORDS[r.heading] ?? r.heading.replaceAll("_", " ");
  return r.label ?? r.heading ?? "Rule row";
}

export function basisWords(r: TariffRow): string | null {
  if (r.family !== "tariff_schedule" || !r.basis) return null;
  return r.basis === "kvah" ? "billed per kVAh" : "billed per kWh";
}

export function versionWords(v: TariffVersion | undefined): string {
  if (!v) return "version not found";
  const parts = [`version ${v.version}`, v.status === "draft" ? "draft" : v.status, ORIGIN_WORDS[v.origin] ?? v.origin.replaceAll("_", " ")];
  if (v.golden_total > 0) parts.push(`golden tests ${v.golden_passed} of ${v.golden_total} passed`);
  else parts.push("no golden tests for this family yet");
  return parts.join(" · ");
}

// Where a figure's source can be opened: the stored page; a cited page whose PDF this server lacks;
// the register (cited by name, no stored page); or nothing (not yet sourced).
export function sourceWords(refs: Ref[], documents: TariffBook["documents"]): "page" | "unstored" | "register" | "not yet sourced" {
  const real = refs.filter((r) => r.kind !== "derived");
  if (real.length === 0 || real.some((r) => r.kind === "assumption")) return "not yet sourced";
  if (real.some((r) => r.kind === "register")) return "register";
  if (real.every((r) => (r.kind === "order" || r.kind === "regulation") && r.document_id && r.page)) {
    return real.some((r) => documents[r.document_id ?? ""]?.stored === false) ? "unstored" : "page";
  }
  return "not yet sourced";
}

export const SOURCE_LABEL: Record<ReturnType<typeof sourceWords>, string> = {
  page: "opens its page",
  unstored: "page cited, PDF not on this server",
  register: "register, no stored page",
  "not yet sourced": "not yet sourced",
};

// A copy of the book in which every order or regulation ref names its document (title, the
// regulator's copy) and, where the PDF is not stored here, says so, so the figure does not link.
export function nameDocuments(book: TariffBook): TariffBook {
  const walk = (n: unknown): unknown => {
    if (Array.isArray(n)) return n.map(walk);
    if (n === null || typeof n !== "object") return n;
    const o = n as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(o)) out[k] = k === "documents" ? v : walk(v);
    const doc = typeof o.document_id === "string" ? book.documents[o.document_id] : undefined;
    if ((o.kind === "order" || o.kind === "regulation") && doc) {
      if (doc.title) out.document_title = doc.title;
      if (doc.url_public) out.url_public = doc.url_public;
      if (!doc.stored) out.document_stored = false;
    }
    return out;
  };
  return walk(book) as TariffBook;
}

// A family's rows: approved ones in the API's order, each draft placed right after the approved row it
// restates (a draft with no approved twin goes last).
export function rowsOf(book: TariffBook, family: string, drafts: boolean): TariffRow[] {
  const mine = book.rows.filter((r) => r.family === family);
  const approved = mine.filter((r) => r.status === "approved");
  if (!drafts) return approved;
  const pending = mine.filter((r) => r.status === "draft");
  const out: TariffRow[] = [];
  for (const a of approved) {
    out.push(a);
    for (const d of pending.filter((x) => x.match_key === a.match_key && !out.includes(x))) out.push(d);
  }
  return [...out, ...pending.filter((d) => !out.includes(d))];
}

export type Difference = { label: string; approved: Sourced | null; draft: Sourced | null };

function allFigures(r: TariffRow): Map<string, TariffFigure> {
  const m = new Map<string, TariffFigure>();
  for (const f of r.figures) m.set(f.key, f);
  for (const s of r.slots) for (const f of s.figures) m.set(f.key, { ...f, label: `${s.name}: ${f.label}` });
  return m;
}

// The approved row a draft row restates (same family, heading and scope), if any.
export function twinOf(book: TariffBook, draft: TariffRow): TariffRow | null {
  return book.rows.find((r) => r.status === "approved" && r.match_key === draft.match_key && !r.nearer) ?? book.rows.find((r) => r.status === "approved" && r.match_key === draft.match_key) ?? null;
}

// Where a draft row and the approved row it restates differ: a figure only one states, or the same
// figure with a different value or unit ("300.00" and "300" are the same value).
export function differences(approved: TariffRow | null, draft: TariffRow): Difference[] {
  const a = approved ? allFigures(approved) : new Map<string, TariffFigure>();
  const d = allFigures(draft);
  const out: Difference[] = [];
  for (const key of new Set([...a.keys(), ...d.keys()])) {
    const x = a.get(key);
    const y = d.get(key);
    if (x && y && Number(x.value.value) === Number(y.value.value) && x.value.unit === y.value.unit) continue;
    out.push({ label: (y ?? x)!.label, approved: x?.value ?? null, draft: y?.value ?? null });
  }
  if (approved) {
    const hours = (r: TariffRow) => r.slots.map((s) => `${s.name} ${s.start}-${s.end} ${monthsWords(s.months)}`).join("; ");
    if (hours(approved) !== hours(draft)) out.push({ label: `slot hours: approved ${hours(approved) || "none"}; draft ${hours(draft) || "none"}`, approved: null, draft: null });
  }
  return out;
}

// The DISCOMs of a state, by name, from /rulebooks/jurisdictions.
export function discomsOf(all: Jurisdiction[], state: string): Jurisdiction[] {
  return all.filter((j) => j.kind === "discom" && j.state_code === state).sort((a, b) => a.name.localeCompare(b.name));
}

export function statesWithDiscoms(all: Jurisdiction[]): Jurisdiction[] {
  const have = new Set(all.filter((j) => j.kind === "discom").map((j) => j.state_code));
  return all.filter((j) => j.kind === "state" && have.has(j.state_code)).sort((a, b) => a.name.localeCompare(b.name));
}

export function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
