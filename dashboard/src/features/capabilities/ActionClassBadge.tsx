import { Badge } from "@/design";
import { ACTION_CLASS_BADGE_TONE, ACTION_CLASS_BLURB, ACTION_CLASS_LABEL } from "./riskModel";
import type { ActionClass } from "./types";

export type ActionClassBadgeProps = {
  actionClass: ActionClass;
  /** Show the risk-tier label ("Read only") instead of the raw enum value. */
  friendly?: boolean;
};

/** The one badge every capability's risk class renders through, everywhere in the app. */
export function ActionClassBadge({ actionClass, friendly = true }: ActionClassBadgeProps) {
  return (
    <Badge
      category="disposition"
      tone={ACTION_CLASS_BADGE_TONE[actionClass]}
      title={ACTION_CLASS_BLURB[actionClass]}
    >
      {friendly ? ACTION_CLASS_LABEL[actionClass] : actionClass}
    </Badge>
  );
}
