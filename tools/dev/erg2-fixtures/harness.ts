// Differential fixtures for ergplan/settlement/cycle.py.
//
// Runs the ORIGINAL erg_plan 2.0 settlement (src/lib/settlement-data.ts, not copied into this
// repository) on a set of cases and writes each case's 15-minute series, configuration and result
// as gzipped JSON under apps/api/tests/fixtures/erg2_settlement/. The Python port is then held to
// those results (tests/settlement/test_cycle_vs_erg2.py).
//
//   ERG2_SRC=/path/to/erg_plan_2.0-main  npx tsx tools/dev/erg2-fixtures/harness.ts
//
// Regenerate only when the erg_plan 2.0 settlement itself changes.
import { gzipSync } from "node:zlib";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const SRC = process.env.ERG2_SRC!;
const OUT = join(__dirname, "../../../apps/api/tests/fixtures/erg2_settlement");

async function main() {
  const sd = await import(join(SRC, "src/lib/settlement-data.ts"));
  const ss = await import(join(SRC, "src/lib/settlement-settings.ts"));

  const profile = (scale: number, tod: boolean) => ({
    savedAt: "x",
    qualityScore: 1,
    months: Array.from({ length: 12 }, (_, i) => {
      const month = i + 1;
      const total = scale * (900_000 + 60_000 * Math.sin(month) + 15_000 * month);
      return tod
        ? { month, consumptionKwh: total, todPeakKwh: total * (0.2 + 0.01 * (month % 3)), todNormalKwh: total * 0.5, todOffPeakKwh: total * (0.3 - 0.01 * (month % 3)) }
        : { month, consumptionKwh: total, todPeakKwh: null, todNormalKwh: null, todOffPeakKwh: null };
    }),
  });
  const generation = (technology: string, mwh: number) => ({
    technology,
    year: 2025,
    solarCapacityMw: technology === "Wind" ? 0 : 5,
    windCapacityMw: technology === "Solar" ? 0 : 5,
    monthlyKwh: Object.fromEntries(Array.from({ length: 12 }, (_, i) => [i + 1, mwh * 1000 * (1 + 0.25 * Math.cos((i + 1) / 2))])),
  });
  const base = {
    bankingAllowed: true, bankingCycle: "Monthly", bankingChargePct: 0.08, todWise: true, unutilisedTreatment: "Lapse",
    fromRegulation: true, capPctOfGeneration: null, capPctOfConsumption: null, capPctOfDrawal: null, capCombine: null,
    bankingLimit: ss.DEFAULT_BANKING_LIMIT, bankingLimitSource: "default", basis: "15min",
    bankingRule: "higher_to_lower", bankingMatrix: ss.bankingMatrixFor("higher_to_lower"),
    transmissionLossPct: 0.035, wheelingLossPct: 0.06,
  };
  const custom = { peak: { peak: true, normal: true, offPeak: true }, normal: { peak: false, normal: true, offPeak: true }, offPeak: { peak: false, normal: true, offPeak: true } };
  const rooftopGen = (n: number, mw: number) =>
    Array.from({ length: n }, (_, i) => { const h = Math.floor((i % 96) / 4); return h >= 7 && h <= 17 ? mw * 250 * Math.max(0, 1 - Math.abs(h - 12) / 6) : 0; });

  const cases: Record<string, { profile: unknown; generation: unknown; cfg: Record<string, unknown> }> = {
    monthly_h2l_default_limit: { profile: profile(1, true), generation: generation("Solar", 1100), cfg: base },
    annual_same_tod_no_limit: {
      profile: profile(1, true), generation: generation("Hybrid", 1500),
      cfg: { ...base, bankingCycle: "Annual", bankingRule: "same_tod", bankingMatrix: ss.bankingMatrixFor("same_tod"), bankingLimit: { enabled: false, drawalPct: null, generationPct: null, combine: "max" }, bankingChargePct: 0.02 },
    },
    monthly_custom_up_tod_basis: {
      profile: profile(1.3, true), generation: generation("Solar", 1700),
      cfg: { ...base, basis: "tod", bankingRule: "custom", bankingMatrix: custom, bankingLimit: { enabled: true, drawalPct: 0.3, generationPct: 0.25, combine: "max" } },
    },
    monthly_drawal_only_sold: {
      profile: profile(0.8, false), generation: generation("Wind", 1300),
      cfg: { ...base, unutilisedTreatment: "Sold", bankingLimit: { enabled: true, drawalPct: 0.3, generationPct: 0, combine: "drawal" }, capPctOfConsumption: 0.3, wheelingLossPct: 0 },
    },
    no_banking_interstate: {
      profile: profile(1, true), generation: generation("Hybrid", 1400),
      cfg: { ...base, bankingAllowed: false, interstate: true, bankingLimit: { enabled: false, drawalPct: null, generationPct: null, combine: "max" } },
    },
    monthly_min_combine_rooftop: {
      profile: profile(1, true), generation: generation("Solar", 2000),
      cfg: { ...base, bankingLimit: { enabled: true, drawalPct: 0.4, generationPct: 0.1, combine: "min" }, rooftop: { capacityMw: 1.5, generationKwh: rooftopGen(35040, 1.5) }, bankingChargePct: 0.05 },
    },
  };

  for (const [name, c] of Object.entries(cases)) {
    // the series before rooftop, to give the port the gross demand
    const gross = sd.buildSettlementSeries(c.profile, c.generation, { ...c.cfg, rooftop: null });
    const series = sd.buildSettlementSeries(c.profile, c.generation, c.cfg);
    const result = sd.runSettlement(c.profile, c.generation, c.cfg);
    const { finalShortfallKwh, ...rest } = result;
    const out = {
      name,
      cfg: { ...c.cfg, rooftop: c.cfg.rooftop ? { capacityMw: (c.cfg.rooftop as { capacityMw: number }).capacityMw } : null },
      blocks: {
        month: series.timestamps.map((t: Date) => t.getMonth() + 1),
        cat: series.todCategory,
        grossConsumptionKwh: gross.consumptionKwh,
        consumptionKwh: series.consumptionKwh,
        generationKwh: series.generationKwh,
        generationInjectionKwh: series.generationInjectionKwh,
        rooftopGenerationKwh: c.cfg.rooftop ? (c.cfg.rooftop as { generationKwh: number[] }).generationKwh : null,
      },
      result: rest,
      finalShortfallKwh,
    };
    writeFileSync(join(OUT, `${name}.json.gz`), gzipSync(JSON.stringify(out), { level: 9 }));
    console.log(name, result.gridImportKwh.toFixed(1), result.bankedUsedKwh.toFixed(1), result.energyBalanceOk);
  }

  // the allocation on its own: random banks, needs, matrices, caps and sink orders
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const cats = ["peak", "normal", "offPeak"];
  const alloc = [];
  for (let k = 0; k < 400; k++) {
    const bank = Object.fromEntries(cats.map((c) => [c, rnd() < 0.2 ? 0 : Math.round(rnd() * 1000)]));
    const need = Object.fromEntries(cats.map((c) => [c, rnd() < 0.2 ? 0 : Math.round(rnd() * 1000)]));
    const matrix = Object.fromEntries(cats.map((f) => [f, Object.fromEntries(cats.map((t) => [t, rnd() < 0.55]))]));
    const cap = rnd() < 0.3 ? 1e12 : Math.round(rnd() * 1500);
    const order = [...cats].sort(() => rnd() - 0.5);
    alloc.push({ bank, need, matrix, cap, order, drawn: sd.allocateBankedEnergy(bank, need, matrix, cap, order) });
  }
  writeFileSync(join(OUT, "allocation.json.gz"), gzipSync(JSON.stringify(alloc), { level: 9 }));
}
main();
