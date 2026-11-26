import { describe, expect, it } from "vitest";
import {
  EMPTY_FORM, againstLibrary, basisWords, formProblem, monthName, pct, provenanceWords, resolutionWords, runWords, technologyWords,
  toFormData, useWords, type Sourced,
} from "../lib/generation";

const sv = (value: string, kind = "measured"): Sourced => ({ value, unit: "percent", source_refs: [{ kind }] as never });

describe("generation profiles", () => {
  it("says what a profile is in plain words, never the stored codes", () => {
    expect(technologyWords({ technology: "solar", applies_to: "open_access" })).toBe("Open-access solar");
    expect(technologyWords({ technology: "wind", applies_to: "open_access" })).toBe("Open-access wind");
    expect(technologyWords({ technology: "solar", applies_to: "rooftop" })).toBe("Rooftop solar");
    expect(provenanceWords({ provenance: "measured" })).toBe("Measured at the plant");
    expect(provenanceWords({ provenance: "stated" })).toBe("Stated by the developer");
    expect(resolutionWords({ resolution_min: 60, first_day: "2025-04-01" })).toBe("Hourly, one year from 2025-04-01");
    expect(resolutionWords({ resolution_min: 15, first_day: "2025-01-01" })).toBe("15-minute, one year from 2025-01-01");
    expect(basisWords({ basis: "per_mw", capacity_mw: null, unit_in_file: "kW" })).toBe("kW for 1 MW of capacity");
    expect(basisWords({ basis: "capacity", capacity_mw: "12.5", unit_in_file: "MWh" })).toBe("MWh for a 12.5 MW plant");
    expect(basisWords({ basis: "per_unit", capacity_mw: null, unit_in_file: "CUF" })).toBe("Fraction of capacity");
    expect(basisWords({ basis: "per_unit", capacity_mw: null, unit_in_file: "CUF %" })).toBe("Percent of capacity");
    expect(monthName(1)).toBe("Jan");
    expect(monthName(12)).toBe("Dec");
  });

  it("says whether new plans use the profile", () => {
    expect(useWords({ in_use: true, withdrawn_at: null })).toBe("Used in new plans");
    expect(useWords({ in_use: false, withdrawn_at: null })).toBe("Replaced by a later file");
    expect(useWords({ in_use: false, withdrawn_at: "2026-11-26T10:00:00Z" })).toBe("Withdrawn");
  });

  it("compares the CUF with the library curve it replaces", () => {
    expect(pct(sv("25.4631"), 2)).toBe("25.46%");
    expect(pct(null)).toBe("—");
    expect(againstLibrary({ annual_cuf: sv("25.46"), library_cuf: sv("19", "assumption") })).toBe("6.5 points above the library curve (19.0%, assumed)");
    expect(againstLibrary({ annual_cuf: sv("17.2"), library_cuf: sv("19", "assumption") })).toBe("1.8 points below the library curve (19.0%, assumed)");
    expect(againstLibrary({ annual_cuf: sv("19.02"), library_cuf: sv("19", "assumption") })).toBe("the same as the library curve (19.0%, assumed)");
    expect(againstLibrary({ annual_cuf: sv("19"), library_cuf: null })).toBeNull();
  });

  it("names the plans worked with a profile", () => {
    const r = { run_id: "r", scenario_id: "s", scenario_name: "Solar and wind", status: "done", created_at: "2026-11-26T09:12:00Z", replay_of: null };
    expect(runWords(r)).toBe("Solar and wind, 2026-11-26, done");
    expect(runWords({ ...r, replay_of: "r0", status: "failed_resettle" })).toBe("Solar and wind (replay), 2026-11-26, failed its settlement check");
  });

  it("asks for what is missing before sending, and sends only what applies", () => {
    expect(formProblem(EMPTY_FORM, false)).toBe("Choose the CSV file.");
    expect(formProblem(EMPTY_FORM, true)).toBeNull();
    expect(formProblem({ ...EMPTY_FORM, basis: "capacity" }, true)).toBe("State the plant's capacity in MW.");
    expect(formProblem({ ...EMPTY_FORM, basis: "capacity", capacityMw: "-2" }, true)).toBe("State the plant's capacity in MW.");
    expect(formProblem({ ...EMPTY_FORM, basis: "capacity", capacityMw: "12.5" }, true)).toBeNull();
    expect(formProblem({ ...EMPTY_FORM, technology: "wind", appliesTo: "rooftop" }, true)).toBe("A rooftop profile is a solar profile.");
    const file = new File(["timestamp,kw\n"], "solar.csv", { type: "text/csv" });
    const fd = toFormData({ ...EMPTY_FORM, basis: "capacity", capacityMw: " 12.5 ", provenance: "measured" }, file);
    expect(Object.fromEntries([...fd.entries()].filter(([k]) => k !== "file"))).toEqual({
      technology: "solar", provenance: "measured", applies_to: "open_access", basis: "capacity", capacity_mw: "12.5",
    });
    const cuf = toFormData({ ...EMPTY_FORM, capacityMw: "5", firstDay: "2025-04-01" }, file);
    expect([...cuf.keys()].sort()).toEqual(["applies_to", "file", "first_day", "provenance", "technology"]);
  });
});
