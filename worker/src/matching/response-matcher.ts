/**
 * ResponseMatcher — compare an actual API response against an expectation.
 *
 * Faithful port of matching/response_matcher.py. Three body modes plus a status-only mode:
 *   - exact       — deep equality of the whole body (extra keys are diffs).
 *   - json_subset — every key/value in `expected` must appear in `actual` (extras allowed). A
 *                   single-node expected list `[tmpl]` is a template checked against EVERY actual
 *                   element (any-length list); a multi-node expected list is matched positionally
 *                   (extra actual elements ignored).
 *   - schema      — `expected` is a JSON Schema validated against `actual` (Draft 2020-12).
 *
 * `ignore_paths` are dotted paths (`*` wildcard) pruned from both sides before comparison.
 * The wildcard value `<<any>>` requires the field's presence but accepts any value.
 *
 * Schema validation uses @cfworker/json-schema rather than ajv: ajv compiles validators with
 * `new Function()`, which Cloudflare Workers block (eval is disallowed), so ajv throws at runtime.
 */
import { Validator } from "@cfworker/json-schema";
import type { AssertResult, MatchMode, ResponseDiff } from "../models.js";

export const ANY_VALUE = "<<any>>";

export interface AssertOptions {
  actual_body: unknown;
  expected: unknown;
  mode?: MatchMode;
  ignore_paths?: string[] | null;
  actual_status?: number | null;
  expected_status?: number | null;
}

export function assertResponse(opts: AssertOptions): AssertResult {
  const mode: MatchMode = opts.mode ?? "json_subset";
  const ignorePaths = opts.ignore_paths ?? [];

  let statusOk = true;
  const diffs: ResponseDiff[] = [];
  if (opts.expected_status != null) {
    statusOk = opts.actual_status === opts.expected_status;
    if (!statusOk) {
      diffs.push({
        path: "<status>",
        expected: opts.expected_status,
        actual: opts.actual_status ?? null,
        message: "status code mismatch",
      });
    }
  }

  if (mode === "status_only") {
    return { passed: statusOk, mode, status_ok: statusOk, diffs };
  }

  const prunedActual = prune(deepClone(opts.actual_body), ignorePaths);

  if (mode === "schema") {
    diffs.push(...checkSchema(prunedActual, opts.expected));
  } else {
    const prunedExpected = prune(deepClone(opts.expected), ignorePaths);
    const subset = mode === "json_subset";
    diffs.push(...compare(prunedExpected, prunedActual, "", subset));
  }

  return { passed: statusOk && diffs.length === 0, mode, status_ok: statusOk, diffs };
}

// --- schema --------------------------------------------------------------------------

