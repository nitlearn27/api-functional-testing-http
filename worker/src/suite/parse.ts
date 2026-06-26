/**
 * Suite parser — parse an .xlsx test suite (in-memory bytes) into structured TestCases.
 *
 * Faithful port of tools/suite.py. The only boundary change is input: there is no filesystem,
 * so this takes the workbook bytes (the MCP tool base64-decodes the attached .xlsx) instead of
 * a path. `.numbers` is unsupported by design. All header detection, metadata extraction, column
 * mapping, JSON-cell parsing, coercion, and the RESULTS-marker stop are preserved exactly.
 */
import * as XLSX from "xlsx";
import type { LogMatchMode, MatchMode, ParseError, TestSuite } from "../models.js";
import { LOG_MATCH_MODES, makeTestCase, MATCH_MODES } from "../models.js";

type Cell = string | number | boolean | null;

// Canonical column header -> TestCase field (case-insensitive/trimmed). Aliases included.
const COLUMNS: Record<string, string> = {
  test_id: "test_id",
  description: "description",
  method: "method",
  url: "url",
  headers: "headers",
  body: "body",
  auth_required: "auth_required",
  expected_status: "expected_status",
  expected_response: "expected_response",
  response_match_mode: "response_match_mode",
  match_mode: "response_match_mode", // alias
  ignore_paths: "ignore_paths",
  validate_logs: "validate_logs",
  expected_log_strings: "expected_log_strings",
  expected_logs: "expected_log_strings", // alias
  log_match_mode: "log_match_mode",
  log_source: "log_source",
};

const BOOL_TRUE = new Set(["yes", "y", "true", "1"]);
const BOOL_FALSE = new Set(["no", "n", "false", "0"]);
export const RESULTS_MARKER = "results";

export function readTestSuite(bytes: Uint8Array): TestSuite {
  let rows: Cell[][];
  try {
    rows = loadRows(bytes);
  } catch (exc) {
    return { base_path: null, application_logs_fetch_url: null, cases: [], parse_errors: [{ row: 0, column: null, message: `could not read sheet: ${errMsg(exc)}` }] };
  }

  const headerIdx = findHeaderRow(rows);
  if (headerIdx === null) {
    return { base_path: null, application_logs_fetch_url: null, cases: [], parse_errors: [{ row: 0, column: null, message: "no header row containing 'test_id' found" }] };
  }

  const metaRows = rows.slice(0, headerIdx);
  const basePath = extractBasePath(metaRows);
  const logUrl = extractLogUrl(metaRows);
  const headerMap = mapHeaders(rows[headerIdx]);

  const suite: TestSuite = { base_path: basePath, application_logs_fetch_url: logUrl, cases: [], parse_errors: [] };
  const seenIds = new Set<string>();
  const tidIdx = headerMap["test_id"];
  for (let i = headerIdx + 1; i < rows.length; i++) {
    const rawRow = rows[i];
    const rowNo = i + 1; // 1-based, matching Python's offset
    if (isBlank(rawRow)) continue;
    const marker = tidIdx < rawRow.length ? asStr(rawRow[tidIdx]) : null;
    if (marker && marker.toLowerCase().startsWith(RESULTS_MARKER)) break;
    parseRow(rawRow, headerMap, rowNo, seenIds, suite);
  }

  return suite;
}

// --- readers ---------------------------------------------------------------------------

export function loadRows(bytes: Uint8Array): Cell[][] {
  const wb = XLSX.read(bytes, { type: "array" });
  const name = wb.SheetNames.find((n) => n.toLowerCase() === "tests") ?? wb.SheetNames[0];
  const sheet = wb.Sheets[name];
  // header:1 -> array-of-arrays; blankrows keeps the metadata/separator rows; defval pads
  // missing cells with null so column indices stay aligned (mirrors openpyxl values_only).
  return XLSX.utils.sheet_to_json<Cell[]>(sheet, { header: 1, blankrows: true, defval: null, raw: true });
}

// --- header / metadata -----------------------------------------------------------------

function findHeaderRow(rows: Cell[][]): number | null {
  for (let idx = 0; idx < rows.length; idx++) {
    for (const cell of rows[idx]) {
      if (asStr(cell) && String(cell).trim().toLowerCase() === "test_id") return idx;
    }
  }
  return null;
}

const BASE_LABELS = new Set(["basepath", "base_path", "base path", "baseurl", "base_url", "base url"]);
const LOG_URL_LABELS = new Set(["application_logs_fetch_url", "applicationlogsfetchurl", "logs_fetch_url", "application_logs_url", "log_url"]);

function extractMetaValue(metaRows: Cell[][], labels: Set<string>): string | null {
  for (const row of metaRows) {
    if (!row || row.length === 0) continue;
    const label = asStr(row[0]);
    if (label && labels.has(label.toLowerCase())) {
      for (const cell of row.slice(1)) {
        const value = asStr(cell);
        if (value) return value;
      }
    }
  }
  return null;
}

function extractBasePath(metaRows: Cell[][]): string | null {
  return extractMetaValue(metaRows, BASE_LABELS);
}

function extractLogUrl(metaRows: Cell[][]): string | null {
  return extractMetaValue(metaRows, LOG_URL_LABELS);
}

function mapHeaders(headerRow: Cell[]): Record<string, number> {
  const mapping: Record<string, number> = {};
  headerRow.forEach((name, idx) => {
    const key = asStr(name);
    if (!key) return;
    const canonical = COLUMNS[key.trim().toLowerCase()];
    if (canonical && !(canonical in mapping)) mapping[canonical] = idx;
  });
  return mapping;
}

