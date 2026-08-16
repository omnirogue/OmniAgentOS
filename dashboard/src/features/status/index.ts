/** Status feature public surface — the `/` homepage's mission-control view
 * over loop liveness, the merge gate, the loop queue, landings, the hang
 * recycler, and ALERTS.md. Everything here reads `GET /api/local/status`
 * (assembled server-side in `server/assembleStatus.ts` from ground truth on
 * this box) plus the existing `/api/accounts` for the bottom strip. */
export * from "./types";
export * from "./format";
export { useStatus } from "./hooks/useStatus";
export { LoopsSection } from "./components/LoopsSection";
export { GateSection } from "./components/GateSection";
export { QueueSection } from "./components/QueueSection";
export { LandingsSection } from "./components/LandingsSection";
export { RecyclerSection } from "./components/RecyclerSection";
export { AlertsSection } from "./components/AlertsSection";
export { AccountsStrip } from "./components/AccountsStrip";
