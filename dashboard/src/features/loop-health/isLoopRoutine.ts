/**
 * Loop-routine detection.
 *
 * `Routine.task_template` is an untyped JSON blob (see
 * `features/routines/types.ts`). A loop instance's row carries its spec at
 * `task_template.input` — see `omniagentos/scheduler/loop_jobs.py::_spec`,
 * which reads `input.module`, `input.instance_id`, `input.instance_module`,
 * `input.params`, `input.timeout_s`.
 *
 * The backend's own authoritative gate is `input.module ===
 * "omniagentos.loops"`. Per the R8 brief, this predicate instead keys off
 * `input.instance_module` (a non-empty string naming the instance's Python
 * module, e.g. `"omniagentos_loops.instances.health_monitor"`) — in
 * practice only a loop row ever carries that field, so the two checks agree
 * on every row seen live. Keeping the derivation behind ONE function here
 * means that when a first-class `kind`/`purpose` column lands on the
 * Routine row, this is a one-line change and every caller (sorting,
 * filtering, the health card) updates for free.
 */

import type { Routine } from "@/features/routines/types";

/** `task_template.input` as a plain object, or `null` if absent/malformed. */
function templateInput(routine: Routine): Record<string, unknown> | null {
  const template = routine.task_template;
  if (!template || typeof template !== "object") return null;
  const input = (template as Record<string, unknown>).input;
  return input && typeof input === "object" ? (input as Record<string, unknown>) : null;
}

/** True when *routine* is a loop instance (see module doc for the derivation). */
export function isLoopRoutine(routine: Routine): boolean {
  const moduleName = templateInput(routine)?.instance_module;
  return typeof moduleName === "string" && moduleName.trim().length > 0;
}

/**
 * The loop's instance id (`task_template.input.instance_id`) — the string
 * every `risk === "loop_approval"` approval's `proposed_action` is prefixed
 * with (`"<instance_id>/<node>: …"`, see `omniagentos_loops/approvals.py
 * ::ensure_approval`). Falls back to the routine id when absent/malformed so
 * a row still renders instead of vanishing.
 */
export function loopInstanceId(routine: Routine): string {
  const id = templateInput(routine)?.instance_id;
  return typeof id === "string" && id.trim().length > 0 ? id : routine.id;
}
