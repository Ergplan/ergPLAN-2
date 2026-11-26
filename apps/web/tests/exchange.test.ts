import { describe, expect, it } from "vitest";
import { exchangeBlocks, type ExchangeVerdict } from "../lib/exchange";
import type { SourcedValue } from "../lib/intake";

const sv = (value: string, unit = "INR"): SourcedValue => ({ value, unit, source_refs: [{ kind: "assumption" }] }) as SourcedValue;
const ref = { kind: "regulation" as const, document_id: "doc", page: 40, quote: "q" };
const base: ExchangeVerdict = {
  kind: "iex_dam", label: "IEX day-ahead (DAM)", state: "Maharashtra", verdict: "feasible_with_conditions",
  verdict_label: "Feasible with conditions", offered: true, words: "Buying on the power exchange is feasible in Maharashtra, with conditions.",
  reasons: [], conditions: [{ field: "banking", text: "Exchange power cannot be banked.", refs: [ref] }],
  apply_for: [{ field: "sldc_clearance", text: "A standing clearance from the state load despatch centre, valid for 90 days.", refs: [ref] }],
  not_stated: [{ field: "lock_in", text: "The stored regulations do not state any minimum period or lock-in.", refs: [] }],
  schedule: "one flat block over the day", rule_id: "r", version_id: "v",
};

describe("exchange verdicts on a result", () => {
  it("an offered source says when it lowers the cost, the schedule and the deviation risk priced", () => {
    const [b] = exchangeBlocks({
      exchange: [base],
      sizes: [{ kind: "iex_dam", exchange: { words: "The exchange lowers the cost in 11 months of the year.", deviation: { cost: sv("11450540.25"), assumed_pct: sv("5", "percent"), refs: [ref] } } }],
    });
    expect(b.chip).toBe("Feasible with conditions");
    expect(b.tone).toBe("warning");
    expect(b.when).toContain("11 months");
    expect(b.schedule).toBe("Scheduled one flat block over the day, as the rule requires.");
    expect(b.deviation).toBe("Deviation risk in the cost: ₹1,14,50,540 a year, taking your drawal to differ from the schedule by 5% each way (an assumption).");
    expect(b.applyFor[0].text).toContain("standing clearance");
    expect(b.conditions[0].refs[0].page).toBe(40);
    expect(b.notStated).toHaveLength(1);
  });

  it("a source the rule bars is not sized: it says why with the rule's page, and nothing about when", () => {
    const barred: ExchangeVerdict = {
      ...base, verdict: "not_practical", verdict_label: "Not practical", offered: false,
      words: "Buying on the power exchange is not practical in Rajasthan: the schedule must be given before the results are known.",
      reasons: [{ field: "barriers", text: "The schedule must be given before the results are known.", refs: [ref] }],
    };
    const [b] = exchangeBlocks({ exchange: [barred], sizes: [] });
    expect(b.tone).toBe("muted");
    expect(b.when).toBeNull();
    expect(b.schedule).toBeNull();
    expect(b.deviation).toBeNull();
    expect(b.reasons[0].refs[0].page).toBe(40);
  });

  it("with no approved rule it reads not yet sourced, and a result with no exchange source shows nothing", () => {
    const none: ExchangeVerdict = { ...base, verdict: "not_yet_sourced", verdict_label: "Not yet sourced", offered: false, conditions: [], apply_for: [], not_stated: [], schedule: null, rule_id: null, version_id: null };
    expect(exchangeBlocks({ exchange: [none], sizes: [] })[0].chip).toBe("Not yet sourced");
    expect(exchangeBlocks({ sizes: [] })).toEqual([]);
    expect(exchangeBlocks({ exchange: null, sizes: [] })).toEqual([]);
  });
});
