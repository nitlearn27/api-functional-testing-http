/**
 * Local end-to-end verification driver. Speaks the MCP Streamable HTTP transport to a running
 * `wrangler dev` and exercises every tool. Run with: npx tsx scripts/smoke.mts
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import * as XLSX from "xlsx";

const BASE = process.env.MCP_URL ?? "http://localhost:8799/mcp";
const here = dirname(fileURLToPath(import.meta.url));
const SPEC = readFileSync(resolve(here, "../../resources/products-eapi1.yaml"), "utf8");

let session = "";
let id = 0;

async function rpc(method: string, params: unknown): Promise<any> {
  const res = await fetch(BASE, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...(session ? { "mcp-session-id": session } : {}),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  if (!session) session = res.headers.get("mcp-session-id") ?? "";
  const text = await res.text();
  const line = text.split("\n").find((l) => l.startsWith("data: "));
  if (!line) throw new Error(`no data in response: ${text.slice(0, 200)}`);
  const msg = JSON.parse(line.slice(6));
  if (msg.error) throw new Error(`RPC error: ${JSON.stringify(msg.error)}`);
  return msg.result;
}

async function call(name: string, args: unknown): Promise<any> {
  const result = await rpc("tools/call", { name, arguments: args });
  return result.structuredContent;
}

const ok = (label: string) => console.log(`  ✓ ${label}`);

async function main() {
  // 1. handshake
  const init = await rpc("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "smoke", version: "0" },
  });
  console.log(`\nMCP server: ${init.serverInfo.name} v${init.serverInfo.version}`);
  const tools = (await rpc("tools/list", {})).tools.map((t: any) => t.name);
  console.log(`tools: ${tools.join(", ")}\n`);

  // 2. generate_test_suite (yaml -> xlsx)
  console.log("[generate_test_suite] products spec -> xlsx");
  const gen = await call("generate_test_suite", { spec_yaml: SPEC });
  console.log(`  base_path=${gen.base_path}  case_count=${gen.case_count}`);
  console.log(`  categories=${JSON.stringify(gen.cases_by_category)}`);
  if (gen.case_count !== 41) throw new Error("expected 41 cases");
  if (!gen.download_url) throw new Error("missing download_url");
  ok(`generated 41 cases, download_url + ${gen.cases.length} cases returned (no base64)`);

  // 3. exactly the two consolidated tools are exposed
  if (tools.length !== 2 || !tools.includes("generate_test_suite") || !tools.includes("run_test_suite")) {
    throw new Error(`expected exactly [generate_test_suite, run_test_suite], got: ${tools.join(", ")}`);
  }
  ok("tool list is exactly the 2 consolidated tools");

  // 4. download the suite via the link and parse it locally (round-trip check)
  console.log("\n[download] fetch the link, parse the workbook locally");
  const suiteBytes = Buffer.from(await (await fetch(gen.download_url)).arrayBuffer());
  const dlWb = XLSX.read(suiteBytes, { type: "buffer" });
  const dlRows = XLSX.utils.sheet_to_json<unknown[]>(dlWb.Sheets[dlWb.SheetNames[0]], { header: 1 });
  const dlCases = dlRows.filter((row) => /^TC-/.test(String(row[0] ?? ""))).length;
  if (dlCases !== 41) throw new Error(`round-trip mismatch: ${dlCases} cases`);
  ok(`link downloads a valid .xlsx with 41 case rows (${suiteBytes.length} bytes)`);

  // 5. run_test_suite (real fetch to example.com, no logs) — start + re-call with job_id
  console.log("\n[run_test_suite] 2-case suite vs example.com, status_only");
  const aoa = [
    ["Basepath", "https://example.com"],
    ["test_id", "method", "url", "auth_required", "expected_status", "response_match_mode", "validate_logs"],
    ["TC-A", "GET", "/", "no", 200, "status_only", "no"],
    ["TC-B", "GET", "/nonexistent-path-xyz", "no", 200, "status_only", "no"], // expect 404 -> FAIL
  ];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(aoa), "tests");
  const fileB64 = Buffer.from(XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array).toString("base64");

  let status: any = await call("run_test_suite", { file_b64: fileB64 });
  console.log(`  job_id=${status.job_id}  status=${status.status}`);

  for (let i = 0; i < 20 && status.status !== "complete" && status.status !== "error"; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    status = await call("run_test_suite", { job_id: status.job_id });
  }
  console.log(`  final status=${status.status}  run_at=${status.run_at}`);
  const r = status.report;
  console.log(`  totals: total=${r.total} passed=${r.passed} failed=${r.failed}`);
  for (const c of r.cases) console.log(`    ${c.test_id}: passed=${c.passed} actual=${c.actual_status} expected=${c.expected_status}`);
  if (status.status !== "complete") throw new Error("run did not complete");
  if (!(r.total === 2 && r.passed === 1 && r.failed === 1)) throw new Error("unexpected run report");
  ok("run completed: TC-A pass (200), TC-B fail (404)");

  // 6. download the results workbook and verify the RESULTS block
  if (!status.result_download_url) throw new Error("missing result_download_url");
  const resultBytes = Buffer.from(await (await fetch(status.result_download_url)).arrayBuffer());
  const out = XLSX.read(resultBytes, { type: "buffer" });
  const rows = XLSX.utils.sheet_to_json<unknown[]>(out.Sheets["tests"], { header: 1, blankrows: true, defval: null });
  const hasResults = rows.some((row) => String(row[0] ?? "").startsWith("RESULTS — run"));
  if (!hasResults) throw new Error("result workbook missing RESULTS block");
  console.log(`\n  result workbook sheets: ${out.SheetNames.join(", ")}`);
  ok("result workbook has the RESULTS block + evidence tabs");

  console.log("\n✅ ALL LOCAL CHECKS PASSED\n");
}

main().catch((e) => {
  console.error("\n❌ FAILED:", e.message);
  process.exit(1);
});
