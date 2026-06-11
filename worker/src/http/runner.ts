/**
 * call_api — the HttpRunner. Faithful port of tools/http_runner.py onto native fetch.
 *
 * Column-driven: fires whatever method/url/headers/body the suite row provides, stamps an
 * X-Correlation-ID (generating one if absent), and returns a normalized ApiResponse. Transport
 * errors become ApiCallError so the runner can attribute them to one case. `fetchFn` is
 * injectable for tests (the no-network equivalent of httpx's MockTransport client).
 */
import type { ApiResponse } from "../models.js";

export const CORRELATION_HEADER = "X-Correlation-ID";
export const DEFAULT_TIMEOUT_MS = 30_000;

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiCallError extends Error {}

export interface CallApiOptions {
  headers?: Record<string, string> | null;
  body?: unknown;
  correlationId?: string | null;
  timeoutMs?: number;
  fetchFn?: FetchLike;
}

export async function callApi(method: string, url: string, opts: CallApiOptions = {}): Promise<ApiResponse> {
  const correlationId = opts.correlationId || crypto.randomUUID().replace(/-/g, "");
  const headers = new Headers(opts.headers ?? {});
  if (!headers.has(CORRELATION_HEADER)) headers.set(CORRELATION_HEADER, correlationId);

  const init: RequestInit = { method: method.toUpperCase(), headers };
  const body = opts.body;
  if (isObjectOrArray(body)) {
    init.body = JSON.stringify(body);
    // setdefault semantics: an explicit Content-Type (e.g. text/plain) is preserved.
    if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  } else if (body !== null && body !== undefined) {
    init.body = typeof body === "string" ? body : String(body);
  }

  const fetchFn = opts.fetchFn ?? (globalThis.fetch.bind(globalThis) as FetchLike);
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  init.signal = controller.signal;

  const started = Date.now();
  let response: Response;
  try {
    response = await fetchFn(url, init);
  } catch (exc) {
    throw new ApiCallError(`${errName(exc)}: ${errMsg(exc)}`);
  } finally {
    clearTimeout(timer);
  }

  const latencyMs = Date.now() - started;
  return {
    status: response.status,
    headers: headersToObject(response.headers),
    body: await parseBody(response),
    latency_ms: latencyMs,
  };
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
  const text = await response.text();
  if (contentType.includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

function isObjectOrArray(v: unknown): v is Record<string, unknown> | unknown[] {
  return typeof v === "object" && v !== null;
}

function headersToObject(h: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  h.forEach((value, key) => {
    out[key] = value;
  });
  return out;
}

function errName(e: unknown): string {
  return e instanceof Error ? e.name : "Error";
}
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
