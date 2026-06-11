/**
 * TestMcpServer — the MCP front door (Streamable HTTP).
 *
 * Exactly TWO user-facing tools (consolidated by design — everything else stays internal):
 *   generate_test_suite — OpenAPI YAML in → .xlsx suite stored server-side + download link.
 *   run_test_suite      — start a run (suite_id or file_b64) AND fetch its report (job_id).
 *
 * The long-running suite run is delegated to a JobRunner Durable Object addressed by job_id,
 * so the job outlives this per-session MCP DO and survives the 60s+ propagation/retry waits.
 * Quick runs complete within the initial short in-call wait; long ones return a job_id to
 * re-call run_test_suite with.
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
      "generate_test_suite",
      {
        description:
          "Generate a runnable .xlsx API test suite from an OpenAPI 3.0 YAML spec (positive, validation, auth and not-found cases for every operation). Returns the summary, the full parsed cases (every column), a suite_id (the suite is stored server-side for 2h) and a download_url for the .xlsx. To execute the suite, call run_test_suite with the suite_id.",
        inputSchema: { spec_yaml: z.string().describe("The OpenAPI 3.0 spec as YAML text.") },
      },
      async ({ spec_yaml }) => {
        const { summary, bytes } = generateTestSuite(spec_yaml);
        // Return the full parsed cases (every column) for display, a download link, and a
        // suite_id so the suite can be run later WITHOUT re-uploading it. The bytes live in a
        // FileStore DO (served at /files/{id}); we never hand the model a large base64 blob.
        const cases = readTestSuite(bytes).cases;
        const stored = await storeFile(this.env, bytes, "test_suite.xlsx");
        return json({
          ...summary,
          cases,
          suite_id: stored.id,
          download_url: stored.url,
          run_hint:
            `To execute this suite, call run_test_suite with { "suite_id": "${stored.id}" }. ` +
            `The stored suite expires in 2h.`,
        });
      },
    );

    this.server.registerTool(
      "run_test_suite",
      {
        description:
          "Run an API test suite (real HTTP requests + response assertions + CloudHub log validation) and get the report. To START a run, provide EITHER suite_id (a suite stored server-side — from generate_test_suite or uploaded via POST /files with raw .xlsx bytes) OR file_b64 (a small .xlsx, base64) — never base64 large workbooks; upload them with `curl --data-binary @suite.xlsx <base>/files` and pass the returned suite_id. If the run finishes quickly the full report and result_download_url are returned immediately; otherwise you get a job_id and status_url — CALL THIS SAME TOOL AGAIN with { job_id } after next_check_seconds to fetch the report (log-validation runs take minutes; the run continues server-side, and the user can also watch status_url in a browser).",
        inputSchema: {
          suite_id: z.string().optional().describe("Server-stored suite id (from generate_test_suite or POST /files) — starts a run."),
          file_b64: z.string().optional().describe("A small .xlsx suite, base64-encoded (alternative to suite_id) — starts a run."),
          job_id: z.string().optional().describe("A job_id returned by a previous run_test_suite call — fetches that run's status/report."),
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
            return json({ error: `suite_id '${suite_id}' not found (it may have expired after 2h — regenerate with generate_test_suite or re-upload via POST /files)` });
          }
          suiteB64 = Buffer.from(bytes).toString("base64");
        } else {
          return json({ error: "provide suite_id or file_b64 to start a run, or job_id to check one" });
        }

        const newJobId = crypto.randomUUID();
        const runner = await getAgentByName(this.env.JobRunner, newJobId);
        await runner.start(suiteB64);

        // Give quick runs a chance to finish inside this call — one tool call, full report.
        const deadline = Date.now() + INLINE_WAIT_MS;
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, INLINE_POLL_MS));
          const status = await this.jobStatus(newJobId);
          if (status.status === "complete" || status.status === "error") return json(status);
        }
        const status = await this.jobStatus(newJobId);
        return json({
          ...status,
          hint:
            `The run continues in the background. Call run_test_suite again with { "job_id": "${newJobId}" } ` +
            `after ${status.next_check_seconds ?? 30}s to get the report — do not busy-poll. The user can also ` +
            `watch the status_url in a browser.`,
        });
      },
    );
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
