/**
 * TestMcpServer — the MCP front door (Streamable HTTP).
 *
 * Exactly TWO user-facing tools (consolidated by design — everything else stays internal):
 *   run_schema — OpenAPI YAML schema in → generate the suite AND run it; returns the report.
 *   run_suite  — run an existing suite (suite_id or file_b64) AND fetch its report.
 *
 * Both "run" tests; they differ only in the input (a schema to generate-then-run, vs a prebuilt
 * suite). The long-running run is delegated to a JobRunner Durable Object addressed by job_id, so
 * the job outlives this per-session MCP DO and survives the 60s+ propagation/retry waits. Quick
 * runs complete within the initial short in-call wait; long ones return a job_id to re-call with.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { getAgentByName } from "agents";
import { z } from "zod";
import { readTestSuite } from "./suite/parse.js";
import { generateTestSuite } from "./suite/generate.js";
import { loadFileBytes, storeFile } from "./file-store.js";
import type { StatusView } from "./job-runner.js";
import type { Env } from "./env.js";

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
