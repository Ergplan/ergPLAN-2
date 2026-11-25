import { describe, expect, it } from "vitest";
import { OWNERSHIP_LABEL, inputApplies, paysTariff } from "../lib/evaluate";
import { crore, levyRows, type LevyOut, mw, mwh, paybackHeadline, presets } from "../lib/runs";

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

describe("payback and IRR are on the owned capital", () => {
  const v = (value: string, unit = "INR", label?: string) => ({ value, unit, label, source_refs: [{ kind: "assumption", note: "planning assumption" }] }) as never;
  const base = { annual: { before: v("191476310.78"), after: v("160590445.93"), saving: v("30885864.85"), capex: v("292000000.00") } };
  it("labels the figures, breaks the capital down by asset and names the saving they rest on", () => {
    const h = paybackHeadline({
      ...base, payback_years: v("4.4", "years"), irr_pct: v("24.41", "percent"),
      owned_capital: {
        total: v("292000000.00"), saving: v("66000000.00"), bought_saving: null, bought: [],
        assets: [
          { id: "rooftop_pv", label: "Rooftop solar (behind the meter)", kind: "plant", capex: v("40000000.00"), words: "capital cost per kW × kW" },
          { id: "oa_solar_intra", label: "Open-access solar, intra-state", kind: "plant", capex: v("252000000.00"), words: "capital cost per kW × kW" },
          { id: "oa_wind_intra", label: "Open-access wind, intra-state", kind: "equity", capex: v("17680000.00"), words: "26% of the plant's capital cost" },
        ],
        basis: "Payback and IRR are on the capital of your own.", payback_absent: null, irr_absent: null,
      },
    });
    expect(h.label).toBe("Payback on the owned capital");
    expect(h.payback).toBe("4.4 years");
    expect(h.irr).toBe("24.4%");
    expect(h.capital).toBe("₹29.20 cr");
    expect(h.assets).toEqual([
      { label: "Rooftop solar (behind the meter)", kind: "your own plant", capex: "₹4.00 cr" },
      { label: "Open-access solar, intra-state", kind: "your own plant", capex: "₹25.20 cr" },
      { label: "Open-access wind, intra-state", kind: "equity outlay", capex: "₹1.77 cr" },
    ]);
    expect(h.saving).toBe("₹6.60 cr");
    expect(h.absent).toBeNull();
  });
  it("says why there is no payback when the plan has no capital of its own, never a zero", () => {
    const why = "No payback or IRR: this plan has no capital of your own. Every source in it is bought by the unit (Open-access solar, intra-state), so its saving needs no investment to pay back.";
    const h = paybackHeadline({
      annual: { ...base.annual, capex: v("0.00") }, payback_years: null, irr_pct: null,
      owned_capital: { total: v("0.00"), saving: v("0.00"), bought_saving: v("13059213.72"), bought: ["Open-access solar, intra-state"], assets: [], basis: why, payback_absent: why, irr_absent: why },
    });
    expect(h.label).toBe("Payback on the owned capital");
    expect(h.payback).toBe("none to state");
    expect(h.irr).toBe("none to state");
    expect(h.capital).toBeNull();
    expect(h.absent).toBe(why);
    expect(`${h.payback} ${h.irr}`).not.toMatch(/0\.0|—/);
  });
  it("labels a run made before the treatment as the plan's as a whole", () => {
    const h = paybackHeadline({ annual: { ...base.annual, capex: v("40000000.00") }, payback_years: v("1.0", "years"), irr_pct: v("113.0", "percent") });
    expect(h.label).toBe("Payback on the plan as a whole");
    expect(h.basis).toContain("against the whole plan's saving");
    expect(h.payback).toBe("1.0 years");
  });
});

describe("what a plant takes under each arrangement", () => {
  it("asks a tariff of every arrangement but a plant of one's own", () => {
    expect(paysTariff("ppa", "captive")).toBe(false);
    expect(paysTariff("ppa", "group_captive")).toBe(true);
    expect(paysTariff("ppa", "third_party")).toBe(true);
    expect(paysTariff("iex", "")).toBe(false);
  });
  it("asks the plant's capital of a captive plant, the equity share of one with 26% equity, neither of a PPA", () => {
    const keys = ["cuf_pct", "capex_inr_per_kw", "om_pct_of_capex", "life_years", "degradation_pct", "equity_share_pct"];
    const of = (own: string) => keys.filter((k) => inputApplies(k, "ppa", own));
    expect(of("captive")).toEqual(["cuf_pct", "capex_inr_per_kw", "om_pct_of_capex", "life_years", "degradation_pct"]);
    expect(of("group_captive")).toEqual(["cuf_pct", "capex_inr_per_kw", "life_years", "equity_share_pct"]);
    expect(of("third_party")).toEqual(["cuf_pct"]);
    expect(inputApplies("capex_inr_per_kw", "lcoe", "")).toBe(true); // rooftop keeps every input
    expect(OWNERSHIP_LABEL.captive).toContain("my own plant");
    expect(OWNERSHIP_LABEL.group_captive).toContain("26% equity");
  });
});
