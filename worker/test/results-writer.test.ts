import { describe, expect, it } from "vitest";
import * as XLSX from "xlsx";
import { buildResultWorkbook } from "../src/suite/results.js";
import { readTestSuite } from "../src/suite/parse.js";
import type { CaseEvidence, SuiteReport } from "../src/models.js";

// Ported from tests/test_results_writer.py (minus the on-disk backup/restore guard, which
// no longer applies without a filesystem).

function suiteBytes(): Uint8Array {
  const aoa: unknown[][] = [
    ["Basepath", "https://api.test/"],
    [],
    ["test_id", "method", "url", "expected_status"],
    ["TC-001", "POST", "/orders", 201],
    ["TC-002", "POST", "/orders", 400],
  ];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(aoa), "tests");
  return XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array;
}

function report(): SuiteReport {
  return {
    total: 2,
    passed: 1,
    failed: 1,
    parse_errors: [],
    cases: [
      { test_id: "TC-001", passed: true, actual_status: 201, expected_status: 201, correlation_id: "TC-001-evid01", response_assert: null, log_validation: null, error: null },
      { test_id: "TC-002", passed: false, actual_status: 201, expected_status: 400, correlation_id: "TC-002-evid02", response_assert: null, log_validation: null, error: null },
    ],
  };
}

function flatten(bytes: Uint8Array, sheet = "tests"): string[] {
  const wb = XLSX.read(bytes, { type: "array" });
  const rows = XLSX.utils.sheet_to_json<unknown[]>(wb.Sheets[sheet], { header: 1, blankrows: true, defval: null });
  return rows.map((r) => r.map((c) => (c === null ? "" : String(c))).join("|"));
}

const NO_EVIDENCE: CaseEvidence[] = [];

describe("buildResultWorkbook", () => {
  it("appends a block and preserves the original cases", () => {
    const out = buildResultWorkbook(suiteBytes(), report(), NO_EVIDENCE, "2026-06-03 21:00:00");
    const flat = flatten(out);

    expect(flat.some((l) => l.includes("RESULTS — run 2026-06-03 21:00:00"))).toBe(true);
    expect(flat.some((l) => l.startsWith("TC-001|✅ PASS|201|201|TC-001-evid01"))).toBe(true);
    expect(flat.some((l) => l.startsWith("TC-002|❌ FAIL|201|400|TC-002-evid02"))).toBe(true);
    expect(flat.some((l) => l.includes("correlation_id"))).toBe(true);

    const suite = readTestSuite(out);
    expect(new Set(suite.cases.map((c) => c.test_id))).toEqual(new Set(["TC-001", "TC-002"]));
  });

  it("stacks two blocks and leaves the case section unchanged", () => {
    const once = buildResultWorkbook(suiteBytes(), report(), NO_EVIDENCE, "2026-06-03 21:00:00");
    const twice = buildResultWorkbook(once, report(), NO_EVIDENCE, "2026-06-03 21:30:00");

    const flat = flatten(twice);
    expect(flat.filter((l) => l.includes("RESULTS — run")).length).toBe(2);

    const suite = readTestSuite(twice);
    expect(new Set(suite.cases.map((c) => c.test_id))).toEqual(new Set(["TC-001", "TC-002"]));
  });
});
