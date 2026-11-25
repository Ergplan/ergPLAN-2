import { describe, expect, it } from "vitest";
import { hhmm, inr, replayTally, replayTallyWords, uniqueRefs } from "../lib/cost";

describe("cost helpers", () => {
  it("formats rupees in the Indian grouping", () => {
    expect(inr("1402050", 2)).toBe("₹14,02,050.00");
    expect(inr(null)).toBe("—");
  });
  it("names 15-minute blocks", () => {
    expect(hhmm(0)).toBe("00:00");
    expect(hhmm(81)).toBe("20:15");
  });
  it("puts document citations first and drops duplicates", () => {
    const order = { kind: "order" as const, document_id: "d", page: 100, quote: "q" };
    const bill = { kind: "bill" as const, bill_id: "b", field_path: "energy.kvah" };
    const refs = uniqueRefs([
      { value: "1", unit: "INR", source_refs: [bill, order] },
      { value: "2", unit: "INR", source_refs: [order] },
    ]);
    expect(refs.map((r) => r.kind)).toEqual(["order", "bill"]);
  });
  it("never counts a bill with no tariff in force as rebuilt, matching or explained", () => {
    // the Uttar Pradesh full test: eight of twelve bills predate the only approved order
    const st = [...Array(8).fill("no_tariff"), "explained", "explained", "match", "unexplained"].map((status) => ({ status }));
    const t = replayTally(st);
    expect(t).toEqual({ bills: 12, rebuilt: 4, match: 1, explained: 2, differs: 1, noTariff: 8 });
    expect(replayTallyWords(t)).toBe(
      "4 of 12 bills rebuilt from the approved tariff (1 matching, 2 explained, 1 differing); 8 not rebuilt: no tariff in force for their dates.",
    );
    expect(replayTallyWords(replayTally([{ status: "match" }]))).toBe("1 of 1 bill rebuilt from the approved tariff (1 matching).");
  });
});
