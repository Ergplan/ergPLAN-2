// Generation profiles supplied for a site (Stage 15): the developer's or the plant's own year of
// solar or wind generation, used in place of the library curve. Shapes of /sites/{id}/generation-profiles.
import type { Ref } from "./intake";

export type Sourced = { value: string; unit: string; source_refs: Ref[]; label?: string | null };

export type RunUse = { run_id: string; scenario_id: string; scenario_name: string; status: string; created_at: string; replay_of: string | null };

export type GenerationProfile = {
  id: string;
  site_id: string;
  version: number;
  technology: "solar" | "wind";
  applies_to: "open_access" | "rooftop";
  provenance: "measured" | "stated";
  filename: string;
  sha256: string;
  resolution_min: 15 | 60;
  first_day: string;
  blocks: number;
  unit_in_file: string;
  basis: "per_mw" | "capacity" | "per_unit";
  capacity_mw: string | null;
  in_use: boolean;
  withdrawn_at: string | null;
  created_by: string;
  created_at: string;
  annual_cuf: Sourced;
  peak: Sourced;
  monthly_cuf: { month: number; cuf: Sourced }[];
  library_cuf: Sourced | null;
  runs: RunUse[];
};

export type UploadForm = {
  technology: "solar" | "wind";
  appliesTo: "open_access" | "rooftop";
  provenance: "measured" | "stated";
  basis: "" | "per_mw" | "capacity";
  capacityMw: string;
  firstDay: string;
};

export const EMPTY_FORM: UploadForm = { technology: "solar", appliesTo: "open_access", provenance: "stated", basis: "", capacityMw: "", firstDay: "" };

// What a file must be, said before the upload so a refusal is no surprise.
export const ACCEPTED: string[] = [
  "A CSV file with a header row and one row per period for a whole year: 8,760 hourly values (8,784 when the year holds 29 February) or 35,040 15-minute values (35,136).",
  "One column of generation whose header names its unit: kW or MW (average power in the period), kWh or MWh (energy in the period), CUF or pu (a fraction of capacity, 0 to 1) or CUF % (0 to 100).",
  "A timestamp column in Indian Standard Time, starting at midnight. Without one, give the first day of the year the file covers.",
  "Every period present, once and in order. No blank, negative or non-numeric value; nothing is filled in for you.",
  "No value above the capacity; an annual CUF between 10% and 35% for solar, 10% and 55% for wind; no solar generation between 20:00 and 04:00.",
];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function monthName(m: number): string {
  return MONTHS[m - 1] ?? String(m);
}

export function technologyWords(p: Pick<GenerationProfile, "technology" | "applies_to">): string {
  if (p.applies_to === "rooftop") return "Rooftop solar";
  return p.technology === "solar" ? "Open-access solar" : "Open-access wind";
}

export function provenanceWords(p: Pick<GenerationProfile, "provenance">): string {
  return p.provenance === "measured" ? "Measured at the plant" : "Stated by the developer";
}

export function resolutionWords(p: Pick<GenerationProfile, "resolution_min" | "first_day">): string {
  return `${p.resolution_min === 60 ? "Hourly" : "15-minute"}, one year from ${p.first_day}`;
}

export function basisWords(p: Pick<GenerationProfile, "basis" | "capacity_mw" | "unit_in_file">): string {
  if (p.basis === "per_mw") return `${p.unit_in_file} for 1 MW of capacity`;
  if (p.basis === "capacity") return `${p.unit_in_file} for a ${p.capacity_mw} MW plant`;
  return p.unit_in_file === "CUF %" ? "Percent of capacity" : "Fraction of capacity";
}

// Whether new plans generate with the profile, in words (never the raw flags).
export function useWords(p: Pick<GenerationProfile, "in_use" | "withdrawn_at">): string {
  if (p.in_use) return "Used in new plans";
  return p.withdrawn_at ? "Withdrawn" : "Replaced by a later file";
}

const RUN_STATUS: Record<string, string> = { queued: "waiting", running: "running", done: "done", failed: "failed", failed_resettle: "failed its settlement check" };

export function runWords(r: RunUse): string {
  const when = r.created_at.slice(0, 10);
  return `${r.scenario_name}${r.replay_of ? " (replay)" : ""}, ${when}, ${RUN_STATUS[r.status] ?? r.status}`;
}

export function pct(v: Sourced | null | undefined, places = 1): string {
  if (!v) return "—";
  const n = Number(v.value);
  return Number.isFinite(n) ? `${n.toFixed(places)}%` : v.value;
}

// The supplied profile against the library curve it replaces, in percentage points.
export function againstLibrary(p: Pick<GenerationProfile, "annual_cuf" | "library_cuf">): string | null {
  if (!p.library_cuf) return null;
  const d = Number(p.annual_cuf.value) - Number(p.library_cuf.value);
  if (!Number.isFinite(d)) return null;
  const lib = pct(p.library_cuf);
  if (Math.abs(d) < 0.05) return `the same as the library curve (${lib}, assumed)`;
  return `${Math.abs(d).toFixed(1)} points ${d > 0 ? "above" : "below"} the library curve (${lib}, assumed)`;
}

// What is still missing before the file can be sent; null when the form is complete.
export function formProblem(f: UploadForm, hasFile: boolean): string | null {
  if (!hasFile) return "Choose the CSV file.";
  if (f.appliesTo === "rooftop" && f.technology !== "solar") return "A rooftop profile is a solar profile.";
  if (f.basis === "capacity") {
    const c = Number(f.capacityMw);
    if (!f.capacityMw.trim() || !Number.isFinite(c) || c <= 0) return "State the plant's capacity in MW.";
  }
  return null;
}

export function toFormData(f: UploadForm, file: File): FormData {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("technology", f.technology);
  fd.append("provenance", f.provenance);
  fd.append("applies_to", f.appliesTo);
  if (f.basis) fd.append("basis", f.basis);
  if (f.basis === "capacity" && f.capacityMw.trim()) fd.append("capacity_mw", f.capacityMw.trim());
  if (f.firstDay) fd.append("first_day", f.firstDay);
  return fd;
}
