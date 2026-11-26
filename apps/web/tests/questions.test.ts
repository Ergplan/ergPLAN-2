import { describe, expect, it } from "vitest";
import { dateOf, type Draft, draftOf, EMPTY, mmddOf, profileWords, type Question, standing, statedOn, toValue } from "../lib/questions";

const q = (over: Partial<Question>): Question => ({
  key: "shifts", label: "Shifts", question: "What are the shifts?", unit: "hours of the day (HH:MM)", why: "w", form: "shifts", used_for: "u",
  shapes_profile_only: true, asked: true, not_asked: null, until_answered: "The operating pattern chosen with the inputs (Two-shift manufacturing).",
  known_from_inputs: null, answer: null, ...over,
});
const answer = (value: unknown, words: string) => ({ value, words, figures: [], stated_by: "client@example.test", stated_at: "2026-11-26T10:00:00Z", source_ref: { kind: "client_stated" as const } });
const d = (over: Partial<Draft>): Draft => ({ ...EMPTY, ...over });

describe("the forms give the API the value as typed, never a float", () => {
  it("shifts", () => {
    expect(toValue("shifts", d({ hours: [{ start: "06:00", end: "14:00" }, { start: "22:00", end: "06:00" }] }))).toEqual({ value: [{ start: "06:00", end: "14:00" }, { start: "22:00", end: "06:00" }] });
    expect(toValue("shifts", d({ hours: [] }))).toEqual({ error: "Add at least one shift." });
    expect(toValue("shifts", d({ hours: [{ start: "06:10", end: "14:00" }] }))).toHaveProperty("error");
    expect(toValue("shifts", d({ hours: [{ start: "06:00", end: "06:00" }] }))).toEqual({ error: "Each shift starts and ends at the same time." });
  });
  it("the weekly off, and none", () => {
    expect(toValue("weekdays", d({ days: [6, 5] }))).toEqual({ value: [5, 6] });
    expect(toValue("weekdays", d({ days: [6], none: true }))).toEqual({ value: [] });
    expect(toValue("weekdays", d({ days: [] }))).toHaveProperty("error");
    expect(toValue("weekdays", d({ days: [0, 1, 2, 3, 4, 5, 6] }))).toHaveProperty("error");
  });
  it("shutdowns keep the day and the month", () => {
    expect(toValue("shutdowns", d({ shutdowns: [{ start: "05-15", end: "05-24", name: " overhaul " }] }))).toEqual({ value: [{ start: "05-15", end: "05-24", name: "overhaul" }] });
    expect(toValue("shutdowns", d({ none: true }))).toEqual({ value: [] });
    expect(toValue("shutdowns", d({ shutdowns: [{ start: "", end: "05-24", name: "" }] }))).toHaveProperty("error");
    expect(mmddOf("2027-12-20")).toBe("12-20");
    expect(dateOf("12-20")).toBe("2026-12-20");
    expect(dateOf("")).toBe("");
  });
  it("large loads", () => {
    expect(toValue("loads", d({ loads: [{ name: " Furnace ", kw: "600", start: "09:00", end: "12:00" }] }))).toEqual({ value: [{ name: "Furnace", kw: "600", start: "09:00", end: "12:00" }] });
    expect(toValue("loads", d({ loads: [{ name: "Furnace", kw: "big", start: "09:00", end: "12:00" }] }))).toEqual({ error: "Give Furnace its size in kW." });
    expect(toValue("loads", d({ loads: [{ name: "", kw: "5", start: "09:00", end: "12:00" }] }))).toEqual({ error: "Give each load a name." });
    expect(toValue("loads", d({ none: true }))).toEqual({ value: [] });
  });
  it("numbers and the expansion", () => {
    expect(toValue("number", d({ text: " 8,000 " }))).toEqual({ value: "8000" });
    expect(toValue("number", d({ text: "0.92" }))).toEqual({ value: "0.92" });
    expect(toValue("number", d({ text: "about 5" }))).toEqual({ error: "Give a number." });
    expect(toValue("percent", d({ text: "12.5" }))).toEqual({ value: { growth_pct: "12.5" } });
    expect(toValue("percent", d({ text: "-10" }))).toEqual({ value: { growth_pct: "-10" } });
    expect(toValue("percent", d({ text: "", none: true }))).toEqual({ value: { growth_pct: "0" } });
    expect(toValue("percent", d({ text: "lots" }))).toHaveProperty("error");
  });
});

describe("a form opens on the answer in force", () => {
  it("round-trips each kind", () => {
    const shifts = [{ start: "08:00", end: "16:00" }];
    expect(toValue("shifts", draftOf(q({ answer: answer(shifts, "08:00 to 16:00") })))).toEqual({ value: shifts });
    expect(toValue("weekdays", draftOf(q({ form: "weekdays", answer: answer([], "No weekly off") })))).toEqual({ value: [] });
    expect(toValue("weekdays", draftOf(q({ form: "weekdays", answer: answer([4], "Friday") })))).toEqual({ value: [4] });
    const loads = [{ name: "Kiln", kw: "450", start: "00:00", end: "24:00", days: null }];
    expect(toValue("loads", draftOf(q({ form: "loads", answer: answer(loads, "x") })))).toEqual({ value: [{ name: "Kiln", kw: "450", start: "00:00", end: "24:00" }] });
    expect(toValue("percent", draftOf(q({ form: "percent", answer: answer({ growth_pct: "20", note: "" }, "x") })))).toEqual({ value: { growth_pct: "20" } });
    expect(draftOf(q({ form: "percent", answer: answer({ growth_pct: "0", note: "" }, "None planned") })).none).toBe(true);
    expect(toValue("number", draftOf(q({ form: "number", answer: answer("0.92", "0.92") })))).toEqual({ value: "0.92" });
    expect(draftOf(q({})).hours).toEqual([{ start: "06:00", end: "14:00" }]);
  });
});

describe("where a question stands, in words", () => {
  it("says what is used until it is answered, what was said, and when it is not asked", () => {
    expect(standing(q({}))).toBe("Until you answer: The operating pattern chosen with the inputs (Two-shift manufacturing).");
    expect(standing(q({ answer: answer([], "08:00 to 16:00") }))).toBe("You said: 08:00 to 16:00");
    expect(standing(q({ known_from_inputs: "8000 m²" }))).toBe("Already known: 8000 m²");
    expect(standing(q({ asked: false, not_asked: "A measured 15-minute file is in use for this site, so its load profile needs no estimate of this." }))).toContain("measured 15-minute file");
    expect(statedOn(answer(1, "1"))).toBe("stated by client@example.test on 26 Nov 2026");
    expect(profileWords("measured", true)).toMatch(/^Measured: your 15-minute meter file/);
    expect(profileWords("synthesised", false)).toMatch(/^Estimated from your bills and your answers/);
  });
});
