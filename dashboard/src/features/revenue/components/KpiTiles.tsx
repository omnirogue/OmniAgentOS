import { Stat } from "@/design";
import { formatMoney, formatRoas, formatRoiPercent } from "../format";
import styles from "../revenue.module.css";
import type { RevenueTotals } from "../contracts";

export type KpiTilesProps = { totals: RevenueTotals };

/**
 * The headline tiles. Gross Contribution is deliberately labeled
 * "Gross Contribution (rev − ad spend)" — never "Profit" — with the server's
 * `totals.note` rendered verbatim as fine print underneath so the honesty
 * caption travels with the number itself, not buried elsewhere on the page.
 *
 * ROI and Blended ROAS are BLENDED real-revenue metrics (revenue_usd over
 * ad_spend_usd) — deliberately distinct from the Meta-attributed `roas` seen
 * in By Vertical / By Campaign. Both render "—" when there is no ad spend,
 * never a fabricated 0.
 */
export function KpiTiles({ totals }: KpiTilesProps) {
  return (
    <div className={styles.statGrid}>
      <Stat label="Revenue" value={formatMoney(totals.revenue_usd)} tone="accent" />
      <Stat label="Ad Spend" value={formatMoney(totals.ad_spend_usd)} tone="warn" />
      <Stat
        label="Gross Contribution (rev − ad spend)"
        value={formatMoney(totals.gross_contribution_usd)}
        tone={totals.gross_contribution_usd >= 0 ? "ok" : "danger"}
        hint={totals.note}
      />
      <Stat
        label="ROI (ad spend)"
        value={formatRoiPercent(totals.roi)}
        tone={totals.roi == null ? "default" : totals.roi >= 0 ? "ok" : "danger"}
      />
      <Stat
        label="Blended ROAS"
        value={formatRoas(totals.blended_roas)}
        tone={totals.blended_roas == null ? "default" : "accent"}
      />
    </div>
  );
}
