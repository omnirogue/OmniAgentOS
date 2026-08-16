import { Badge, Button, Card } from "../../design";
import type { ApiReflectionProposal } from "./api";
import styles from "../reliability/improvements.module.css";

interface Props {
  proposal: ApiReflectionProposal;
  onApprove: () => void;
  onReject: () => void;
}

export function ReflectionProposalCard({ proposal, onApprove, onReject }: Props) {
  const isPending = proposal.status === "pending";
  return (
    <Card padding="md">
      <div className={styles.cardHeader}>
        <div>
          <h3 className={styles.cardTitle}>{proposal.id}</h3>
          <div className={styles.badgeRow}>
            <Badge tone="challenger">{proposal.kind}</Badge>
            <Badge tone={proposal.risk_class === "irreversible" ? "danger" : "neutral"}>
              {proposal.risk_class}
            </Badge>
            <Badge tone="neutral" className={styles.badgeDate}>
              {new Date(proposal.created_at).toLocaleDateString()}
            </Badge>
          </div>
        </div>
        <Badge tone={isPending ? "warn" : "ok"}>{proposal.status}</Badge>
      </div>

      <div className={styles.proposalTarget}>
        <strong>Target: </strong>{" "}
        <code>{typeof proposal.target === "object" ? JSON.stringify(proposal.target) : String(proposal.target)}</code>
      </div>

      <p className={styles.proposalRationale}>{proposal.rationale}</p>

      {proposal.predicted_impact && (
        <div className={styles.proposalImpact}>
          <strong>Predicted Impact: </strong> {proposal.predicted_impact}
        </div>
      )}

      {isPending && (
        <div className={styles.cardActions}>
          <Button size="sm" variant="primary" onClick={onApprove}>
            Approve & Apply
          </Button>
          <Button size="sm" variant="ghost" onClick={onReject}>
            Reject
          </Button>
        </div>
      )}
    </Card>
  );
}
