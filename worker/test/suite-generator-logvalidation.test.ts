import { describe, expect, it } from "vitest";
import { generateTestSuite } from "../src/suite/generate.js";
import { readTestSuite } from "../src/suite/parse.js";

// A POST with a JSON body so the generator emits body-validation (400) and wrong-content-type
// (415) cases alongside the positive (201) case — enough to cover every log-string branch.
const SPEC = `
openapi: 3.0.0
info: { title: Orders, version: "1.0" }
servers:
  - url: https://api.example.com/api
paths:
  /orders:
    post:
      summary: Create order
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [customerId]
              properties:
                customerId: { type: string }
      responses:
        "201": { description: Created }
`;

function casesByStatus() {
  const { bytes } = generateTestSuite(SPEC);
  const cases = readTestSuite(new Uint8Array(bytes)).cases;
  return cases;
}

describe("generate_test_suite — default log validation with APIkit error types", () => {
  it("sets validate_logs=true on every generated case", () => {
    expect(casesByStatus().every((c) => c.validate_logs === true)).toBe(true);
  });

  it("asserts APIKIT:BAD_REQUEST for 400 cases and APIKIT:UNSUPPORTED_MEDIA_TYPE for 415", () => {
    const cases = casesByStatus();
    const c400 = cases.find((c) => c.expected_status === 400);
    const c415 = cases.find((c) => c.expected_status === 415);
    expect(c400?.expected_log_strings).toEqual(["APIKIT:BAD_REQUEST"]);
    expect(c415?.expected_log_strings).toEqual(["APIKIT:UNSUPPORTED_MEDIA_TYPE"]);
  });

  it("asserts the request line <METHOD> <path> for a 2xx success case (no APIkit error type)", () => {
    const c201 = casesByStatus().find((c) => c.expected_status === 201);
    expect(c201?.validate_logs).toBe(true);
    expect(c201?.expected_log_strings).toEqual(["POST /orders"]);
  });
});
