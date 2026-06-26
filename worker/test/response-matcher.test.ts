import { describe, expect, it } from "vitest";
import { assertResponse } from "../src/matching/response-matcher.js";

// Ported 1:1 from tests/test_response_matcher.py.
describe("response matcher", () => {
  it("json_subset passes with extra keys", () => {
    const r = assertResponse({ actual_body: { a: 1, b: 2, extra: 9 }, expected: { a: 1, b: 2 }, mode: "json_subset" });
    expect(r.passed).toBe(true);
  });

  it("json_subset reports missing and mismatch", () => {
    const r = assertResponse({ actual_body: { a: 1 }, expected: { a: 2, b: 3 }, mode: "json_subset" });
    expect(r.passed).toBe(false);
    const paths = new Set(r.diffs.map((d) => d.path));
    expect(paths.has("a")).toBe(true);
    expect(paths.has("b")).toBe(true);
  });

  it("exact rejects extra keys", () => {
    const r = assertResponse({ actual_body: { a: 1, b: 2 }, expected: { a: 1 }, mode: "exact" });
    expect(r.passed).toBe(false);
    expect(r.diffs.some((d) => d.message.includes("unexpected key"))).toBe(true);
  });

  it("json_subset: a single-node template passes when every object matches, any count", () => {
    const r = assertResponse({
      actual_body: [{ a: 1 }, { a: 2 }, { a: 3 }],
      expected: [{ a: "<<any>>" }],
      mode: "json_subset",
    });
    expect(r.passed).toBe(true);
  });

  it("json_subset: a single-node template is checked against EVERY object (not just the first)", () => {
    const r = assertResponse({
      actual_body: [{ a: 1 }, { b: 2 }], // second object missing `a`
      expected: [{ a: "<<any>>" }],
      mode: "json_subset",
    });
    expect(r.passed).toBe(false);
    expect(r.diffs.some((d) => d.path === "1.a" && d.message === "missing key")).toBe(true);
  });

  it("json_subset: an empty list passes a single-node template (count doesn't matter)", () => {
    const r = assertResponse({ actual_body: [], expected: [{ a: "<<any>>" }], mode: "json_subset" });
    expect(r.passed).toBe(true);
  });

  it("json_subset: a multi-node template is positional, extra actual nodes ignored", () => {
    const r = assertResponse({
      actual_body: [{ a: 1 }, { b: 2 }, { c: 3 }],
      expected: [{ a: "<<any>>" }, { b: "<<any>>" }],
      mode: "json_subset",
    });
    expect(r.passed).toBe(true); // index 0,1 match; index 2 ignored
  });

  it("exact still requires equal list length", () => {
    const r = assertResponse({
      actual_body: [{ a: 1 }, { a: 2 }],
      expected: [{ a: 1 }],
      mode: "exact",
    });
    expect(r.passed).toBe(false);
    expect(r.diffs.some((d) => d.message === "list length mismatch")).toBe(true);
  });

  it("ignore_paths prune volatile fields", () => {
    const r = assertResponse({
      actual_body: { id: "xyz", data: { ts: 123, v: 1 } },
      expected: { id: "abc", data: { ts: 999, v: 1 } },
      mode: "json_subset",
      ignore_paths: ["id", "data.ts"],
    });
    expect(r.passed).toBe(true);
  });

  it("ignore_paths wildcard in list", () => {
    const r = assertResponse({
      actual_body: { items: [{ id: 1, v: "a" }, { id: 2, v: "b" }] },
      expected: { items: [{ id: 9, v: "a" }, { id: 8, v: "b" }] },
      mode: "json_subset",
      ignore_paths: ["items.*.id"],
    });
    expect(r.passed).toBe(true);
  });

  it("schema mode", () => {
    const schema = { type: "object", properties: { n: { type: "integer" } }, required: ["n"] };
    expect(assertResponse({ actual_body: { n: 5 }, expected: schema, mode: "schema" }).passed).toBe(true);
    expect(assertResponse({ actual_body: { n: "x" }, expected: schema, mode: "schema" }).passed).toBe(false);
  });

  it("schema mode validates a variable-length array body (list endpoint)", () => {
    // What the generator now emits for an array success body. Must hold for any element count and
    // run under Workers' no-eval CSP (regression for ajv's new Function() EvalError).
    const schema = { type: "array", items: { type: "object", required: ["orderId", "status"] } };
    const ok = assertResponse({
      actual_body: [
        { orderId: "ORD-001", status: "pending" },
        { orderId: "ORD-002", status: "shipped" },
      ],
      expected: schema,
      mode: "schema",
    });
    expect(ok.passed).toBe(true);
    const bad = assertResponse({
      actual_body: [{ orderId: "ORD-001" }], // missing required "status"
      expected: schema,
      mode: "schema",
    });
    expect(bad.passed).toBe(false);
  });

  it("<<any>> accepts any present value", () => {
    const r = assertResponse({
      actual_body: { id: "generated-123", status: "ok" },
      expected: { id: "<<any>>", status: "ok" },
      mode: "json_subset",
    });
    expect(r.passed).toBe(true);
  });

  it("<<any>> still requires presence", () => {
    const r = assertResponse({ actual_body: { status: "ok" }, expected: { id: "<<any>>", status: "ok" }, mode: "json_subset" });
    expect(r.passed).toBe(false);
    expect(r.diffs.some((d) => d.path === "id" && d.message === "missing key")).toBe(true);
  });

  it("<<any>> nested and exact mode", () => {
    const r = assertResponse({
      actual_body: { data: { token: "xyz", n: 5 } },
      expected: { data: { token: "<<any>>", n: 5 } },
      mode: "exact",
    });
    expect(r.passed).toBe(true);
  });

  it("<<any>> in list item", () => {
    const r = assertResponse({
      actual_body: { items: [{ id: 1 }, { id: 99 }] },
      expected: { items: ["<<any>>", { id: 99 }] },
      mode: "json_subset",
    });
    expect(r.passed).toBe(true);
  });

  it("status check", () => {
    const r = assertResponse({ actual_body: {}, expected: {}, actual_status: 500, expected_status: 200 });
    expect(r.passed).toBe(false);
    expect(r.status_ok).toBe(false);
  });
});
