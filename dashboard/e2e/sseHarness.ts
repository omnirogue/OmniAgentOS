/**
 * Isolated SSE fixtures for L13 EventSource tests.
 * Binds ephemeral 127.0.0.1 ports only — never live product ports.
 *
 * CORS policy (L12 / B06):
 * - Product-origin cross-origin cases use an EXPLICIT allowed Origin only.
 * - Wildcard `Access-Control-Allow-Origin: *` is never emitted.
 * - Same-origin cases omit CORS headers (not required for same-origin EventSource).
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

export const CONTRACT_EVENT = {
  id: 1,
  type: "run.updated",
  ts: new Date().toISOString(),
  actor: "test",
  action: "updated",
  target_type: "run",
  target_id: "run-1",
  payload: {},
  trace_id: "t1",
} as const;

export const EVENTBUS_STATUS_EVENT = {
  contract_version: 1,
  type: "eventbus.status",
  state: "degraded",
  reason: "persistent_tail_failure",
  degraded: true,
  consecutive_failures: 3,
  last_error: "event_sample: RuntimeError: store down",
  tailer_restarts: 0,
  max_tailer_restarts: 5,
  ts: 1721923200.0,
} as const;

/** Canonical product dashboard origin host (L12 default family). */
export const PRODUCT_ORIGIN_HOST = "127.0.0.1";

const FIXTURE_HTML = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8" /><title>L13 EventSource fixture</title></head>
  <body><pre id="status">ready</pre></body>
</html>`;

export type SseFixture = {
  server: Server;
  port: number;
  baseUrl: string;
  /** Explicit product origin allowed by CORS (null for same-origin-only fixtures). */
  allowedOrigin: string | null;
  /** Server-observed browser requests, including the actual Origin and response policy. */
  observations: SseRequestObservation[];
};

export type SseRequestObservation = {
  method: string;
  url: string;
  origin: string | null;
  status: number;
  accessControlAllowOrigin: string | null;
};

export type DualOriginFixture = {
  api: SseFixture;
  page: SseFixture;
  /** Full product origin, e.g. http://127.0.0.1:<pagePort> */
  productOrigin: string;
  apiOrigin: string;
};

function corsHeaders(
  req: IncomingMessage,
  allowedOrigin: string | null,
): Record<string, string> {
  if (!allowedOrigin) return {};
  const requestOrigin = req.headers.origin;
  // Reflect only the single explicit allowed product origin — never "*".
  if (typeof requestOrigin === "string" && requestOrigin === allowedOrigin) {
    return {
      "Access-Control-Allow-Origin": allowedOrigin,
      Vary: "Origin",
    };
  }
  // Deny: no ACAO header (and never a wildcard fallback).
  return { Vary: "Origin" };
}

function writeSse(
  req: IncomingMessage,
  res: ServerResponse,
  allowedOrigin: string | null,
  body: string,
): void {
  const headers: Record<string, string> = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    ...corsHeaders(req, allowedOrigin),
  };
  // Only open the stream when same-origin (no allowedOrigin) or Origin is allowed.
  if (allowedOrigin) {
    const requestOrigin = req.headers.origin;
    if (requestOrigin !== allowedOrigin) {
      res.statusCode = 403;
      res.setHeader("Content-Type", "text/plain");
      res.setHeader("Vary", "Origin");
      res.end("origin not allowed");
      return;
    }
  }
  res.statusCode = 200;
  for (const [name, value] of Object.entries(headers)) {
    res.setHeader(name, value);
  }
  res.write(body);
  // Keep open; client closes the connection.
}

function handleRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: {
    allowedOrigin: string | null;
    serveHtml: boolean;
    observations: SseRequestObservation[];
  },
): void {
  const url = req.url ?? "/";

  if (req.method === "OPTIONS") {
    const headers = corsHeaders(req, opts.allowedOrigin);
    if (opts.allowedOrigin && req.headers.origin === opts.allowedOrigin) {
      res.writeHead(204, {
        ...headers,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Accept, Cache-Control, Last-Event-ID",
      });
    } else {
      res.writeHead(403, { Vary: "Origin" });
    }
    res.end();
    return;
  }

  if (opts.serveHtml && (url === "/" || url.startsWith("/index"))) {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(FIXTURE_HTML);
    return;
  }

  if (url.startsWith("/api/events")) {
    const requestOrigin =
      typeof req.headers.origin === "string" ? req.headers.origin : null;
    const frames = [
      `event: run.updated\ndata: ${JSON.stringify(CONTRACT_EVENT)}\n\n`,
      `event: eventbus.status\ndata: ${JSON.stringify(EVENTBUS_STATUS_EVENT)}\n\n`,
    ].join("");
    writeSse(req, res, opts.allowedOrigin, frames);
    const acao = res.getHeader("access-control-allow-origin");
    opts.observations.push({
      method: req.method ?? "GET",
      url,
      origin: requestOrigin,
      status: res.statusCode,
      accessControlAllowOrigin: typeof acao === "string" ? acao : null,
    });
    return;
  }

  res.writeHead(404, {
    "Content-Type": "text/plain",
    ...corsHeaders(req, opts.allowedOrigin),
  });
  res.end("not found");
}

function listen(server: Server): Promise<{ port: number; baseUrl: string }> {
  return new Promise((resolve, reject) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address() as AddressInfo;
      resolve({
        port: addr.port,
        baseUrl: `http://127.0.0.1:${addr.port}`,
      });
    });
    server.on("error", reject);
  });
}

/**
 * Same-origin SSE fixture: HTML + events on one origin.
 * No CORS headers (same-origin EventSource does not need them).
 */
