import { RefChips } from "@/components/cost/RefChips";
import { Fig, traceOf } from "./Fig";
import type { Ref } from "@/lib/intake";
import { inputText, markUnstored, monthLabel, rate, slotLabel, type BookDetail, type BookPoint, type BookRoute, type Sourced } from "@/lib/statebook";

const FAMILY: Record<string, string> = {
  oa_charge: "Open-access charges",
  tariff_schedule: "Retail tariff",
  tod_slots: "Time-of-day slots",
  settlement: "Banking and settlement",
  regulatory_limit: "Limits",
};

function Saving({ v }: { v: Sourced | null }) {
  return <Fig v={v} strong tone />;
}

function Points({ points }: { points: BookPoint[] }) {
  return (
    <ol className="ep-book__points">
      {points.map((p, i) => (
        <li key={i}>
          {p.text} {p.ref && <RefChips refs={[p.ref as Ref]} />}
        </li>
      ))}
    </ol>
  );
}

// One route (or one exchange segment): the cost lines that add up to the landed cost in each slot.
function Waterfall({ r }: { r: BookRoute }) {
  if (!r.available) return <p className="eg-muted" style={{ margin: 0 }}>Not priced: {r.reason}.</p>;
  const labels = [...new Set(r.slots.flatMap((s) => s.lines.map((l) => l.label ?? "")))];
  const bank = r.slots.map((s) => s.banking).find(Boolean);
  const line = (slot: number, label: string) => r.slots[slot].lines.find((l) => (l.label ?? "") === label);
  return (
    <div>
      <table className="ep-table ep-small">
        <thead>
          <tr>
            <th>₹/kWh</th>
            {r.slots.map((s) => <th key={s.slot} className="num">{slotLabel(s.slot)}</th>)}
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {labels.map((label) => {
            const first = r.slots.map((_, i) => line(i, label)).find(Boolean);
            return (
              <tr key={label}>
                <td>{label}</td>
                {r.slots.map((s, i) => <td key={s.slot} className="num"><Fig v={line(i, label)} /></td>)}
                <td>{first && <RefChips refs={first.source_refs} max={2} />}</td>
              </tr>
            );
          })}
          <tr style={{ fontWeight: 600 }}>
            <td>Landed cost</td>
            {r.slots.map((s) => <td key={s.slot} className="num"><Fig v={s.landed} strong /></td>)}
            <td></td>
          </tr>
          <tr>
            <td>Grid tariff</td>
            {r.slots.map((s) => <td key={s.slot} className="num"><Fig v={s.grid} /></td>)}
            <td></td>
          </tr>
          <tr>
            <td>Saving on the grid tariff</td>
            {r.slots.map((s) => <td key={s.slot} className="num"><Saving v={s.saving} /></td>)}
            <td></td>
          </tr>
          {bank && (
            <tr className="eg-muted">
              <td>Banking charge, on banked units only</td>
              {r.slots.map((s) => <td key={s.slot} className="num"><Fig v={s.banking} /></td>)}
              <td><RefChips refs={bank.source_refs} max={2} /></td>
            </tr>
          )}
        </tbody>
      </table>
      {(r.inputs?.length ?? 0) > 0 && (
        <p className="ep-small" style={{ margin: "4px 0" }}>
          Built from: {r.inputs!.map((x, i) => <span key={i}>{inputText(x)} <RefChips refs={x.source_refs} max={1} />{" "}</span>)}
        </p>
      )}
      {[...r.gaps.map((g) => `Not sourced: ${g.replace(/_/g, " ")}`), ...r.notes].map((n) => (
        <p key={n} className="eg-muted ep-small" style={{ margin: "6px 0 0" }}>{n}</p>
      ))}
    </div>
  );
}

