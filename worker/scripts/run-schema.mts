/**
 * Run an OpenAPI YAML schema end-to-end on the deployed worker: generate the suite AND run it
 * (real HTTP + response assertions + CloudHub log validation), then download both the generated
 * suite and the results workbook. One MCP tool — run_schema — does the generate-and-run; this
 * script just polls it and saves the files.
 *
 * Arg 1: path to the OpenAPI 3.0 YAML schema.
 * Arg 2 (optional): output results path (default: resources/<schema-stem>_results.xlsx).
 * The generated suite is also saved as resources/<schema-stem>_suite.xlsx.
 *
 * Usage:
 *   MCP_URL=<url>/mcp npx tsx scripts/run-schema.mts ../resources/employee-api-oas.yaml
 */
import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

const BASE = process.env.MCP_URL ?? "http://localhost:8799/mcp";
const specPath = process.argv[2];
if (!specPath) throw new Error("provide a path to an OpenAPI YAML schema");
const stem = basename(specPath).replace(/\.(ya?ml)$/i, "");
const suiteOut = `../resources/${stem}_suite.xlsx`;
const resultsOut = process.argv[3] ?? `../resources/${stem}_results.xlsx`;

let session = "";
let id = 0;

async function rpc(method: string, params: unknown): Promise<any> {
  const res = await fetch(BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...(session ? { "mcp-session-id": session } : {}), ...(process.env.MCP_TOKEN ? { Authorization: `Bearer ${process.env.MCP_TOKEN}` } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  if (!session) session = res.headers.get("mcp-session-id") ?? "";
  const line = (await res.text()).split("\n").find((l) => l.startsWith("data: "))!;
  const msg = JSON.parse(line.slice(6));
  if (msg.error) throw new Error(JSON.stringify(msg.error));
  return msg.result;
}
const call = async (name: string, args: unknown) => (await rpc("tools/call", { name, arguments: args })).structuredContent;

await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "run-schema", version: "0" } });

// run_schema generates the suite AND starts the run in one call (re-called with job_id, it reports).
let status: any = await call("run_schema", { spec_yaml: readFileSync(specPath, "utf8") });
if (status.error) throw new Error(status.error);
console.log(`generated: suite_id=${status.suite_id}  cases=${status.case_count}  base_path=${status.base_path}`);
console.log(`application_logs_fetch_url: ${status.application_logs_fetch_url ?? "(blank — no deployment id / base configured)"}`);
if (status.suite_download_url) {
  const dl = await fetch(status.suite_download_url);
  writeFileSync(suiteOut, Buffer.from(await dl.arrayBuffer()));
  console.log(`suite saved : ${suiteOut}`);
}
console.log(`started: job_id=${status.job_id}`);
if (status.status_url) console.log(`status (check anytime, even after Ctrl-C): ${status.status_url}`);

const deadline = Date.now() + 15 * 60 * 1000;
while (status.status !== "complete" && status.status !== "error") {
  if (Date.now() > deadline) {
    console.log(`\ngiving up locally — the run continues server-side; check ${status.status_url ?? "run_schema with the job_id"}`);
    process.exit(2);
  }
  const wait = Math.min(Math.max(status.next_check_seconds ?? 5, 2), 60);
  process.stdout.write(`\r  status=${status.status} (${status.detail ?? "..."}) — next check in ${wait}s   `);
  await new Promise((r) => setTimeout(r, wait * 1000));
  status = await call("run_schema", { job_id: status.job_id });
}
console.log(`\nfinal: status=${status.status}  run_at=${status.run_at}`);
if (status.status === "error") throw new Error(status.error);

const r = status.report;
console.log(`report: total=${r.total} passed=${r.passed} failed=${r.failed}`);
for (const c of r.cases) console.log(`  ${c.test_id}: ${c.passed ? "PASS" : "FAIL"} actual=${c.actual_status} expected=${c.expected_status}`);

if (status.result_download_url) {
  const dl = await fetch(status.result_download_url);
  writeFileSync(resultsOut, Buffer.from(await dl.arrayBuffer()));
  console.log(`results link : ${status.result_download_url}`);
  console.log(`results saved: ${resultsOut} (sheets: tests + one evidence tab per case)`);
} else {
  console.log("(no results link)");
}
