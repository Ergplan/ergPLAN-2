import { RefChips } from "@/components/cost/RefChips";
import { uniqueRefs } from "@/lib/cost";
import type { SourcedValue } from "@/lib/intake";
import { crore, mwh } from "@/lib/runs";
import { addedText, basisLabel, capacityText, payBasis, pct, type Phase, type TransitionOut } from "@/lib/transition";

const chips = (v: (SourcedValue | null | undefined)[], max = 2) => <RefChips refs={uniqueRefs(v)} max={max} />;

// The transition plan as a page section: where the client stands today, the target, the phases in
// order (what each builds, costs and saves, the rule that holds it, what to sign or apply for), the
// routes compared and the cost curve. Each figure carries its sources; a step that no rule states is
// shown as assumed.
export function TransitionView({ plan }: { plan: TransitionOut }) {
  const t = plan.target;
  return (
    <div className="ep-transition">
      <p>{t.words}</p>
      {plan.today && (
        <table className="ep-table ep-small">
          <tbody>
            <tr><td>Your grid cost today, a year</td><td className="num">{crore(plan.today.annual_cost)}</td><td>{chips([plan.today.annual_cost])}</td></tr>
            <tr><td>Your consumption, a year</td><td className="num">{mwh(plan.today.load_kwh)} MWh</td><td>{chips([plan.today.load_kwh])}</td></tr>
            {t.asked && <tr><td>Your target: renewable share</td><td className="num">{pct(t.asked)}</td><td>{chips([t.asked])}</td></tr>}
            {t.reached && <tr><td>Renewable share the plan reaches</td><td className="num">{pct(t.reached)}</td><td>{chips([t.reached])}</td></tr>}
            {t.most && <tr><td>{t.most_proven === false ? "The rules in force allow at least (the search for the most ended on its time limit)" : "The most the rules in force allow"}</td><td className="num">{pct(t.most)}</td><td>{chips([t.most])}</td></tr>}
          </tbody>
        </table>
      )}
      {t.most && t.most_binding.length > 0 && (
        <>
          <div className="eg-overline">What holds the share at the most the rules allow</div>
          <ul className="ep-list ep-small">{t.most_binding.map((b) => <li key={b.name}>{b.words} {chips(b.refs, 3)}</li>)}</ul>
        </>
      )}

      {plan.phases.map((ph) => <PhaseCard key={ph.key} ph={ph} />)}

      {plan.alternatives.length > 1 && (
        <section>
          <h3 className="eg-card__title">How to take the open-access plant: the routes compared</h3>
          <table className="ep-table ep-small">
            <thead><tr><th>Route</th><th className="num">Cost a year</th><th className="num">Saving a year</th><th className="num">Capital of your own</th><th /></tr></thead>
            <tbody>
              {plan.alternatives.map((a) => (
                <tr key={a.route}>
                  <td>{a.label}{a.chosen ? " (taken)" : ""}<div className="eg-muted">{a.words}</div></td>
                  <td className="num">{crore(a.annual_cost)}</td><td className="num">{crore(a.annual_saving)}</td><td className="num">{crore(a.capital)}</td>
                  <td>{a.annual_cost && chips([a.annual_cost])}{!a.settlement_rule_approved && <span className="eg-chip eg-chip--warning">settlement not yet sourced</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {plan.curve.length > 1 && (
        <section>
          <h3 className="eg-card__title">What each further step of replacement costs</h3>
          <p className="eg-card__sub">Each row is the least yearly cost found for that share of your consumption no longer drawn from the DISCOM, from today to the most the rules allow.</p>
          <table className="ep-table ep-small">
            <thead><tr><th>Step</th><th className="num">Renewable share</th><th className="num">Cost a year</th><th className="num">Saving a year</th><th className="num">Capital of your own</th><th>What it takes</th></tr></thead>
            <tbody>
              {plan.today && <tr><td>Today</td><td className="num">{pct(plan.today.share)}</td><td className="num">{crore(plan.today.annual_cost)}</td><td className="num">—</td><td className="num">—</td><td>Grid supply only {chips([plan.today.annual_cost], 1)}</td></tr>}
              {plan.curve.map((c, i) => (
                <tr key={`${c.role}-${i}`}>
                  <td>{c.label}{c.cheapest ? " (the cheapest plan)" : ""}{!c.proven && <div><span className="eg-chip eg-chip--warning">not proven the cheapest</span> <span className="eg-muted">{c.proven_words}</span></div>}</td>
                  <td className="num">{pct(c.share)}</td><td className="num">{crore(c.annual_cost)}</td><td className="num">{crore(c.annual_saving)}</td><td className="num">{crore(c.capital)}</td>
                  <td>{c.sizes.map((z) => `${z.label} ${capacityText(z.kind, z.capacity, z.energy)}`).join(" · ") || "nothing built"} {chips([c.annual_cost], 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {plan.not_worked_out.length > 0 && (
        <ul className="ep-list ep-small eg-muted">{plan.not_worked_out.map((x) => <li key={x.key}>{x.words}</li>)}</ul>
      )}
    </div>
  );
}

function PhaseCard({ ph }: { ph: Phase }) {
  const taken = ph.status === "taken";
  const e = ph.energy;
  return (
    <section className="eg-card ep-panel" style={{ marginTop: 16 }}>
      <div className="eg-overline">Phase {ph.order}{ph.route_words ? ` · ${ph.route_words}` : ""}</div>
      <h3 className="eg-card__title">{ph.title}</h3>
      <p className="eg-card__sub">{ph.what}</p>
      {!ph.proven && <p className="ep-small"><span className="eg-chip eg-chip--warning">not proven the cheapest</span> {ph.proven_words}</p>}
      {!taken && <p className="ep-small"><span className="eg-chip">adds nothing</span> {ph.status_words}</p>}

      <table className="ep-table">
        <thead><tr><th>Source</th><th className="num">Capacity after this phase</th><th>This phase</th><th className="num">Capital of your own</th><th className="num">Tariff</th><th /></tr></thead>
        <tbody>
          {ph.sources.map((s) => (
            <tr key={s.id}>
              <td>{s.label}{s.gaps.length > 0 && <div className="ep-small"><span className="eg-chip eg-chip--warning">not yet sourced</span> <span className="eg-muted">{s.gaps.join(" ")}</span></div>}</td>
              <td className="num">{capacityText(s.kind, s.capacity, s.energy)}</td>
              <td>{addedText(s)}</td>
              <td className="num">{s.capital ? crore(s.capital) : "—"}{s.capital_words && <div className="ep-small eg-muted">{s.capital_words}</div>}</td>
              <td className="num">{s.tariff ? `₹${Number(s.tariff.value).toFixed(2)}/kWh` : payBasis(s) === "capital" ? "none: your own" : "—"}</td>
              <td>{chips([s.capacity, s.capital, s.tariff])}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="ep-table ep-small">
        <tbody>
          <tr><td>Cost a year after this phase</td><td className="num">{crore(ph.annual_cost)}</td><td>{chips([ph.annual_cost])}</td></tr>
          <tr><td>Saving a year against today&apos;s bill</td><td className="num">{crore(ph.annual_saving)}</td><td>this phase adds {crore(ph.added_saving)} {chips([ph.annual_saving])}</td></tr>
          <tr><td>Capital of your own so far</td><td className="num">{crore(ph.capital)}</td><td>this phase adds {crore(ph.added_capital)} {chips([ph.capital])}</td></tr>
          <tr><td>Renewable share so far</td><td className="num">{pct(ph.share)}</td><td>this phase adds {pct(ph.added_share)} {chips([ph.share])}</td></tr>
          {ph.payback_years && <tr><td>Payback on the owned capital</td><td className="num">{Number(ph.payback_years.value).toFixed(1)} years</td><td>{ph.irr_pct ? `IRR ${Number(ph.irr_pct.value).toFixed(1)}%` : ""} {chips([ph.payback_years], 1)}</td></tr>}
          <tr><td>Renewable energy at your meter, a year</td><td className="num">{mwh(e.delivered_kwh)} MWh</td><td>{mwh(e.setoff_kwh)} MWh set off as it arrives · {mwh(e.banked_kwh)} MWh banked · {mwh(e.unbanked_kwh)} MWh could not be banked · {mwh(e.lapsed_kwh)} MWh lapsed {chips([e.delivered_kwh], 1)}</td></tr>
          <tr><td>Still drawn from the DISCOM, a year</td><td className="num">{mwh(e.grid_kwh)} MWh</td><td>{chips([e.grid_kwh])}</td></tr>
        </tbody>
      </table>

      {ph.rules.length > 0 && (
        <>
          <div className="eg-overline">The state&apos;s rules that shape this phase</div>
          <ul className="ep-list ep-small">{ph.rules.map((r, i) => <li key={i}>{r.text} <RefChips refs={[r.ref]} max={1} kinds={1} /></li>)}</ul>
        </>
      )}
      <div className="eg-overline">What holds this phase where it is</div>
      {ph.binding.length === 0 && <p className="eg-muted ep-small">Nothing binds: the sizes are where the money says.</p>}
      <ul className="ep-list ep-small">{ph.binding.map((b) => <li key={b.name}>{b.words} {chips(b.refs, 3)}</li>)}</ul>

      {taken && ph.actions.length > 0 && (
        <>
          <div className="eg-overline">What must be signed or applied for</div>
          <table className="ep-table ep-small">
            <thead><tr><th>Step</th><th>With</th><th>Why</th></tr></thead>
            <tbody>
              {ph.actions.map((a, i) => (
                <tr key={i}>
                  <td>{a.what}</td><td>{a.with_whom}</td>
                  <td>{basisLabel(a)} <RefChips refs={[a.ref]} max={1} kinds={1} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
