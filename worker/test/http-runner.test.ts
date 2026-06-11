import { describe, expect, it } from "vitest";
import { ApiCallError, callApi, CORRELATION_HEADER, type FetchLike } from "../src/http/runner.js";

// Mirrors tests/test_http_runner.py — a mock fetch stands in for httpx MockTransport.
function mockFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>): FetchLike {
  return async (url, init) => handler(url, init ?? {});
}

describe("call_api", () => {
  it("posts JSON and stamps the correlation id", async () => {
    const seen: Record<string, unknown> = {};
    const fetchFn = mockFetch((url, init) => {
      seen.method = init.method;
      seen.url = url;
      seen.corr = (init.headers as Headers).get(CORRELATION_HEADER);
      seen.body = init.body;
      return new Response(JSON.stringify({ status: "ACCEPTED", sku: "ABC-100" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    });

    const resp = await callApi("post", "https://api.test/orders", {
      headers: { "Content-Type": "application/json" },
      body: { sku: "ABC-100", qty: 2 },
      correlationId: "TC-001-abc",
      fetchFn,
    });

    expect(resp.status).toBe(201);
    expect(resp.body).toEqual({ status: "ACCEPTED", sku: "ABC-100" });
    expect(seen.method).toBe("POST");
    expect(seen.corr).toBe("TC-001-abc");
    expect(String(seen.body)).toContain("ABC-100");
  });

  it("generates a correlation id when absent and returns text for non-JSON", async () => {
    const captured: Record<string, unknown> = {};
    const fetchFn = mockFetch((_url, init) => {
      captured.corr = (init.headers as Headers).get(CORRELATION_HEADER);
      return new Response("ok", { status: 200, headers: { "content-type": "text/plain" } });
    });

    const resp = await callApi("get", "https://api.test/health", { fetchFn });
    expect(resp.status).toBe(200);
    expect(resp.body).toBe("ok");
    expect(captured.corr).toBeTruthy();
  });

  it("wraps transport errors as ApiCallError", async () => {
    const fetchFn = mockFetch(() => {
      throw new TypeError("boom");
    });
    await expect(callApi("get", "https://unreachable.test/", { fetchFn })).rejects.toBeInstanceOf(ApiCallError);
  });
});
