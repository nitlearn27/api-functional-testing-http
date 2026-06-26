/**
 * AnypointLogSource + token provider — ports logsource/anypoint_source.py and
 * logsource/anypoint_auth.py onto native fetch.
 *
 * Token: OAuth2 client-credentials against the Anypoint token endpoint, cached until shortly
 * before expiry; JSON body with a form-encoded fallback on 400/415. Logs: resolve the
 * deployment's *current* spec version (desiredVersion) at fetch time and build
 * `.../specs/{version}/logs/file` from it (a pinned `/specs/...` segment is replaced), with a
 * bounded backoff on transient 401/429/5xx. The client secret and token are never logged.
 */
import type { FetchLike } from "../http/runner.js";
import type { LogSource, RawSnapshot } from "./snapshot.js";

export interface AnypointSettings {
  application_logs_fetch_url?: string;
  token_endpoint?: string;
  client_id?: string;
  client_secret?: string;
  grant_type?: string;
}

export type SleepFn = (seconds: number) => Promise<void>;

const realSleep: SleepFn = (s) => new Promise((r) => setTimeout(r, s * 1000));

// --- auth ------------------------------------------------------------------------------

const EXPIRY_SKEW_SECONDS = 60;

export class AnypointAuthError extends Error {}

export class AnypointAuthProvider {
  private token: string | null = null;
  private expiresAt = 0;

  // fetch must stay bound to globalThis: calling it as `this.fetchFn(...)` with a bare
  // `globalThis.fetch` rebinds `this` and Workers throws "Illegal invocation".
  constructor(
    private settings: AnypointSettings,
    private fetchFn: FetchLike = globalThis.fetch.bind(globalThis) as FetchLike,
  ) {}

  async getToken(forceRefresh = false): Promise<string> {
    if (!forceRefresh && this.token && nowSeconds() < this.expiresAt) return this.token;
    return this.fetchToken();
  }

  private async fetchToken(): Promise<string> {
    const s = this.settings;
    if (!(s.token_endpoint && s.client_id && s.client_secret)) {
      throw new AnypointAuthError("missing Anypoint credentials (token_endpoint/client_id/client_secret in .env)");
    }
    const payload: Record<string, string> = {
      grant_type: s.grant_type ?? "client_credentials",
      client_id: s.client_id,
      client_secret: s.client_secret,
    };

    let resp: Response;
    try {
      resp = await this.fetchFn(s.token_endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (resp.status === 400 || resp.status === 415) {
        resp = await this.fetchFn(s.token_endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(payload).toString(),
        });
      }
    } catch (exc) {
      throw new AnypointAuthError(`token request failed: ${errStr(exc)}`);
    }

    if (resp.status !== 200) throw new AnypointAuthError(`token endpoint returned HTTP ${resp.status}`);

    let data: any;
    try {
      data = await resp.json();
    } catch {
      throw new AnypointAuthError("token response was not JSON");
    }
    const token = data.access_token;
    if (!token) throw new AnypointAuthError("token response had no access_token");

    const expiresIn = Number(data.expires_in ?? 3600);
    this.token = token;
    this.expiresAt = nowSeconds() + Math.max(expiresIn - EXPIRY_SKEW_SECONDS, 0);
    return token;
  }
}

// --- log source ------------------------------------------------------------------------

const INSTANCE = "cloudhub";
const MAX_RETRIES = 3;
const BACKOFF_BASE_SECONDS = 2.0;

export class AnypointLogError extends Error {}

export class AnypointLogSource implements LogSource {
  private auth: AnypointAuthProvider;

  constructor(
    private settings: AnypointSettings,
    auth?: AnypointAuthProvider,
    private fetchFn: FetchLike = globalThis.fetch.bind(globalThis) as FetchLike,
    private sleep: SleepFn = realSleep,
  ) {
    this.auth = auth ?? new AnypointAuthProvider(settings, this.fetchFn);
  }

  discoverInstances(): string[] {
    return [INSTANCE];
  }

  async snapshot(_instances?: string[] | null): Promise<RawSnapshot> {
    const url = await this.logUrl();
    if (!url) throw new AnypointLogError("application_logs_fetch_url is not set in the suite");
    const response = await this.getWithRetry(url);
    const lines = await parseLogBody(response);
    return { lines_by_instance: { [INSTANCE]: lines } };
  }

  private async logUrl(): Promise<string | null> {
    const configured = this.settings.application_logs_fetch_url;
    if (!configured) return null;
    const base = configured.split("/specs/")[0].replace(/\/+$/, ""); // base = .../deployments/{id}
    const version = await this.currentVersion(base);
    if (!version) throw new AnypointLogError(`could not resolve the deployment's current spec version from ${base}`);
    return `${base}/specs/${version}/logs/file`;
  }

  private async currentVersion(deploymentUrl: string): Promise<string | null> {
    const token = await this.auth.getToken();
    const response = await this.fetchFn(deploymentUrl, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    if (response.status !== 200) throw new AnypointLogError(`deployment lookup returned HTTP ${response.status}`);
    const data: any = await response.json();
    return data.desiredVersion ?? data.lastSuccessfulVersion ?? null;
  }

  private async getWithRetry(url: string): Promise<Response> {
    let lastStatus: number | null = null;
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      const token = await this.auth.getToken(attempt > 0 && lastStatus === 401);
      let response: Response;
      try {
        response = await this.fetchFn(url, { headers: { Authorization: `Bearer ${token}`, Accept: "*/*" } });
      } catch (exc) {
        throw new AnypointLogError(`log download failed: ${errStr(exc)}`);
      }
      if (response.status === 200) return response;
      lastStatus = response.status;
      // Retry on transient throttle/file-limit/auth errors; otherwise fail fast.
      if (![401, 429, 500, 502, 503].includes(response.status)) {
        throw new AnypointLogError(`log endpoint returned HTTP ${response.status}`);
      }
      if (attempt < MAX_RETRIES - 1) await this.sleep(BACKOFF_BASE_SECONDS * 2 ** attempt);
    }
    throw new AnypointLogError(`log download failed after ${MAX_RETRIES} attempts (last HTTP ${lastStatus})`);
  }
}

// --- log body parsing ------------------------------------------------------------------

async function parseLogBody(response: Response): Promise<string[]> {
  const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
  if (contentType.includes("json")) {
    try {
      return linesFromJson(await response.json());
    } catch {
      // fall through to text
    }
  }
  return splitlines(await response.text());
}

function linesFromJson(payload: any): string[] {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    for (const key of ["data", "logs", "events", "records", "items"]) {
      if (Array.isArray(payload[key])) {
        payload = payload[key];
        break;
      }
    }
  }
  if (!Array.isArray(payload)) return [String(payload)];

  const lines: string[] = [];
  for (const item of payload) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      let found = false;
      for (const key of ["message", "line", "log", "logLine", "msg", "text"]) {
        if (key in item) {
          lines.push(String(item[key]));
          found = true;
          break;
        }
      }
      if (!found) lines.push(String(item));
    } else {
      lines.push(String(item));
    }
  }
  return lines;
}

/** Mirror Python str.splitlines(): split on newlines, dropping a single trailing empty. */
function splitlines(text: string): string[] {
  const parts = text.split(/\r\n|\r|\n/);
  if (parts.length && parts[parts.length - 1] === "") parts.pop();
  return parts;
}

function nowSeconds(): number {
  return Date.now() / 1000;
}

function errStr(e: unknown): string {
  return e instanceof Error ? `${e.name}: ${e.message}` : String(e);
}
