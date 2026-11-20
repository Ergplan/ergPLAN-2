// What the site drawing (components/site/IsoSite) shows, from the form's draft (targets) or from a
// run's sizes. Display only: sizes are the API's figures, formatted.

import type { IsoPart, IsoParts } from "@/components/site/IsoSite";
import type { Draft } from "./evaluate";
import type { SizeOut } from "./runs";

const SOLAR = ["oa_solar_intra", "oa_solar_ists", "group_captive"];
const WIND = ["oa_wind_intra", "oa_wind_ists"];
const HYBRID = ["oa_hybrid_intra", "oa_hybrid_ists"];

const mw = (kw: number) => `${(kw / 1000).toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: kw >= 100_000 ? 0 : 2 })} MW`;
const mwh = (kwh: number) => `${(kwh / 1000).toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 })} MWh`;

function fromDraft(d: Draft, kinds: string[]): IsoPart {
  const picks = kinds.map((k) => d.tech[k]).filter((t) => t?.on);
  const targets = picks.map((t) => Number(t.max_mw)).filter((n) => n > 0);
  const sum = targets.reduce((a, b) => a + b, 0);
  return { on: picks.length > 0, target: targets.length ? `${sum.toLocaleString("en-IN", { maximumFractionDigits: 2 })} MW` : undefined };
}

export function isoFromDraft(d: Draft, names: { factory?: string; discom?: string; state?: string }): IsoParts {
  const hybrid = fromDraft(d, HYBRID);
  const solar = fromDraft(d, SOLAR), wind = fromDraft(d, WIND);
  return {
    ...names,
    rooftop: fromDraft(d, ["rooftop_pv"]),
    oa_solar: hybrid.on && !solar.on ? { ...hybrid, note: "hybrid" } : solar,
    oa_wind: hybrid.on && !wind.on ? { on: true, target: hybrid.target, note: "hybrid" } : wind,
    btm_bess: fromDraft(d, ["btm_bess"]),
    oa_bess: fromDraft(d, ["oa_bess"]),
  };
}

function fromSizes(sizes: SizeOut[], kinds: string[], targets: Record<string, string>, storage = false): IsoPart {
  const rows = sizes.filter((z) => kinds.includes(z.kind));
  if (rows.length === 0) return { on: false };
  const kw = rows.reduce((a, z) => a + Number(z.kw.value), 0);
  const kwh = rows.reduce((a, z) => a + Number(z.kwh?.value ?? 0), 0);
  const t = rows.map((z) => Number(targets[z.kind] ?? 0)).filter((n) => n > 0).reduce((a, b) => a + b, 0);
  return {
    on: true,
    sized: kw > 0 || kwh > 0 ? mw(kw) : "not chosen",
    note: storage && kwh > 0 ? mwh(kwh) : undefined,
    target: t > 0 ? `${t.toLocaleString("en-IN", { maximumFractionDigits: 2 })} MW` : undefined,
  };
}

// `targets`: kind → the target in MW the scenario was run with (its spec's max_mw), when known.
export function isoFromSizes(sizes: SizeOut[], targets: Record<string, string>, names: { factory?: string; discom?: string; state?: string }): IsoParts {
  const hybrid = fromSizes(sizes, HYBRID, targets);
  const solar = fromSizes(sizes, SOLAR, targets), wind = fromSizes(sizes, WIND, targets);
  return {
    ...names,
    rooftop: fromSizes(sizes, ["rooftop_pv"], targets),
    oa_solar: hybrid.on && !solar.on ? { ...hybrid, note: "solar-wind hybrid" } : solar,
    oa_wind: hybrid.on && !wind.on ? { on: true, sized: "in the hybrid" } : wind,
    btm_bess: fromSizes(sizes, ["btm_bess"], targets, true),
    oa_bess: fromSizes(sizes, ["oa_bess"], targets, true),
  };
}
