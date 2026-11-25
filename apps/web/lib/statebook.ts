// State rule books: one month's issue for a DISCOM (a reference industrial consumer, or one client's
// connection) with landed cost by source and route, the saving on the grid tariff and how energy is settled.
import type { Ref } from "@/lib/intake";

export type Sourced = { value: string; unit: string; source_refs: Ref[]; label?: string };

export type BookSlot = { slot: string; lines: Sourced[]; landed: Sourced; grid: Sourced | null; saving: Sourced | null; banking?: Sourced | null };

export type BookRoute = {
  route?: string;
  route_label?: string;
  route_note?: string;
  source: string;
  label: string;
  available: boolean;
  reason: string | null;
  slots: BookSlot[];
  landed_avg: Sourced | null;
  saving_avg: Sourced | null;
  gaps: string[];
  notes: string[];
};

export type BookPoint = { text: string; ref: Ref | null };

export type BookContent = {
  title: string;
  state: string;
  state_name: string;
  discom: string;
  discom_name: string;
  month: string;
  as_of: string;
  consumer: { kind: string; client: string | null; site: string | null; category: string | null; rate_block: string | null; voltage_kv: string | null; contract_demand_kva: string | null };
  slots: { name: string; start: string; end: string; hours: string }[];
  grid: { slot: string; rate: Sourced }[];
  renewable: { source: string; label: string; routes: BookRoute[] }[];
  exchange: BookRoute[];
  settlement: { rule: string; rule_ref: Ref | null; method: string; applies_to: string[]; points: BookPoint[] }[];
  iex_settlement: BookPoint[];
  limits: { limit: string; label: string; value: Sourced }[];
  versions: { id: string; family: string; jurisdiction: string; version: number; title: string; origin: string; effective_from: string }[];
  gaps: Record<string, string>;
  covers?: { discom: string; name: string }[];
  changes?: { what: string; before: string | null; after: string | null }[];
  compared_with?: { id: string; month: string; issue: number; title: string } | null;
};

export type Book = {
  id: string;
  state: string;
  discom_id: string;
  connection_id: string | null;
  month: string;
  issue: number;
  status: "draft" | "approved" | "superseded" | "rejected";
  title: string;
  consumer: BookContent["consumer"];
  changes: number;
  gaps: string[];
  created_by: string;
  created_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
};

export type BookDetail = Book & {
  inputs: Record<string, unknown>;
  content: BookContent;
  content_sha256: string;
  // documents the book cites whose PDF is not in this server's library now
  documents_missing?: string[];
};

// A copy of a book's content in which every order or regulation ref to a document that is not in the
// server's library carries document_stored: false, so its figure says so instead of linking to a page.
export function markUnstored<T>(node: T, missing: string[]): T {
  if (missing.length === 0) return node;
  const gone = new Set(missing);
  const walk = (n: unknown): unknown => {
    if (Array.isArray(n)) return n.map(walk);
    if (n === null || typeof n !== "object") return n;
    const o = n as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(o)) out[k] = walk(v);
    if ((o.kind === "order" || o.kind === "regulation") && typeof o.document_id === "string" && gone.has(o.document_id)) {
      out.document_stored = false;
    }
    return out;
  };
  return walk(node) as T;
}

export const STATUS_TONE: Record<string, string> = {
  approved: "eg-chip--accent",
  draft: "eg-chip--warning",
  rejected: "eg-chip--negative",
  superseded: "",
};

export const STATUS_LABEL: Record<string, string> = {
  approved: "Approved",
  draft: "Awaiting review",
  rejected: "Rejected",
  superseded: "Superseded",
};

// A rate in the book, to the paisa. The stored value keeps full precision.
export function rate(v: Sourced | null | undefined): string {
  if (!v) return "–";
  const n = Number(v.value);
  return Number.isFinite(n) ? n.toFixed(2) : v.value;
}

export function monthLabel(iso: string): string {
  const [y, m] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-IN", { month: "long", year: "numeric", timeZone: "UTC" });
}

export function slotLabel(s: string): string {
  return s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function whoFor(c: Book["consumer"]): string {
  if (c.kind !== "reference") return [c.client, c.site].filter(Boolean).join(" · ") || "Client connection";
  return `Reference consumer · ${c.category ?? "HT industrial"} · ${c.voltage_kv ?? "?"} kV · ${c.contract_demand_kva ?? "?"} kVA`;
}
