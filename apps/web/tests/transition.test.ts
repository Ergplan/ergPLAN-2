import { describe, expect, it } from "vitest";
import { addedText, basisLabel, capacityText, payBasis, pct, progress, targetBody } from "../lib/transition";

const sv = (value: string, unit = "kW") => ({ value, unit, source_refs: [{ kind: "assumption", note: "test" }] }) as never;

describe("the transition plan on screen", () => {
  it("shows a capacity in MW and a battery as power and energy", () => {
    expect(capacityText("oa_wind_intra", sv("4609.550"))).toBe("4.61 MW");
    expect(capacityText("btm_bess", sv("2500.000"), sv("10000.000", "kWh"))).toBe("2.5 MW / 10 MWh");
    expect(capacityText("rooftop_pv", null)).toBe("—");
  });

  it("says what a phase adds to each source", () => {
    const src = (kind: string, capacity: string, added: string, addedEnergy: string | null = null) =>
      ({ kind, capacity: sv(capacity), added: sv(added), added_energy: addedEnergy === null ? null : sv(addedEnergy, "kWh") }) as never;
    expect(addedText(src("rooftop_pv", "5000.000", "5000.000"))).toBe("new in this phase");
    expect(addedText(src("rooftop_pv", "5000.000", "0.000"))).toBe("as before");
    expect(addedText(src("oa_solar_intra", "0.000", "0.000"))).toBe("not taken");
    expect(addedText(src("oa_wind_intra", "6000.000", "1390.450"))).toBe("+1.39 MW");
    expect(addedText(src("btm_bess", "2000.000", "500.000", "1000.000"))).toBe("+0.5 MW / +1 MWh");
  });

  it("says whether a source is paid for by capital, by a tariff, or both", () => {
    expect(payBasis({ capital: sv("1", "INR"), tariff: null })).toBe("capital");
    expect(payBasis({ capital: null, tariff: sv("3.6", "INR/kWh") })).toBe("tariff");
    expect(payBasis({ capital: sv("1", "INR"), tariff: sv("3.6", "INR/kWh") })).toBe("capital and tariff");
    expect(payBasis({ capital: null, tariff: null })).toBe("none");
  });

  it("never calls a step a rule unless a rule states it", () => {
    expect(basisLabel({ basis: "rule" })).toBe("the rule requires it");
    expect(basisLabel({ basis: "assumption" })).toContain("not a rule");
  });

  it("formats a share and builds the request for a target", () => {
    expect(pct(sv("80.16", "percent"))).toBe("80.2%");
    expect(pct(null)).toBe("—");
    expect(targetBody("lowest_cost", "")).toEqual({ body: { kind: "lowest_cost" } });
    expect(targetBody("share", "50")).toEqual({ body: { kind: "share", share_pct: "50" } });
    expect(targetBody("share", "")).toHaveProperty("error");
    expect(targetBody("share", "120")).toHaveProperty("error");
    expect(targetBody("share", "abc")).toHaveProperty("error");
  });

  it("says how far a running plan has got", () => {
    expect(progress({ status: "running", runs: 5, pending_runs: 2 })).toContain("3 of 5 runs settled");
    expect(progress({ status: "done", runs: 5, pending_runs: 0 })).toBe("");
  });
});
