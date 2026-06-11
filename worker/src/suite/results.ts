/**
 * Build the results workbook — the no-filesystem port of tools/results_writer.py.
 *
 * The Python version appended a timestamped RESULTS block + per-case evidence tabs *into the
 * user's file on disk*, guarding it with a backup→verify→restore dance. With no filesystem we
 * instead take the original uploaded workbook bytes, append the same RESULTS block to the
 * `tests` sheet and add the same evidence tabs, and return new bytes for the user to download.
 * The backup/verify/restore guard is dropped (it only protected an on-disk file from save-time
 * corruption); the block-construction and evidence-layout logic is preserved exactly.
 */
import * as XLSX from "xlsx";
import type { CaseEvidence, CaseReport, SuiteReport } from "../models.js";
import { asStr, RESULTS_MARKER } from "./parse.js";

const RESULTS_HEADER = ["test_id", "status", "actual_status", "expected_status", "correlation_id", "detail"];

type Cell = string | number | boolean | null;

/** Append a RESULTS block + evidence tabs to the original workbook; return new bytes. */
export function buildResultWorkbook(
  originalBytes: Uint8Array,
  report: SuiteReport,
  evidence: CaseEvidence[],
  runAt: string,
): Uint8Array {
  const wb = XLSX.read(originalBytes, { type: "array" });
  const testsName = wb.SheetNames.find((n) => n.toLowerCase() === "tests") ?? wb.SheetNames[0];

  // Rebuild the tests sheet: existing rows (trailing blanks trimmed) + separator + block.
  const existing = trimTrailingBlankRows(
    XLSX.utils.sheet_to_json<Cell[]>(wb.Sheets[testsName], { header: 1, blankrows: true, defval: null, raw: true }),
  );
  const block = buildBlock(report, runAt);
  const aoa: Cell[][] = [...existing, [], ...block];
  wb.Sheets[testsName] = XLSX.utils.aoa_to_sheet(aoa);

  // One evidence tab per case (latest run), overwriting any prior tab of the same name.
  const used = new Set<string>();
  for (const ev of evidence) {
    const name = safeSheetName(ev.test_id, used);
    if (wb.SheetNames.includes(name)) {
      delete wb.Sheets[name];
      wb.SheetNames.splice(wb.SheetNames.indexOf(name), 1);
    }
    XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(evidenceRows(ev, runAt)), name);
  }

  return XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array;
}

// --- block construction (ports _build_block / _detail) ---------------------------------

export function buildBlock(report: SuiteReport, runAt: string): Cell[][] {
  const summary = `RESULTS — run ${runAt}  (passed ${report.passed}/${report.total})`;
  const rows: Cell[][] = [[summary], [...RESULTS_HEADER]];
  for (const c of report.cases) {
    rows.push([
      c.test_id,
      c.passed ? "PASS" : "FAIL",
      c.actual_status === null ? "" : String(c.actual_status),
      c.expected_status === null ? "" : String(c.expected_status),
      c.correlation_id ?? "",
      detail(c),
    ]);
  }
  return rows;
}

export function detail(c: CaseReport): string {
  if (c.error) return c.error;
  const parts: string[] = [];
  const ra = c.response_assert;
  if (ra && !ra.passed) {
    if (!ra.status_ok) parts.push("status mismatch");
    const missing = ra.diffs.filter((d) => d.message === "missing key").map((d) => d.path);
    if (missing.length) parts.push("missing keys: " + missing.join(", "));
    const mismatched = ra.diffs.filter((d) => d.message === "value mismatch").map((d) => d.path);
    if (mismatched.length) parts.push("value mismatch: " + mismatched.join(", "));
  }
  const lv = c.log_validation;
  if (lv) {
    if (lv.missing.length) parts.push("missing logs: " + lv.missing.join(", "));
    else if (lv.used_fallback) parts.push("logs ok (whole-log fallback)");
    else parts.push("logs ok");
  }
  if (!parts.length) {
    const mode = ra ? ra.mode : "";
    return c.passed ? `response matched (${mode})` : "did not match";
  }
  return parts.join("; ");
}

