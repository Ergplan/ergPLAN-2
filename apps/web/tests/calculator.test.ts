import { describe, expect, it } from "vitest";
import { bestSlot, bodyOf, gapLabel, routeStatus, type CalcRoute, type Form } from "../lib/calculator";

const form: Form = {
  discom: "pvvnl",
  category: "HV-2",
  voltage: "33",
  cd: "5,000",
  month: "2026-11",
  connection: "intra_state",
  solar: { tariff: "3.10", capex: "", cuf: " 21 " },
  wind: { tariff: "", capex: "abc", cuf: "" },
  equity: "26",
  market: "gdam",
};

const sv = (value: string) => ({ value, unit: "INR/kWh", source_refs: [{ kind: "derived" as const }] });
const route = (over: Partial<CalcRoute> = {}): CalcRoute => ({
  route: "third_party", label: "Third party", note: "", source: "oa_solar_intra", allowed: true, shown: true,
  reason: null, reason_values: [], withheld: null, slots: [], landed_avg: null, saving_avg: null,
  not_sourced: [], notes: [], inputs: [], ...over,
});

describe("bodyOf", () => {
  it("sends numbers as strings, drops what is empty or not a number, and dates the month", () => {
    expect(bodyOf(form)).toEqual({
      discom: "pvvnl",
      category: "HV-2",
      connection: "intra_state",
      iex_market: "gdam",
      voltage_kv: "33",
      contract_demand_kva: "5000",
      month: "2026-11-01",
      equity_share_pct: "26",
      solar: { ppa_tariff_inr_per_kwh: "3.10", cuf_pct: "21" },
      wind: {},
    });
  });
});

describe("routeStatus", () => {
  it("says why a route is barred, not shown or shown only to an analyst", () => {
    expect(routeStatus(route({ allowed: false, shown: false, reason: "Not practical" }))).toEqual({ tone: "bad", text: "Not practical" });
    expect(routeStatus(route({ shown: false, reason: "Not yet sourced: x" })).tone).toBe("warn");
    expect(routeStatus(route({ withheld: "The analyst's figure" })).text).toBe("The analyst's figure");
    expect(routeStatus(route())).toEqual({ tone: "ok", text: "" });
  });
});

describe("bestSlot", () => {
  it("picks the slot with the largest saving", () => {
    const slot = (name: string, saving: string | null) => ({ slot: name, lines: [], landed: sv("5"), banking: null, grid: sv("7"), saving: saving ? sv(saving) : null });
    expect(bestSlot(route({ slots: [slot("a", "1.2"), slot("b", "2.5"), slot("c", "0.1")] }))).toBe("b");
    expect(bestSlot(route({ slots: [slot("a", null)] }))).toBe("a");
    expect(bestSlot(route())).toBeNull();
  });
});

describe("gapLabel", () => {
  it("puts a component the rules do not state in plain words", () => {
    expect(gapLabel("oa_additional_surcharge")).toBe("Additional surcharge");
    expect(gapLabel("something_new")).toBe("Something new");
  });
});
