import { describe, expect, it } from "vitest";
import { deploymentLogsUrl, generateTestSuite } from "../src/suite/generate.js";
import { readTestSuite } from "../src/suite/parse.js";

// Spec whose server description carries a CloudHub deployment id (the real-world shape).
const SPEC = `
openapi: 3.0.0
info: { title: Employees, version: "1.0" }
servers:
  - url: https://employee-api-impl.example.cloudhub.io/api
    description: Production server deployed in CloudHub with id 351c3653-f9db-4a6a-864a-624f7b5eaa91
paths:
  /employees:
    get:
      summary: List employees
      responses:
        "200": { description: OK }
`;

const BASE = "https://anypoint.mulesoft.com/amc/application-manager/api/v2/organizations/ORG/environments/ENV/deployments";

describe("generate_test_suite — application_logs_fetch_url from deployment id", () => {
  it("fills the log-fetch URL as base + '/' + id when a base is configured", () => {
    const { bytes } = generateTestSuite(SPEC, BASE);
    const suite = readTestSuite(new Uint8Array(bytes));
    expect(suite.application_logs_fetch_url).toBe(`${BASE}/351c3653-f9db-4a6a-864a-624f7b5eaa91`);
  });

  it("trims a trailing slash on the base", () => {
    expect(deploymentLogsUrl({ servers: [{ description: "id 351c3653-f9db-4a6a-864a-624f7b5eaa91" }] }, BASE + "/")).toBe(
      `${BASE}/351c3653-f9db-4a6a-864a-624f7b5eaa91`,
    );
  });

  it("leaves the cell blank when no base is configured", () => {
    const { bytes } = generateTestSuite(SPEC);
    const suite = readTestSuite(new Uint8Array(bytes));
    expect(suite.application_logs_fetch_url).toBeNull();
  });

  it("leaves the cell blank when the description has no deployment id", () => {
    const noId = SPEC.replace(/description:.*/, "description: Production server (no id here)");
    const { bytes } = generateTestSuite(noId, BASE);
    const suite = readTestSuite(new Uint8Array(bytes));
    expect(suite.application_logs_fetch_url).toBeNull();
  });
});
