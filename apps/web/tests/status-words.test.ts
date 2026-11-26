import { describe, expect, it } from "vitest";
import { RUN_STATUS_WORDS, SOLVER_WORDS, runStatusWords, solverWords } from "../lib/runs";

describe("status words on the results page", () => {
  it("says how the optimizer ended in plain words, never the stored word", () => {
    expect(solverWords("time_limit")).toBe("stopped on its time limit: the best plan found, not proven the cheapest");
    expect(solverWords("status_quo_time_limit")).toContain("today's supply");
    expect(solverWords("optimal")).toBe("proven the cheapest within its gap");
    expect(solverWords(undefined)).toBe("—");
    expect(solverWords("something_new")).not.toContain("_");
  });
  it("says a run's status in plain words", () => {
    expect(runStatusWords("failed_resettle")).toBe("set aside: the settlement disagreed with the optimizer");
    expect(runStatusWords("queued")).toBe("waiting to be sized");
    expect(runStatusWords("unknown_word")).not.toContain("_");
  });
  it("has no stored word among the words", () => {
    for (const [k, w] of [...Object.entries(RUN_STATUS_WORDS), ...Object.entries(SOLVER_WORDS)]) {
      expect(w).not.toContain("_");
      expect(w).not.toBe(k);
    }
  });
});
