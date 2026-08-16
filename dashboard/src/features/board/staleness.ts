const DAY_MS = 24 * 60 * 60 * 1000;

export function taskStaleness(updatedAtIso: string, now?: Date): { days: number; stale: boolean } {
  const updatedAt = Date.parse(updatedAtIso);
  const currentTime = (now ?? new Date()).getTime();
  if (!Number.isFinite(updatedAt) || !Number.isFinite(currentTime)) {
    return { days: 0, stale: false };
  }

  // Intentional elapsed 24-hour periods, not calendar-local day boundaries.
  const days = Math.max(0, Math.floor((currentTime - updatedAt) / DAY_MS));
  return { days, stale: days >= 7 };
}

/**
 * Returns stale card ids that may be hidden without changing a live swarm's
 * visible membership. Solo cards hide independently; a swarm run hides only
 * after every member is terminal and stale.
 *
 * Evaluates "every card in the run" over the already-filtered set, so with
 * narrowing filters a live run's residual settled members can hide — documented,
 * accepted.
 */
export function runGranularityStaleHideSet<T extends { id: string; swarm_run_id?: string | null }>(
  tasks: T[],
  isTerminalAndStale: (task: T) => boolean,
): Set<string> {
  const tasksByRun = new Map<string | null, T[]>();
  for (const task of tasks) {
    const runId = task.swarm_run_id ?? null;
    const runTasks = tasksByRun.get(runId);
    if (runTasks) runTasks.push(task);
    else tasksByRun.set(runId, [task]);
  }

  const hidden = new Set<string>();
  for (const [runId, runTasks] of tasksByRun) {
    if (runId === null) {
      for (const task of runTasks) {
        if (isTerminalAndStale(task)) hidden.add(task.id);
      }
    } else if (runTasks.every(isTerminalAndStale)) {
      for (const task of runTasks) hidden.add(task.id);
    }
  }
  return hidden;
}
