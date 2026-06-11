/**
 * Orchestration core — the testable, DO-free port of tools/orchestrate.py.
 *
 * The Python orchestrator is one synchronous function with time.sleep() for the propagation
 * wait and the log-fetch retry loop. Here those waits are owned by the JobRunner Durable Object
 * (via this.schedule / DO alarms), so the logic is split into pure pieces the DO composes:
 *
 *   1. runRequestsPhase  — Phase 1: per-case request + response assertion (no logs).
 *   2. distinctSources   — group log-validating cases by log_source (download once each).
 *   3. decideRetry       — the _snapshot_with_retry stop/continue decision, per firing.
 *   4. validateGroup     — strict, correlation-scoped log validation + evidence merge.
 *   5. finalizeReport / buildEvidence — aggregate.
 *
 * All network/IO is injected so this module is fully unit-testable offline, exactly like the
 * Python tests that monkeypatch call_api / snapshot_logs / time.sleep.
 */
import { assertResponse } from "./matching/response-matcher.js";
import { callApi as defaultCallApi, ApiCallError } from "./http/runner.js";
import type { FetchLike } from "./http/runner.js";
import {
  matchedLogLines,
  type Snapshot,
  validateLogs,
} from "./logs/snapshot.js";
import type {
  ApiResponse,
  CaseEvidence,
  CaseReport,
  ParseError,
  SuiteReport,
  TestCase,
  TestSuite,
} from "./models.js";

/** The auth stub — Phase 3 in the Python project; still unimplemented. */
export class NotImplementedError extends Error {}
export function getAuthTokenStub(): never {
  throw new NotImplementedError(
    "get_auth_token is a Phase 3 stub (OAuth2 client-credentials); blocked on Gate-0 OAuth details.",
  );
}

export interface CaseRun {
  case: TestCase;
  report: CaseReport;
  correlation_id: string | null;
  sent_request: { method: string; url: string; headers: Record<string, string>; body: unknown } | null;
  response: ApiResponse | null;
  matched_log_lines: Record<string, string[]> | null;
}

export interface RequestDeps {
  callApi: (method: string, url: string, opts: { headers: Record<string, string>; body: unknown; correlationId: string }) => Promise<ApiResponse>;
  getAuthToken: () => Promise<string> | string;
  newCorrelationId: (testId: string) => string;
}

export function defaultRequestDeps(fetchFn?: FetchLike): RequestDeps {
  return {
    callApi: (method, url, opts) => defaultCallApi(method, url, { ...opts, fetchFn }),
    getAuthToken: getAuthTokenStub,
    newCorrelationId: (testId) => `${testId}-${randomHex(12)}`,
  };
}

// --- Phase 1: requests + assertions ----------------------------------------------------

export async function runRequestsPhase(suite: TestSuite, deps: RequestDeps): Promise<CaseRun[]> {
  const runs: CaseRun[] = [];
  for (const c of suite.cases) {
    runs.push(await runRequest(c, suite.base_path, deps));
  }
  return runs;
}

export async function runRequest(c: TestCase, basePath: string | null, deps: RequestDeps): Promise<CaseRun> {
  // Generate the correlation id up front so it is recorded even if the request fails.
  const correlationId = deps.newCorrelationId(c.test_id);
  try {
    const url = joinUrl(basePath, c.url);
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(c.headers)) headers[k] = String(v);
    if (c.auth_required) headers.Authorization = `Bearer ${await deps.getAuthToken()}`;

    const sentRequest = { method: c.method, url, headers, body: c.body };
    const response = await deps.callApi(c.method, url, { headers, body: c.body, correlationId });
    const responseAssert = assertResponse({
      actual_body: response.body,
      expected: c.expected_response,
      mode: c.response_match_mode,
      ignore_paths: c.ignore_paths,
      actual_status: response.status,
      expected_status: c.expected_status,
    });
    const report: CaseReport = {
      test_id: c.test_id,
      passed: responseAssert.passed,
      correlation_id: correlationId,
      actual_status: response.status,
      expected_status: c.expected_status,
      response_assert: responseAssert,
      log_validation: null,
      error: null,
    };
    return { case: c, report, correlation_id: correlationId, sent_request: sentRequest, response, matched_log_lines: null };
  } catch (exc) {
    let message: string;
    if (exc instanceof ApiCallError) message = `request failed: ${exc.message}`;
    else if (exc instanceof NotImplementedError) message = exc.message;
    else message = `${exc instanceof Error ? exc.name : "Error"}: ${exc instanceof Error ? exc.message : String(exc)}`;
    return failedRun(c, correlationId, message);
  }
}

