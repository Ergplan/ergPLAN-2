import type { Ref, SourcedValue } from "./intake";

export type TariffLine = { code: string; label: string; slot: string | null; amount: SourcedValue; quantity: SourcedValue | null; rate: SourcedValue | null };
export type ReplayLine = {
  group: string; label: string; billed: SourcedValue | null; computed: SourcedValue | null; difference: SourcedValue | null;
  status: "match" | "explained" | "unexplained" | "not_billed" | "no_tariff"; explanation: string; gap: string | null; billed_items: string[];
};
export type ReplayOut = {
  bill_id: string; bill_month: string | null; evaluated_on: string; status: string; schedule: string | null; tod: string | null;
  computed_total: SourcedValue | null; billed_total: SourcedValue | null; total_difference: SourcedValue | null; lines: ReplayLine[];
  tariff_lines: TariffLine[]; gaps: Record<string, string>; notes: string[]; file_url: string;
};
export type RateCard = { month: string; slot: string | null; components: SourcedValue[]; kvah_factor: SourcedValue | null; rate: SourcedValue };
export type AvoidedOut = {
  connection_id: string; profile_version: number; profile_provenance: string; year_start: string; tariff_as_of: string;
  matrix: number[][]; cards: RateCard[]; annual: SourcedValue[]; gaps: Record<string, string>; notes: string[];
};

// How the bills came out of the replay. A bill with no approved tariff in force for its supply period
// (`no_tariff`) was never rebuilt: it is counted on its own, never as matching or explained.
export type ReplayTally = { bills: number; rebuilt: number; match: number; explained: number; differs: number; noTariff: number };
export function replayTally(replays: Pick<ReplayOut, "status">[]): ReplayTally {
  const n = (st: string) => replays.filter((r) => r.status === st).length;
  const t = { bills: replays.length, match: n("match"), explained: n("explained"), differs: n("unexplained"), noTariff: n("no_tariff") };
  return { ...t, rebuilt: t.match + t.explained + t.differs };
}
export function replayTallyWords(t: ReplayTally): string {
  const parts = [`${t.rebuilt} of ${t.bills} bill${t.bills === 1 ? "" : "s"} rebuilt from the approved tariff`];
  const how = [t.match && `${t.match} matching`, t.explained && `${t.explained} explained`, t.differs && `${t.differs} differing`].filter(Boolean);
  if (how.length) parts[0] += ` (${how.join(", ")})`;
  if (t.noTariff) parts.push(`${t.noTariff} not rebuilt: no tariff in force for ${t.noTariff === 1 ? "its" : "their"} dates`);
  return `${parts.join("; ")}.`;
}

export function inr(v: string | number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

// Unique refs across values, documents first.
export function uniqueRefs(values: (SourcedValue | null | undefined)[]): Ref[] {
  const seen = new Set<string>();
  const out: Ref[] = [];
  for (const v of values) {
    for (const r of v?.source_refs ?? []) {
      const key = `${r.kind}|${r.document_id ?? ""}|${r.page ?? ""}|${r.bill_id ?? ""}|${r.field_path ?? ""}|${r.note ?? ""}`;
      if (!seen.has(key)) {
        seen.add(key);
        out.push(r);
      }
    }
  }
  const rank = (r: Ref) => (r.kind === "order" || r.kind === "regulation" ? 0 : r.kind === "bill" ? 1 : 2);
  return out.sort((a, b) => rank(a) - rank(b));
}

export function hhmm(block: number): string {
  const m = block * 15;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

export type SlotWaterfall = { slot: string; lines: SourcedValue[]; total: SourcedValue };
export type LandedSource = { id: string; label: string; available: boolean; reason: string | null; by_slot: SlotWaterfall[]; gaps: string[]; notes: string[]; rules_used: string[] };
export type LandedOut = {
  connection_id: string; as_of: string; month: number; slots: { name: string; start: string; end: string }[];
  avoided: { slot: string; rate: SourcedValue }[]; sources: LandedSource[];
  limits: { limit: string; value: SourcedValue; label: string | null }[]; gaps: Record<string, string>;
};
