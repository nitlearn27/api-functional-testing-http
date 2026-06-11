import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";
import { generateTestSuite } from "../src/suite/generate.js";
import { readTestSuite } from "../src/suite/parse.js";

// Ported from tests/test_suite_generator.py. Reads the real spec from the repo's resources/.
const here = dirname(fileURLToPath(import.meta.url));
const SPEC_PATH = resolve(here, "../../resources/products-eapi1.yaml");
const SPEC_YAML = readFileSync(SPEC_PATH, "utf8");
const SPEC = parseYaml(SPEC_YAML) as any;
const SPEC_BASE_PATH = SPEC.servers[0].url;

function generated() {
  const { summary, bytes } = generateTestSuite(SPEC_YAML);
  return { summary, bytes };
}

describe("generate_test_suite", () => {
  it("uses the spec base path and produces comprehensive coverage", () => {
    const { summary } = generated();
    expect(summary.base_path).toBe(SPEC_BASE_PATH);
    expect(summary.case_count).toBeGreaterThanOrEqual(30);
  });

  it("produces the expected generic coverage for the products spec (count + categories)", () => {
    // The generator was generalized to walk any spec; for products this yields one auth case
    // per secured operation (3) rather than the Python reference's single one — hence 41 vs 39.
    const { summary } = generated();
    expect(summary.case_count).toBe(41);
    expect(summary.cases_by_category).toEqual({
      positive: 3,
      query_validation: 6,
      auth: 3,
      body_validation: 24,
      bad_request: 1,
      media_type: 1,
      path_validation: 2,
      not_found: 1,
    });
  });

  it("round-trips through the parser without errors", () => {
    const { summary, bytes } = generated();
    const suite = readTestSuite(bytes);
    expect(suite.base_path).toBe(summary.base_path);
    expect(suite.parse_errors).toEqual([]);
    expect(suite.cases.length).toBe(summary.case_count);
  });

  it("covers every validation category", () => {
    const suite = readTestSuite(generated().bytes);
    const statuses = new Set(suite.cases.map((c) => c.expected_status));
    for (const s of [200, 201, 400, 401, 404, 415]) expect(statuses.has(s)).toBe(true);
    expect(statuses.has(422)).toBe(false);

    const descriptions = suite.cases.map((c) => c.description ?? "").join(" || ");
    expect(descriptions).toContain("missing required");
    expect(descriptions).toContain("violates pattern");
    expect(descriptions).toContain("not in allowed enum");
    expect(descriptions).toContain("exceeds maxItems");
  });

  it("error cases use the full spec error envelope", () => {
    const envelope = new Set(Object.keys(SPEC.components.schemas.ErrorResponse.properties));
    const suite = readTestSuite(generated().bytes);
    const errors = suite.cases.filter((c) => [400, 401, 404, 415].includes(c.expected_status ?? -1));
    expect(errors.length).toBeGreaterThan(0);
    for (const c of errors) {
      const resp = c.expected_response as Record<string, unknown>;
      expect(new Set(Object.keys(resp))).toEqual(envelope);
      expect(resp.status).toBe(c.expected_status);
      for (const dyn of ["timestamp", "message", "path", "errors"]) {
        if (dyn in resp) expect(resp[dyn]).toBe("<<any>>");
      }
    }
  });

  it("a positive case asserts all required Product fields", () => {
    const required: string[] = SPEC.components.schemas.Product.required;
    const suite = readTestSuite(generated().bytes);
    // The get-by-name 200 derives its expectation from the Product schema's required fields.
    const c = suite.cases.find((x) => {
      const resp = x.expected_response;
      return x.expected_status === 200 && resp && typeof resp === "object" && required.every((f) => f in (resp as object));
    });
    expect(c).toBeDefined();
  });

  it("all body-validation negatives map to 400", () => {
    const suite = readTestSuite(generated().bytes);
    const keys = ["missing required", "violates pattern", "not in allowed enum", "maxLength", "minLength", "minimum", "maximum", "maxItems", "duplicate items", "additionalProperties"];
    const bodyNegs = suite.cases.filter(
      (c) => c.method === "POST" && keys.some((k) => (c.description ?? "").includes(k)),
    );
    expect(bodyNegs.length).toBeGreaterThan(0);
    expect(bodyNegs.every((c) => c.expected_status === 400)).toBe(true);
  });

  it("malformed body case kept as a raw string", () => {
    const suite = readTestSuite(generated().bytes);
    const malformed = suite.cases.filter((c) => (c.description ?? "").includes("malformed JSON"));
    expect(malformed.length).toBeGreaterThan(0);
    expect(typeof malformed[0].body).toBe("string");
  });
});

// A second spec (a single POST /items operation) locks the generic, non-products coverage.
describe("generate_test_suite — generic spec (/items)", () => {
  const ITEMS_YAML = readFileSync(resolve(here, "../../resources/openapi.yaml"), "utf8");

  it("generates body-validation coverage for a single POST operation", () => {
    const { summary, bytes } = generateTestSuite(ITEMS_YAML);
    const suite = readTestSuite(bytes);

    expect(summary.base_path).toBe("https://api.example.com/v2");
    expect(suite.parse_errors).toEqual([]);
    expect(suite.cases.length).toBe(summary.case_count);

    const statuses = new Set(suite.cases.map((c) => c.expected_status));
    for (const s of [201, 400, 415]) expect(statuses.has(s)).toBe(true);

    const descriptions = suite.cases.map((c) => c.description ?? "");
    expect(descriptions.some((d) => d.includes("missing required 'name'"))).toBe(true);
    expect(descriptions.some((d) => d.includes("maxLength"))).toBe(true);

    // Positive 201 echoes the request fields; error cases use the ErrorResponse envelope.
    const positive = suite.cases.find((c) => c.expected_status === 201)!;
    expect(positive.expected_response).toMatchObject({ name: "Sample Item" });
    const err = suite.cases.find((c) => c.expected_status === 400)!;
    expect(new Set(Object.keys(err.expected_response as object))).toEqual(new Set(["code", "message", "details"]));

    // The malformed-JSON body survives as a raw scalar string.
    const malformed = suite.cases.find((c) => (c.description ?? "").includes("malformed JSON"))!;
    expect(typeof malformed.body).toBe("string");
  });
});