function failedRun(c: TestCase, correlationId: string, error: string): CaseRun {
  return {
    case: c,
    report: {
      test_id: c.test_id,
      passed: false,
      correlation_id: correlationId,
      actual_status: null,
      expected_status: c.expected_status,
      response_assert: null,
      log_validation: null,
      error,
    },
    correlation_id: correlationId,
    sent_request: null,
    response: null,
    matched_log_lines: null,
  };
}

export function joinUrl(basePath: string | null, url: string): string {
  if (url.startsWith("http://") || url.startsWith("https://") || !basePath) return url;
  return `${basePath.replace(/\/+$/, "")}/${url.replace(/^\/+/, "")}`;
}

// --- Phase 2: log validation pieces ----------------------------------------------------

/** Cases that opted into log validation and have a correlation id, grouped by source (first-seen order). */
export function distinctSources(runs: CaseRun[]): { source: string; runs: CaseRun[] }[] {
  const order: string[] = [];
  const bySource = new Map<string, CaseRun[]>();
  for (const r of runs) {
    if (!(r.case.validate_logs && r.correlation_id)) continue;
    const src = r.case.log_source;
    if (!bySource.has(src)) {
      bySource.set(src, []);
      order.push(src);
    }
    bySource.get(src)!.push(r);
  }
  return order.map((source) => ({ source, runs: bySource.get(source)! }));
}

/**
 * The _snapshot_with_retry decision, evaluated once per logPhase firing: validate now if every
 * correlation id has surfaced OR the retry budget is exhausted; otherwise retry after the wait.
 */
export function decideRetry(allPresent: boolean, attempt: number, maxRetries: number): "validate" | "retry" {
  return allPresent || attempt >= maxRetries ? "validate" : "retry";
}

/**
 * Correlation-scoped validation of one source's group against a fresh snapshot. When
 * `correlationFallback` is set and no lines carry a case's correlation id, the whole snapshot
 * is searched instead (Python parity: log_correlation_fallback defaults to true there).
 */
export function validateGroup(snap: Snapshot, group: CaseRun[], correlationFallback = false): void {
  for (const r of group) {
    const corr = r.correlation_id!;
    const lv = validateLogs(snap, corr, r.case.expected_log_strings, r.case.log_match_mode, correlationFallback);
    r.report.log_validation = lv;
    if (!lv.passed) r.report.passed = false;
    r.matched_log_lines = matchedLogLines(snap, corr, r.case.expected_log_strings, r.case.log_match_mode, correlationFallback);
  }
}

/** Attribute a snapshot-download failure to every case in the group (matches the Python path). */
export function failGroupSnapshot(group: CaseRun[], errorMessage: string): void {
  for (const r of group) {
    r.report.error = (r.report.error ?? "") + ` log snapshot failed: ${errorMessage}`;
    r.report.passed = false;
  }
}

// --- aggregation -----------------------------------------------------------------------

export function finalizeReport(runs: CaseRun[], parseErrors: ParseError[]): SuiteReport {
  const cases = runs.map((r) => r.report);
  const passed = cases.filter((c) => c.passed).length;
  return { total: cases.length, passed, failed: cases.length - passed, cases, parse_errors: parseErrors };
}

export function buildEvidence(run: CaseRun): CaseEvidence {
  const { case: c, report: rep } = run;
  const ra = rep.response_assert;
  const lv = rep.log_validation;
  const resp = run.response;
  const req = run.sent_request ?? ({} as NonNullable<CaseRun["sent_request"]>);
  return {
    test_id: c.test_id,
    description: c.description,
    passed: rep.passed,
    error: rep.error,
    method: req.method ?? null,
    url: req.url ?? null,
    request_headers: req.headers ?? {},
    request_body: req.body ?? null,
    actual_status: rep.actual_status,
    expected_status: rep.expected_status,
    latency_ms: resp ? resp.latency_ms : null,
    match_mode: ra ? ra.mode : null,
    response_passed: ra ? ra.passed : null,
    response_diffs: ra ? ra.diffs : [],
    expected_response: c.expected_response,
    actual_body: resp ? resp.body : null,
    validated_logs: c.validate_logs,
    logs_passed: lv ? lv.passed : null,
    log_source: c.validate_logs ? c.log_source : null,
    correlation_id: rep.correlation_id,
    expected_log_strings: c.expected_log_strings,
    matched_logs: lv ? lv.matched : [],
    missing_logs: lv ? lv.missing : [],
    used_fallback: lv ? lv.used_fallback : false,
    lines_considered: lv ? lv.lines_considered : 0,
    matched_log_lines: run.matched_log_lines ?? {},
  };
}

function randomHex(n: number): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, n);
}
