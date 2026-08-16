import { createHmac, timingSafeEqual } from "node:crypto";

/** Host-only by construction: `__Host-` cookies may not carry Domain. */
export const BROWSER_CREDENTIAL_COOKIE = "__Host-omni-browser-session";
export const BROWSER_CREDENTIAL_MAX_AGE_SECONDS = 8 * 60 * 60;

type CredentialPayload = {
  version: 1;
  principal: string;
  expiresAt: number;
};

export type DashboardPrincipal = Readonly<{
  id: string;
}>;

function signingKey(sessionToken: string): Buffer {
  // Do not use the FastAPI bearer token directly as a cookie MAC key.  This
  // derives a purpose-specific key while keeping the bearer token server-only.
  return createHmac("sha256", sessionToken).update("omniagentos/browser-credential/v1").digest();
}

function signature(payload: string, sessionToken: string): Buffer {
  return createHmac("sha256", signingKey(sessionToken))
    .update(`omniagentos/browser-credential/v1.${payload}`)
    .digest();
}

function isValidPrincipal(principal: unknown): principal is string {
  // Principal creation belongs to Phase 2.  P0 preserves the trusted-hop
  // identity exactly, but rejects control characters and empty values before
  // placing it in a signed browser credential.
  return (
    typeof principal === "string" &&
    principal.length > 0 &&
    principal.length <= 256 &&
    !/[\u0000-\u001f\u007f]/.test(principal)
  );
}

/** Mint a signed, opaque-to-the-client proof that resolves to a principal. */
export function mintBrowserCredential(
  principal: string,
  sessionToken: string,
  now = Date.now(),
): string {
  if (!isValidPrincipal(principal)) throw new Error("trusted-hop principal is invalid");

  const payload: CredentialPayload = {
    version: 1,
    principal,
    expiresAt: now + BROWSER_CREDENTIAL_MAX_AGE_SECONDS * 1000,
  };
  const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${encoded}.${signature(encoded, sessionToken).toString("base64url")}`;
}

/**
 * Verify a browser credential and resolve its principal without trusting any
 * client-provided identity header.  The constant-time comparison is important:
 * this is an authentication boundary, not merely cookie parsing.
 */
export function resolveBrowserCredential(
  credential: string | undefined,
  sessionToken: string,
  now = Date.now(),
): DashboardPrincipal | null {
  if (!credential) return null;
  const separator = credential.lastIndexOf(".");
  if (separator < 1 || separator === credential.length - 1) return null;

  const encoded = credential.slice(0, separator);
  const suppliedSignature = credential.slice(separator + 1);
  let supplied: Buffer;
  let expected: Buffer;
  let payload: CredentialPayload;
  try {
    supplied = Buffer.from(suppliedSignature, "base64url");
    expected = signature(encoded, sessionToken);
    payload = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as CredentialPayload;
  } catch {
    return null;
  }

  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return null;
  if (
    payload.version !== 1 ||
    !isValidPrincipal(payload.principal) ||
    !Number.isSafeInteger(payload.expiresAt) ||
    payload.expiresAt <= now
  ) {
    return null;
  }
  return { id: payload.principal };
}