export function startSseFixture(): Promise<SseFixture> {
  return new Promise((resolve, reject) => {
    const observations: SseRequestObservation[] = [];
    const server = createServer((req, res) => {
      handleRequest(req, res, {
        allowedOrigin: null,
        serveHtml: true,
        observations,
      });
    });
    listen(server)
      .then(({ port, baseUrl }) => {
        resolve({
          server,
          port,
          baseUrl,
          allowedOrigin: null,
          observations,
        });
      })
      .catch(reject);
    server.on("error", reject);
  });
}

/**
 * True product-origin cross-origin fixture (L12/B06):
 * - Page served from an ephemeral product origin (simulates Grok dashboard).
 * - SSE API on a different origin.
 * - API allows ONLY the explicit product Origin — never wildcard CORS.
 */
export async function startProductOriginCrossOriginFixture(): Promise<DualOriginFixture> {
  // Start page server first so we know the explicit allowed product origin.
  const pageServer = createServer((req, res) => {
    const url = req.url ?? "/";
    if (url === "/" || url.startsWith("/index")) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(FIXTURE_HTML);
      return;
    }
    res.writeHead(404, { "Content-Type": "text/plain" });
    res.end("not found");
  });
  const pageListen = await listen(pageServer);
  const productOrigin = pageListen.baseUrl;

  const apiObservations: SseRequestObservation[] = [];
  const apiServer = createServer((req, res) => {
    handleRequest(req, res, {
      allowedOrigin: productOrigin,
      serveHtml: false,
      observations: apiObservations,
    });
  });
  const apiListen = await listen(apiServer);

  return {
    api: {
      server: apiServer,
      port: apiListen.port,
      baseUrl: apiListen.baseUrl,
      allowedOrigin: productOrigin,
      observations: apiObservations,
    },
    page: {
      server: pageServer,
      port: pageListen.port,
      baseUrl: pageListen.baseUrl,
      allowedOrigin: null,
      observations: [],
    },
    productOrigin,
    apiOrigin: apiListen.baseUrl,
  };
}

export async function stopSseFixture(fixture: SseFixture): Promise<void> {
  await new Promise<void>((resolve) => fixture.server.close(() => resolve()));
}

export async function stopDualOriginFixture(fixture: DualOriginFixture): Promise<void> {
  await Promise.all([stopSseFixture(fixture.api), stopSseFixture(fixture.page)]);
}

export type EventSourceProbeResult = {
  opened: boolean;
  receivedType: string | null;
  eventBusState: string | null;
  errorSeen: boolean;
  /** Raw navigator.userAgent — Chromium headless reports HeadlessChrome; do not claim otherwise. */
  userAgent?: string;
  /** Origin of the document that opened EventSource. */
  documentOrigin?: string;
  /** Absolute EventSource URL used. */
  eventSourceUrl?: string;
  corsMode?: "same-origin" | "cross-origin";
};

/**
 * In-page probe: native browser EventSource.
 * @param eventsUrl absolute or same-origin relative URL for /api/events
 */
export async function probeNativeEventSource(
  page: {
    evaluate: <R, A = unknown>(
      pageFunction: (arg: A) => R | Promise<R>,
      arg: A,
    ) => Promise<R>;
  },
  eventsUrl: string,
  opts?: { corsMode?: "same-origin" | "cross-origin" },
): Promise<EventSourceProbeResult> {
  return page.evaluate(
    async ({ url, corsMode }) => {
      return await new Promise<EventSourceProbeResult>((resolve) => {
        const source = new EventSource(url);
        let opened = false;
        let receivedType: string | null = null;
        let eventBusState: string | null = null;
        let errorSeen = false;
        const finish = () => {
          try {
            source.close();
          } catch {
            /* ignore */
          }
          resolve({
            opened,
            receivedType,
            eventBusState,
            errorSeen,
            userAgent: navigator.userAgent,
            documentOrigin: location.origin,
            eventSourceUrl: url,
            corsMode,
          });
        };
        source.onopen = () => {
          opened = true;
        };
        source.addEventListener("run.updated", (ev) => {
          try {
            const row = JSON.parse((ev as MessageEvent).data) as { type?: string };
            receivedType = row.type ?? "run.updated";
          } catch {
            receivedType = "parse-error";
          }
        });
        source.addEventListener("eventbus.status", (ev) => {
          try {
            const row = JSON.parse((ev as MessageEvent).data) as { state?: string };
            eventBusState = row.state ?? "unknown";
          } catch {
            eventBusState = "parse-error";
          }
        });
        // Wait briefly for both frames, then probe failure path.
        const afterOpen = () => {
          source.close();
          const missingUrl = new URL(url, location.href);
          missingUrl.pathname = "/api/missing";
          missingUrl.search = "";
          const dead = new EventSource(missingUrl.toString());
          dead.onerror = () => {
            errorSeen = true;
            dead.close();
            finish();
          };
          setTimeout(() => {
            if (!errorSeen) {
              errorSeen = dead.readyState === EventSource.CLOSED;
              dead.close();
              finish();
            }
          }, 1500);
        };
        // Proceed once we have run.updated (and optionally eventbus), or timeout.
        const waitTimer = setInterval(() => {
          if (receivedType) {
            clearInterval(waitTimer);
            afterOpen();
          }
        }, 50);
        source.onerror = () => {
          if (!opened && !receivedType) {
            clearInterval(waitTimer);
            errorSeen = true;
            finish();
          }
        };
        setTimeout(() => {
          clearInterval(waitTimer);
          if (receivedType || opened) {
            afterOpen();
          } else {
            finish();
          }
        }, 5000);
      });
    },
    { url: eventsUrl, corsMode: opts?.corsMode ?? "same-origin" },
  );
}
