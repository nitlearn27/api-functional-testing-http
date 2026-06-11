/**
 * Run a suite end-to-end on the deployed worker and download the results workbook.
 *
 * Suite source (arg 1) is EITHER:
 *   - a path to a local .xlsx        -> uploaded as file_b64
 *   - "id:<suite_id>"                -> a server-stored suite from generate_test_suite
 * Arg 2 (optional) is the output results path (default: <source-stem>_results.xlsx next to it,
 * or resources/results.xlsx for an id source).
 *
 * Usage:
 *   MCP_URL=<url>/mcp npx tsx scripts/run-and-record.mts resources/openapi_suite.xlsx
 *   MCP_URL=<url>/mcp npx tsx scripts/run-and-record.mts id:<suite_id> resources/openapi_results.xlsx
 */
import { readFileSync, writeFileSync } from "node:fs";

const BASE = process.env.MCP_URL ?? "http://localhost:8799/mcp";
const source = process.argv[2];
if (!source) throw new Error("provide a .xlsx path or id:<suite_id>");
let session = "";
let id = 0;

async function rpc(method: string, params: unknown): Promise<any> {
  const res = await fetch(BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...(session ? { "mcp-session-id": session } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  if (!session) session = res.headers.get("mcp-session-id") ?? "";
  const line = (await res.text()).split("\n").find((l) => l.startsWith("data: "))!;
  const msg = JSON.parse(line.slice(6));
  if (msg.error) throw new Error(JSON.stringify(msg.error));
  return msg.result;
}
const call = async (name: string, args: unknown) => (await rpc("tools/call", { name, arguments: args })).structuredContent;

// Resolve the run argument and a default output path.
let runArg: Record<string, string>;
let defaultOut: string;
if (source.startsWith("id:")) {
  runArg = { suite_id: source.slice(3) };
  defaultOut = "resources/results.xlsx";
} else {
  runArg = { file_b64: readFileSync(source).toString("base64") };
  defaultOut = source.replace(/\.xlsx$/i, "") + "_results.xlsx";
}
const outPath = process.argv[3] ?? defaultOut;

await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "run-record", version: "0" } });

// run_test_suite both starts the run and (re-called with job_id) reports it. Quick suites
// complete inside the first call; log-validation runs return a job_id to keep checking.
let status: any = await call("run_test_suite", runArg);
if (status.error) throw new Error(status.error);
console.log(`started: job_id=${status.job_id}`);
if (status.status_url) console.log(`status (check anytime, even after Ctrl-C): ${status.status_url}`);

// Poll until done, pacing by the server's next_check_seconds hint (log-validation runs
// legitimately take minutes: 60s propagation + up to 3x60s retries). Hard cap ~15 min.
const deadline = Date.now() + 15 * 60 * 1000;
while (status.status !== "complete" && status.status !== "error") {
  if (Date.now() > deadline) {
    console.log(`\ngiving up locally — the run continues server-side; check ${status.status_url ?? "run_test_suite with the job_id"}`);
    process.exit(2);
  }
  const wait = Math.min(Math.max(status.next_check_seconds ?? 5, 2), 60);
  process.stdout.write(`\r  status=${status.status} (${status.detail ?? "..."}) — next check in ${wait}s   `);
  await new Promise((r) => setTimeout(r, wait * 1000));
  status = await call("run_test_suite", { job_id: status.job_id });
}
console.log(`\nfinal: status=${status.status}  run_at=${status.run_at}`);
if (status.status === "error") throw new Error(status.error);

const r = status.report;
console.log(`report: total=${r.total} passed=${r.passed} failed=${r.failed}`);
for (const c of r.cases) console.log(`  ${c.test_id}: ${c.passed ? "PASS" : "FAIL"} actual=${c.actual_status} expected=${c.expected_status}`);

if (status.result_download_url) {
  const dl = await fetch(status.result_download_url);
  writeFileSync(outPath, Buffer.from(await dl.arrayBuffer()));
  console.log(`results link : ${status.result_download_url}`);
  console.log(`results saved: ${outPath} (sheets: tests + one evidence tab per case)`);
} else {
  console.log("(no results link)");
}
