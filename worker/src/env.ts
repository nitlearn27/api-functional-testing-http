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

  // Behaviour tunables (wrangler vars) — same defaults as the Python ALT_* settings.
  PROPAGATION_WAIT_SECONDS: number;
  LOG_FETCH_MAX_RETRIES: number;
  LOG_FETCH_RETRY_WAIT_SECONDS: number;
  LOG_CORRELATION_FALLBACK: boolean;

  // Anypoint credentials (secrets; unprefixed lowercase like the Python .env keys).
  application_logs_fetch_url?: string;
  token_endpoint?: string;
  client_id?: string;
  client_secret?: string;
  grant_type?: string;
}