// --- evidence sheet layout (ports _fill_evidence_sheet) --------------------------------

function evidenceRows(ev: CaseEvidence, runAt: string): Cell[][] {
  const rows: Cell[][] = [
    [`${ev.test_id} — evidence`, `run ${runAt}`, `RESULT: ${ev.passed ? "PASS" : "FAIL"}`],
  ];
  if (ev.description) rows.push([ev.description]);
  if (ev.error) rows.push(["error", ev.error]);

  rows.push([], ["[Request]"]);
  rows.push(["method", ev.method ?? ""]);
  rows.push(["url", ev.url ?? ""]);
  rows.push(["headers", jsonCell(ev.request_headers)]);
  rows.push(["body", jsonCell(ev.request_body)]);

  const respStatus = ev.response_passed === null ? "" : ev.response_passed ? "PASS" : "FAIL";
  rows.push([], ["[Response validation]", respStatus]);
  rows.push(["expected_status", s(ev.expected_status)]);
  rows.push(["actual_status", s(ev.actual_status)]);
  rows.push(["match_mode", s(ev.match_mode)]);
  rows.push(["latency_ms", ev.latency_ms === null ? "" : s(Math.round(ev.latency_ms * 10) / 10)]);
  rows.push([], ["expected_result", jsonCell(ev.expected_response)]);
  rows.push(["actual_result", jsonCell(ev.actual_body)]);
  if (ev.response_diffs.length) {
    rows.push(["diffs"]);
    for (const d of ev.response_diffs) {
      rows.push(["", `${d.path}: ${d.message} (expected=${repr(d.expected)}, actual=${repr(d.actual)})`]);
    }
  } else {
    rows.push(["diffs", "(none)"]);
  }

  const logStatus = !ev.validated_logs ? "not validated" : ev.logs_passed ? "PASS" : "FAIL";
  rows.push([], ["[Log validation]", logStatus]);
  if (ev.validated_logs) {
    rows.push(["log_source", ev.log_source ?? ""]);
    rows.push(["correlation_id", ev.correlation_id ?? ""]);
    rows.push(["used_fallback", ev.used_fallback ? "yes (whole-log)" : "no"]);
    rows.push(["lines_considered", s(ev.lines_considered)]);
    rows.push([], ["expected_log_string", "matched_lines"]);
    for (const needle of ev.expected_log_strings) {
      const lines = ev.matched_log_lines[needle] ?? [];
      rows.push([needle, lines[0] ?? ""]);
      for (const extra of lines.slice(1)) rows.push(["", extra]);
    }
  }
  return rows;
}

// --- helpers ---------------------------------------------------------------------------

function trimTrailingBlankRows(rows: Cell[][]): Cell[][] {
  const out = rows.map((r) => [...r]);
  const isBlank = (r: Cell[]) => r.every((c) => c === null || (typeof c === "string" && c.trim() === ""));
  while (out.length && isBlank(out[out.length - 1])) out.pop();
  return out;
}

const INVALID_SHEET_CHARS = /[[\]:*?/\\]/g;

function safeSheetName(testId: string, used: Set<string>): string {
  let base = (testId.replace(INVALID_SHEET_CHARS, "_").trim() || "case").slice(0, 31);
  if (base.toLowerCase() === "tests") base = `${base}_evi`.slice(0, 31);
  let name = base;
  let n = 2;
  while (used.has(name.toLowerCase())) {
    const suffix = `~${n}`;
    name = base.slice(0, 31 - suffix.length) + suffix;
    n += 1;
  }
  used.add(name.toLowerCase());
  return name;
}

function jsonCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function s(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

/** Python-repr-ish rendering for diff cells (quotes strings, keeps numbers/None bare). */
function repr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "string") return `'${value}'`;
  return String(value);
}

// re-export so callers (and tests) can detect the marker the parser stops at.
export { RESULTS_MARKER };
export { asStr };