function checkSchema(actual: unknown, schema: unknown): ResponseDiff[] {
  const validator = new Validator(schema as object, "2020-12");
  const { errors } = validator.validate(actual);
  // Sort by instanceLocation to mirror jsonschema's absolute-path ordering. instanceLocation is a
  // JSON pointer ("#", "#/n", "#/0/orderId"); map it to our dotted path form ("<root>", "n", ...).
  const sorted = [...errors].sort((a, b) => a.instanceLocation.localeCompare(b.instanceLocation));
  return sorted.map((err) => {
    const loc = err.instanceLocation.replace(/^#\/?/, "");
    return { path: loc ? loc.replace(/\//g, ".") : "<root>", message: err.error };
  });
}

// --- comparison ----------------------------------------------------------------------

function compare(expected: unknown, actual: unknown, path: string, subset: boolean): ResponseDiff[] {
  const diffs: ResponseDiff[] = [];

  // `<<any>>` accepts whatever value is present here (presence already checked by caller).
  if (expected === ANY_VALUE) return diffs;

  if (isPlainObject(expected)) {
    if (!isPlainObject(actual)) return [typeDiff(path, expected, actual)];
    if (!subset) {
      const extra = Object.keys(actual).filter((k) => !(k in expected));
      for (const key of extra.sort()) {
        diffs.push({ path: join(path, key), actual: actual[key], message: "unexpected key (exact mode)" });
      }
    }
    for (const [key, expVal] of Object.entries(expected)) {
      const child = join(path, key);
      if (!(key in actual)) {
        diffs.push({ path: child, expected: expVal, message: "missing key" });
        continue;
      }
      diffs.push(...compare(expVal, actual[key], child, subset));
    }
    return diffs;
  }

  if (Array.isArray(expected)) {
    if (!Array.isArray(actual)) return [typeDiff(path, expected, actual)];
    // json_subset with a single-node template: that node is checked against EVERY actual element,
    // so a list of any length passes iff every element matches the template (the count is
    // irrelevant). An empty actual list passes vacuously.
    if (subset && expected.length === 1) {
      actual.forEach((item, idx) => {
        diffs.push(...compare(expected[0], item, join(path, String(idx)), subset));
      });
      return diffs;
    }
    // exact mode, or a multi-node expected: positional. Only exact requires the lengths to match;
    // for json_subset the extra actual nodes beyond the template are ignored.
    if (!subset && expected.length !== actual.length) {
      diffs.push({ path, expected: expected.length, actual: actual.length, message: "list length mismatch" });
    }
    for (let idx = 0; idx < expected.length; idx++) {
      const child = join(path, String(idx));
      if (idx >= actual.length) {
        diffs.push({ path: child, expected: expected[idx], message: "missing list item" });
        continue;
      }
      diffs.push(...compare(expected[idx], actual[idx], child, subset));
    }
    return diffs;
  }

  if (!deepEqual(expected, actual)) {
    diffs.push({ path: path || "<root>", expected, actual, message: "value mismatch" });
  }
  return diffs;
}

function typeDiff(path: string, expected: unknown, actual: unknown): ResponseDiff {
  return { path: path || "<root>", expected: pyType(expected), actual: pyType(actual), message: "type mismatch" };
}

function join(path: string, segment: string): string {
  return path ? `${path}.${segment}` : segment;
}

// --- pruning -------------------------------------------------------------------------

function prune(value: unknown, ignorePaths: string[]): unknown {
  for (const raw of ignorePaths) {
    const segments = raw.trim().split(".").filter((s) => s.length > 0);
    if (segments.length) pruneOne(value, segments);
  }
  return value;
}

function pruneOne(value: unknown, segments: string[]): void {
  const head = segments[0];
  const rest = segments.slice(1);

  if (isPlainObject(value)) {
    const targets = head === "*" ? Object.keys(value) : [head];
    for (const key of targets) {
      if (!(key in value)) continue;
      if (rest.length) pruneOne(value[key], rest);
      else delete value[key];
    }
  } else if (Array.isArray(value)) {
    let indices: number[];
    if (head === "*") indices = value.map((_, i) => i);
    else if (/^\d+$/.test(head) && Number(head) < value.length) indices = [Number(head)];
    else return;
    // Prune trailing elements first so index removal stays valid in the leaf case.
    for (const idx of [...indices].sort((a, b) => b - a)) {
      if (rest.length) pruneOne(value[idx], rest);
      else value.splice(idx, 1);
    }
  }
}

// --- helpers -------------------------------------------------------------------------

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function deepClone<T>(v: T): T {
  return v === undefined ? v : structuredClone(v);
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((x, i) => deepEqual(x, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ak = Object.keys(a);
    const bk = Object.keys(b);
    if (ak.length !== bk.length) return false;
    return ak.every((k) => k in b && deepEqual(a[k], b[k]));
  }
  return false;
}

/** Python-style type name, so diff output reads the same as the reference implementation. */
function pyType(v: unknown): string {
  if (v === null || v === undefined) return "NoneType";
  if (Array.isArray(v)) return "list";
  if (typeof v === "object") return "dict";
  if (typeof v === "string") return "str";
  if (typeof v === "boolean") return "bool";
  if (typeof v === "number") return Number.isInteger(v) ? "int" : "float";
  return typeof v;
}
