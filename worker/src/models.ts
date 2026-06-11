/**
 * Shared contract types — the TypeScript port of models.py.
 *
 * These are the JSON-serializable shapes that cross the MCP boundary and flow between the
 * MCP server, the JobRunner, and the pure logic modules. String-literal unions mirror the
 * Python StrEnum values exactly (so suite cells and reports serialize identically).
 */

export type MatchMode = "exact" | "json_subset" | "schema" | "status_only";
export const MATCH_MODES: MatchMode[] = ["exact", "json_subset", "schema", "status_only"];

export type LogMatchMode = "contains" | "regex" | "all_of" | "any_of";
export const LOG_MATCH_MODES: LogMatchMode[] = ["contains", "regex", "all_of", "any_of"];

// --- Test suite ------------------------------------------------------------------------

export interface TestCase {
  test_id: string;
  description: string | null;
  method: string; // upper-cased; default "GET"
  url: string;
  headers: Record<string, unknown>;
  body: unknown;
  auth_required: boolean; // default true
  expected_status: number | null;
  expected_response: unknown;
  response_match_mode: MatchMode; // default "json_subset"
  ignore_paths: string[];
  validate_logs: boolean; // default false
  expected_log_strings: string[];
  log_match_mode: LogMatchMode; // default "contains"
  log_source: string; // default "anypoint"
}

/** Factory applying the same field defaults as the Python TestCase model. */
export function makeTestCase(partial: Partial<TestCase> & { test_id: string }): TestCase {
  return {
    description: null,
    method: "GET",
    url: "",
    headers: {},
    body: null,
    auth_required: true,
    expected_status: null,
    expected_response: null,
    response_match_mode: "json_subset",
    ignore_paths: [],
    validate_logs: false,
    expected_log_strings: [],
    log_match_mode: "contains",
    log_source: "anypoint",
    ...partial,
  };
}

export interface ParseError {
  row: number; // 1-based row number in the sheet (including header); 0 for file-level
  column: string | null;
  message: string;
}

export interface TestSuite {
  base_path: string | null;
  cases: TestCase[];
  parse_errors: ParseError[];
}

// --- API call / assertion --------------------------------------------------------------

export interface ApiResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
  latency_ms: number | null;
}

export interface ResponseDiff {
  path: string;
  expected?: unknown;
  actual?: unknown;
  message: string;
}

export interface AssertResult {
  passed: boolean;
  mode: MatchMode;
  status_ok: boolean;
  diffs: ResponseDiff[];
}

// --- Log validation --------------------------------------------------------------------

export interface LogValidationResult {
  passed: boolean;
  correlation_id: string;
  matched: string[];
  missing: string[];
  lines_considered: number;
  used_fallback: boolean;
}

// --- Suite report ----------------------------------------------------------------------

export interface CaseReport {
  test_id: string;
  passed: boolean;
  correlation_id: string | null;
  actual_status: number | null;
  expected_status: number | null;
  response_assert: AssertResult | null;
  log_validation: LogValidationResult | null;
  error: string | null;
}

export interface SuiteReport {
  total: number;
  passed: number;
  failed: number;
  cases: CaseReport[];
  parse_errors: ParseError[];
}

// --- Per-case evidence (results workbook tabs) -----------------------------------------

export interface CaseEvidence {
  test_id: string;
  description: string | null;
  passed: boolean;
  error: string | null;
  method: string | null;
  url: string | null;
  request_headers: Record<string, unknown>;
  request_body: unknown;
  actual_status: number | null;
  expected_status: number | null;
  latency_ms: number | null;
  match_mode: MatchMode | null;
  response_passed: boolean | null;
  response_diffs: ResponseDiff[];
  expected_response: unknown;
  actual_body: unknown;
  validated_logs: boolean;
  logs_passed: boolean | null;
  log_source: string | null;
  correlation_id: string | null;
  expected_log_strings: string[];
  matched_logs: string[];
  missing_logs: string[];
  used_fallback: boolean;
  lines_considered: number;
  matched_log_lines: Record<string, string[]>;
}
