import { describe, expect, it } from "vitest";
import { AnypointAuthError, AnypointAuthProvider } from "../src/logs/anypoint.js";
import type { FetchLike } from "../src/http/runner.js";

// Ports tests/test_anypoint_auth.py.
const TOKEN_URL = "https://anypoint.test/accounts/api/v2/oauth2/token";

function mockFetch(handler: (url: string, init: RequestInit) => Response): FetchLike {
  return async (url, init) => handler(url, init ?? {});
}

function contentType(init: RequestInit): string {
  const h = init.headers;
  if (h instanceof Headers) return h.get("content-type") ?? "";
  return (h as Record<string, string>)?.["Content-Type"] ?? "";
}

function settings() {
  return { token_endpoint: TOKEN_URL, client_id: "cid", client_secret: "secret" };
}

describe("AnypointAuthProvider", () => {
  it("fetches a token", async () => {
    let seenUrl = "";
    const provider = new AnypointAuthProvider(settings(), mockFetch((url) => {
      seenUrl = url;
      return new Response(JSON.stringify({ access_token: "tok-1", expires_in: 3600 }), { status: 200 });
    }));
    expect(await provider.getToken()).toBe("tok-1");
    expect(seenUrl.endsWith("/oauth2/token")).toBe(true);
  });

  it("caches the token until forced refresh", async () => {
    let n = 0;
    const provider = new AnypointAuthProvider(settings(), mockFetch(() => {
      n += 1;
      return new Response(JSON.stringify({ access_token: `tok-${n}`, expires_in: 3600 }), { status: 200 });
    }));
    expect(await provider.getToken()).toBe("tok-1");
    expect(await provider.getToken()).toBe("tok-1");
    expect(n).toBe(1);
    expect(await provider.getToken(true)).toBe("tok-2");
    expect(n).toBe(2);
  });

  it("falls back to form encoding on 415", async () => {
    const attempts = { json: 0, form: 0 };
    const provider = new AnypointAuthProvider(settings(), mockFetch((_url, init) => {
      if (contentType(init).startsWith("application/json")) {
        attempts.json += 1;
        return new Response(JSON.stringify({ error: "unsupported" }), { status: 415 });
      }
      attempts.form += 1;
      return new Response(JSON.stringify({ access_token: "tok-form", expires_in: 3600 }), { status: 200 });
    }));
    expect(await provider.getToken()).toBe("tok-form");
    expect(attempts).toEqual({ json: 1, form: 1 });
  });

  it("raises on missing credentials", async () => {
    const provider = new AnypointAuthProvider({});
    await expect(provider.getToken()).rejects.toBeInstanceOf(AnypointAuthError);
  });

  it("raises on an error status", async () => {
    const provider = new AnypointAuthProvider(settings(), mockFetch(() => new Response("{}", { status: 401 })));
    await expect(provider.getToken()).rejects.toBeInstanceOf(AnypointAuthError);
  });
});
