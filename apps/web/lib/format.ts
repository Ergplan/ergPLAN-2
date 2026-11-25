// Indian grouping for rupees and counts (Harmony rule: every number has a unit; Indian grouping).
export function inr(value: string | number, places = 2): string {
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString("en-IN", { minimumFractionDigits: places, maximumFractionDigits: places });
}

// A quantity as printed: Indian grouping and no trailing zeros the source never had ("33.000" is
// 33 kV, "5000.000" is 5,000 kVA). Text that is not a number is shown as it stands.
export function qty(value: string | number | null | undefined, fallback = "?"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString("en-IN", { maximumFractionDigits: 3 });
}

export function pageRanges(pages: number[]): string {
  const s = [...new Set(pages)].sort((a, b) => a - b);
  const out: string[] = [];
  for (let i = 0; i < s.length; i++) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
    out.push(i === j ? `${s[i]}` : `${s[i]}–${s[j]}`);
    i = j;
  }
  return out.join(", ");
}

export function pdfPageUrl(documentId: string, page?: number | null): string {
  return `/api/documents/${documentId}${page ? `#page=${page}` : ""}`;
}
