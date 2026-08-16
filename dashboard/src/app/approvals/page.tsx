import { redirect } from "next/navigation";

/**
 * /approvals was renamed to /inbox in the P0 IA rebuild (Approvals is now the
 * default tab inside the Inbox shell). This stub keeps every old bookmark,
 * Slack deep link, and `?tab=` query string working — it forwards the whole
 * search string unchanged so `/approvals?tab=decisions` still lands on the
 * Decisions tab.
 */
export default async function ApprovalsRedirectPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const v of value) qs.append(key, v);
    } else {
      qs.append(key, value);
    }
  }
  const suffix = qs.toString();
  redirect(suffix ? `/inbox?${suffix}` : "/inbox");
}
