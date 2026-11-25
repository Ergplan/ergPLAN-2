import { describe, expect, it } from "vitest";
import { qty } from "../lib/format";
import { markUnstored, whoFor } from "../lib/statebook";

describe("markUnstored", () => {
  const content = {
    grid: [
      { slot: "Peak", rate: { value: "9.10", unit: "INR/kWh", source_refs: [
        { kind: "order", document_id: "doc-here", page: 12, quote: "a" },
        { kind: "order", document_id: "doc-gone", page: 40, quote: "b" },
        { kind: "register", note: "cited by name" },
      ] } },
    ],
    settlement: [{ points: [{ text: "x", ref: { kind: "regulation", document_id: "doc-gone", page: 3, quote: "c" } }] }],
  };

  it("marks only the order and regulation refs whose document is missing", () => {
    const out = markUnstored(content, ["doc-gone"]);
    const refs = out.grid[0].rate.source_refs as { document_stored?: boolean }[];
    expect(refs.map((r) => r.document_stored)).toEqual([undefined, false, undefined]);
    expect((out.settlement[0].points[0].ref as { document_stored?: boolean }).document_stored).toBe(false);
  });

  it("leaves the book as it is when nothing is missing, and never changes the original", () => {
    expect(markUnstored(content, [])).toBe(content);
    markUnstored(content, ["doc-gone"]);
    expect(JSON.stringify(content)).not.toContain("document_stored");
  });
});

describe("quantities in a rule book", () => {
  it("prints no trailing zeros and groups the Indian way", () => {
    expect(qty("33.000")).toBe("33");
    expect(qty("5000.000")).toBe("5,000");
    expect(qty("150000.000")).toBe("1,50,000");
    expect(qty("0.400")).toBe("0.4");
    expect(qty(null)).toBe("?");
  });
  it("names the reference consumer as 33 kV and 5,000 kVA, not 33.000 and 5000.000", () => {
    const c = { kind: "reference", client: null, site: null, category: "HV-2", rate_block: null, voltage_kv: "33.000", contract_demand_kva: "5000.000" };
    expect(whoFor(c)).toBe("Reference consumer · HV-2 · 33 kV · 5,000 kVA");
  });
});
