import { RefChips } from "@/components/cost/RefChips";
import { exchangeBlocks, type ExchangeCondition, type ExchangeVerdict } from "@/lib/exchange";

type Summary = { exchange?: ExchangeVerdict[] | null; sizes: Parameters<typeof exchangeBlocks>[0]["sizes"] };

function Lines({ title, items }: { title: string; items: ExchangeCondition[] }) {
  if (!items.length) return null;
  return (
    <>
      <div className="eg-overline">{title}</div>
      <ul className="ep-small">
        {items.map((c, i) => <li key={`${c.field}${i}`}>{c.text} <RefChips refs={c.refs} max={2} /></li>)}
      </ul>
    </>
  );
}

// Buying on the power exchange: the state's verdict for each exchange source the plan asked for.
// Offered: when it lowers the cost, the schedule the rule requires, what must be applied for and
// the conditions, each with its page. Not offered: why, citing the rule, or what is not yet sourced.
export function ExchangeVerdicts({ s }: { s: Summary }) {
  const blocks = exchangeBlocks(s);
  if (!blocks.length) return null;
  return (
    <div className="ep-exchange">
      {blocks.map((b) => (
        <div key={b.kind} className="eg-card">
          <div className="eg-card__title">
            {b.title} <span className={`eg-chip ${b.tone === "ok" ? "eg-chip--accent" : b.tone === "warning" ? "eg-chip--warning" : ""}`}>{b.chip}</span>
          </div>
          <p className="ep-small">{b.words}</p>
          {b.when && <p className="ep-small">{b.when}</p>}
          {b.schedule && <p className="ep-small eg-muted">{b.schedule}</p>}
          {b.deviation && <p className="ep-small eg-muted">{b.deviation}</p>}
          <Lines title="Why" items={b.reasons.filter((r) => r.refs.length > 0)} />
          <Lines title="What must be applied for" items={b.applyFor} />
          <Lines title="Conditions" items={b.conditions} />
          <Lines title="Not stated by a stored regulation" items={b.notStated} />
        </div>
      ))}
    </div>
  );
}