export function BookView({ book }: { book: BookDetail }) {
  const c = markUnstored(book.content, book.documents_missing ?? []);
  const changes = c.changes ?? [];
  const gaps = Object.entries(c.gaps);
  const hasGrid = c.grid.length > 0;
  // how far the book's own figures (not the totals computed from them) are traced
  const base: Sourced[] = [
    ...c.grid.map((g) => g.rate),
    ...[...c.renewable.flatMap((x) => x.routes), ...c.exchange].flatMap((r) => r.slots.flatMap((sl) => [...sl.lines, ...(sl.banking ? [sl.banking] : [])])),
    ...c.limits.map((l) => l.value),
  ];
  const count = (k: string) => base.filter((v) => traceOf(v.source_refs) === k).length;
  const traced = { page: count("page"), unstored: count("unstored"), register: count("register"), assumption: count("assumption") };
  return (
    <div className="ep-book">
      {changes.length > 0 && (
        <section className="eg-card ep-panel">
          <div className="eg-overline" style={{ marginBottom: 8 }}>
            {changes.length} {changes.length === 1 ? "change" : "changes"} since
            {c.compared_with ? ` ${monthLabel(c.compared_with.month)}, issue ${c.compared_with.issue}` : " the last approved issue"}
          </div>
          <div style={{ overflowX: "auto", maxHeight: 320, overflowY: "auto" }}>
            <table className="ep-table ep-small">
              <thead><tr><th>Figure</th><th className="num">Before</th><th className="num">Now</th></tr></thead>
              <tbody>
                {changes.map((ch) => (
                  <tr key={ch.what}>
                    <td>{ch.what}</td>
                    <td className="num">{fig(ch.before)}</td>
                    <td className="num"><b>{fig(ch.after)}</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {c.compared_with === null && book.status === "draft" && (
        <p className="eg-muted ep-small" style={{ margin: 0 }}>First issue for this consumer: there is no approved book to compare with.</p>
      )}

      <section className="eg-card ep-panel">
        <div className="eg-overline" style={{ marginBottom: 8 }}>Where the numbers come from</div>
        <div className="ep-row">
          <span className="eg-chip eg-chip--accent">{traced.page} open a stored order or regulation page</span>
          {traced.unstored > 0 && <span className="eg-chip eg-chip--warning">{traced.unstored} cite a page whose PDF is not yet on this server</span>}
          <span className="eg-chip">{traced.register} cite an order by name (State Regulation Master)</span>
          <span className="eg-chip eg-chip--warning">{traced.assumption} assumed</span>
        </div>
        <p className="eg-muted ep-small" style={{ margin: "8px 0 0" }}>
          Click any number to see its source. A solid underline opens the page of the order; a dotted one is cited by name or assumed, and says so.
        </p>
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">Landed cost and saving at a glance</h2>
        <p className="eg-card__sub">Average over the day, ₹ per kWh at the consumer&apos;s meter. The saving is the grid tariff less the landed cost; the banking charge applies only to units banked and is shown with each route.</p>
        <div>
          <table className="ep-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Route</th>
                <th className="num">Landed cost</th>
                <th className="num">Saving</th>
              </tr>
            </thead>
            <tbody>
              {c.renewable.flatMap((src) =>
                src.routes.map((r, i) => (
                  <tr key={`${src.source}-${r.route}`}>
                    <td>{i === 0 ? src.label : ""}</td>
                    <td>{r.route_label}</td>
                    <td className="num">{r.available ? <Fig v={r.landed_avg} /> : <span className="eg-muted" title={r.reason ?? undefined}>not priced</span>}</td>
                    <td className="num">{r.available ? <Saving v={r.saving_avg} /> : "–"}</td>
                  </tr>
                )),
              )}
              {c.exchange.map((x) => (
                <tr key={x.source}>
                  <td>{x.label}</td>
                  <td>Power exchange</td>
                  <td className="num">{x.available ? <Fig v={x.landed_avg} /> : <span className="eg-muted">{x.reason}</span>}</td>
                  <td className="num">{x.available ? <Saving v={x.saving_avg} /> : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!hasGrid && <p className="eg-muted ep-small" style={{ margin: "8px 0 0" }}>No approved retail tariff for this DISCOM yet, so no saving is shown.</p>}
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">1. What the grid costs</h2>
        {hasGrid ? (
          <table className="ep-table">
            <thead><tr><th>Time-of-day slot</th><th>Hours</th><th className="num">₹/kWh</th><th>Source</th></tr></thead>
            <tbody>
              {c.grid.map((g) => {
                const s = c.slots.find((x) => x.name === g.slot);
                return (
                  <tr key={g.slot}>
                    <td>{slotLabel(g.slot)}</td>
                    <td className="nowrap">{s ? `${s.start}–${s.end}` : ""}</td>
                    <td className="num"><Fig v={g.rate} /></td>
                    <td><RefChips refs={g.rate.source_refs} max={2} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <p className="eg-muted" style={{ margin: 0 }}>The retail tariff schedule for this DISCOM is not approved yet.</p>
        )}
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">2. Open-access solar and wind, by route</h2>
        {c.renewable.map((src) => (
          <div key={src.source} className="ep-book__source">
            <h3 className="eg-h3" style={{ margin: "var(--space-4) 0 var(--space-2)" }}>{src.label}</h3>
            {src.routes.map((r, i) => (
              <details key={r.route} open={i === 0} className="ep-book__route">
                <summary>
                  <b>{r.route_label}</b>
                  <span className="eg-muted"> · {r.available ? `landed ₹${rate(r.landed_avg)}` : "not priced"}</span>
                  {r.available && r.saving_avg && <span> · saving ₹{rate(r.saving_avg)}</span>}
                </summary>
                <p className="eg-muted ep-small" style={{ margin: "6px 0" }}>{r.route_note}</p>
                <Waterfall r={r} />
              </details>
            ))}
          </div>
        ))}
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">3. Power exchange (IEX)</h2>
        {c.exchange.map((x) => (
          <details key={x.source} className="ep-book__route" open={x.available}>
            <summary>
              <b>{x.label}</b>
              <span className="eg-muted"> · {x.available ? `landed ₹${rate(x.landed_avg)}` : x.reason}</span>
            </summary>
            <Waterfall r={x} />
          </details>
        ))}
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">4. How open-access solar and wind are settled after banking</h2>
        {c.settlement.length === 0 && <p className="eg-muted" style={{ margin: 0 }}>No approved banking and settlement rule for this state yet.</p>}
        {c.settlement.map((b, i) => (
          <div key={i} style={{ marginTop: i ? "var(--space-4)" : 0 }}>
            <div className="ep-row">
              <b>{b.rule}</b>
              {b.rule_ref && <RefChips refs={[b.rule_ref as Ref]} />}
            </div>
            <p className="eg-muted ep-small" style={{ margin: "4px 0" }}>Applies to: {b.applies_to.join("; ")}</p>
            <Points points={b.points} />
          </div>
        ))}
      </section>

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">5. How an IEX trade is settled</h2>
        <Points points={c.iex_settlement} />
      </section>

      {c.limits.length > 0 && (
        <section className="eg-card ep-panel">
          <h2 className="eg-card__title">6. Limits</h2>
          <table className="ep-table">
            <tbody>
              {c.limits.map((l) => (
                <tr key={l.limit}>
                  <td>{l.label || l.limit.replace(/_/g, " ")}</td>
                  <td className="num"><Fig v={l.value} places={0} /> {l.value.unit === "percent" ? "%" : l.value.unit}</td>
                  <td><RefChips refs={l.value.source_refs} max={2} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {gaps.length > 0 && (
        <section className="eg-card ep-panel">
          <h2 className="eg-card__title">Not yet sourced</h2>
          <ul className="ep-book__points">
            {gaps.map(([k, v]) => <li key={k}><b>{k.replace(/_/g, " ")}</b>: {v}</li>)}
          </ul>
        </section>
      )}

      <section className="eg-card ep-panel">
        <h2 className="eg-card__title">Rules this book was computed from</h2>
        <table className="ep-table ep-small">
          <thead><tr><th>Rule family</th><th>Jurisdiction</th><th>Version</th><th>In force from</th><th>Title</th></tr></thead>
          <tbody>
            {c.versions.map((v) => (
              <tr key={v.id}>
                <td><a href={`/rulebooks/${v.id}`}>{FAMILY[v.family] ?? v.family}</a></td>
                <td>{v.jurisdiction.toUpperCase()}</td>
                <td>v{v.version}</td>
                <td className="nowrap">{v.effective_from}</td>
                <td className="eg-muted">{v.title}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="eg-muted ep-small" style={{ margin: "8px 0 0" }}>Content fingerprint {book.content_sha256.slice(0, 16)}</p>
      </section>
    </div>
  );
}

function fig(v: string | null): string {
  if (v === null) return "–";
  const n = Number(v);
  if (v.trim() !== "" && Number.isFinite(n)) return n.toFixed(2);
  return v.length > 90 ? `${v.slice(0, 90)}…` : v;
}
