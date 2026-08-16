import { open, stat } from "node:fs/promises";

/**
 * Reads only the last `maxBytes` of a file. The loop/gate/alerts logs this
 * feature reads grow without bound while the daemons run — a status
 * endpoint that read them whole would get slower every day the box stays
 * up. Falls back to the whole file when it's smaller than the window.
 *
 * Server-only (uses `node:fs/promises`) — never import this from a
 * component; the pure parsers in `../parsers/*` are what client code and
 * tests use instead.
 */
export async function readTail(path: string, maxBytes: number): Promise<string> {
  const info = await stat(path);
  const start = Math.max(0, info.size - maxBytes);
  const length = info.size - start;
  if (length <= 0) return "";
  const handle = await open(path, "r");
  try {
    const buffer = Buffer.alloc(length);
    await handle.read(buffer, 0, length, start);
    return buffer.toString("utf8");
  } finally {
    await handle.close();
  }
}
