import type { Ref } from "@/lib/intake";
import type { Sourced } from "@/lib/statebook";

const KIND: Record<string, string> = {
  register: "State Regulation Master",
  assumption: "ergPLAN's assumption",
  client_stated: "Stated by the client",
  bill: "From the client's bill",
  measured: "Metered",
};

// How far a figure is traced: every source opens a stored page; a cited page's PDF is not in this
// server's library; some are cited by name only; or assumed.
export function traceOf(refs: Ref[]): "page" | "unstored" | "register" | "assumption" | "other" {
  const real = refs.filter((r) => r.kind !== "derived");
  if (real.some((r) => r.kind === "assumption")) return "assumption";
  if (real.some((r) => r.kind === "register")) return "register";
  if (real.length && real.every((r) => (r.kind === "order" || r.kind === "regulation") && r.document_id && r.page)) {
    return real.some((r) => r.document_stored === false) ? "unstored" : "page";
  }
  return "other";
}

function Source({ r }: { r: Ref }) {
  if ((r.kind === "order" || r.kind === "regulation") && r.document_stored === false) {
    return (
      <li>
        <b>{r.document_title ?? `The ${r.kind}`}, page {r.page}</b>
        <em>This PDF is not yet in this server&apos;s library, so the page does not open here.</em>
        {r.quote && <q>{r.quote.length > 260 ? `${r.quote.slice(0, 260)}…` : r.quote}</q>}
      </li>
    );
  }
  if ((r.kind === "order" || r.kind === "regulation") && r.document_id && r.page) {
    return (
      <li>
        <a href={`/api/documents/${r.document_id}#page=${r.page}`} target="_blank" rel="noreferrer">
          {r.document_title ?? `The ${r.kind}`}, page {r.page}
        </a>
        {r.url_public && <span> · <a href={r.url_public} target="_blank" rel="noreferrer">regulator&apos;s copy</a></span>}
        {r.quote && <q>{r.quote.length > 260 ? `${r.quote.slice(0, 260)}…` : r.quote}</q>}
      </li>
    );
  }
  if (r.kind === "bill" && r.bill_id) {
    return (
      <li>
        <a href={`/api/bills/${r.bill_id}/file`} target="_blank" rel="noreferrer">Open the bill</a>
        {r.field_path && <span> · {r.field_path}</span>}
      </li>
    );
  }
  return (
    <li>
      <b>{KIND[r.kind] ?? r.kind}</b>
      {r.note && <span>: {r.note}</span>}
      {r.kind === "register" && <em>Cited by name. The order is not yet stored, so no page opens.</em>}
    </li>
  );
}

// A number you can click: it opens the order page, register citation or assumption it comes from.
export function Fig({ v, places = 2, strong, tone }: { v: Sourced | null | undefined; places?: number; strong?: boolean; tone?: boolean }) {
  if (!v) return <span className="eg-muted">–</span>;
  const n = Number(v.value);
  const text = Number.isFinite(n) ? n.toFixed(places) : v.value;
  const refs = v.source_refs.filter((r) => r.kind !== "derived");
  const seen = new Set<string>();
  const unique = refs.filter((r) => {
    const k = `${r.kind}|${r.document_id ?? ""}|${r.page ?? ""}|${r.note ?? ""}|${r.bill_id ?? ""}|${r.field_path ?? ""}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  const trace = traceOf(v.source_refs);
  const color = tone ? (n < 0 ? "var(--negative, #b42318)" : "var(--positive, #0a7d4f)") : undefined;
  return (
    <details className={`ep-fig ep-fig--${trace}`} name="ep-fig">
      <summary style={{ color, fontWeight: strong ? 600 : undefined }} title="Show where this number comes from">{text}</summary>
      <div className="ep-fig__pop">
        <div className="eg-overline">
          {v.label ?? "Source"} · {text} {v.unit === "INR/kWh" ? "₹/kWh" : v.unit}
        </div>
        {unique.length > 1 && <p>Computed from {unique.length} sources:</p>}
        <ul>{unique.slice(0, 12).map((r, i) => <Source key={i} r={r} />)}</ul>
        {unique.length > 12 && <p>and {unique.length - 12} more.</p>}
        {unique.length === 0 && <p>No source is recorded for this figure.</p>}
      </div>
    </details>
  );
}
