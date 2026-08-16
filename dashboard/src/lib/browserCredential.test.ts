import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  BROWSER_CREDENTIAL_MAX_AGE_SECONDS,
  mintBrowserCredential,
  resolveBrowserCredential,
} from "./browserCredential";

const SESSION_TOKEN = "server-only-session-token";
const NOW = 1_750_000_000_000;

function signPayload(encoded: string, sessionToken: string): string {
  const key = createHmac("sha256", sessionToken)
    .update("omniagentos/browser-credential/v1")
    .digest();
  return createHmac("sha256", key)
    .update(`omniagentos/browser-credential/v1.${encoded}`)
    .digest()
    .toString("base64url");
}

describe("browser credentials", () => {
  it("resolves a correctly signed credential to its trusted-hop principal", () => {
    const credential = mintBrowserCredential("owner@example.test", SESSION_TOKEN, NOW);

    expect(resolveBrowserCredential(credential, SESSION_TOKEN, NOW)).toEqual({ id: "owner@example.test" });
  });

  it("rejects a tampered credential instead of trusting its client-side payload", () => {
    const credential = mintBrowserCredential("owner@example.test", SESSION_TOKEN, NOW);
    const [payload, signature] = credential.split(".");
    const alteredPayload = Buffer.from(
      JSON.stringify({ version: 1, principal: "attacker@example.test", expiresAt: NOW + 1 }),
    ).toString("base64url");

    expect(resolveBrowserCredential(`${alteredPayload}.${signature}`, SESSION_TOKEN, NOW)).toBeNull();
    expect(resolveBrowserCredential(`${payload}.${signature}`, "another-server-token", NOW)).toBeNull();
  });

  it("rejects an expired credential", () => {
    const credential = mintBrowserCredential("owner@example.test", SESSION_TOKEN, NOW);

    expect(
      resolveBrowserCredential(credential, SESSION_TOKEN, NOW + BROWSER_CREDENTIAL_MAX_AGE_SECONDS * 1000),
    ).toBeNull();
  });

  it("fails closed for a signed payload with a non-string principal", () => {
    const encoded = Buffer.from(
      JSON.stringify({ version: 1, principal: null, expiresAt: NOW + 1 }),
      "utf8",
    ).toString("base64url");
    const credential = `${encoded}.${signPayload(encoded, SESSION_TOKEN)}`;

    expect(resolveBrowserCredential(credential, SESSION_TOKEN, NOW)).toBeNull();
  });
});
