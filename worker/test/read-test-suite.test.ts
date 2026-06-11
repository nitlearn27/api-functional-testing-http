import { describe, expect, it } from "vitest";
import * as XLSX from "xlsx";
import { readTestSuite } from "../src/suite/parse.js";

// Mirrors tests/conftest.py: build the sample suite programmatically (visible schema), then
// parse the in-memory bytes — the no-filesystem equivalent of the pytest fixture.
const HEADERS = [
  "test_id", "description", "method", "url", "headers", "body", "auth_required",
  "expected_status", "expected_response", "response_match_mode", "validate_logs",
  "expected_log_strings", "log_match_mode", "log_source", "ignore_paths",
];

const ROWS: Record<string, unknown>[] = [
  {
    test_id: "order-001",
    method: "get",
    url: "/orders",
    headers: JSON.stringify({ Accept: "application/json" }),
    auth_required: "no",
    expected_status: 200,
    expected_response: JSON.stringify({ status: "ok" }),
    response_match_mode: "json_subset",
    validate_logs: "yes",
    ignore_paths: "data.id, data.timestamp",
    expected_log_strings: JSON.stringify(["Order lookup succeeded", "returning 200"]),
    log_match_mode: "contains",
    log_source: "file",
  },
  {
    test_id: "pay-042",
    method: "POST",
    url: "/payments",
    body: JSON.stringify({ amount: 10 }),
    auth_required: "no",
    expected_status: 402,
    response_match_mode: "exact",
    validate_logs: "no",
    expected_log_strings: "Payment declined||gateway slow",
    log_match_mode: "contains",
  },
  { test_id: "order-001", method: "GET", url: "/orders" }, // duplicate id -> error
  { test_id: "bad-json", method: "GET", url: "/x", headers: "{not valid json" }, // bad JSON -> error
  { test_id: "", method: "GET", url: "/y" }, // missing test_id -> error
];

function sampleSuiteBytes(): Uint8Array {
  const aoa: unknown[][] = [
    ["Basepath", "https://api.example.test/"],
    ["Auth"],
    [],
    HEADERS,
    ...ROWS.map((row) => HEADERS.map((h) => row[h] ?? "")),
  ];
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "tests");
  return XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array;
}

describe("read_test_suite", () => {
  it("parses valid cases and base path", () => {
    const suite = readTestSuite(sampleSuiteBytes());
    expect(suite.base_path).toBe("https://api.example.test/");
    expect(new Set(suite.cases.map((c) => c.test_id))).toEqual(new Set(["order-001", "pay-042"]));
  });

  it("collects parse errors", () => {
    const suite = readTestSuite(sampleSuiteBytes());
    const messages = suite.parse_errors.map((e) => [e.column, e.message] as const);
    expect(messages.some(([, m]) => m.includes("duplicate"))).toBe(true);
    expect(messages.some(([col]) => col === "headers")).toBe(true);
    expect(messages.some(([, m]) => m.includes("missing test_id"))).toBe(true);
  });

  it("normalizes fields", () => {
    const suite = readTestSuite(sampleSuiteBytes());
    const order = suite.cases.find((c) => c.test_id === "order-001")!;
    expect(order.method).toBe("GET");
    expect(order.headers).toEqual({ Accept: "application/json" });
    expect(order.auth_required).toBe(false);
    expect(order.expected_status).toBe(200);
    expect(order.response_match_mode).toBe("json_subset");
    expect(order.ignore_paths).toEqual(["data.id", "data.timestamp"]);
    expect(order.validate_logs).toBe(true);
    expect(order.expected_log_strings).toEqual(["Order lookup succeeded", "returning 200"]);
    expect(order.log_source).toBe("file");
  });

  it("expected logs delimiter fallback", () => {
    const suite = readTestSuite(sampleSuiteBytes());
    const pay = suite.cases.find((c) => c.test_id === "pay-042")!;
    expect(pay.expected_log_strings).toEqual(["Payment declined", "gateway slow"]);
    expect(pay.response_match_mode).toBe("exact");
    expect(pay.validate_logs).toBe(false);
    expect(pay.log_match_mode).toBe("contains");
    expect(pay.body).toEqual({ amount: 10 });
  });

  it("unreadable bytes return an error", () => {
    const suite = readTestSuite(new Uint8Array([1, 2, 3, 4]));
    expect(suite.cases).toEqual([]);
    expect(suite.parse_errors.length).toBeGreaterThan(0);
  });
});
