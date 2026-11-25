import { describe, expect, it } from "vitest";
import { signInModes, signInWarning, type Me } from "../lib/signin";

const analyst: Me = { email: "plantadmin", role: "analyst", signed_in: true, required: true, password_sign_in: true, warnings: [] };

describe("sign-in page", () => {
  it("offers only the emailed link when the server has no administrator account", () => {
    expect(signInModes(null)).toEqual(["link"]);
    expect(signInModes({ password_sign_in: false })).toEqual(["link"]);
    expect(signInModes({})).toEqual(["link"]);
  });
  it("offers the password form first when it has one", () => {
    expect(signInModes({ password_sign_in: true })).toEqual(["password", "link"]);
  });
});

describe("weak-password banner", () => {
  it("is shown to a signed-in analyst with the server's reasons", () => {
    const text = signInWarning({ ...analyst, warnings: ["the administrator password is shorter than 12 characters", "the administrator password is the username with a suffix"] });
    expect(text).toContain("shorter than 12 characters; the administrator password is the username with a suffix");
    expect(text).toContain("ERGPLAN_ADMIN_PASSWORD");
  });
  it("is not shown without a warning, to a client, or signed out", () => {
    expect(signInWarning(analyst)).toBeNull();
    expect(signInWarning(null)).toBeNull();
    expect(signInWarning({ ...analyst, role: "client", warnings: ["x"] })).toBeNull();
    expect(signInWarning({ ...analyst, signed_in: false, warnings: ["x"] })).toBeNull();
  });
});
