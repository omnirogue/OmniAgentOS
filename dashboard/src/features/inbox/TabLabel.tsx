import { Badge } from "../../design";
import styles from "./inbox.module.css";

/** Tab header label with a cheap pending/unread count badge — the count is
 * always derived from a payload the tab's own hook already fetched (never a
 * dedicated poll of its own), per the per-tab badge requirement. */
export function TabLabel({ label, count }: { label: string; count: number }) {
  return (
    <span className={styles.tabLabel}>
      {label}
      {count > 0 ? <Badge tone="awaiting">{count > 99 ? "99+" : count}</Badge> : null}
    </span>
  );
}
