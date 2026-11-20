"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { GeneratePanel } from "@/components/statebook/GeneratePanel";
import { analystFetch } from "@/lib/analyst";
import { monthLabel, STATUS_LABEL, STATUS_TONE, whoFor, type Book } from "@/lib/statebook";

type Discom = { id: string; name: string; state: string; state_name: string };

// The month's rule books by state, with the drafts awaiting review first in the count.
export function BookList({ month, status }: { month?: string; status?: string }) {
  const q = { month, status };
  const [books, setBooks] = useState<Book[]>([]);
  const [discoms, setDiscoms] = useState<Discom[]>([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (month) qs.set("month", `${month}-01`);
    if (status) qs.set("status", status);
    try {
      setBooks(await analystFetch<Book[]>(`/statebooks${qs.size ? `?${qs}` : ""}`));
      setError("");
    } catch (e) {
      setBooks([]);
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [month, status]);
  useEffect(() => {
    void load();
    analystFetch<Discom[]>("/intake/discoms").then(setDiscoms).catch(() => setDiscoms([]));
  }, [load]);
  const name = new Map(discoms.map((d) => [d.id, d]));
  const pending = books.filter((b) => b.status === "draft");
  const byState = new Map<string, Book[]>();
  for (const b of books) byState.set(b.state, [...(byState.get(b.state) ?? []), b]);
  const states = [...byState.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const filter = (status?: string) => {
    const p = new URLSearchParams();
    if (q.month) p.set("month", q.month);
    if (status) p.set("status", status);
    return `/statebooks${p.size ? `?${p}` : ""}`;
  };
  return (
    <>
      <h1 className="eg-h1">
        Monthly <em>rule books</em>
      </h1>
      <p className="eg-muted" style={{ margin: 0, maxWidth: 780 }}>
        One book per state each month, and one per client connection: the landed cost of open-access solar and wind
        and of exchange power by route, the saving on the grid tariff, and how the energy is settled. A book reaches
        a client only after an analyst approves it.
      </p>
      <div className="ep-row">
        <Link className={`eg-chip ${!q.status ? "eg-chip--accent" : ""}`} href={filter()}>All · {books.length}</Link>
        <Link className={`eg-chip ${q.status === "draft" ? "eg-chip--accent" : "eg-chip--warning"}`} href={filter("draft")}>
          Awaiting review{!q.status ? ` · ${pending.length}` : ""}
        </Link>
        <Link className={`eg-chip ${q.status === "approved" ? "eg-chip--accent" : ""}`} href={filter("approved")}>Approved</Link>
        <Link className={`eg-chip ${q.status === "rejected" ? "eg-chip--accent" : ""}`} href={filter("rejected")}>Rejected</Link>
        {q.month && <span className="eg-chip">{monthLabel(`${q.month}-01`)}</span>}
      </div>
      <GeneratePanel discoms={discoms.map((d) => ({ id: d.id, name: d.name, state_name: d.state_name }))} onChange={() => void load()} />
      {error && <p role="alert" className="ep-empty">Sign in as an analyst, or enter your email under “Reviewing as”, to see the rule books. ({error})</p>}
      {loaded && !error && books.length === 0 && <p className="ep-empty">No rule books yet. Generate this month&apos;s books above.</p>}
      {states.map(([state, bs]) => (
        <section key={state} className="eg-card">
          <div className="eg-card__head">
            <h2 className="eg-card__title">
              <span data-icon="layers" data-size="20"></span>
              {name.get(bs[0].discom_id)?.state_name ?? state}
            </h2>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="ep-table">
              <thead>
                <tr>
                  <th>Month</th>
                  <th>DISCOM</th>
                  <th>For</th>
                  <th>Issue</th>
                  <th>Status</th>
                  <th className="num">Changes</th>
                  <th className="num">Not sourced</th>
                  <th>Reviewed by</th>
                </tr>
              </thead>
              <tbody>
                {bs.map((b) => (
                  <tr key={b.id} style={b.status === "superseded" || b.status === "rejected" ? { opacity: 0.55 } : undefined}>
                    <td className="nowrap">
                      <Link href={`/statebooks/${b.id}`}>{monthLabel(b.month)}</Link>
                    </td>
                    <td>{name.get(b.discom_id)?.name ?? b.discom_id}</td>
                    <td>{whoFor(b.consumer)}</td>
                    <td>{b.issue}</td>
                    <td>
                      <span className={`eg-chip ${STATUS_TONE[b.status] ?? ""}`}>{STATUS_LABEL[b.status] ?? b.status}</span>
                    </td>
                    <td className="num">{b.changes || "–"}</td>
                    <td className="num">{b.gaps.length || "–"}</td>
                    <td className="eg-muted ep-small">{b.reviewed_by ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </>
  );
}
