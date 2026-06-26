import { describe, expect, it } from "vitest";
import { correlationPresent, type LogSource, type RawSnapshot, SnapshotStore, validateLogs } from "../src/logs/snapshot.js";

// Ports the in-memory pipeline from tests/test_snapshot_validate.py. There is no filesystem,
// so a fake LogSource supplies the lines that the file mock used to read from disk.
const LINES = [
  "2026-06-04 10:00:01 INFO Order lookup succeeded [correlationId: order-001]",
  "2026-06-04 10:00:02 INFO returning 200 [correlationId: order-001]",
  "2026-06-04 10:00:03 INFO extra detail [correlationId: order-001]",
  "2026-06-04 10:00:04 INFO Payment declined [correlationId: pay-042]",
  "2026-06-04 10:00:05 INFO gateway slow [correlationId: pay-042]",
];

class FakeSource implements LogSource {
  discoverInstances() {
    return ["cloudhub"];
  }
  async snapshot(): Promise<RawSnapshot> {
    return { lines_by_instance: { cloudhub: LINES } };
  }
}

async function snap() {
  const store = new SnapshotStore();
  return store.create(new FakeSource());
}

describe("snapshot + validate", () => {
  it("passes and fails on expected strings, correlation-scoped", async () => {
    const s = await snap();

    const ok = validateLogs(s, "order-001", ["Order lookup succeeded", "returning 200"]);
    expect(ok.passed).toBe(true);
    expect(ok.lines_considered).toBe(3);
    expect(ok.missing).toEqual([]);

    const fail = validateLogs(s, "pay-042", ["Payment declined", "this never appears"]);
    expect(fail.passed).toBe(false);
    expect(fail.missing).toEqual(["this never appears"]);
    expect(fail.matched).toContain("Payment declined");
  });

  it("strict unknown correlation id considers nothing", async () => {
    const s = await snap();
    const res = validateLogs(s, "nope", ["x"], "contains", false);
    expect(res.passed).toBe(false);
    expect(res.lines_considered).toBe(0);
    expect(res.used_fallback).toBe(false);
  });

  it("unknown correlation id falls back to the whole snapshot", async () => {
    const s = await snap();
    const res = validateLogs(s, "nope", ["Order lookup succeeded"], "contains", true);
    expect(res.passed).toBe(true);
    expect(res.used_fallback).toBe(true);
  });

  it("any_of passes when at least one string matches; regex mode works", async () => {
    const s = await snap();
    expect(validateLogs(s, "pay-042", ["nope", "gateway slow"], "any_of").passed).toBe(true);
    expect(validateLogs(s, "order-001", ["returning \\d+"], "regex").passed).toBe(true);
  });

  it("correlationPresent gates on indexed ids", async () => {
    const s = await snap();
    expect(correlationPresent(s, "order-001")).toBe(true);
    expect(correlationPresent(s, "missing-id")).toBe(false);
  });

  it("groups multi-line event continuation lines under the header's correlation id", async () => {
    // A Mule APIKIT:BAD_REQUEST exception: only the header line carries the correlation id;
    // the boxed message + error type are continuation lines with no id of their own.
    const event = [
      "2026-06-17 10:46:54 DefaultExceptionListener [correlationId: TC-004-ff7d9d4a6ab7]",
      "********************************************************************************",
      "Message    : required key [customerId] not found",
      "Error type : APIKIT:BAD_REQUEST",
      "********************************************************************************",
    ];
    class EventSource implements LogSource {
      discoverInstances() {
        return ["cloudhub"];
      }
      async snapshot(): Promise<RawSnapshot> {
        return { lines_by_instance: { cloudhub: event } };
      }
    }
    const s = await new SnapshotStore().create(new EventSource());
    // The expected string lives on a continuation line, not the header — strict (no fallback).
    const res = validateLogs(s, "TC-004-ff7d9d4a6ab7", ["APIKIT:BAD_REQUEST"], "contains", false);
    expect(res.passed).toBe(true);
    expect(res.used_fallback).toBe(false);
    expect(res.lines_considered).toBe(event.length);
  });
});
