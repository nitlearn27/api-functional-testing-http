import { describe, expect, it } from "vitest";
import { AnypointLogError, AnypointLogSource, type AnypointSettings } from "../src/logs/anypoint.js";
import { SnapshotStore, totalLines } from "../src/logs/snapshot.js";
import type { FetchLike } from "../src/http/runner.js";

// Ports tests/test_anypoint_source.py.
const TOKEN_URL = "https://anypoint.test/accounts/api/v2/oauth2/token";
const DEPLOYMENT_URL = "https://anypoint.test/amc/.../deployments/dep-1";
const LIVE_VERSION = "spec-live";
const SAMPLE_LOG =
  "2026-06-04 10:00:01 INFO Order intake started [correlationId: TC-001-abc]\n" +
  "2026-06-04 10:00:02 INFO Order ACCEPTED sku=ABC-100 qty=2 [correlationId: TC-001-abc]\n" +
  "2026-06-04 10:00:03 INFO unrelated line [correlationId: other-1]\n";

const noSleep = async () => {};

function settings(url = DEPLOYMENT_URL): AnypointSettings {
  return { token_endpoint: TOKEN_URL, application_logs_fetch_url: url, client_id: "cid", client_secret: "secret" };
}

function mockFetch(handler: (url: string, init: RequestInit) => Response): FetchLike {
  return async (url, init) => handler(url, init ?? {});
}

/** Handle token + deployment-version lookups; delegate the log fetch to logResponse. */
function tokenOr(logResponse: (url: string, init: RequestInit) => Response) {
  return (url: string, init: RequestInit): Response => {
    if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), { status: 200 });
    if (url === DEPLOYMENT_URL) return new Response(JSON.stringify({ desiredVersion: LIVE_VERSION }), { status: 200 });
    return logResponse(url, init);
  };
}

function source(handler: (url: string, init: RequestInit) => Response, s: AnypointSettings = settings()) {
  return new AnypointLogSource(s, undefined, mockFetch(handler), noSleep);
}

describe("AnypointLogSource", () => {
  it("parses text logs", async () => {
    const snap = await source(tokenOr(() => new Response(SAMPLE_LOG, { status: 200 }))).snapshot();
    const lines = snap.lines_by_instance.cloudhub;
    expect(lines.length).toBe(3);
    expect(lines[1]).toContain("Order ACCEPTED sku=ABC-100 qty=2");
  });

  it("sends a bearer token on the log request", async () => {
    let auth: string | null = null;
    const snap = source(tokenOr((_url, init) => {
      auth = (init.headers as Record<string, string>).Authorization;
      return new Response(SAMPLE_LOG, { status: 200 });
    }));
    await snap.snapshot();
    expect(auth).toBe("Bearer tok");
  });

  it("parses JSON logs", async () => {
    const snap = await source(
      tokenOr(() => new Response(JSON.stringify({ data: [{ message: "line A" }, { message: "line B" }] }), { status: 200, headers: { "content-type": "application/json" } })),
    ).snapshot();
    expect(snap.lines_by_instance.cloudhub).toEqual(["line A", "line B"]);
  });

  it("retries on 429 then succeeds", async () => {
    let n = 0;
    const snap = await source(tokenOr(() => {
      n += 1;
      return n === 1 ? new Response("slow down", { status: 429 }) : new Response(SAMPLE_LOG, { status: 200 });
    })).snapshot();
    expect(n).toBe(2);
    expect(totalLines(snap)).toBe(3);
  });

  it("resolves a pinned spec URL to the live deployment version", async () => {
    const dep = DEPLOYMENT_URL;
    const pinned = `${dep}/specs/OLD-spec/logs/file`;
    let logUrl = "";
    const handler = (url: string): Response => {
      if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), { status: 200 });
      if (url === dep) return new Response(JSON.stringify({ desiredVersion: "NEW-spec", lastSuccessfulVersion: "NEW-spec" }), { status: 200 });
      logUrl = url;
      return new Response(SAMPLE_LOG, { status: 200 });
    };
    await source(handler, settings(pinned)).snapshot();
    expect(logUrl).toBe(`${dep}/specs/NEW-spec/logs/file`);
  });

  it("builds the log URL from a bare deployment base", async () => {
    let logUrl = "";
    const handler = (url: string): Response => {
      if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), { status: 200 });
      if (url === DEPLOYMENT_URL) return new Response(JSON.stringify({ desiredVersion: "NEW-spec" }), { status: 200 });
      logUrl = url;
      return new Response(SAMPLE_LOG, { status: 200 });
    };
    await source(handler).snapshot();
    expect(logUrl).toBe(`${DEPLOYMENT_URL}/specs/NEW-spec/logs/file`);
  });

  it("raises when the deployment lookup fails (no pinned fallback)", async () => {
    const handler = (url: string): Response => {
      if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), { status: 200 });
      if (url === DEPLOYMENT_URL) return new Response("boom", { status: 500 });
      return new Response(SAMPLE_LOG, { status: 200 });
    };
    await expect(source(handler).snapshot()).rejects.toBeInstanceOf(AnypointLogError);
  });

  it("raises on a non-retryable log status", async () => {
    await expect(source(tokenOr(() => new Response("not found", { status: 404 }))).snapshot()).rejects.toBeInstanceOf(AnypointLogError);
  });

  it("indexes correlation ids through the snapshot store", async () => {
    const store = new SnapshotStore();
    const snap = await store.create(source(tokenOr(() => new Response(SAMPLE_LOG, { status: 200 }))));
    expect(snap.linesFor("TC-001-abc").length).toBe(2);
    expect(snap.linesFor("other-1").length).toBeGreaterThan(0);
  });
});
