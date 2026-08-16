"use client";

import { Badge, Button } from "@/design";
import { ActionClassBadge } from "./ActionClassBadge";
import { readOnlyCapabilityIds } from "./riskModel";
import type { CapabilitiesCatalog } from "./types";
import styles from "./capabilities.module.css";

export type CapabilityPickerProps = {
  catalog: CapabilitiesCatalog;
  granted: string[];
  onChange: (granted: string[]) => void;
};

/**
 * Grants selection grouped by group -> connector, with select-all-reads / clear-all
 * affordances. Used by the create-agent flow and the agent detail page's "edit
 * access" dialog, so a capability grant is composed identically everywhere.
 */
export function CapabilityPicker({ catalog, granted, onChange }: CapabilityPickerProps) {
  const grantedSet = new Set(granted);

  const toggle = (capId: string, checked: boolean) => {
    if (checked) {
      if (grantedSet.has(capId)) return;
      onChange([...granted, capId]);
    } else {
      onChange(granted.filter((id) => id !== capId));
    }
  };

  const toggleConnector = (connectorId: string, capIds: string[], checked: boolean) => {
    const next = checked
      ? [...new Set([...granted, ...capIds])]
      : granted.filter((id) => !capIds.includes(id));
    onChange(next);
  };

  const selectAllReads = () => {
    onChange([...new Set([...granted, ...readOnlyCapabilityIds(catalog)])]);
  };

  const clearAll = () => onChange([]);

  const groupIds = Object.keys(catalog.groups);

  return (
    <div className={styles.picker}>
      <div className={styles.pickerToolbar}>
        <Button variant="secondary" size="sm" onClick={selectAllReads}>
          Select all reads
        </Button>
        <Button variant="ghost" size="sm" onClick={clearAll} disabled={granted.length === 0}>
          Clear all
        </Button>
        <span className={styles.pickerCount}>{granted.length} selected</span>
      </div>

      {groupIds.map((groupId) => {
        const group = catalog.groups[groupId];
        if (!group) return null;
        const connectors = catalog.connectors.filter((c) => c.group === groupId);
        if (connectors.length === 0) return null;
        return (
          <details key={groupId} className={styles.pickerGroup} open={group.danger}>
            <summary className={styles.pickerGroupSummary}>
              <span>{group.label}</span>
              {group.danger ? <Badge tone="danger">Danger</Badge> : null}
              <span className={styles.pickerGroupCount}>
                {connectors.reduce((n, c) => n + c.capabilities.filter((cap) => grantedSet.has(cap.id)).length, 0)}/
                {connectors.reduce((n, c) => n + c.capabilities.length, 0)}
              </span>
            </summary>
            <div className={styles.pickerConnectors}>
              {connectors.map((connector) => {
                const capIds = connector.capabilities.map((c) => c.id);
                const allGranted = capIds.length > 0 && capIds.every((id) => grantedSet.has(id));
                const someGranted = capIds.some((id) => grantedSet.has(id));
                return (
                  <div key={connector.id} className={styles.pickerConnector}>
                    <div className={styles.pickerConnectorHead}>
                      <label className={styles.pickerCheckboxRow}>
                        <input
                          type="checkbox"
                          checked={allGranted}
                          ref={(el) => {
                            if (el) el.indeterminate = someGranted && !allGranted;
                          }}
                          onChange={(e) => toggleConnector(connector.id, capIds, e.target.checked)}
                        />
                        <span className={styles.pickerConnectorLabel}>{connector.label}</span>
                      </label>
                      <span className={styles.faint}>{connector.env_names.join(", ") || "no credentials"}</span>
                    </div>
                    <ul className={styles.pickerCapList}>
                      {connector.capabilities.map((cap) => {
                        const checked = grantedSet.has(cap.id);
                        return (
                          <li key={cap.id} className={styles.pickerCapRow}>
                            <div>
                              <label className={styles.pickerCheckboxRow}>
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={(e) => toggle(cap.id, e.target.checked)}
                                />
                                <span className={styles.pickerCapLabel}>{cap.label}</span>
                              </label>
                              {checked && cap.always_human ? (
                                <p className={styles.blastFootnote} style={{ marginLeft: "1.5rem" }}>
                                  Always requires a human — granting this only lets the agent request the
                                  action; it can never run unattended.
                                </p>
                              ) : null}
                            </div>
                            <span className={styles.pickerCapMeta}>
                              <ActionClassBadge actionClass={cap.action_class} />
                              {!cap.callable_now ? <Badge tone="neutral">Declared — not yet callable</Badge> : null}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                );
              })}
            </div>
          </details>
        );
      })}
    </div>
  );
}
