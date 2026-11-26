import Link from "next/link";
import { GenerationProfiles } from "@/components/generation/GenerationProfiles";
import { apiOrNull } from "@/lib/api";
import type { ConnectionOut } from "@/lib/intake";

// The site's own generation profiles: a year of solar or wind from the developer or the plant's
// meter, used in place of the library curve when the plan is sized and settled.
export default async function GenerationPage({ searchParams }: { searchParams: Promise<{ c?: string }> }) {
  const { c } = await searchParams;
  const conn = c ? await apiOrNull<ConnectionOut>(`/connections/${c}`) : null;
  if (!c || !conn) {
    return (
      <section className="eg-card ep-panel">
        <h1 className="eg-h1">Generation profiles</h1>
        <p className="eg-muted">Pick an evaluation first.</p>
        <Link className="eg-btn eg-btn--primary" href="/">Your evaluations</Link>
      </section>
    );
  }
  return (
    <>
      <div>
        <div className="eg-overline">{conn.site.name}</div>
        <h1 className="eg-h1">Generation profiles</h1>
        <p className="eg-muted ep-lede">
          Give the solar or wind plant&apos;s own year of generation and the plan is sized and settled with it, hour by hour,
          in place of the library curve for the state. Each plan records the file it used, so a later file never changes a past plan.
        </p>
      </div>
      <GenerationProfiles siteId={conn.site.id} connectionId={c} />
    </>
  );
}
