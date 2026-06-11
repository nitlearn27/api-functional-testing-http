/**
 * JobRunner — one Durable Object instance per job_id; owns a full suite run end to end.
 *
 * It composes the pure orchestration pieces (src/orchestrate.ts) and drives the two long waits
 * with DO alarms (this.schedule), so nothing blocks an HTTP request:
 *
 *   start  → schedule(0, runRequests)
 *   runRequests → Phase 1 (requests + assertions). No log cases? → finalize.
 *                 Else status=waiting_propagation, schedule(PROPAGATION_WAIT, logPhase).
 *   logPhase → for the current log_source: take a fresh snapshot; if every correlation id is
 *              present (or the retry budget is exhausted) validate + advance to the next source,
 *              else attempt++ and schedule(RETRY_WAIT, logPhase). After the last source → finalize.
 *   finalize → aggregate report + build the results workbook bytes; status=complete.
 *
 * All state (suite bytes, per-case runs, per-source attempt counter, the final report + result
 * workbook) lives in the DO's SQLite, so the job survives MCP-session loss and DO eviction.
 */
import { Agent } from "agents";
import { buildResultWorkbook } from "./suite/results.js";
import { readTestSuite } from "./suite/parse.js";
import {
  buildEvidence,
  type CaseRun,
  decideRetry,
  defaultRequestDeps,
  distinctSources,
  failGroupSnapshot,
  finalizeReport,
  runRequestsPhase,
  validateGroup,
} from "./orchestrate.js";
import { AnypointLogSource } from "./logs/anypoint.js";
import { correlationPresent, SnapshotStore, type LogSource } from "./logs/snapshot.js";
import { storeFile } from "./file-store.js";
import type { Env } from "./env.js";
import type { ParseError, SuiteReport } from "./models.js";

export type JobState = "pending" | "running" | "waiting_propagation" | "validating_logs" | "complete" | "error";

interface RunnerState {
  status: JobState;
  error: string | null;
  run_at: string | null;
  suite_b64: string | null;
  parse_errors: ParseError[];
  runs: CaseRun[] | null;
  source_idx: number;
  attempt: number;
  report: SuiteReport | null;
  result_download_url: string | null;
}

const EMPTY: RunnerState = {
  status: "pending",
  error: null,
  run_at: null,
  suite_b64: null,
  parse_errors: [],
  runs: null,
  source_idx: 0,
  attempt: 0,
  report: null,
  result_download_url: null,
};

export interface StatusView {
  /** False when this DO was never started — lets callers 404 unknown/expired job ids. */
  started: boolean;
  status: JobState;
  /** Human-readable phase, e.g. "fetching logs (attempt 2/4)". */
  detail: string;
  /** Suggested seconds before the next status check (null when the run is over). */
  next_check_seconds: number | null;
  run_at: string | null;
  error: string | null;
  report: SuiteReport | null;
  result_download_url: string | null;
}

export class JobRunner extends Agent<Env, RunnerState> {
  initialState: RunnerState = EMPTY;

  /** Kick off a run and return immediately; the work proceeds on DO alarms. */
  async start(suiteB64: string): Promise<{ status: JobState }> {
    this.setState({ ...EMPTY, status: "running", suite_b64: suiteB64 });
    await this.schedule(0, "runRequests");
    return { status: "running" };
  }

  async getStatus(): Promise<StatusView> {
    const s = this.state;
    return {
      started: s.suite_b64 !== null || s.status !== "pending",
      status: s.status,
      detail: this.phaseDetail(),
      next_check_seconds: this.nextCheckSeconds(),
      run_at: s.run_at,
      error: s.error,
      report: s.report,
      result_download_url: s.result_download_url,
    };
  }

  private phaseDetail(): string {
    switch (this.state.status) {
      case "pending":
        return "not started";
      case "running":
        return "executing HTTP requests and response assertions";
      case "waiting_propagation":
        return `waiting ${this.propagationWaitSeconds()}s for CloudHub log propagation`;
      case "validating_logs":
        return `fetching and validating logs (attempt ${this.state.attempt + 1}/${this.maxRetries() + 1})`;
      case "complete":
        return "run complete — results workbook ready";
      case "error":
        return `run failed: ${this.state.error ?? "unknown error"}`;
    }
  }

  private nextCheckSeconds(): number | null {
    switch (this.state.status) {
      case "pending":
      case "running":
        return 5;
      case "waiting_propagation":
        return this.propagationWaitSeconds();
      case "validating_logs":
        return this.retryWaitSeconds();
      default:
        return null; // complete / error — nothing left to wait for
    }
  }

  // --- alarm callbacks -----------------------------------------------------------------

