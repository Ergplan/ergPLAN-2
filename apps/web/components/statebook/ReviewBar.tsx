"use client";

import { useEffect, useState } from "react";
import { analystFetch, getAnalyst, setAnalyst } from "@/lib/analyst";
import { monthLabel, STATUS_LABEL, STATUS_TONE, whoFor, type BookDetail } from "@/lib/statebook";

// The review decision. Approving publishes exactly the content shown (the API checks its hash); rejecting
// needs a reason, which stays on the book.
export function ReviewBar({ book, onChange }: { book: BookDetail; onChange: () => void }) {
  const [analyst, setAnalystState] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => setAnalystState(getAnalyst()), []);
  const c = book.content;
  const draft = book.status === "draft";

  async function act(what: "approve" | "reject") {
    setBusy(what);
    setError("");
    try {
      await analystFetch(`/statebooks/${book.id}/${what}`, { method: "POST", body: JSON.stringify({ note: note.trim() || null }) });
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  // The file is fetched with the reviewer's identity (a draft is not served to anyone else), then saved.
  async function download() {
    setError("");
    try {
      const who = getAnalyst();
      const r = await fetch(`/api/statebooks/${book.id}/file`, { headers: who ? { "X-Ergplan-Actor": who, "X-Ergplan-Role": "analyst" } : {} });
      if (!r.ok) throw new Error(`The file could not be downloaded (${r.status})`);
      const name = /filename="?([^";]+)"?/.exec(r.headers.get("Content-Disposition") ?? "")?.[1] ?? `rule-book-${book.state}-${book.month.slice(0, 7)}.md`;
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <section className="eg-glass" style={{ padding: "var(--space-6)" }}>
      <div className="eg-card__head">
        <div>
          <h1 className="eg-card__title" style={{ fontSize: 26 }}>
            {c.state_name} · {monthLabel(book.month)}
          </h1>
          <p className="eg-card__sub">
            {c.discom_name}
            {c.covers?.length ? ` (also ${c.covers.map((x) => x.name).join(", ")})` : ""} · {whoFor(c.consumer)} · issue {book.issue} · rules in force on {c.as_of}
          </p>
        </div>
        <span className={`eg-chip ${STATUS_TONE[book.status] ?? ""}`}>{STATUS_LABEL[book.status] ?? book.status}</span>
      </div>
      <div className="ep-row" style={{ alignItems: "flex-end", marginTop: "var(--space-3)" }}>
        {draft && (
          <>
            <label className="ep-field" style={{ minWidth: 220 }}>
              <span className="eg-overline">Reviewing as</span>
              <span className="eg-input">
                <input value={analyst} placeholder="name@joulewise.in" onChange={(e) => { setAnalystState(e.target.value); setAnalyst(e.target.value); }} />
              </span>
            </label>
            <label className="ep-field" style={{ flex: 1, minWidth: 240 }}>
              <span className="eg-overline">Note (needed to reject)</span>
              <span className="eg-input"><input value={note} maxLength={2000} placeholder="What you checked, or what is wrong" onChange={(e) => setNote(e.target.value)} /></span>
            </label>
            <button className="eg-btn eg-btn--primary" disabled={!!busy} onClick={() => void act("approve")}>
              {busy === "approve" ? "Approving…" : "Approve rule book"}
            </button>
            <button className="eg-btn eg-btn--ghost" disabled={!!busy || !note.trim()} onClick={() => void act("reject")}>
              {busy === "reject" ? "Rejecting…" : "Reject"}
            </button>
          </>
        )}
        <button className="eg-btn eg-btn--ghost" disabled={!!busy} onClick={() => void download()}>
          <span data-icon="download" data-size="16"></span> Download file
        </button>
      </div>
      {!draft && book.reviewed_by && (
        <p className="eg-muted" style={{ margin: "var(--space-3) 0 0" }}>
          {STATUS_LABEL[book.status]} · reviewed by {book.reviewed_by}
          {book.reviewed_at ? ` on ${new Date(book.reviewed_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}` : ""}
          {book.review_note ? ` · “${book.review_note}”` : ""}
        </p>
      )}
      {draft && (
        <p className="eg-muted ep-small" style={{ margin: "var(--space-3) 0 0" }}>
          Clients do not see this book until it is approved. Approving supersedes this consumer&apos;s earlier issue for the month.
        </p>
      )}
      {error && <p role="alert" style={{ color: "var(--negative, #b42318)", margin: "var(--space-3) 0 0" }}>{error}</p>}
    </section>
  );
}
