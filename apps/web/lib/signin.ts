// Who is signed in, as GET /auth/me answers (Stage 13). `password_sign_in` says the server has an
// administrator account (username and password); `warnings` is filled only for a signed-in analyst.
export type Me = {
  email: string | null;
  role: string;
  signed_in: boolean;
  required: boolean;
  password_sign_in?: boolean;
  warnings?: string[];
};

// The forms the sign-in page offers: the emailed link always; the password form only when the
// server has an administrator account. Until the server has answered, only the link.
export function signInModes(me: Pick<Me, "password_sign_in"> | null): ("password" | "link")[] {
  return me?.password_sign_in ? ["password", "link"] : ["link"];
}

// The banner on the analyst's screens when the administrator's password is weak; null otherwise.
// A client, or a signed-out visitor, is never told about the server's set-up.
export function signInWarning(me: Me | null): string | null {
  if (!me || !me.signed_in || me.role !== "analyst" || !me.warnings || me.warnings.length === 0) return null;
  const reasons = me.warnings.join("; ");
  return `Weak sign-in: ${reasons}. Change ERGPLAN_ADMIN_PASSWORD in the server's .env (twelve characters or more, not the username with a suffix) and restart the api.`;
}
