import Link from "next/link";
import { Questions } from "@/components/intake/Questions";
import { apiOrNull } from "@/lib/api";
import type { ConnectionOut, ProfileOut } from "@/lib/intake";
import type { QuestionsOut } from "@/lib/questions";

export const metadata = { title: "About your plant · ergPLAN" };

// What the bills do not say: shifts, the weekly off, shutdowns, large loads, the power factor, planned
// expansion, the roof and land free, the contract demand intended. Each answer redraws the profile.
export default async function QuestionsPage({ searchParams }: { searchParams: Promise<{ c?: string }> }) {
  const { c } = await searchParams;
  const conn = c ? await apiOrNull<ConnectionOut>(`/connections/${c}`) : null;
  if (!c || !conn) {
    return (
      <section className="eg-card ep-panel">
        <h1 className="eg-h1">About your plant</h1>
        <p className="eg-muted">These questions open once a site exists. Start with your bill.</p>
        <Link className="eg-btn eg-btn--primary" href="/evaluate">New evaluation</Link>
      </section>
    );
  }
  const [questions, profile] = await Promise.all([
    apiOrNull<QuestionsOut>(`/sites/${conn.site.id}/questions`),
    apiOrNull<ProfileOut>(`/connections/${c}/profile`),
  ]);
  if (!questions) return <p className="ep-empty">The questions for this site could not be read.</p>;
  return (
    <>
      <div>
        <div className="eg-overline">Detail</div>
        <h1 className="eg-h1">About your plant</h1>
        <p className="eg-muted ep-lede">
          {conn.client.name} · {conn.site.name}. Your bills give the units, the time-of-day split and the highest demand of each month. They do not say
          when the plant works. A few answers here shape the load profile the plan is sized on; each one is kept as what you said, and you can change or
          withdraw it. Nothing here is needed to get a plan.
        </p>
        <div className="ep-row" style={{ marginTop: "var(--space-3)" }}>
          <Link className="eg-btn eg-btn--ghost eg-btn--sm" href={`/evaluate?c=${c}`}>Your inputs</Link>
          <Link className="eg-btn eg-btn--ghost eg-btn--sm" href={`/results?c=${c}`}>Results</Link>
          <Link className="eg-btn eg-btn--ghost eg-btn--sm" href={`/plan/report?c=${c}`}>Your plan</Link>
        </div>
      </div>
      <Questions connectionId={c} siteId={conn.site.id} initial={questions} initialProfile={profile} />
    </>
  );
}
