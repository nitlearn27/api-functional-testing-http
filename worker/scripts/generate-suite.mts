/**
 * Call the deployed (or local) generate_test_suite tool on a spec file and save the .xlsx.
 * Usage: MCP_URL=<url>/mcp npx tsx scripts/generate-suite.mts <spec.yaml> <out.xlsx>
 */
import { readFileSync, writeFileSync } from "node:fs";

const BASE = process.env.MCP_URL ?? "http://localhost:8799/mcp";
const [specPath, outPath] = process.argv.slice(2);
let session = "";
let id = 0;

async function rpc(method: string, params: unknown): Promise<any> {
  const res = await fetch(BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...(session ? { "mcp-session-id": session } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
  });
  if (!session) session = res.headers.get("mcp-session-id") ?? "";
  const line = (await res.text()).split("\n").find((l) => l.startsWith("data: "));
  if (!line) throw new Error("no data in MCP response");
  const msg = JSON.parse(line.slice(6));
  if (msg.error) throw new Error(JSON.stringify(msg.error));
  return msg.result;
}

await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "gen", version: "0" } });
const r = await rpc("tools/call", { name: "generate_test_suite", arguments: { spec_yaml: readFileSync(specPath, "utf8") } });
const sc = r.structuredContent;
console.log("base_path :", sc.base_path);
console.log("case_count:", sc.case_count);
console.log("categories:", JSON.stringify(sc.cases_by_category));
console.log("download  :", sc.download_url);
if (outPath && sc.case_count > 0) {
  // Download the .xlsx from the link (no base64) and save it locally.
  const dl = await fetch(sc.download_url);
  if (!dl.ok) throw new Error(`download failed: HTTP ${dl.status}`);
  writeFileSync(outPath, Buffer.from(await dl.arrayBuffer()));
  console.log("saved     :", outPath);
} else if (sc.case_count === 0) {
  console.log("(no cases generated — nothing saved)");
}
