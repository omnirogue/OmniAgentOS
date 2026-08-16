/** Start an interval that does not make network requests in hidden tabs. */
export function startVisibilityPoll(fn: () => void, ms: number = 60_000): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") return () => {};
  const timer = window.setInterval(() => {
    if (!document.hidden) fn();
  }, ms);
  return () => window.clearInterval(timer);
}
