import type { JobRunner } from "./job-runner.js";
import type { TestMcpServer } from "./mcp-server.js";
import type { FileStore } from "./file-store.js";

/** Worker bindings + vars + secrets. Mirrors the Python Settings/AnypointSettings split. */
export interface Env {
  // Durable Object bindings
  TestMcpServer: DurableObjectNamespace<TestMcpServer>;
  JobRunner: DurableObjectNamespace<JobRunner>;
  FILES: DurableObjectNamespace<FileStore>; // stores/serves generated files (class: FileStore)

  // The Worker's own public origin, used to build download links (e.g. workers.dev URL).
  PUBLIC_BASE_URL: string;

  // Shared secret guarding /mcp and POST /files. Set via `wrangler secret put MCP_AUTH_TOKEN`.
  // Clients send it as `Authorization: Bearer <token>`. The guard fails closed if unset.
  MCP_AUTH_TOKEN?: string;

  // Behaviour tunables (wrangler vars) — same defaults as the Python ALT_* settings.
  PROPAGATION_WAIT_SECONDS: number;
  LOG_FETCH_MAX_RETRIES: number;
  LOG_FETCH_RETRY_WAIT_SECONDS: number;
  LOG_CORRELATION_FALLBACK: boolean;

  // Anypoint credentials (secrets; unprefixed lowercase like the Python .env keys).
  // The per-suite log-fetch URL is NOT here — it travels with each suite sheet (see job-runner).
  token_endpoint?: string;
  client_id?: string;
  client_secret?: string;
  grant_type?: string;

  // CloudHub deployments base URL (secret; embeds org/env ids), e.g.
  // ".../environments/<ENV>/deployments". generate_test_suite appends "/<deployment-id>" parsed
  // from the spec's server description to build the suite's application_logs_fetch_url.
  deployments_base_url?: string;
}
