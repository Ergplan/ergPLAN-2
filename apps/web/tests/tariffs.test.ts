import { describe, expect, it } from "vitest";
import {
  differences,
  discomsOf,
  monthsWords,
  nameDocuments,
  rowsOf,
  rowTitle,
  scopeWords,
  sourceWords,
  statesWithDiscoms,
  twinOf,
  versionWords,
  windowWords,
  type TariffBook,
  type TariffRow,
} from "../lib/tariffs";

const order = (doc: string, page: number) => ({ kind: "order" as const, document_id: doc, page, quote: "q" });
const fig = (key: string, value: string, unit = "INR/kVAh", refs = [order("doc-here", 100)]) => ({ key, label: key, value: { value, unit, source_refs: refs } });

function row(over: Partial<TariffRow>): TariffRow {
  return {
    family: "tariff_schedule", rule_id: "r1", version_id: "v1", status: "approved", jurisdiction_id: "msedcl", label: "HT I · EHV",
    scope: {}, heading: "HT I (A)", basis: "kvah", match_key: "k1", figures: [], slots: [], points: [], notes: [], nearer: null, ...over,
  };
}

const book = (rows: TariffRow[]): TariffBook => ({
  discom_id: "msedcl", discom_name: "MSEDCL", state_code: "MH", chain: [], on: "2026-11-26", drafts: true, rows, versions: [],
  documents: { "doc-here": { title: "Case 75/2025", url_public: "https://merc.example/order.pdf", stored: true }, "doc-gone": { title: null, url_public: null, stored: false } },
  not_sourced: [],
});

describe("scopeWords", () => {
  it("says what a row covers in plain words", () => {
    expect(scopeWords({ category: ["HT I (A)", "HT I (A) (i)"], voltage_kv: { min: 66 } })).toEqual(["category HT I (A) or HT I (A) (i)", "supply at or above 66 kV"]);
    expect(scopeWords({ voltage_kv: { min: 11, max: 66, min_exclusive: true } })).toEqual(["supply above 11 up to 66 kV"]);
    expect(scopeWords({ connection: ["intra_state", "ists"], source: ["any_re"], ownership: ["third_party"] })).toEqual([
      "connected within the state or inter-state", "from any renewable", "third party supply",
    ]);
    expect(scopeWords({ months: [4, 5, 6] })).toEqual(["Apr, May, Jun"]);
    expect(scopeWords({})).toEqual([]);
  });
  it("names months, windows and versions", () => {
    expect(monthsWords([])).toBe("all year");
    expect(monthsWords([10, 11])).toBe("Oct, Nov");
    expect(windowWords("2026-04-01", null)).toBe("in force from 1 April 2026");
    expect(windowWords("2026-04-01", "2027-03-31")).toBe("1 April 2026 to 31 March 2027");
    const v = { id: "v", family: "tariff_schedule", jurisdiction_id: "msedcl", jurisdiction_name: "MSEDCL", version: 3, status: "draft", origin: "arr_network", title: null, effective_from: "2026-04-01", effective_to: null, approved_by: null, golden_passed: 0, golden_total: 8 };
    expect(versionWords(v)).toBe("version 3 · draft · from the ARR Network · golden tests 0 of 8 passed");
    expect(versionWords({ ...v, golden_total: 0 })).toContain("no golden tests");
    expect(rowTitle(row({ family: "oa_charge", heading: "cross_subsidy_surcharge" }))).toBe("Cross-subsidy surcharge");
  });
});