// --- row parsing -----------------------------------------------------------------------

function parseRow(rawRow: Cell[], headerMap: Record<string, number>, rowNo: number, seenIds: Set<string>, suite: TestSuite): void {
  const cell = (field: string): Cell => {
    const idx = headerMap[field];
    if (idx === undefined || idx >= rawRow.length) return null;
    return rawRow[idx];
  };

  const testId = asStr(cell("test_id"));
  if (!testId) {
    suite.parse_errors.push({ row: rowNo, column: "test_id", message: "missing test_id" });
    return;
  }
  if (seenIds.has(testId)) {
    suite.parse_errors.push({ row: rowNo, column: "test_id", message: `duplicate test_id '${testId}'` });
    return;
  }

  const rowErrors: ParseError[] = [];

  const headers = parseJsonCell(cell("headers"), "headers", rowNo, rowErrors, {}, false);
  const body = parseJsonCell(cell("body"), "body", rowNo, rowErrors, null, true);
  const expectedResponse = parseJsonCell(cell("expected_response"), "expected_response", rowNo, rowErrors, null, true);

  const expectedStatus = asInt(cell("expected_status"), "expected_status", rowNo, rowErrors);
  const responseMatchMode = asEnum<MatchMode>(cell("response_match_mode"), MATCH_MODES, "json_subset", "response_match_mode", rowNo, rowErrors);
  const logMatchMode = asEnum<LogMatchMode>(cell("log_match_mode"), LOG_MATCH_MODES, "contains", "log_match_mode", rowNo, rowErrors);
  const authRequired = asBool(cell("auth_required"), "auth_required", rowNo, rowErrors, true);
  const validateLogs = asBool(cell("validate_logs"), "validate_logs", rowNo, rowErrors, false);

  if (rowErrors.length) {
    suite.parse_errors.push(...rowErrors);
    return;
  }

  const c = makeTestCase({
    test_id: testId,
    description: asStr(cell("description")),
    method: (asStr(cell("method")) ?? "GET").toUpperCase(),
    url: asStr(cell("url")) ?? "",
    headers: isPlainObject(headers) ? (headers as Record<string, unknown>) : {},
    body,
    auth_required: authRequired,
    expected_status: expectedStatus,
    expected_response: expectedResponse,
    response_match_mode: responseMatchMode,
    ignore_paths: splitList(cell("ignore_paths")),
    validate_logs: validateLogs,
    expected_log_strings: parseExpectedLogs(cell("expected_log_strings")),
    log_match_mode: logMatchMode,
    log_source: (asStr(cell("log_source")) ?? "anypoint").toLowerCase(),
  });
  seenIds.add(testId);
  suite.cases.push(c);
}

// --- cell helpers ----------------------------------------------------------------------

function isBlank(row: Cell[]): boolean {
  return row.every((c) => c === null || (typeof c === "string" && c.trim() === ""));
}

export function asStr(value: Cell): string | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  return text || null;
}

function asInt(value: Cell, column: string, rowNo: number, errors: ParseError[]): number | null {
  if (value === null || (typeof value === "string" && value.trim() === "")) return null;
  const n = typeof value === "string" ? Number(value) : Number(value);
  if (!Number.isFinite(n)) {
    errors.push({ row: rowNo, column, message: `not an integer: ${JSON.stringify(value)}` });
    return null;
  }
  return Math.trunc(n);
}

function asBool(value: Cell, column: string, rowNo: number, errors: ParseError[], def: boolean): boolean {
  if (typeof value === "boolean") return value;
  const text = asStr(value);
  if (!text) return def;
  const lowered = text.toLowerCase();
  if (BOOL_TRUE.has(lowered)) return true;
  if (BOOL_FALSE.has(lowered)) return false;
  errors.push({ row: rowNo, column, message: `not yes/no: ${JSON.stringify(text)}` });
  return def;
}

function asEnum<T extends string>(value: Cell, allowed: readonly T[], def: T, column: string, rowNo: number, errors: ParseError[]): T {
  const text = asStr(value);
  if (!text) return def;
  const lowered = text.toLowerCase() as T;
  if (allowed.includes(lowered)) return lowered;
  errors.push({ row: rowNo, column, message: `invalid ${column} ${JSON.stringify(text)}; allowed: ${allowed.join(", ")}` });
  return def;
}

function parseJsonCell(value: Cell, column: string, rowNo: number, errors: ParseError[], def: unknown, allowScalar: boolean): unknown {
  if (value === null || value === undefined) return def;
  if (typeof value !== "string") return allowScalar ? value : def;
  const text = value.trim();
  if (!text) return def;
  try {
    return JSON.parse(text);
  } catch {
    if (allowScalar) return text;
    errors.push({ row: rowNo, column, message: "invalid JSON" });
    return def;
  }
}

function splitList(value: Cell): string[] {
  const text = asStr(value);
  if (!text) return [];
  const parts = text.split(/\r?\n/).flatMap((chunk) => chunk.split(",").map((p) => p.trim()));
  return parts.filter((p) => p.length > 0);
}

function parseExpectedLogs(value: Cell): string[] {
  const text = asStr(value);
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed.map((item) => String(item));
  } catch {
    // fall through to delimiter split
  }
  const delimiter = text.includes("||") ? "||" : "\n";
  return text.split(delimiter).map((p) => p.trim()).filter((p) => p.length > 0);
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
