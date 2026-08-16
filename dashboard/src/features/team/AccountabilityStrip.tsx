"use client";

/**
 * AccountabilityStrip (migration 132, spec §8) — per-active-dev card:
 * today's commitments (status glyphs), done-today cards with the completion
 * tri-state badge, blocked/overdue counts, the improvement-of-day line, and
 * points pace. Per-person, deliberately NOT a leaderboard (open question 6 —
 * `ScoreboardStrip`, above, already ranks).
 *
 * Data: `GET /api/team/accountability` (`useTeamAccountability`) — copied
 * from `useTeamScoreboard`'s defensive shape: network/auth/parse failures all
 * settle to `"unavailable"`. UNLIKE `ScoreboardStrip` (which renders nothing
 * in that state — an accepted favourable-absence for a package that may
 * simply not exist yet), this strip renders an explicit "⚠ Accountability
 * unavailable" line instead of silently vanishing (Sol@high cross-review,
 * 2026-08-14): once this endpoint IS live, its silence would read as "nobody
 * has anything to report" rather than "the call failed" — the strip must
 * still never BLOCK the rest of `/team`, and polling continues underneath so
 * a recovered endpoint repairs the line on its own. Genuinely zero active
 * devs (a `"ready"` response with an empty `people` array) is a THIRD,
 * distinguishable state and keeps rendering nothing, same as before.
 */

import { Badge, Card } from "@/design";
import { useTeamAccountability } from "./hooks";
import { employeeName } from "./types";
import type { TeamAccountabilityPerson, TeamCommitment } from "./types";
import styles from "./team.module.css";

const COMMITMENT_GLYPH: Record<TeamCommitment["status"], string> = {
  committed: "•",
  delivered: "✓",
  missed: "✗",
  carried: "↻",
};

const COMMITMENT_LABEL: Record<TeamCommitment["status"], string> = {
  committed: "Committed",
  delivered: "Delivered",
  missed: "Missed",
  carried: "Carried",
};

function CommitmentRow({ commitment }: { commitment: TeamCommitment }) {
  const title = [commitment.title, commitment.expected_outcome].filter(Boolean).join(" — ");
  return (
    <li className={styles.commitmentRow} title={title || undefined}>
      <span className={styles.commitmentGlyph} aria-hidden="true">
        {COMMITMENT_GLYPH[commitment.status]}
      </span>
      <span className={styles.commitmentTitle}>{commitment.title}</span>
      <span className={styles.muted}>{COMMITMENT_LABEL[commitment.status]}</span>
    </li>
  );
}

/** verified ✓ / unverified ○ / failed ✗ — the tri-state badge, reused
 * verbatim from `TaskOverview`'s derivation (`completion_state` is always
 * present on a `done_today` card here, so no `completionStateOf` call is
 * needed on this surface). */
function completionBadge(state: string | null) {
  if (state === "verified") return { symbol: "✓", tone: "ok" as const, label: "Verified" };
  if (state === "failed_verification") return { symbol: "✗", tone: "danger" as const, label: "Verification failed" };
  if (state === "unverified") return { symbol: "○", tone: "neutral" as const, label: "Unverified" };
  return null;
}

function DoneTodayRow({ card }: { card: TeamAccountabilityPerson["done_today"][number] }) {
  const badge = completionBadge(card.completion_state);
  const title = card.verification_failed_reason
    ? `Verification failed: ${card.verification_failed_reason}`
    : badge?.label;
  return (
    <li className={styles.doneTodayRow} title={title}>
      {badge ? <Badge tone={badge.tone}>{badge.symbol}</Badge> : null}
      <span className={styles.commitmentTitle}>{card.title}</span>
    </li>
  );
}

function PersonAccountabilityCard({ person }: { person: TeamAccountabilityPerson }) {
  const name = employeeName(person.employee_id) ?? person.name;
  const blockedCount = person.blocked.length;
  // `blocked_reason` (per-card) when the server has it; falls back to naming
  // WHICH cards are blocked against an un-upgraded server that only sends
  // id/ref/title (optional field — see `TeamAccountabilityBlockedCard`).
  const blockedTitle = blockedCount
    ? person.blocked
        .map((card) => (card.blocked_reason ? `${card.ref ?? card.title}: ${card.blocked_reason}` : (card.ref ?? card.title)))
        .join("; ")
    : undefined;

  return (
    <Card padding="sm" className={styles.accountabilityCard}>
      <div className={styles.accountabilityHead}>
        <h4 className={styles.personName}>{name}</h4>
        {person.points_pace ? (
          <span className={styles.muted}>
            {person.points_pace.points} pts{person.points_pace.on_pace === false ? " · behind pace" : person.points_pace.on_pace ? " · on pace" : ""}
          </span>
        ) : null}
      </div>

      {person.commitments.length ? (
        <ul className={styles.commitmentList} aria-label={`${name}'s commitments today`}>
          {person.commitments.map((commitment) => (
            <CommitmentRow key={commitment.id} commitment={commitment} />
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>No commitments recorded</p>
      )}

      {person.done_today.length ? (
        <ul className={styles.doneTodayList} aria-label={`${name}'s cards done today`}>
          {person.done_today.map((card) => (
            <DoneTodayRow key={card.id} card={card} />
          ))}
        </ul>
      ) : null}

      <div className={styles.accountabilityFoot}>
        {blockedCount ? (
          <Badge tone="danger" title={blockedTitle}>
            {blockedCount} blocked
          </Badge>
        ) : null}
        {person.overdue ? <Badge tone="warn">{person.overdue} overdue</Badge> : null}
        {typeof person.evidence_today === "number" && person.evidence_today > 0 ? (
          <Badge tone="neutral" title="Evidence rows attributed to this person today">
            {person.evidence_today} evidence today
          </Badge>
        ) : null}
      </div>

      {person.improvement_of_day ? (
        <p
          className={styles.improvementLine}
          title={person.improvement_of_day.expected_outcome || undefined}
        >
          ★ {person.improvement_of_day.title} — {COMMITMENT_LABEL[person.improvement_of_day.status]}
        </p>
      ) : null}
    </Card>
  );
}

export function AccountabilityStrip() {
  const { state, value } = useTeamAccountability();
  // "loading" (first fetch in flight) renders NOTHING — not a failure, and
  // not yet a known-empty answer either, so it gets neither the warning nor
  // the muted empty line below (a badge-vs-line round-2 review keeps all
  // four states visually distinct: null / a muted line / a warn badge / cards).
  if (state === "loading") return null;
  if (state === "unavailable") {
    return (
      <section className={styles.accountability} aria-label="Developer accountability">
        <Badge tone="warn" title="The accountability endpoint did not respond; retrying in the background.">
          ⚠ Accountability unavailable
        </Badge>
      </section>
    );
  }
  const people = Array.isArray(value?.people) ? value.people : [];
  if (people.length === 0) {
    // Successfully fetched AND genuinely empty — distinct from both the
    // silent "loading" state above and the "⚠ unavailable" failure state:
    // this is a confirmed "nobody to report today" answer, not a guess.
    return (
      <section className={styles.accountability} aria-label="Developer accountability">
        <p className={styles.muted}>No accountability data yet</p>
      </section>
    );
  }

  return (
    <section className={styles.accountability} aria-label="Developer accountability">
      {people.map((person) => (
        <PersonAccountabilityCard key={person.employee_id} person={person} />
      ))}
    </section>
  );
}
