import { describe, expect, it } from "vitest";
import { crore, levyRows, type LevyOut, mw, mwh, presets } from "../lib/runs";

const sv = (value: string) => ({ value, unit: "kW", source_refs: [{ kind: "derived" }] }) as never;

describe("step 5 helpers", () => {
  it("formats kW as MW and INR as crore", () => {
    expect(mw(sv("10730.112"))).toBe("10.73");
    expect(crore(sv("31707367.64"))).toBe("₹3.17 cr");
    expect(mw(null)).toBe("—");
    expect(mwh(sv("16810280.5"))).toBe("16,810");
  });
  it("offers three scenarios at the chosen resolution", () => {
    const p = presets("2026-09-01", 15);
    expect(p.map((x) => x.name)).toEqual(["Captive solar", "Hybrid + battery", "Rooftop + solar + battery"]);
    expect(p.every((x) => x.spec.resolution_min === 15 && x.spec.as_of === "2026-09-01")).toBe(true);
  });
  it("says in words how FPPCA and duty are treated on units not bought from the DISCOM", () => {
    const v = (value: string, unit = "INR") => ({ value, unit, source_refs: [{ kind: "bill" }] }) as never;
    const levy = (code: string, charged: boolean, rate: string): LevyOut => ({
      code, label: code === "fppca" ? "FPPCA" : "Electricity duty", bills: 12, rates: [v(rate, "INR/kVAh")], on_discom_kwh: v("0.19", "INR/kWh"),
      on_other_kwh: charged ? v("0.52", "INR/kWh") : null, charged_on_other_units: charged, treatment: v(charged ? "100" : "0", "percent"),
      treatment_words: charged ? "Electricity duty is assumed charged. Not sourced to a regulation." : "FPPCA is assumed not charged. Not sourced to a regulation.",
      sourced_to_regulation: false, discom_kwh_before: v("100", "kWh"), discom_kwh_after: v("40", "kWh"), other_kwh: v(charged ? "60" : "0", "kWh"),
      before: v("4247808.00"), after_discom: v("1044000.00"), after_other: v(charged ? "900.00" : "0.00"),
    });
    const [fp, ed] = levyRows([levy("fppca", false, "0.1800"), levy("electricity_duty", true, "0.5000")]);
    expect(fp.rate).toBe("₹0.18/kVAh on your 12 bills");
    expect(fp.afterOther).toBe("not charged (assumed)"); // said, never a silent zero
    expect(ed.afterOther).toBe("900.00");
    expect(fp.words).toContain("Not sourced to a regulation");
    expect(levyRows(undefined)).toEqual([]);
  });
});
