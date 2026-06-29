/**
 * TestMcpServer — the MCP front door (Streamable HTTP).
 *
 * THREE user-facing tools (consolidated by design — everything else stays internal):
 *   run_schema — OpenAPI YAML schema in → generate the suite AND run it; returns the report.
 *   run_suite  — run an existing suite (suite_id or file_b64) AND fetch its report.
 *   create_test_case_all — model-analyzed test cases in → CREATE the suite (.xlsx) and return it
 *                (suite_id + suite_download_url). Does NOT run the tests (run separately with
 *                run_suite). The model (client-side) analyzes the MuleSoft app's flows + schema;
 *                only the distilled cases reach the server, which just renders them into the sheet.
 *
 * The long-running run is delegated to a JobRunner Durable Object addressed by job_id, so the job
 * outlives this per-session MCP DO and survives the 60s+ propagation/retry waits. Quick runs
 * complete within the initial short in-call wait; long ones return a job_id to re-call with.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { getAgentByName } from "agents";
import { z } from "zod";
import { readTestSuite } from "./suite/parse.js";
import { generateTestSuite } from "./suite/generate.js";
import { buildSuiteFromCases, type CaseInput } from "./suite/build.js";
import { LOG_MATCH_MODES, MATCH_MODES } from "./models.js";
import { loadFileBytes, storeFile } from "./file-store.js";
import type { StatusView } from "./job-runner.js";
import type { Env } from "./env.js";

// Shape of one model-supplied test case for create_test_case_all — the TestCase fields, all optional except
// the request essentials (method, url, expected_status). makeTestCase fills the rest server-side.
const caseSchema = z.object({
  test_id: z.string().optional().describe("Unique id (also stamped into the correlation id); auto-numbered TC-001… if omitted."),
  description: z.string().optional(),
  method: z.string().describe("HTTP method, e.g. GET/POST."),
  url: z.string().describe("Path joined onto base_path, e.g. /patients (include query string for negatives)."),
  headers: z.record(z.unknown()).optional(),
  body: z.unknown().optional().describe("Request body; a raw string is sent verbatim (use for malformed-JSON cases)."),
  auth_required: z.boolean().optional(),
  expected_status: z.number().int().describe("Expected HTTP status."),
  expected_response: z.unknown().optional().describe('Expected body; use "<<any>>" where a field must exist but the value is dynamic.'),
  response_match_mode: z.enum(MATCH_MODES as [string, ...string[]]).optional(),
  ignore_paths: z.array(z.string()).optional(),
  validate_logs: z.boolean().optional().describe("Fetch CloudHub logs and assert expected_log_strings for this case."),
  expected_log_strings: z.array(z.string()).optional().describe('Log strings proving the flow path ran, e.g. ["Start GET","End GET"] or ["APIKIT:NOT_FOUND"].'),
  log_match_mode: z.enum(LOG_MATCH_MODES as [string, ...string[]]).optional().describe("all_of to require every string (branch logic); contains for a single substring."),
  log_source: z.string().optional(),
});

type State = Record<string, never>;

// How long run_test_suite waits in-call before handing back a job_id. Suites without log
// validation finish in seconds, so most runs return their report in a single tool call.
const INLINE_WAIT_MS = 15_000;
const INLINE_POLL_MS = 2_000;

function json(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }], structuredContent: value as Record<string, unknown> };
}

export class TestMcpServer extends McpAgent<Env, State, Record<string, never>> {
  server = new McpServer({ name: "api-log-test-mcp", version: "0.2.0" });

  async init() {
    this.server.registerTool(
      "run_schema",
      {
        description:
          "Generate an API test suite from an OpenAPI 3.0 YAML schema AND run it — real HTTP requests + response assertions + CloudHub log validation — returning the report. Provide spec_yaml to generate-and-start: you get the generated suite (suite_id + suite_download_url) and the run's status. If it finishes quickly the full report and result_download_url come back immediately; otherwise you get a job_id and status_url — CALL THIS SAME TOOL AGAIN with { job_id } after next_check_seconds to fetch the report (log-validation runs take minutes; the run continues server-side).",
        inputSchema: {
          spec_yaml: z.string().optional().describe("The OpenAPI 3.0 schema as YAML text — generates the suite and starts the run."),
          job_id: z.string().optional().describe("A job_id from a previous run_schema call — fetches that run's status/report."),
        },
      },
      async ({ spec_yaml, job_id }) => {
        if (job_id) {
          if (spec_yaml) return json({ error: "job_id checks an existing run — do not combine it with spec_yaml" });
          return json(await this.jobStatus(job_id));
        }
        if (!spec_yaml) return json({ error: "provide spec_yaml to generate-and-run, or job_id to check a run" });

        const { summary, bytes } = generateTestSuite(spec_yaml, this.env.deployments_base_url);
        const parsed = readTestSuite(bytes);
        const stored = await storeFile(this.env, bytes, "test_suite.xlsx");
        const status = await this.startAndWait(Buffer.from(bytes).toString("base64"), "run_schema");
        return json({
          ...summary,
          // Auto-filled from deployments_base_url + the spec's deployment id, or null if neither
          // was available (then it must be hand-filled before logs can be validated).
          application_logs_fetch_url: parsed.application_logs_fetch_url ?? null,
          suite_id: stored.id,
          suite_download_url: stored.url,
          ...status,
        });
      },
    );

    this.server.registerTool(
      "run_suite",
      {
        description:
          "Run an existing API test suite (real HTTP requests + response assertions + CloudHub log validation) and get the report. To START a run, provide EITHER suite_id (a suite stored server-side — from run_schema or uploaded via POST /files with raw .xlsx bytes) OR file_b64 (a small .xlsx, base64) — never base64 large workbooks; upload them with `curl --data-binary @suite.xlsx <base>/files` and pass the returned suite_id. If the run finishes quickly the full report and result_download_url are returned immediately; otherwise you get a job_id and status_url — CALL THIS SAME TOOL AGAIN with { job_id } after next_check_seconds (the run continues server-side, and the user can also watch status_url in a browser).",
        inputSchema: {
          suite_id: z.string().optional().describe("Server-stored suite id (from run_schema or POST /files) — starts a run."),
          file_b64: z.string().optional().describe("A small .xlsx suite, base64-encoded (alternative to suite_id) — starts a run."),
          job_id: z.string().optional().describe("A job_id returned by a previous run_suite call — fetches that run's status/report."),
        },
      },
      async ({ suite_id, file_b64, job_id }) => {
        if (job_id) {
          if (suite_id || file_b64) return json({ error: "job_id checks an existing run — do not combine it with suite_id/file_b64" });
          return json(await this.jobStatus(job_id));
        }
        if (suite_id && file_b64) return json({ error: "provide only one of suite_id or file_b64, not both" });
        let suiteB64: string;
        if (file_b64) {
          suiteB64 = file_b64;
        } else if (suite_id) {
          const bytes = await loadFileBytes(this.env, suite_id);
          if (!bytes) {
            return json({ error: `suite_id '${suite_id}' not found (it may have expired after 2h — regenerate with run_schema or re-upload via POST /files)` });
          }
          suiteB64 = Buffer.from(bytes).toString("base64");
        } else {
          return json({ error: "provide suite_id or file_b64 to start a run, or job_id to check one" });
        }
        return json(await this.startAndWait(suiteB64, "run_suite"));
      },
    );

    this.server.registerTool(
      "create_test_case_all",
      {
        description:
          "Create an API functional test-case suite (.xlsx) from a MuleSoft application — using BOTH the application's flows/logic AND its OpenAPI schema. This tool does NOT run the tests; it only builds the suite and returns it (suite_id + suite_download_url) so you can run it separately (e.g. with run_suite). YOU analyze the app client-side and pass the distilled `cases` here (the server only renders them into the canonical sheet — it does not see the app): from the flows derive happy-path + branch-logic + APIkit-error cases (assert internal logic via expected_log_strings, e.g. a gender=male POST asserts log \"first flow for male\"); from the schema derive query-param, header and body validation cases (send required query params/headers in the happy path; add negatives for missing/invalid ones). Provide `cases` (+ optional base_path, application_logs_fetch_url or deployment_id).",
        inputSchema: {
          cases: z.array(caseSchema).describe("The model-analyzed test cases to render into the suite."),
          base_path: z.string().optional().describe("The live API base URL each case's url is joined onto."),
          application_logs_fetch_url: z.string().optional().describe("CloudHub log-fetch URL (for later log validation); takes precedence over deployment_id."),
          deployment_id: z.string().optional().describe("CloudHub deployment id; the worker joins it onto its deployments_base_url to build the log-fetch URL."),
        },
      },
      async ({ cases, base_path, application_logs_fetch_url, deployment_id }) => {
        if (!cases || cases.length === 0) {
          return json({ error: "provide a non-empty cases array to create the suite" });
        }

        const { summary, bytes, application_logs_fetch_url: logsUrl } = buildSuiteFromCases(
          // zod validated the enums to their allowed values; cast the inferred `string` back.
          { cases: cases as CaseInput[], base_path, application_logs_fetch_url, deployment_id },
          this.env.deployments_base_url,
        );
        const parsed = readTestSuite(bytes);
        if (parsed.parse_errors.length) {
          // The rendered sheet must round-trip cleanly, else a later run would silently drop cases.
          return json({ error: "rendered suite failed to parse", parse_errors: parsed.parse_errors });
        }
        const stored = await storeFile(this.env, bytes, "test_suite.xlsx");
        return json({
          ...summary,
          application_logs_fetch_url: logsUrl ?? null,
          suite_id: stored.id,
          suite_download_url: stored.url,
        });
      },
    );
  }

  /** Start a run for the given suite, wait briefly for quick runs, and return the status payload. */
  private async startAndWait(suiteB64: string, toolName: string): Promise<StatusView & { job_id: string; status_url: string; hint?: string }> {
    const newJobId = crypto.randomUUID();
    const runner = await getAgentByName(this.env.JobRunner, newJobId);
    await runner.start(suiteB64);

    // Give quick runs a chance to finish inside this call — one tool call, full report.
    const deadline = Date.now() + INLINE_WAIT_MS;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, INLINE_POLL_MS));
      const status = await this.jobStatus(newJobId);
      if (status.status === "complete" || status.status === "error") return status;
    }
    const status = await this.jobStatus(newJobId);
    return {
      ...status,
      hint:
        `The run continues in the background. Call ${toolName} again with { "job_id": "${newJobId}" } ` +
        `after ${status.next_check_seconds ?? 30}s to get the report — do not busy-poll. The user can also ` +
        `watch the status_url in a browser.`,
    };
  }

  private async jobStatus(jobId: string): Promise<StatusView & { job_id: string; status_url: string }> {
    const runner = await getAgentByName(this.env.JobRunner, jobId);
    const status = (await runner.getStatus()) as StatusView;
    return { ...status, job_id: jobId, status_url: this.statusUrl(jobId) };
  }

  private statusUrl(jobId: string): string {
    const base = (this.env.PUBLIC_BASE_URL ?? "").replace(/\/+$/, "");
    return `${base}/jobs/${jobId}`;
  }
}
