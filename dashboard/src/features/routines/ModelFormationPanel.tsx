import { useEffect, useState } from "react";
import { Badge, Card, ErrorState, Loading, Pill, Table, type TableColumn } from "@/design";
import type { ModelFormation } from "./types";
import styles from "./routines.module.css";

interface RoleRow {
  role: string;
  harness: string;
  model: string;
  effort: string | null;
}

export function ModelFormationPanel() {
  const [formation, setFormation] = useState<ModelFormation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchFormation = async () => {
      try {
        setLoading(true);
        const response = await fetch("/api/models/formation");
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        setFormation(data);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load model formation");
      } finally {
        setLoading(false);
      }
    };

    void fetchFormation();
  }, []);

  if (loading) {
    return (
      <Card>
        <Loading variant="skeleton" label="Loading model formation…" lines={3} />
      </Card>
    );
  }

  if (error) {
    return <ErrorState message={error} />;
  }

  if (!formation) {
    return null;
  }

  // Build integration roles table data
  const integrationRoles: RoleRow[] = [];
  if (formation.integration_roles) {
    Object.entries(formation.integration_roles).forEach(([role, config]) => {
      const cfg = config as Record<string, unknown>;
      integrationRoles.push({
        role,
        harness: (cfg.harness as string) || "—",
        model: (cfg.model as string) || "—",
        effort: (cfg.effort as string | null) || "—",
      });
    });
  }

  // Build loop models table data
  const loopModelRows: RoleRow[] = [];
  if (formation.loop_models) {
    Object.entries(formation.loop_models).forEach(([role, config]) => {
      const cfg = config as Record<string, unknown>;
      loopModelRows.push({
        role,
        harness: (cfg.harness as string) || "—",
        model: (cfg.model as string) || "—",
        effort: (cfg.effort as string | null) || "—",
      });
    });
  }

  const roleColumns: TableColumn<RoleRow>[] = [
    {
      key: "role",
      header: "Role",
      render: (r) => <div className={styles.roleCell}>{r.role}</div>,
    },
    {
      key: "model",
      header: "Model",
      render: (r) => <Badge tone="neutral">{r.model}</Badge>,
    },
    {
      key: "effort",
      header: "Effort",
      render: (r) => (
        <span className={styles.effortLabel}>
          {r.effort === "—" ? "—" : <Pill tone="neutral">{r.effort}</Pill>}
        </span>
      ),
    },
  ];

  return (
    <Card>
      <div className={styles.panelHeader}>
        <div>
          <h3 className={styles.panelTitle}>Models & Formation</h3>
          <p className={styles.panelSubtitle}>System model assignments and escalation policy.</p>
        </div>
      </div>

      {/* Swarm Planner */}
      {formation.swarm_planner && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Swarm Planner</div>
          <div className={styles.plannerRow}>
            <div>
              <div className={styles.plannerLabel}>Model</div>
              <Badge tone="champion">{formation.swarm_planner.model}</Badge>
            </div>
            <div>
              <div className={styles.plannerLabel}>Effort</div>
              <Pill tone="neutral">{formation.swarm_planner.effort}</Pill>
            </div>
          </div>
        </div>
      )}

      {/* Model Ladder */}
      {formation.model_ladder && formation.model_ladder.length > 0 && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Escalation Ladder</div>
          <div className={styles.ladderRow}>
            {formation.model_ladder.map((model, idx) => (
              <div key={idx} className={styles.ladderItem}>
                <div className={styles.ladderRung}>{idx + 1}</div>
                <Badge tone={idx === 0 ? "champion" : "neutral"}>{model}</Badge>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Lane Floors */}
      {formation.lane_floors && Object.keys(formation.lane_floors).length > 0 && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Lane Floors</div>
          {Object.entries(formation.lane_floors).map(([tier, models]) => (
            <div key={tier} className={styles.floorTier}>
              <div className={styles.tierLabel}>{tier}</div>
              <div className={styles.floorModels}>
                {(models as string[]).map((m, idx) => (
                  <Pill key={idx} tone="neutral">
                    {m}
                  </Pill>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Default Model */}
      {formation.default_model && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Default Model</div>
          <Badge tone="neutral">{formation.default_model}</Badge>
        </div>
      )}

      {/* Integration Roles Table */}
      {integrationRoles.length > 0 && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Integration Roles</div>
          <div className={styles.tableScroll}>
            <Table columns={roleColumns} rows={integrationRoles} rowKey={(r) => r.role} />
          </div>
        </div>
      )}

      {/* Loop Models Table */}
      {loopModelRows.length > 0 && (
        <div className={styles.formationSection}>
          <div className={styles.sectionTitle}>Improvement Loop Roles</div>
          <div className={styles.tableScroll}>
            <Table columns={roleColumns} rows={loopModelRows} rowKey={(r) => r.role} />
          </div>
        </div>
      )}
    </Card>
  );
}
