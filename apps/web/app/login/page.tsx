"use client";

import { useEffect, useState } from "react";
import { signInModes } from "../../lib/signin";

function nextPath(): string {
  const n = new URLSearchParams(window.location.search).get("next") ?? "/";
  return n.startsWith("/") ? n : "/";
}

function remember(me: { email: string; role: string }) {
  try {
    if (me.role === "analyst") localStorage.setItem("ergplan-analyst", me.email);
    else localStorage.removeItem("ergplan-analyst");
  } catch {
    /* storage may be unavailable */
  }
}

// Sign in. Clients and analysts by an emailed one-time link. Where the server has an administrator
// account (Stage 13), the administrator by username and password; where it has none, the password
// form is not shown at all.
export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [modes, setModes] = useState<("password" | "link")[]>(["link"]);
  const [mode, setMode] = useState<"password" | "link">("link");
  useEffect(() => {
    fetch("/api/auth/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => {
        const offered = signInModes(m);
        setModes(offered);
        setMode(offered[0]);
      })
      .catch(() => undefined);
  }, []);
  const [state, setState] = useState<"idle" | "busy" | "sent">("idle");
  const [dev, setDev] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function signIn() {
    setState("busy");
    setError("");
    try {
      const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      if (!r.ok) {
        const d = (await r.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(typeof d.detail === "string" ? d.detail : r.status === 422 ? "Type the username and the password." : `${r.status}`);
      }
      remember((await r.json()) as { email: string; role: string });
      window.location.replace(nextPath());
    } catch (e) {
      setError((e as Error).message);
      setState("idle");
    }
  }

  async function sendLink() {
    setState("busy");
    setError("");
    try {
      const r = await fetch("/api/auth/request", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, next: nextPath() }) });
      if (!r.ok) throw new Error(r.status === 422 ? "That does not look like an email address." : r.status === 503 ? "Sign-in mail is not set up on this server yet. Ask jouleWise for a sign-in link." : r.status === 429 ? "Too many requests from this address. Try again in a minute." : `${r.status}`);
      const d = (await r.json()) as { sent: boolean; dev_link: string | null };
      setDev(d.dev_link);
      setState("sent");
    } catch (e) {
      setError((e as Error).message);
      setState("idle");
    }
  }

  const ready = mode === "link" ? email.includes("@") : username.length > 0 && password.length > 0;
  return (
    <section className="eg-card ep-panel" style={{ maxWidth: 520 }}>
      <div className="eg-overline">jouleWise</div>
      <h1 className="eg-h1">Sign in</h1>
      {state === "sent" ? (
        <>
          <p>We sent a sign-in link to <b>{email}</b>. It works once, for 15 minutes.</p>
          {dev && <p className="ep-small eg-muted">Development: no mail server is set up. <a href={dev}>Open the link</a>.</p>}
        </>
      ) : (
        <form className="ep-form" onSubmit={(e) => { e.preventDefault(); if (ready) void (mode === "password" ? signIn() : sendLink()); }}>
          {mode === "link" ? (
            <label className="ep-field">
              <span className="ep-small eg-muted">Email</span>
              <span className="eg-input"><input type="email" autoComplete="email" aria-label="Email" placeholder="you@company.in" value={email} onChange={(e) => setEmail(e.target.value)} /></span>
            </label>
          ) : (
            <>
              <label className="ep-field">
                <span className="ep-small eg-muted">Username</span>
                <span className="eg-input"><input type="text" autoComplete="username" autoCapitalize="none" spellCheck={false} aria-label="Username" value={username} onChange={(e) => setUsername(e.target.value)} /></span>
              </label>
              <label className="ep-field">
                <span className="ep-small eg-muted">Password</span>
                <span className="eg-input"><input type="password" autoComplete="current-password" aria-label="Password" value={password} onChange={(e) => setPassword(e.target.value)} /></span>
              </label>
            </>
          )}
          <div className="ep-row">
            <button className="eg-btn eg-btn--primary" type="submit" disabled={state === "busy" || !ready}>
              {mode === "password" ? "Sign in" : "Email me a link"}
            </button>
            {modes.length > 1 && (
              <button className="eg-btn eg-btn--ghost eg-btn--sm" type="button" onClick={() => { setMode(mode === "password" ? "link" : "password"); setError(""); }}>
                {mode === "password" ? "Sign in with an emailed link" : "Administrator: sign in with a password"}
              </button>
            )}
          </div>
        </form>
      )}
      {error && <p className="ep-error">{error}</p>}
    </section>
  );
}
