/**
 * Worker entry point.
 *
 * Serves the MCP server over Streamable HTTP at /mcp (the recommended transport). The
 * JobRunner Durable Object is addressed internally by job_id and is never reached directly
 * over HTTP.
 */
import { getAgentByName } from "agents";
import type { Env } from "./env.js";
import { TestMcpServer } from "./mcp-server.js";
import { JobRunner, type StatusView } from "./job-runner.js";
import { FileStore, storeFile } from "./file-store.js";
import { readTestSuite } from "./suite/parse.js";

export { TestMcpServer, JobRunner, FileStore };

// SQLite-backed DO storage caps a single value at 2 MB; suites are far smaller.
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

/**
 * Manual suite upload: lets a user push an .xlsx directly (e.g. curl --data-binary) instead of
 * routing base64 through an MCP client model, then run it by suite_id. The workbook is parsed
 * on arrival so a bad file fails here, not mid-run.
 */
async function handleUpload(request: Request, env: Env): Promise<Response> {
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length === 0) {
    return Response.json({ error: "empty body — send the .xlsx bytes (curl --data-binary @suite.xlsx)" }, { status: 400 });
  }
  if (bytes.length > MAX_UPLOAD_BYTES) {
    return Response.json({ error: `file is ${bytes.length} bytes; max upload is ${MAX_UPLOAD_BYTES} (2 MB)` }, { status: 413 });
  }
  const suite = readTestSuite(bytes);
  if (suite.cases.length === 0) {
    return Response.json(
      { error: "could not parse any test cases from the workbook", parse_errors: suite.parse_errors },
      { status: 400 },
    );
  }
  const filename = new URL(request.url).searchParams.get("filename") ?? "uploaded_suite.xlsx";
  const stored = await storeFile(env, bytes, filename);
  return Response.json({
    suite_id: stored.id,
    case_count: suite.cases.length,
    parse_errors: suite.parse_errors,
    download_url: stored.url,
    expires_in: "2 hours",
    run_hint: `Call run_test_suite with { "suite_id": "${stored.id}" } — no file upload needed.`,
  });
}

async function handleJobStatus(jobId: string, env: Env): Promise<Response> {
  const runner = await getAgentByName(env.JobRunner, jobId);
  const status = (await runner.getStatus()) as StatusView;
  if (!status.started) {
    return Response.json({ error: `unknown job_id '${jobId}'` }, { status: 404 });
  }
  return Response.json(status, { headers: { "Cache-Control": "no-store" } });
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext): Response | Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    // Manual upload of a suite workbook; returns a suite_id usable in run_test_suite.
    if (url.pathname === "/files" && request.method === "POST") {
      return handleUpload(request, env);
    }

    // Plain-HTTP job status: check a run from a browser/curl without holding any connection
    // open — the run itself proceeds on DO alarms regardless of who is watching.
    if (url.pathname.startsWith("/jobs/")) {
      const jobId = url.pathname.slice("/jobs/".length);
      if (!jobId) return new Response("Not found", { status: 404 });
      return handleJobStatus(jobId, env);
    }

    // Download a generated suite / results workbook by id (capability URL).
    if (url.pathname.startsWith("/files/")) {
      const id = url.pathname.slice("/files/".length);
      if (!id) return new Response("Not found", { status: 404 });
      return env.FILES.get(env.FILES.idFromName(id)).fetch(request);
    }

    // Streamable HTTP MCP transport.
    if (url.pathname.startsWith("/mcp")) {
      return TestMcpServer.serve("/mcp", { binding: "TestMcpServer" }).fetch(request, env, ctx);
    }

    return new Response("Not found", { status: 404 });
  },
};
