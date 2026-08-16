export interface TranscriptEntry {
  ts: string;
  type: "tool_call" | "message" | "event" | string;
  actor: string;
  summary: string;
  toolName?: string;
  toolInput?: Record<string, unknown>;
  filePath?: string;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as UnknownRecord : null;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function toolInput(value: UnknownRecord): UnknownRecord | undefined {
  const candidate = record(value.input) ?? record(value.tool_input) ?? record(value.arguments);
  return candidate ?? undefined;
}

function filePath(input: UnknownRecord | undefined, raw: UnknownRecord): string | undefined {
  const candidates = [
    input?.file_path, input?.filePath, input?.path, input?.filename, input?.uri,
    raw.file_path, raw.path,
  ];
  for (const candidate of candidates) {
    const value = text(candidate);
    if (value && (value.includes("/") || value.includes("\\\\") || value.includes("."))) return value;
  }
  const command = text(input?.command) ?? text(raw.command);
  return command?.match(/(?:^|\s)((?:\.?\/?)[\w@./-]+\.[\w-]+)/)?.[1];
}

function summarize(raw: UnknownRecord, type: string, toolName?: string, path?: string): string {
  const message = text(raw.summary) ?? text(raw.message) ?? text(raw.content) ?? text(raw.action);
  if (toolName) return path ? `${toolName} · ${path}` : toolName;
  return message ?? (type === "event" ? "Session event" : "Activity recorded");
}

export function parseTranscript(entries: unknown[]): TranscriptEntry[] {
  return entries.flatMap((value) => {
    const raw = record(value);
    if (!raw) return [];
    const type = text(raw.type) ?? (raw.tool_name ? "tool_call" : "event");
    const input = toolInput(raw);
    const name = text(raw.tool_name) ?? text(raw.toolName) ?? text(raw.name);
    const path = filePath(input, raw);
    return [{
      ts: text(raw.ts) ?? text(raw.timestamp) ?? text(raw.created_at) ?? "",
      type,
      actor: text(raw.actor) ?? text(raw.role) ?? "agent",
      summary: summarize(raw, type, name, path),
      toolName: name,
      toolInput: input,
      filePath: path,
    }];
  });
}

export function extractFilesTouched(entries: TranscriptEntry[]): string[] {
  return [...new Set(entries.map((entry) => entry.filePath).filter((path): path is string => Boolean(path)))];
}

export function extractAPIsUsed(entries: TranscriptEntry[]): string[] {
  return [...new Set(entries.map((entry) => entry.toolName).filter((name): name is string => Boolean(name)))];
}
