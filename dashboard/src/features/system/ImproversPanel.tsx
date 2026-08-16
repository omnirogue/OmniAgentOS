"use client";

import { useEffect, useState } from "react";
import { Badge, Button, EmptyState, Icon, Section, useToast } from "@/design";
import { patchImproverPrompt } from "./client";
import {
  PROMPT_MAX_BYTES,
  type ImproverLoop,
  type ImproversResponse,
  type ImprovementEntry,
} from "./types";
import { EditableNote, PathChip, fmtDate, relFuture, relTime } from "./shared";
import styles from "./system.module.css";

const encoder = new TextEncoder();

function LogEntryView({ entry }: { entry: ImprovementEntry }) {
  const changes = entry.changes ?? [];
  return (
    <div className={styles.logEntry}>
      <div className={styles.logEntryHead}>
        <span className={styles.logTs}>{fmtDate(entry.ts)}</span>
        {entry.improver ? <Badge tone="challenger">{entry.improver}</Badge> : null}
        {entry.account_used ? <span className={styles.faintCell}>{entry.account_used}</span> : null}
        {changes.length ? (
          <span className={styles.faintCell}>
            {changes.length} change{changes.length === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
      {entry.notes ? <p className={styles.logSummary}>{entry.notes}</p> : null}
      {changes.length ? (
        <ul className={styles.changeList}>
          {changes.map((change, i) => (
            <li key={i} className={styles.changeItem}>
              {change.kind ? <Badge tone="neutral">{change.kind}</Badge> : null}
              {change.path ? <code className={styles.changePath}>{change.path}</code> : null}
              {change.summary ? <span>{change.summary}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** The editable system prompt for an improver — a design-system-styled textarea
 * with a byte counter, cap guard, Save (→ PATCH + commit toast), and the runtime
 * policy-block caption. */
function PromptEditor({
  loop,
  onPromptSaved,
}: {
  loop: ImproverLoop;
  onPromptSaved: (label: string, content: string) => void;
}) {
  const { push } = useToast();
  const [draft, setDraft] = useState(loop.prompt_md ?? "");
  const [saving, setSaving] = useState(false);

  // Re-sync when the server value changes (e.g. after a refresh or our own save).
  useEffect(() => {
    setDraft(loop.prompt_md ?? "");
  }, [loop.prompt_md]);

  const bytes = encoder.encode(draft).length;
  const overCap = bytes > PROMPT_MAX_BYTES;
  const dirty = draft !== (loop.prompt_md ?? "");

  const save = async () => {
    if (!dirty || overCap || saving) return;
    setSaving(true);
    try {
      const result = await patchImproverPrompt(loop.label, draft);
      onPromptSaved(loop.label, draft);
      push({
        title: "Prompt saved",
        message: result.committed
          ? `${loop.label}: saved + committed.`
          : `${loop.label}: saved (commit skipped — repo not committing).`,
        tone: "success",
      });
    } catch (reason) {
      push({
        title: "Save failed",
        message: reason instanceof Error ? reason.message : "Could not save the prompt.",
        tone: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.promptEditWrap}>
      <span className={styles.filterLabel}>System prompt</span>
      <textarea
        className={styles.promptEditor}
        aria-label={`${loop.label} system prompt`}
        spellCheck={false}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        disabled={saving}
      />
      <div className={styles.saveRow}>
        <Button size="sm" variant="primary" disabled={!dirty || overCap || saving} onClick={save}>
          {saving ? "Saving…" : "Save prompt"}
        </Button>
        <span className={overCap ? `${styles.byteCount} ${styles.byteCountOver}` : styles.byteCount}>
          {(bytes / 1024).toFixed(1)} / {PROMPT_MAX_BYTES / 1024} KB
          {overCap ? " — over cap" : ""}
        </span>
        {dirty && !overCap ? <Badge tone="warn">unsaved</Badge> : null}
        {loop.prompt_path ? <PathChip path={loop.prompt_path} label="file" /> : null}
      </div>
      <p className={styles.policyHint}>
        The yaml policy block is read at runtime — edits change this loop&apos;s behavior on its next
        run.
      </p>
    </div>
  );
}

function LoopCard({
  loop,
  onPromptSaved,
}: {
  loop: ImproverLoop;
  onPromptSaved: (label: string, content: string) => void;
}) {
  const isGuard = loop.kind === "guard";
  const activity = loop.recent_activity ?? [];
  const hasHistory = activity.length > 0 || loop.last_log_tail.length > 0;
  // Guards have no prompt — their log tail is the primary content, so open by default.
  const [showHistory, setShowHistory] = useState(isGuard);

  return (
    <div className={styles.loopCard}>
      <div className={styles.loopHead}>
        <span className={styles.loopLabel}>{loop.label}</span>
        <div className={styles.badgeRow}>
          <Badge tone={isGuard ? "neutral" : "champion"}>{isGuard ? "guard" : "improver"}</Badge>
          <Badge tone="neutral">{loop.schedule_human}</Badge>
          {loop.next_fire ? (
            <Badge tone="queued" title={fmtDate(loop.next_fire)}>
              next {relFuture(loop.next_fire)}
            </Badge>
          ) : null}
          {loop.last_run_guess ? (
            <Badge tone="ok" title={fmtDate(loop.last_run_guess)}>
              last run {relTime(loop.last_run_guess)}
            </Badge>
          ) : (
            <Badge tone="neutral">no run observed</Badge>
          )}
        </div>
      </div>

      {loop.script_path ? (
        <div className={styles.loopMeta}>
          <PathChip path={loop.script_path} label="runs" />
        </div>
      ) : null}

      {isGuard ? (
        <div className={styles.loopMetaSpaced}>
          <Badge tone="neutral">read-only — a protective guard loop, no editable prompt</Badge>
        </div>
      ) : loop.editable && loop.prompt_md !== null ? (
        <PromptEditor loop={loop} onPromptSaved={onPromptSaved} />
      ) : (
        <div className={styles.loopMetaSpaced}>
          <Badge tone="neutral">no prompt file found — steer this loop via its script</Badge>
        </div>
      )}

      {hasHistory ? (
        <>
          <div className={styles.historyToggle}>
            <Button size="sm" variant="ghost" onClick={() => setShowHistory((v) => !v)}>
              <Icon name={showHistory ? "chevronDown" : "chevronRight"} size={13} />
              What it&apos;s done
              {activity.length ? ` (${activity.length})` : ""}
            </Button>
          </div>
          {showHistory ? (
            <div className={styles.historyBlock}>
              {activity.length ? (
                <div className={styles.timeline}>
                  {activity.map((entry, i) => (
                    <LogEntryView key={`${entry.ts ?? "x"}-${i}`} entry={entry} />
                  ))}
                </div>
              ) : (
                <p className={styles.faintCell}>No improvement-log entries attributed yet.</p>
              )}
              {loop.last_log_tail.length ? (
                <>
                  <div className={styles.loopMetaSpaced}>
                    <span className={styles.filterLabel}>Log tail</span>
                    {loop.log_path ? <PathChip path={loop.log_path} label="full log" /> : null}
                  </div>
                  <pre className={styles.logTail} aria-label={`${loop.label} log tail`}>
                    {loop.last_log_tail.join("\n")}
                  </pre>
                </>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

export function ImproversPanel({
  data,
  onPromptSaved,
}: {
  data: ImproversResponse;
  onPromptSaved: (label: string, content: string) => void;
}) {
  return (
    <div>
      <Section
        title="Improver & guard loops"
        description="Every scheduled launchd loop that curates, reviews, or guards the estate — discovered live from ~/Library/LaunchAgents. Prompt-bearing improvers are editable in place; guards (backups) are shown read-only."
      >
        {data.loops.length ? (
          data.loops.map((loop) => (
            <LoopCard key={loop.label} loop={loop} onPromptSaved={onPromptSaved} />
          ))
        ) : (
          <EmptyState
            icon={<Icon name="clock" size={22} />}
            title="No loops discovered"
            message="No com.omniagentos.* improver/guard launchd jobs found."
          />
        )}
      </Section>

      <Section
        title="Improvement log"
        description="One line per improver run — what each loop changed. This is how we improve the improvers."
        actions={<PathChip path={data.improvement_log_path} label="file" />}
      >
        {data.improvement_log.length ? (
          <div className={styles.timeline}>
            {[...data.improvement_log].reverse().map((entry, i) => (
              <LogEntryView key={`${entry.ts ?? "x"}-${i}`} entry={entry} />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<Icon name="checkCircle" size={22} />}
            title="No improvements logged yet"
            message="var/improvement-log.jsonl is empty or absent — the nightly loops append one line per run once they fire."
          />
        )}
      </Section>

      {data.curator_reports.length ? (
        <Section title="Curator reports" description="Latest nightly reports (first lines) from ~/.claude/curator-reports/.">
          <div className={styles.timeline}>
            {data.curator_reports.map((report) => (
              <div key={report.filename} className={styles.loopCard}>
                <div className={styles.loopHead}>
                  <span className={styles.loopLabel}>{report.filename}</span>
                </div>
                {report.first_lines.map((line, i) => (
                  <p key={i} className={styles.logSummary}>
                    {line}
                  </p>
                ))}
                <div className={styles.loopMeta}>
                  <PathChip path={report.path} label="file" />
                </div>
              </div>
            ))}
          </div>
        </Section>
      ) : null}

      {data.optimizer_playbook_tail ? (
        <Section title="Optimizer playbook (tail)" description="The most recent lines the swarm optimizer has written to its playbook.">
          <pre className={styles.logTail} aria-label="Optimizer playbook tail">
            {data.optimizer_playbook_tail}
          </pre>
          <EditableNote>vault/swarm/playbook.md — the optimizer&apos;s accumulated tuning notes.</EditableNote>
        </Section>
      ) : null}
    </div>
  );
}
