import { statedOn, type StatedAnswer } from "@/lib/questions";

// What the client told us about the plant, on the plan: each answer a "you said" chip (client-stated,
// never a sourced fact). With `editHref` the chip opens the question so the answer can be changed.
export function StatedAnswers({ answers, profileWords, editHref }: { answers: StatedAnswer[]; profileWords?: string | null; editHref?: string }) {
  if (answers.length === 0 && !profileWords) return null;
  return (
    <section className="eg-card ep-panel" data-testid="stated-answers">
      <h2 className="eg-card__title">What you told us about the plant</h2>
      {profileWords && <p className="ep-small eg-muted" data-testid="profile-words">Load profile: {profileWords}</p>}
      {answers.length === 0 && <p className="eg-muted">Nothing yet beyond the bills.{editHref ? " A few answers sharpen the plan." : ""}</p>}
      <div className="ep-facts">
        {answers.map((a) => (
          <div key={a.key} className="ep-fact" data-testid={`stated-${a.key}`}>
            <div className="ep-fact__label">{a.label}</div>
            <div className="ep-fact__value"><b>{a.words}</b></div>
            <div>
              {editHref ? (
                <a className="eg-chip" href={`${editHref}#q-${a.key}`} title={`${statedOn(a)}; open to change it`} style={{ textDecoration: "none" }}>you said · change</a>
              ) : (
                <span className="eg-chip" title={statedOn(a)}>you said</span>
              )}
            </div>
            <div className="ep-fact__note eg-muted">
              {a.used_for}
              {!a.in_use && " A measured meter file is in use, so this answer shapes nothing now."}
              {a.in_this_plan === false && <> <span className="eg-chip eg-chip--warning">changed since this plan was sized</span> Size the plan again to use it.</>}
            </div>
          </div>
        ))}
      </div>
      {editHref && <p style={{ marginTop: 12 }}><a className="eg-btn eg-btn--ghost eg-btn--sm" href={editHref}>Answer or change the questions about your plant</a></p>}
    </section>
  );
}
