/**
 * Snapshot cache + correlation-ID index + validation — ports cache/snapshot_store.py and the
 * in-memory half of tools/logs.py.
 *
 * Lifecycle: download once via a LogSource -> build a correlationId -> lines index -> validate
 * locally as many times as needed -> discard. The correlation id is pulled from each line with
 * the same regex the Python uses (Mule/MDC `correlationId: <id>` forms plus the runtime's
 * per-event `event:<id>` prefix, which is where CloudHub stamps the inbound X-Correlation-ID).
 */
import type { LogMatchMode, LogValidationResult } from "../models.js";

// --- LogSource interface ---------------------------------------------------------------

export interface RawSnapshot {
  lines_by_instance: Record<string, string[]>;
}

export function totalLines(raw: RawSnapshot): number {
  return Object.values(raw.lines_by_instance).reduce((n, v) => n + v.length, 0);
}

export interface LogSource {
  discoverInstances(): string[];
  snapshot(instances?: string[] | null): Promise<RawSnapshot>;
}

// --- correlation index -----------------------------------------------------------------

export const DEFAULT_CORRELATION_PATTERN =
  /(?:correlation[_-]?id["']?\s*[:=]\s*["']?|event:)([A-Za-z0-9._-]+)/i;

// A new log event starts with its own timestamp (ISO `T` or space separated). Continuation lines
// of a multi-line Mule event (boxed exception, stack frames) have none, which is how we tell a
// continuation apart from a fresh, uncorrelated event.
const NEW_EVENT_PREFIX = /^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/;

export class Snapshot {
  constructor(
    public snapshotId: string,
    public raw: RawSnapshot,
    public index: Record<string, string[]>,
  ) {}

  linesFor(correlationId: string): string[] {
    return this.index[correlationId] ?? [];
  }

  allLines(): string[] {
    return Object.values(this.raw.lines_by_instance).flat();
  }
}

export class SnapshotStore {
  private snapshots = new Map<string, Snapshot>();
  constructor(private pattern: RegExp = DEFAULT_CORRELATION_PATTERN) {}

  async create(source: LogSource, instances?: string[] | null): Promise<Snapshot> {
    const raw = await source.snapshot(instances ?? null);
    const snapshotId = crypto.randomUUID().replace(/-/g, "");
    const snap = new Snapshot(snapshotId, raw, this.buildIndex(raw));
    this.snapshots.set(snapshotId, snap);
    return snap;
  }

  get(snapshotId: string): Snapshot {
    const snap = this.snapshots.get(snapshotId);
    if (!snap) throw new Error(`unknown snapshot_id: ${snapshotId}`);
    return snap;
  }

  discard(snapshotId: string): boolean {
    return this.snapshots.delete(snapshotId);
  }

  discardAll(): void {
    this.snapshots.clear();
  }

  private buildIndex(raw: RawSnapshot): Record<string, string[]> {
    const index: Record<string, string[]> = {};
    for (const lines of Object.values(raw.lines_by_instance)) {
      // A Mule/CloudHub log event is a header line carrying the correlation id followed by
      // continuation lines (boxed exception, stack trace) that have no id of their own. Carry
      // the last-seen id forward onto those continuation lines so the whole event groups under
      // it — but a fresh, timestamped event with no id (e.g. a scheduler heartbeat) resets it.
      let current: string | null = null;
      for (const line of lines) {
        const match = this.pattern.exec(line);
        if (match) current = match[1];
        else if (NEW_EVENT_PREFIX.test(line)) current = null;
        if (current) (index[current] ??= []).push(line);
      }
    }
    return index;
  }
}

// --- validation (in-memory) ------------------------------------------------------------

export function validateLogs(
  snap: Snapshot,
  correlationId: string,
  expected: string[],
  mode: LogMatchMode = "contains",
  correlationFallback = true,
): LogValidationResult {
  let lines = snap.linesFor(correlationId);
  let usedFallback = false;
  if (lines.length === 0 && correlationFallback) {
    lines = snap.allLines();
    usedFallback = true;
  }

  const matched: string[] = [];
  const missing: string[] = [];
  for (const needle of expected) {
    if (matches(needle, lines, mode)) matched.push(needle);
    else missing.push(needle);
  }

  // any_of passes if at least one string is found; every other mode requires all of them.
  const passed = mode === "any_of" ? matched.length > 0 : missing.length === 0;

  return {
    passed,
    correlation_id: correlationId,
    matched,
    missing,
    lines_considered: lines.length,
    used_fallback: usedFallback,
  };
}

export function matchedLogLines(
  snap: Snapshot,
  correlationId: string,
  expected: string[],
  mode: LogMatchMode = "contains",
  correlationFallback = true,
): Record<string, string[]> {
  let lines = snap.linesFor(correlationId);
  if (lines.length === 0 && correlationFallback) lines = snap.allLines();
  const out: Record<string, string[]> = {};
  for (const needle of expected) out[needle] = matchingLines(needle, lines, mode);
  return out;
}

export function correlationPresent(snap: Snapshot, correlationId: string): boolean {
  return snap.linesFor(correlationId).length > 0;
}

function matchingLines(needle: string, lines: string[], mode: LogMatchMode): string[] {
  if (mode === "regex") {
    const pattern = new RegExp(needle);
    return lines.filter((line) => pattern.test(line));
  }
  // contains / all_of / any_of all use plain substring matching per string.
  return lines.filter((line) => line.includes(needle));
}

function matches(needle: string, lines: string[], mode: LogMatchMode): boolean {
  return matchingLines(needle, lines, mode).length > 0;
}