describe("sourceWords", () => {
  const docs = book([]).documents;
  it("tells a stored page, an unstored one, the register and nothing apart", () => {
    expect(sourceWords([order("doc-here", 3)], docs)).toBe("page");
    expect(sourceWords([order("doc-gone", 3)], docs)).toBe("unstored");
    expect(sourceWords([{ kind: "register", note: "master" }], docs)).toBe("register");
    expect(sourceWords([{ kind: "assumption", note: "x" }], docs)).toBe("not yet sourced");
    expect(sourceWords([], docs)).toBe("not yet sourced");
  });
  it("names documents and marks unstored ones", () => {
    const b = nameDocuments(book([row({ figures: [fig("/energy_charge/base", "8.44"), fig("/x", "1", "INR/kWh", [order("doc-gone", 9)])] })]));
    const [a, g] = b.rows[0].figures.map((f) => f.value.source_refs[0]);
    expect(a.document_title).toBe("Case 75/2025");
    expect(a.url_public).toBe("https://merc.example/order.pdf");
    expect(a.document_stored).toBeUndefined();
    expect(g.document_stored).toBe(false);
    expect(b.documents).toEqual(book([]).documents);
  });
});

describe("drafts beside the approved rows", () => {
  const approved = row({ figures: [fig("/demand_charge/value", "650.00", "INR/kVA/month"), fig("/energy_charge/base", "8.44")] });
  const draft = row({ rule_id: "r2", version_id: "v2", status: "draft", figures: [fig("/demand_charge/value", "650", "INR/kVA/month"), fig("/energy_charge/base", "999.99"), fig("/fppca_inr_per_unit", "0.2")] });
  it("hides drafts unless asked", () => {
    const b = book([approved, draft]);
    expect(rowsOf(b, "tariff_schedule", false)).toEqual([approved]);
    expect(rowsOf(b, "tariff_schedule", true)).toHaveLength(2);
  });
  it("puts each draft right after the approved row it restates", () => {
    const other = row({ rule_id: "r3", match_key: "k2" });
    const lone = row({ rule_id: "r4", status: "draft", match_key: "k9" });
    const order = rowsOf(book([approved, other, lone, draft]), "tariff_schedule", true).map((r) => r.rule_id);
    expect(order).toEqual(["r1", "r2", "r3", "r4"]);
  });
  it("lists only the figures that differ", () => {
    const b = book([approved, draft]);
    expect(twinOf(b, draft)).toBe(approved);
    const d = differences(twinOf(b, draft), draft);
    expect(d.map((x) => [x.label, x.approved?.value ?? null, x.draft?.value ?? null])).toEqual([
      ["/energy_charge/base", "8.44", "999.99"],
      ["/fppca_inr_per_unit", null, "0.2"],
    ]);
    expect(twinOf(book([draft]), draft)).toBeNull();
    expect(differences(null, draft)).toHaveLength(3);
  });
  it("compares ToD slots by name and hours", () => {
    const slot = (name: string, start: string, figures = [fig(`${name}:surcharge_pct`, "25", "percent")]) => ({ name, start, end: "24:00", months: [], category: null, ref: order("doc-here", 77), figures });
    const a = row({ family: "tod_slots", slots: [slot("peak", "17:00")] });
    const d = row({ family: "tod_slots", status: "draft", slots: [slot("peak", "18:00", [fig("peak:surcharge_pct", "20", "percent")])] });
    const out = differences(a, d);
    expect(out[0].label).toBe("peak: peak:surcharge_pct");
    expect(out[1].label).toContain("slot hours");
  });
});

describe("the picker", () => {
  const js = [
    { id: "MH", kind: "state", name: "Maharashtra", state_code: "MH", parent_id: null },
    { id: "KL", kind: "state", name: "Kerala", state_code: "KL", parent_id: null },
    { id: "tpcd", kind: "discom", name: "Tata Power", state_code: "MH", parent_id: "MH" },
    { id: "aeml", kind: "discom", name: "Adani Electricity", state_code: "MH", parent_id: "MH" },
    { id: "MERC", kind: "regulator", name: "MERC", state_code: "MH", parent_id: null },
  ];
  it("offers states that have DISCOMs and their DISCOMs by name", () => {
    expect(statesWithDiscoms(js).map((s) => s.id)).toEqual(["MH"]);
    expect(discomsOf(js, "MH").map((d) => d.id)).toEqual(["aeml", "tpcd"]);
  });
});
