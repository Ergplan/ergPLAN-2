// Exchange purchase (Stage 18): the state's verdict for each exchange source a plan asked for, as the
// run's summary carries it, turned into the lines a results page or a plan shows. The verdict and
// every condition come from the API (approved rules only); nothing is decided here.
import type { Ref, SourcedValue } from "./intake";

export type ExchangeCondition = { field: string; text: string; refs: Ref[] };
export type ExchangeVerdict = {
  kind: string | null; label: string | null; state: string;
  verdict: "feasible" | "feasible_with_conditions" | "not_practical" | "not_yet_sourced";
  verdict_label: string; offered: boolean; words: string;
  reasons: ExchangeCondition[]; conditions: ExchangeCondition[]; apply_for: ExchangeCondition[];
  not_stated: ExchangeCondition[]; schedule: string | null; rule_id: string | null; version_id: string | null;
};
export type ExchangeMonth = { month: number; name: string; kwh: SourcedValue; avg_landed: SourcedValue; avg_grid: SourcedValue; saved: SourcedValue };
export type ExchangeDetail = {
  words?: string | null; by_month?: ExchangeMonth[]; by_hour_kwh?: SourcedValue[];
  verdict?: ExchangeVerdict | null;
  deviation?: { cost: SourcedValue; assumed_pct: SourcedValue; refs: Ref[] } | null;
};
type Size = { kind: string; exchange?: ExchangeDetail | null };

export type ExchangeBlock = {
  kind: string; title: string; chip: string; tone: "ok" | "warning" | "muted";
  words: string;           // the verdict in a sentence
  when: string | null;     // offered: when the exchange lowered the cost
  schedule: string | null; // offered: the schedule the rule requires, in words
  deviation: string | null; // offered: the deviation risk priced, in words
  reasons: ExchangeCondition[]; applyFor: ExchangeCondition[]; conditions: ExchangeCondition[]; notStated: ExchangeCondition[];
};

const inr = (v: string) => `₹${Math.round(Number(v)).toLocaleString("en-IN")}`;

export function exchangeBlocks(summary: { exchange?: ExchangeVerdict[] | null; sizes: Size[] }): ExchangeBlock[] {
  return (summary.exchange ?? []).map((v) => {
    const size = summary.sizes.find((z) => z.kind === v.kind);
    const d = size?.exchange ?? null;
    const dev = d?.deviation;
    return {
      kind: v.kind ?? "",
      title: v.label ?? "Power exchange",
      chip: v.verdict_label,
      tone: v.verdict === "feasible" ? "ok" : v.offered ? "warning" : "muted",
      words: v.words,
      when: v.offered ? d?.words ?? null : null,
      schedule: v.offered && v.schedule ? `Scheduled ${v.schedule}, as the rule requires.` : null,
      deviation: v.offered && dev
        ? `Deviation risk in the cost: ${inr(dev.cost.value)} a year, taking your drawal to differ from the schedule by ${Number(dev.assumed_pct.value)}% each way (an assumption).`
        : null,
      reasons: v.reasons, applyFor: v.apply_for, conditions: v.conditions, notStated: v.not_stated,
    };
  });
}
