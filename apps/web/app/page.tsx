import Link from "next/link";
import { apiOrNull } from "@/lib/api";
import { inr, type EvaluationOut } from "@/lib/evaluate";
import { runStatusWords } from "@/lib/runs";

// Home: the evaluations this account can see, and the way in.
export default async function Home() {
  const rows = (await apiOrNull<EvaluationOut[]>("/connections")) ?? [];
  return (
    <>
      <div className="ep-hero">
        <div>
          <div className="eg-overline">ergPLAN 2</div>
          <h1 className="eg-h1">Replace costly grid power with cheaper green power</h1>
          <p className="eg-muted ep-lede">
            Drop your electricity bill: ergPLAN 2 reads the state, the DISCOM, the tariff category, the time-of-day slots, and your energy and
            fixed charges. Give your targets for rooftop solar, open-access solar and wind, and batteries behind the meter and at the plant,
            with their costs and CUF. The optimizer sizes each one at least cost, settles the energy under your state&apos;s banking rules,
            and shows the saving over 25 years, each figure with its source.
          </p>
          <div className="ep-row">
            <Link className="eg-btn eg-btn--primary eg-btn--lg" href="/evaluate">
              New evaluation <span data-icon="arrow-right" data-size="16"></span>
            </Link>
            <Link className="eg-btn eg-btn--ghost" href="/documents">The regulation library</Link>
          </div>
        </div>
        <ol className="ep-hero__steps">
          <li><b>Your bill</b><span>state, DISCOM, tariff, ToD, charges read from it</span></li>
          <li><b>Your factory</b><span>name, how it runs, twelve months of units</span></li>
          <li><b>Today&apos;s cost</b><span>what the grid costs you, slot by slot</span></li>
          <li><b>Targets</b><span>solar, wind and batteries: size, cost, CUF</span></li>
          <li><b>Your plan, drawn</b><span>the site and the open-access plant</span></li>
          <li><b>Results</b><span>optimised sizes, savings, 25-year cash flows</span></li>
        </ol>
      </div>

      <section className="eg-card ep-panel">
        <div className="eg-card__head">
          <div>
            <h2 className="eg-card__title">Your evaluations</h2>
            <p className="eg-card__sub">{rows.length ? `${rows.length} site${rows.length > 1 ? "s" : ""}` : "None yet — start one above."}</p>
          </div>
        </div>
        {rows.length > 0 && (
          <div className="ep-scroll">
          <table className="ep-table ep-evals">
            <thead><tr><th>Site</th><th>DISCOM</th><th>Contract demand</th><th>Inputs</th><th>Latest result</th><th></th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.connection_id}>
                  <td><b>{r.client}</b><div className="eg-muted ep-small">{r.site}{r.state ? ` · ${r.state}` : ""}</div></td>
                  <td>{r.discom.toUpperCase()}{r.category ? <div className="eg-muted ep-small">{r.category}</div> : null}</td>
                  <td className="num">{r.contract_demand_kva ? `${Number(r.contract_demand_kva).toLocaleString("en-IN")} kVA` : "—"}</td>
                  <td>{r.bills ? `${r.bills} bill${r.bills > 1 ? "s" : ""}` : "typed"}{r.profile_provenance ? <div className="eg-muted ep-small">{r.profile_provenance} profile</div> : null}</td>
                  <td>
                    {r.latest_run ? (
                      r.latest_run.status === "done" ? (
                        <><b>{inr(r.latest_run.saving?.value)}</b> a year<div className="eg-muted ep-small">{r.latest_run.scenario}</div></>
                      ) : (
                        <span className="eg-chip">{runStatusWords(r.latest_run.status)}</span>
                      )
                    ) : <span className="eg-muted">not sized yet</span>}
                  </td>
                  <td className="ep-actions">
                    <Link className="eg-btn eg-btn--sm eg-btn--ghost" href={`/evaluate?c=${r.connection_id}`}>Inputs</Link>
                    <Link className="eg-btn eg-btn--sm eg-btn--primary" href={r.latest_run ? `/results?c=${r.connection_id}` : `/evaluate?c=${r.connection_id}`}>{r.latest_run ? "Results" : "Size it"}</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>
    </>
  );
}
