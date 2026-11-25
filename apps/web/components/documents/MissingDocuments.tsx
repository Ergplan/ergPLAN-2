"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getAnalyst } from "@/lib/analyst";
import type { MissingDocument } from "@/lib/api";

// Analyst: the orders and regulations approved rules cite whose PDF is not in this server's library,
// and a form to add one. A document's id is its SHA-256, so the same file as on the machine the rule
// was written on resolves every citation to it; a different copy of the order is stored, but the
// citations stay unresolved, and the form says so.
export function MissingDocuments({ missing }: { missing: MissingDocument[] }) {
  const router = useRouter();
  const file = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [analyst, setAnalyst] = useState("");
  useEffect(() => setAnalyst(getAnalyst()), []);

  async function upload() {
    const f = file.current?.files?.[0];
    if (!f || !title.trim()) return;
    setBusy(true);
    setError("");
    setMsg("");
    const fd = new FormData();
    fd.append("file", f);
    fd.append("title", title.trim());
    if (analyst) fd.append("uploaded_by", analyst);
    try {
      const r = await fetch("/api/documents", { method: "POST", body: fd, headers: { "X-Ergplan-Actor": analyst, "X-Ergplan-Role": "analyst" } });
      const body = await r.json();
      if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : `${r.status}`);
      const hit = missing.some((m) => m.document_id === body.id);
      setMsg(hit
        ? `Stored. The rules that cite “${body.title}” now open its pages.`
        : missing.length
          ? `Stored as “${body.title}”, but it is not byte-for-byte one of the files listed here, so their citations stay unresolved.`
          : `Stored as “${body.title}”.`);
      setTitle("");
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="eg-card ep-panel">
      <div className="eg-overline" style={{ marginBottom: 8 }}>
        {missing.length > 0 ? `${missing.length} cited ${missing.length === 1 ? "document is" : "documents are"} not yet on this server` : "Add a document"}
      </div>
      {missing.length > 0 && (
        <>
          <p className="eg-muted ep-small" style={{ margin: "0 0 8px" }}>
            Approved rules quote these pages, but the PDF is not in this library, so their numbers say so instead of opening a page.
            Add the same file that was used when the rule was written.
          </p>
          <div style={{ overflowX: "auto" }}>
            <table className="ep-table ep-small">
              <thead><tr><th>Cited in</th><th>State</th><th className="num">Rules</th><th>Pages</th><th>Quoted</th></tr></thead>
              <tbody>
                {missing.map((m) => (
                  <tr key={m.document_id}>
                    <td>{m.cited_in.slice(0, 2).join(" · ") || m.document_id}</td>
                    <td>{m.jurisdictions.join(", ")}</td>
                    <td className="num">{m.rules}</td>
                    <td className="nowrap">{m.pages.length ? `${m.pages[0]}–${m.pages[m.pages.length - 1]}` : "—"}</td>
                    <td>{m.quote ? `“${m.quote.slice(0, 110)}${m.quote.length > 110 ? "…" : ""}”` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="ep-form" style={{ marginTop: 12 }}>
        <label className="eg-field"><span className="eg-field__label">PDF</span>
          <span className="eg-input"><input ref={file} type="file" accept="application/pdf,.pdf" /></span></label>
        <label className="eg-field"><span className="eg-field__label">Title</span>
          <span className="eg-input"><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="KERC Tariff Order FY 2025-26" /></span></label>
        <button className="eg-btn eg-btn--primary" style={{ alignSelf: "end" }} disabled={busy || !title.trim()} onClick={upload}>
          {busy ? "Storing…" : "Add to the library"}
        </button>
      </div>
      {msg && <p className="ep-small" style={{ margin: "8px 0 0" }}>{msg}</p>}
      {error && <p className="ep-small" style={{ margin: "8px 0 0", color: "var(--negative, #b42318)" }}>{error}</p>}
    </section>
  );
}
