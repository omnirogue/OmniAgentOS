/**
 * The risk ramp — the whole point of this UI. One canonical mapping from
 * ActionClass to tone/label/copy, imported everywhere a capability is rendered
 * (catalogue, agent cards, agent detail, create-agent blast-radius panel) so the
 * same class always reads the same way.
 *
 * Tone choice matches the precedent already set for run steps in
 * app/runs/[id]/page.tsx (`actionTones`) — read_only/ok, sandboxed_creation/challenger,
 * internal_reversible/promote, external_reversible/warn, consequential/danger — so a
 * capability grant and a run's executed steps render identically.
 */
import type { BadgeTone, PillTone } from "@/design";
import type { ActionClass, CapabilitiesCatalog, CapabilityInfo, GrantSummary } from "./types";

export const ACTION_CLASS_ORDER: ActionClass[] = [
  "read_only",
  "sandboxed_creation",
  "internal_reversible",
  "external_reversible",
  "consequential",
  "irreversible",
];

export const ACTION_CLASS_LABEL: Record<ActionClass, string> = {
  read_only: "Read only",
  sandboxed_creation: "Sandboxed creation",
  internal_reversible: "Auto-write",
  external_reversible: "Needs approval",
  consequential: "Always human",
  irreversible: "Irreversible",
};

/** One-line explanation shown in tooltips/legends — the exact risk-tier wording from the brief. */
export const ACTION_CLASS_BLURB: Record<ActionClass, string> = {
  read_only: "Runs automatically. Reads nothing but data.",
  sandboxed_creation: "Runs automatically. Produces artifacts in the run workspace.",
  internal_reversible: "Runs automatically. Mutates our own systems, trivially undone.",
  external_reversible: "A customer sees it. Parks for human approval before it runs.",
  consequential: "Refunds, charges, payouts, ad launch/budget, DNS, DB writes, deletion. Always a human — hard-blocked in code.",
  irreversible: "Cannot be undone once done. The sole member of HARD_STOP_CLASSES: always a human, refused in code, and is_hard_stop fails CLOSED on anything unrecognised.",
};

export const ACTION_CLASS_BADGE_TONE: Record<ActionClass, BadgeTone> = {
  read_only: "ok",
  sandboxed_creation: "challenger",
  internal_reversible: "promote",
  external_reversible: "warn",
  consequential: "danger",
  irreversible: "danger",
};

export const ACTION_CLASS_PILL_TONE: Record<ActionClass, PillTone> = {
  read_only: "ok",
  sandboxed_creation: "accent",
  internal_reversible: "promote",
  external_reversible: "warn",
  consequential: "danger",
  irreversible: "danger",
};

/** True for classes that may run with no human in the loop (read_only/sandboxed_creation/internal_reversible). */
export function isAutoClass(actionClass: ActionClass): boolean {
  return (
    actionClass === "read_only" ||
    actionClass === "sandboxed_creation" ||
    actionClass === "internal_reversible"
  );
}

export function actionClassRank(actionClass: ActionClass): number {
  return ACTION_CLASS_ORDER.indexOf(actionClass);
}

function uniqueSorted(values: Iterable<string>): string[] {
  return [...new Set(values)].sort((a, b) => a.localeCompare(b));
}

type HeldCapability = { cap: CapabilityInfo; connectorId: string; group: string };

/** Flat index of every capability in a catalogue, keyed by capability id. */
export function indexCatalog(catalog: CapabilitiesCatalog): Map<string, HeldCapability> {
  const index = new Map<string, HeldCapability>();
  for (const connector of catalog.connectors) {
    for (const cap of connector.capabilities) {
      index.set(cap.id, { cap, connectorId: connector.id, group: connector.group });
    }
  }
  return index;
}

/**
 * Client-side mirror of `broker.describe_grant()` — used by the create-agent flow's
 * live blast-radius panel, which has no agent id yet to call GET .../access against.
 * Kept structurally identical to the backend summary so the two never disagree.
 */
export function computeGrantSummary(catalog: CapabilitiesCatalog, granted: string[]): GrantSummary {
  const index = indexCatalog(catalog);
  const held: HeldCapability[] = [];
  for (const id of granted) {
    const entry = index.get(id);
    if (entry) held.push(entry);
  }

  const dangerGroups = new Set(
    Object.entries(catalog.groups)
      .filter(([, group]) => group.danger)
      .map(([id]) => id),
  );

  const by_action_class: Partial<Record<ActionClass, number>> = {};
  for (const { cap } of held) {
    by_action_class[cap.action_class] = (by_action_class[cap.action_class] ?? 0) + 1;
  }

  const envByConnector = new Map(catalog.connectors.map((c) => [c.id, c.env_names]));
  const envNames = new Set<string>();
  for (const { connectorId } of held) {
    for (const name of envByConnector.get(connectorId) ?? []) envNames.add(name);
  }

  return {
    total: held.length,
    by_action_class,
    connectors: uniqueSorted(held.map((h) => h.connectorId)),
    groups: uniqueSorted(held.map((h) => h.group)),
    auto_writes: uniqueSorted(
      held.filter((h) => h.cap.action_class === "internal_reversible").map((h) => h.cap.id),
    ),
    needs_approval: uniqueSorted(
      held.filter((h) => h.cap.action_class === "external_reversible").map((h) => h.cap.id),
    ),
    always_human: uniqueSorted(
      held
        .filter(
          (h) =>
            h.cap.action_class === "consequential" ||
            h.cap.action_class === "irreversible",
        )
        .map((h) => h.cap.id),
    ),
    touches_danger_group: uniqueSorted(held.filter((h) => dangerGroups.has(h.group)).map((h) => h.group)),
    not_yet_callable: uniqueSorted(held.filter((h) => !h.cap.callable_now).map((h) => h.cap.id)),
    env_names: uniqueSorted(envNames),
  };
}

/** All read_only capability ids in a catalogue — backs "select all reads". */
export function readOnlyCapabilityIds(catalog: CapabilitiesCatalog): string[] {
  const ids: string[] = [];
  for (const connector of catalog.connectors) {
    for (const cap of connector.capabilities) {
      if (cap.action_class === "read_only") ids.push(cap.id);
    }
  }
  return ids;
}