  async runRequests(): Promise<void> {
    try {
      const suite = readTestSuite(base64ToBytes(this.state.suite_b64!));
      const runs = await runRequestsPhase(suite, defaultRequestDeps());

      if (distinctSources(runs).length === 0) {
        await this.finalizeRun(runs, suite.parse_errors);
        return;
      }
      this.setState({ ...this.state, status: "waiting_propagation", runs, parse_errors: suite.parse_errors, source_idx: 0, attempt: 0 });
      await this.schedule(this.propagationWaitSeconds(), "logPhase");
    } catch (exc) {
      this.fail(exc);
    }
  }

  async logPhase(): Promise<void> {
    try {
      const runs = structuredClone(this.state.runs!) as CaseRun[];
      const groups = distinctSources(runs);
      const sourceIdx = this.state.source_idx;
      if (sourceIdx >= groups.length) {
        await this.finalizeRun(runs, this.state.parse_errors);
        return;
      }

      this.setState({ ...this.state, status: "validating_logs" });
      const group = groups[sourceIdx];
      const corrIds = group.runs.map((r) => r.correlation_id!).filter(Boolean);

      let snap;
      try {
        const store = new SnapshotStore();
        snap = await store.create(this.buildLogSource(group.source));
      } catch (exc) {
        failGroupSnapshot(group.runs, errMsg(exc));
        await this.advanceSource(runs, sourceIdx, groups.length);
        return;
      }

      const allPresent = corrIds.every((cid) => correlationPresent(snap, cid));
      if (decideRetry(allPresent, this.state.attempt, this.maxRetries()) === "validate") {
        validateGroup(snap, group.runs, this.correlationFallback());
        await this.advanceSource(runs, sourceIdx, groups.length);
      } else {
        this.setState({ ...this.state, runs, attempt: this.state.attempt + 1 });
        await this.schedule(this.retryWaitSeconds(), "logPhase");
      }
    } catch (exc) {
      this.fail(exc);
    }
  }

  // --- helpers -------------------------------------------------------------------------

  private async advanceSource(runs: CaseRun[], sourceIdx: number, total: number): Promise<void> {
    this.setState({ ...this.state, runs, source_idx: sourceIdx + 1, attempt: 0 });
    if (sourceIdx + 1 < total) await this.schedule(0, "logPhase");
    else await this.finalizeRun(runs, this.state.parse_errors);
  }

  private async finalizeRun(runs: CaseRun[], parseErrors: ParseError[]): Promise<void> {
    const report = finalizeReport(runs, parseErrors);
    const evidence = runs.map(buildEvidence);
    const runAt = nowStamp();
    const resultBytes = buildResultWorkbook(base64ToBytes(this.state.suite_b64!), report, evidence, runAt);
    // Store the results workbook and expose a download link (no base64 in the status payload).
    const stored = await storeFile(this.env, resultBytes, `results-${runAt.replace(/[: ]/g, "-")}.xlsx`);
    this.setState({ ...this.state, status: "complete", runs, report, run_at: runAt, result_download_url: stored.url });
  }

  private buildLogSource(source: string): LogSource {
    if (source === "anypoint") {
      return new AnypointLogSource({
        application_logs_fetch_url: this.env.application_logs_fetch_url,
        token_endpoint: this.env.token_endpoint,
        client_id: this.env.client_id,
        client_secret: this.env.client_secret,
        grant_type: this.env.grant_type,
      });
    }
    // The Python "file" mock backend needs a filesystem, which Workers do not have.
    throw new Error(`unsupported log source: '${source}' (only 'anypoint' is supported in the Worker)`);
  }

  private fail(exc: unknown): void {
    this.setState({ ...this.state, status: "error", error: errMsg(exc) });
  }

  private propagationWaitSeconds(): number {
    return numEnv(this.env.PROPAGATION_WAIT_SECONDS, 60);
  }
  private retryWaitSeconds(): number {
    return numEnv(this.env.LOG_FETCH_RETRY_WAIT_SECONDS, 60);
  }
  private maxRetries(): number {
    return numEnv(this.env.LOG_FETCH_MAX_RETRIES, 3);
  }
  private correlationFallback(): boolean {
    // Mirrors the Python default (log_correlation_fallback = True); vars may arrive as strings.
    const v = this.env.LOG_CORRELATION_FALLBACK as unknown;
    if (v === undefined || v === null) return true;
    return String(v).toLowerCase() !== "false";
  }
}

// --- module helpers --------------------------------------------------------------------

function numEnv(v: unknown, def: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : def;
}

function nowStamp(): string {
  // "YYYY-MM-DD HH:MM:SS" (UTC), matching the Python results-block timestamp shape.
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}

function base64ToBytes(b64: string): Uint8Array {
  return new Uint8Array(Buffer.from(b64, "base64"));
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
