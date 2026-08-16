"use client";

import { useMemo, useState, type KeyboardEvent } from "react";
import { EmptyState, Icon, Input, Pill, Select } from "../../../design";
import { formatDateTime, kbStatusTone, truncate } from "../format";
import { KB_STATUSES } from "../types";
import type { CommsMessage, CommsMessageFilters, CommsSource } from "../types";
import styles from "../steward.module.css";

export type CommsListProps = {
  messages: CommsMessage[];
  sources: CommsSource[];
  filters: CommsMessageFilters;
  onFiltersChange: (next: CommsMessageFilters) => void;
};

function messageSnippet(message: CommsMessage): string {
  return truncate(message.body_text, 220);
}

export function CommsList({ messages, sources, filters, onFiltersChange }: CommsListProps) {
  const [searchDraft, setSearchDraft] = useState(filters.q ?? "");
  const [groupByThread, setGroupByThread] = useState(false);

  const submitSearch = () => onFiltersChange({ ...filters, q: searchDraft.trim() || undefined });
  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") submitSearch();
  };

  const groups = useMemo(() => {
    if (!groupByThread) return null;
    const byThread = new Map<string, CommsMessage[]>();
    for (const message of messages) {
      const key = message.thread_id || `msg-${message.id}`;
      const bucket = byThread.get(key);
      if (bucket) bucket.push(message);
      else byThread.set(key, [message]);
    }
    return [...byThread.entries()];
  }, [groupByThread, messages]);

  const renderMessage = (message: CommsMessage) => (
    <li className="ds-card" key={message.id} style={{ padding: 0 }}>
      <div className={styles.messageItem}>
        <div className={styles.messageHead}>
          <div className={styles.messageIdentity}>
            <strong>{message.sender || "(unknown sender)"}</strong>
            <span className={styles.faint}>{message.source}</span>
            <Pill tone={kbStatusTone(message.kb_status)}>{message.kb_status}</Pill>
          </div>
          <span className={styles.faint}>{formatDateTime(message.sent_at ?? message.created_at)}</span>
        </div>
        {message.subject ? <p className={styles.messageSubject}>{message.subject}</p> : null}
        <p className={styles.messageBody}>{messageSnippet(message)}</p>
      </div>
    </li>
  );

  return (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <div className={styles.filterGroup}>
          <Select
            label="Source"
            value={filters.source ?? ""}
            onChange={(value) => onFiltersChange({ ...filters, source: value || undefined })}
            options={[{ value: "", label: "All sources" }, ...sources.map((source) => ({ value: source.name, label: source.name }))]}
          />
          <Select
            label="Knowledge status"
            value={filters.kb_status ?? ""}
            onChange={(value) => onFiltersChange({ ...filters, kb_status: value || undefined })}
            options={[{ value: "", label: "All statuses" }, ...KB_STATUSES.map((status) => ({ value: status, label: status }))]}
          />
          <Input
            label="Search"
            icon={<Icon name="search" size={14} />}
            placeholder="Subject, sender, or body"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
            onKeyDown={onSearchKeyDown}
            onBlur={submitSearch}
            clearable
            onClear={() => {
              setSearchDraft("");
              onFiltersChange({ ...filters, q: undefined });
            }}
          />
        </div>
        <label className={styles.threadGroupLabel}>
          <input type="checkbox" checked={groupByThread} onChange={(event) => setGroupByThread(event.target.checked)} />
          Group by thread
        </label>
      </div>

      {messages.length === 0 ? (
        <EmptyState icon={<Icon name="inbox" size={22} />} title="No messages" message="Nothing matches these filters yet." />
      ) : groups ? (
        <div className={styles.stack}>
          {groups.map(([threadId, thread]) => (
            <div className={styles.threadGroup} key={threadId}>
              <p className={styles.threadHeading}>Thread {threadId}</p>
              <ul className={styles.messageList} style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {thread.map(renderMessage)}
              </ul>
            </div>
          ))}
        </div>
      ) : (
        <ul className={styles.messageList} style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {messages.map(renderMessage)}
        </ul>
      )}
    </div>
  );
}
