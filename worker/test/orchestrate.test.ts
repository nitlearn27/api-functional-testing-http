import { describe, expect, it } from "vitest";
import {
  buildEvidence,
  decideRetry,
  distinctSources,
  finalizeReport,
  joinUrl,
  getAuthTokenStub,
  runRequestsPhase,
  validateGroup,
  type RequestDeps,
} from "../src/orchestrate.js";
import { SnapshotStore, type LogSource, type RawSnapshot } from "../src/logs/snapshot.js";
import { makeTestCase, type ApiResponse, type TestSuite } from "../src/models.js";

// Ports tests/test_run_suite.py and tests/test_orchestrate_logs.py (the pure pieces; the long
// waits are owned by the JobRunner DO and exercised live in the Phase 5 smoke test).

function deps(callApi: RequestDeps["callApi"], fixedCorr?: (id: string) => string): RequestDeps {
  return {
    callApi,
    getAuthToken: () => "unused",
    newCorrelationId: fixedCorr ?? ((id) => `${id}-x`),
  };
}

function jsonResponse(status: number, body: unknown): ApiResponse {
  return { status, headers: { "content-type": "application/json" }, body, latency_ms: 1 };
}

const SUITE: TestSuite = {
  base_path: "https://api.test/",
  parse_errors: [],
  cases: [
    makeTestCase({ test_id: "TC-001", method: "POST", url: "/orders", auth_required: false, body: { sku: "ABC-100", qty: 2 }, expected_status: 201, expected_response: { status: "ACCEPTED", sku: "ABC-100" }, response_match_mode: "json_subset" }),
    makeTestCase({ test_id: "TC-002", method: "POST", url: "/orders", auth_required: false, body: { sku: "ABC-100" }, expected_status: 400, expected_response: { error: "VALIDATION_ERROR", field: "qty" }, response_match_mode: "json_subset" }),
  ],
};

describe("orchestrate — request phase", () => {
  it("all cases pass against a matching backend", async () => {
    const callApi: RequestDeps["callApi"] = async (_m, _u, opts) => {
      const body = opts.body as Record<string, unknown>;
      return "qty" in body
        ? jsonResponse(201, { status: "ACCEPTED", sku: body.sku })
        : jsonResponse(400, { error: "VALIDATION_ERROR", field: "qty" });
    };
    const runs = await runRequestsPhase(SUITE, deps(callApi));
    const report = finalizeReport(runs, []);
    expect(report.total).toBe(2);
    expect(report.passed).toBe(2);
    expect(report.failed).toBe(0);
    expect(report.cases.every((c) => c.log_validation === null)).toBe(true);
  });

  it("reports failure when the backend 500s", async () => {
    const callApi: RequestDeps["callApi"] = async () => jsonResponse(500, { error: "boom" });
    const runs = await runRequestsPhase(SUITE, deps(callApi));
    const report = finalizeReport(runs, []);
    expect(report.passed).toBe(0);
    expect(report.failed).toBe(2);
    expect(report.cases.every((c) => !c.passed)).toBe(true);
  });

  it("joins base path and case url preserving the base subpath", () => {
    expect(joinUrl("https://api.test/api", "/products")).toBe("https://api.test/api/products");
    expect(joinUrl("https://api.test/api", "https://other/x")).toBe("https://other/x");
    expect(joinUrl(null, "/products")).toBe("/products");
  });

  it("auth_required case fails with the stub message", async () => {
    const suite: TestSuite = {
      base_path: null,
      parse_errors: [],
      cases: [makeTestCase({ test_id: "TC-AUTH", url: "https://api.test/x", auth_required: true })],
    };
    const authDeps: RequestDeps = { ...deps(async () => jsonResponse(200, {})), getAuthToken: getAuthTokenStub };
    const runs = await runRequestsPhase(suite, authDeps);
    expect(runs[0].report.passed).toBe(false);
    expect(runs[0].report.error).toContain("get_auth_token");
  });
});

// --- log phase ---------------------------------------------------------------------------

const LOG_LINES = [
  "2026-06-04T10:00:01Z INFO event:TC-1-abcdef012345 - Order lookup succeeded",
  "2026-06-04T10:00:02Z INFO event:OTHER-999 - Order lookup succeeded",
];

class FakeSource implements LogSource {
  discoverInstances() {
    return ["cloudhub"];
  }
  async snapshot(): Promise<RawSnapshot> {
    return { lines_by_instance: { cloudhub: LOG_LINES } };
  }
}

describe("orchestrate — log phase", () => {
  it("validates strictly on the correlation line only and merges results", async () => {
    const logSuite: TestSuite = {
      base_path: "https://api.test/",
      parse_errors: [],
      cases: [
        makeTestCase({ test_id: "TC-1", method: "POST", url: "/orders", auth_required: false, expected_status: 201, validate_logs: true, expected_log_strings: ["Order lookup succeeded"], log_source: "anypoint" }),
        makeTestCase({ test_id: "TC-2", method: "POST", url: "/orders", auth_required: false, expected_status: 201, validate_logs: true, expected_log_strings: ["string that is not in the log"], log_source: "anypoint" }),
      ],
    };
    const fixed: Record<string, string> = { "TC-1": "TC-1-abcdef012345", "TC-2": "TC-2-deadbeef0000" };
    const runs = await runRequestsPhase(logSuite, deps(async () => jsonResponse(201, null), (id) => fixed[id]));

    const groups = distinctSources(runs);
    expect(groups.length).toBe(1); // both share anypoint -> one snapshot
    const snap = await new SnapshotStore().create(new FakeSource());
    validateGroup(snap, groups[0].runs);

    const byId = Object.fromEntries(runs.map((r) => [r.case.test_id, r]));
    const tc1 = byId["TC-1"];
    expect(tc1.report.log_validation!.passed).toBe(true);
    expect(tc1.report.passed).toBe(true);
    expect(tc1.report.log_validation!.used_fallback).toBe(false);
    expect(tc1.report.log_validation!.lines_considered).toBe(1); // OTHER-999 line excluded

    const tc2 = byId["TC-2"];
    expect(tc2.report.log_validation!.passed).toBe(false);
    expect(tc2.report.passed).toBe(false);
    expect(tc2.report.log_validation!.missing).toEqual(["string that is not in the log"]);
  });

  it("decideRetry stops when present or budget exhausted", () => {
    expect(decideRetry(false, 0, 3)).toBe("retry");
    expect(decideRetry(true, 0, 3)).toBe("validate");
    expect(decideRetry(false, 2, 2)).toBe("validate"); // exhausted
    expect(decideRetry(false, 1, 2)).toBe("retry");
  });

  it("skips the log phase entirely when no case opts in", async () => {
    const noLogSuite: TestSuite = {
      base_path: null,
      parse_errors: [],
      cases: [makeTestCase({ test_id: "TC-1", url: "https://api.test/x", auth_required: false, expected_status: 201, validate_logs: false })],
    };
    const runs = await runRequestsPhase(noLogSuite, deps(async () => jsonResponse(201, null)));
    expect(distinctSources(runs).length).toBe(0);
    const ev = buildEvidence(runs[0]);
    expect(ev.validated_logs).toBe(false);
  });
});
