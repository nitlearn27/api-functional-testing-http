import { describe, expect, it } from "vitest";
import { buildSuiteFromCases, type CaseInput } from "../src/suite/build.js";
import { readTestSuite } from "../src/suite/parse.js";

// build_suite renders model-analyzed cases into the canonical sheet (the inverse of generate's
// schema-driven build). These lock the round-trip, the log-url resolution, auto-numbering and
// the makeTestCase defaults — exercising the same parser the runner uses.

const MULE_CASES: CaseInput[] = [
  {
    test_id: "TC-001",
    description: "GET /patients → 200, entry+exit loggers",
    method: "GET",
    url: "/patients",
    auth_required: false,
    expected_status: 200,
    expected_response: { patientId: "<<any>>" },
    validate_logs: true,
    expected_log_strings: ["Start GET", "End GET"],
    log_match_mode: "all_of",
  },
  {
    test_id: "TC-002",
    description: "POST /patients gender=male → 201, male branch logger",
    method: "POST",
    url: "/patients",
    headers: { "Content-Type": "application/json" },
    body: { gender: "male" },
    auth_required: false,
    expected_status: 201,
    validate_logs: true,
    expected_log_strings: ["Start POST", "first flow for male", "End POST"],
    log_match_mode: "all_of",
  },
  {
    test_id: "TC-003",
    description: "Unknown path → 404, APIkit error",
    method: "GET",
    url: "/unknown",
    auth_required: false,
    expected_status: 404,
    validate_logs: true,
    expected_log_strings: ["APIKIT:NOT_FOUND"],
  },
];

describe("buildSuiteFromCases", () => {
  it("renders the cases and round-trips through the parser with no errors", () => {
    const { summary, bytes } = buildSuiteFromCases({ cases: MULE_CASES, base_path: "https://app.example/api" });
    expect(summary.base_path).toBe("https://app.example/api");
    expect(summary.case_count).toBe(3);

    const suite = readTestSuite(bytes);
    expect(suite.parse_errors).toEqual([]);
    expect(suite.base_path).toBe("https://app.example/api");
    expect(suite.cases.length).toBe(3);
    expect(suite.cases.map((c) => c.expected_status)).toEqual([200, 201, 404]);
  });

  it("preserves branch-logic log assertions (the core value vs schema generation)", () => {
    const suite = readTestSuite(buildSuiteFromCases({ cases: MULE_CASES }).bytes);
    const male = suite.cases.find((c) => c.test_id === "TC-002")!;
    expect(male.expected_log_strings).toEqual(["Start POST", "first flow for male", "End POST"]);
    expect(male.log_match_mode).toBe("all_of");
    expect(male.body).toEqual({ gender: "male" });

    const notFound = suite.cases.find((c) => c.test_id === "TC-003")!;
    expect(notFound.expected_log_strings).toEqual(["APIKIT:NOT_FOUND"]);
    // omitted log_match_mode falls back to the default
    expect(notFound.log_match_mode).toBe("contains");
  });

  it("resolves the log-fetch URL from deployment_id + base, and blank when neither is given", () => {
    const base = "https://anypoint.mulesoft.com/.../environments/ENV/deployments";
    const id = "351c3653-1234-4abc-9def-0123456789ab";
    const withId = buildSuiteFromCases({ cases: MULE_CASES, deployment_id: id }, base);
    expect(withId.application_logs_fetch_url).toBe(`${base}/${id}`);
    expect(readTestSuite(withId.bytes).application_logs_fetch_url).toBe(`${base}/${id}`);

    // An explicit URL wins over deployment_id.
    const explicit = buildSuiteFromCases(
      { cases: MULE_CASES, deployment_id: id, application_logs_fetch_url: "https://logs.example/x" },
      base,
    );
    expect(explicit.application_logs_fetch_url).toBe("https://logs.example/x");

    // No id and no base → blank cell (parser returns null).
    const none = buildSuiteFromCases({ cases: MULE_CASES });
    expect(none.application_logs_fetch_url).toBeNull();
    expect(readTestSuite(none.bytes).application_logs_fetch_url).toBeNull();
  });

  it("auto-numbers TC-### when test_id is omitted", () => {
    const cases: CaseInput[] = [
      { method: "GET", url: "/a", expected_status: 200 },
      { method: "GET", url: "/b", expected_status: 200 },
    ];
    const suite = readTestSuite(buildSuiteFromCases({ cases }).bytes);
    expect(suite.cases.map((c) => c.test_id)).toEqual(["TC-001", "TC-002"]);
  });

  it("applies makeTestCase defaults for omitted optional fields", () => {
    const suite = readTestSuite(buildSuiteFromCases({ cases: [{ method: "GET", url: "/a", expected_status: 200 }] }).bytes);
    const c = suite.cases[0];
    expect(c.auth_required).toBe(true);
    expect(c.validate_logs).toBe(false);
    expect(c.response_match_mode).toBe("json_subset");
    expect(c.log_match_mode).toBe("contains");
    expect(c.log_source).toBe("anypoint");
    expect(c.expected_log_strings).toEqual([]);
  });
});
