import { describe, expect, it } from "vitest";
import { generateTestSuite } from "../src/suite/generate.js";
import { readTestSuite } from "../src/suite/parse.js";

// A minimal spec whose GET returns an *array* success body (a list endpoint). The positive list
// case must get a readable single-node `<<any>>` json_subset template — not an empty
// expected_response, and not a JSON Schema.
const SPEC = `
openapi: 3.0.0
info: { title: Orders, version: "1.0" }
servers:
  - url: https://api.example.com/api
paths:
  /orders:
    get:
      summary: Retrieve a list of orders
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                type: array
                items: { $ref: "#/components/schemas/Order" }
components:
  schemas:
    Order:
      type: object
      required: [orderId, customerId, status]
      properties:
        orderId: { type: string }
        customerId: { type: string }
        status: { type: string }
`;

// Same shape but the item schema declares NO `required` — the template must NOT force every
// property; it should be `["<<any>>"]` (any object accepted).
const SPEC_NO_REQUIRED = SPEC.replace("required: [orderId, customerId, status]\n      ", "");

describe("generate_test_suite — array success body", () => {
  it("emits a single-node <<any>> template (json_subset) for a list (array) GET", () => {
    const { bytes } = generateTestSuite(SPEC);
    const suite = readTestSuite(new Uint8Array(bytes));
    const positive = suite.cases.find((c) => c.method === "GET" && c.expected_status === 200);
    expect(positive).toBeDefined();
    expect(positive!.response_match_mode).toBe("json_subset");
    expect(positive!.expected_response).toEqual([
      { orderId: "<<any>>", customerId: "<<any>>", status: "<<any>>" },
    ]);
  });

  it("emits [\"<<any>>\"] when the array item schema declares no required fields", () => {
    const { bytes } = generateTestSuite(SPEC_NO_REQUIRED);
    const suite = readTestSuite(new Uint8Array(bytes));
    const positive = suite.cases.find((c) => c.method === "GET" && c.expected_status === 200);
    expect(positive!.response_match_mode).toBe("json_subset");
    expect(positive!.expected_response).toEqual(["<<any>>"]);
  });
});
