/**
 * Create a test-case suite from model-analyzed cases via the worker's create_test_case_all tool,
 * and download the generated .xlsx into resources/. The model does the heavy lifting (analyzing the
 * app's flows + schema) and writes a small cases JSON; this script just ships it.
 *
 * NOTE: create_test_case_all only CREATES the suite — it does NOT run the tests. Run the suite
 * separately (the run-suite skill / run_suite tool, or the Python run_and_record).
 *
 * Arg 1: path to a cases JSON file: { base_path?, deployment_id?, application_logs_fetch_url?,
 *        cases: [ { method, url, expected_status, ... }, … ] }
 * Arg 2 (optional): output suite path (default: resources/<cases-stem>_suite.xlsx).
 *
 * Usage:
 *   MCP_URL=<url>/mcp npx tsx scripts/create-cases.mts ../resources/test-enroll-impl4.json
 */
import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

const BASE = process.env.MCP_URL ?? "http://localhost:8799/mcp";
const casesPath = process.argv[2];
if (!casesPath) throw new Error("provide a path to a cases JSON file");
const stem = basename(casesPath).replace(/\.json$/i, "");
const suiteOut = process.argv[3] ?? `../resources/${stem}_suite.xlsx`;

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

await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "create-cases", version: "0" } });

const input = JSON.parse(readFileSync(casesPath, "utf8"));
const result: any = await call("create_test_case_all", input);
if (result.error) throw new Error(typeof result.error === "string" ? result.error : JSON.stringify(result));

console.log(`created: suite_id=${result.suite_id}  cases=${result.case_count}  base_path=${result.base_path}`);
console.log(`application_logs_fetch_url: ${result.application_logs_fetch_url ?? "(blank)"}`);
if (result.suite_download_url) {
  const dl = await fetch(result.suite_download_url);
  writeFileSync(suiteOut, Buffer.from(await dl.arrayBuffer()));
  console.log(`suite saved : ${suiteOut}`);
} else {
  console.log("(no suite_download_url returned)");
}
console.log("create_test_case_all only CREATES the suite — run it separately (run-suite skill / run_suite).");
