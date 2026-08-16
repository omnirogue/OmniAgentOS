"use client";

import { Badge, Button, Card, EmptyState, ErrorState, Loading, Page, PageHeader, Section, useToast } from "@/design";
import { BriefingCard, useBriefing } from "@/features/steward";
import styles from "@/features/steward/steward.module.css";

export default function BriefingPage() {
  const { briefing, history, loading, error, generating, refresh, generate, ack } = useBriefing();
  const { push } = useToast();

  const onGenerate = async () => {
    try {
      await generate();
      push({ title: "Briefing generated", message: "The latest briefing has been composed and delivered.", tone: "success" });
    } catch (reason) {
      push({ title: "Generate failed", message: reason instanceof Error ? reason.message : "Could not generate a briefing.", tone: "error" });
    }
  };

  const onAck = async (id: string) => {
    try {
      await ack(id);
    } catch (reason) {
      push({ title: "Ack failed", message: reason instanceof Error ? reason.message : "Could not acknowledge this briefing.", tone: "error" });
    }
  };

  return (
    <Page>
      <PageHeader
        eyebrow="Steward"
        title="Morning briefing"
        lead="What changed overnight across goals, comms, and runs — read it or press play."
        actions={
          <Button onClick={() => void onGenerate()} disabled={generating}>
            {generating ? "Generating…" : "Generate now"}
          </Button>
        }
      />

      {loading ? (
        <Card>
          <Loading label="Loading the briefing…" />
        </Card>
      ) : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && !briefing ? (
        <Card>
          <EmptyState title="No briefing yet" message="Press “Generate now” to compose the first one." />
        </Card>
      ) : null}
      {!loading && !error && briefing ? <BriefingCard briefing={briefing} onAck={onAck} /> : null}

      <Section title="History" description="Previously composed briefings.">
        {history.length === 0 ? (
          <EmptyState message="No briefing history yet." />
        ) : (
          <div className={styles.historyList}>
            {history.map((row) => (
              <div className={styles.historyRow} key={row.id}>
                <div>
                  <strong>{row.briefing_date}</strong>
                  <p className={styles.muted} style={{ margin: 0 }}>{row.summary.headline}</p>
                </div>
                <Badge tone={row.acked_at ? "ok" : "warn"}>{row.acked_at ? "Acked" : "Unread"}</Badge>
              </div>
            ))}
          </div>
        )}
      </Section>
    </Page>
  );
}
