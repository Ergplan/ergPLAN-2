import { describe, expect, it } from "vitest";
import { markUnstored } from "../lib/statebook";

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
