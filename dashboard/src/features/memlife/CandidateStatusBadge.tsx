import { Badge } from "@/design";
import { candidateStatusPresentation } from "./candidateStatus";

type Props = {
  status: string | null | undefined;
};

/** Status chip for a memlife candidate. Unknown/absent → UNKNOWN, never success. */
export function CandidateStatusBadge({ status }: Props) {
  const presentation = candidateStatusPresentation(status);
  return (
    <Badge
      tone={presentation.tone}
      category="result"
      data-success={presentation.isSuccess ? "true" : "false"}
      data-status-label={presentation.label}
    >
      {presentation.label}
    </Badge>
  );
}
