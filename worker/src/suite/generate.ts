/**
 * generate_test_suite — build a runnable .xlsx suite from an OpenAPI 3.0 YAML spec.
 *
 * Faithful port of tools/suite_generator.py. The only boundary change: it takes the spec YAML
 * as text and returns the workbook as bytes (base64-encoded by the MCP tool) instead of reading
 * a path and writing a file. Coverage logic is deliberately `/products`-specific, exactly as the
 * Python reference — a positive case per operation plus one negative per validation rule.
 */
import * as XLSX from "xlsx";
import { parse as parseYaml } from "yaml";
import type { MatchMode, TestCase } from "../models.js";
import { makeTestCase } from "../models.js";

const ANY = "<<any>>";
const JSON_HEADERS = { "Content-Type": "application/json" };

// Body-validation failures expect 400 (the Mulesoft app cannot return the spec's 422).
const BODY_VALIDATION_STATUS = 400;

const SHEET_COLUMNS = [
  "test_id", "description", "method", "url", "headers", "body", "auth_required",
  "expected_status", "expected_response", "response_match_mode", "validate_logs",
  "expected_log_strings", "log_match_mode", "log_source",
];

const REASON: Record<number, string> = {
  400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
  409: "Conflict", 415: "Unsupported Media Type", 422: "Unprocessable Entity",
};

// Standard error types the Mulesoft APIkit Router raises, by HTTP status. Used as the default
// expected_log_strings so every generated case validates that the router logged the right error
// type (e.g. a 400 case asserts "APIKIT:BAD_REQUEST" appears in the CloudHub logs).
const APIKIT_ERROR_TYPES: Record<number, string> = {
  400: "APIKIT:BAD_REQUEST",
  404: "APIKIT:NOT_FOUND",
  405: "APIKIT:METHOD_NOT_ALLOWED",
  406: "APIKIT:NOT_ACCEPTABLE",
  415: "APIKIT:UNSUPPORTED_MEDIA_TYPE",
  501: "APIKIT:NOT_IMPLEMENTED",
};

/**
 * Expected log strings for a case. Error cases assert the APIkit router's error type; non-error
 * cases (2xx success, 401 auth) assert the request line `<METHOD> <path>` — Mule logs it on every
 * routed request (e.g. "Processing GET /orders request" contains "GET /orders"), so the success
 * case never has a blank expectation. The query string is dropped (the logger omits it).
 */
function expectedLogStrings(status: number, method: string, url: string): string[] {
  const errorType = APIKIT_ERROR_TYPES[status];
  if (errorType) return [errorType];
  return [`${method} ${url.split("?")[0]}`];
}

type Json = Record<string, any>;

export interface GenerateSummary {
  base_path: string | null;
  case_count: number;
  cases_by_category: Record<string, number>;
}

export function generateTestSuite(
  specYaml: string,
  deploymentsBaseUrl?: string,
): { summary: GenerateSummary; bytes: Uint8Array } {
  const spec = parseYaml(specYaml) as Json;
  const builder = new SuiteBuilder(spec);
  builder.build();
  // Pre-fill the suite's CloudHub log-fetch URL when a deployments base is configured and the
  // spec's server description carries a deployment id; otherwise leave it blank for the user.
  const logsFetchUrl = deploymentLogsUrl(spec, deploymentsBaseUrl);
  const bytes = writeSheet(builder.basePath, builder.cases, logsFetchUrl);
  return {
    summary: { base_path: builder.basePath, case_count: builder.cases.length, cases_by_category: builder.categories },
    bytes,
  };
}

const DEPLOYMENT_ID_RE = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;

/**
 * Build the suite's `application_logs_fetch_url` as `<base>/<deployment-id>`. The id is the first
 * UUID in `servers[0].description` (e.g. "…deployed in CloudHub with id 351c3653-…"). Returns null
 * when no base is configured or no id is present — the cell then stays blank (back-compat).
 */
export function deploymentLogsUrl(spec: Json, base?: string): string | null {
  if (!base) return null;
  const description = String(spec.servers?.[0]?.description ?? "");
  const id = description.match(DEPLOYMENT_ID_RE)?.[0];
  return joinDeploymentUrl(base, id);
}

/** Join a deployments base and a deployment id into `<base>/<id>` (null when either is missing). */
export function joinDeploymentUrl(base: string | undefined, id: string | null | undefined): string | null {
  if (!base || !id) return null;
  return `${base.replace(/\/+$/, "")}/${id}`;
}

// --- case building ---------------------------------------------------------------------

class SuiteBuilder {
  spec: Json;
  basePath: string | null;
  errorSchema: Json;
  cases: TestCase[] = [];
  categories: Record<string, number> = {};
  private n = 0;

  constructor(spec: Json) {
    this.spec = spec;
    this.basePath = basePath(spec);
    this.errorSchema = errorSchema(spec);
  }

  private errorExpected(code: number): Json {
    const props = this.errorSchema.properties;
    if (!props) return { status: code, error: REASON[code] ?? ANY };
    const expected: Json = {};
    for (const field of Object.keys(props)) {
      if (field === "status") expected[field] = code;
      else if (field === "error") expected[field] = REASON[code] ?? ANY;
      else expected[field] = ANY;
    }
    return expected;
  }

  private successExpected(op: Json, echo?: Json): { expected: unknown; mode: MatchMode } | null {
    const schema = successSchema(this.spec, op);

    // A list endpoint returns an *array* success body (e.g. GET /orders). Emit a readable
    // single-node `<<any>>` template: `[{ field: "<<any>>", … }]`. Under json_subset that one node
    // is checked against EVERY object in the response (any count), so the whole list is validated;
    // a user can replace it with multiple node templates to assert specific nodes positionally.
    if (schema.type === "array") {
      return { expected: arrayAnyTemplate(this.spec, schema), mode: "json_subset" };
    }

    // Object success body. Assert existence (<<any>>) of only the schema's *declared* required
    // fields — never the optional ones (a response model with no `required` shouldn't force every
    // property). `echo` overlays concrete values the request itself sends (create cases).
    const node: Json = {};
    for (const field of schema.required ?? []) node[field] = ANY;
    if (echo) Object.assign(node, echo);
    if (Object.keys(node).length) return { expected: node, mode: "json_subset" };
    // No declared required fields (and nothing echoed): accept any object body with `<<any>>`, but
    // only when the operation actually declares a structured body (else no expectation at all).
    if (schema.type === "object" || schema.properties) {
      return { expected: ANY, mode: "json_subset" };
    }
    return null;
  }

  private add(category: string, description: string, method: string, url: string, opts: {
    expected_status: number; body?: unknown; headers?: Json; expected_response?: unknown;
    response_match_mode?: MatchMode;
  }): void {
    this.n += 1;
    this.cases.push(makeTestCase({
      test_id: `TC-${String(this.n).padStart(3, "0")}`,
      description,
      method,
      url,
      headers: opts.headers ?? {},
      body: opts.body ?? null,
      auth_required: false,
      expected_status: opts.expected_status,
      expected_response: opts.expected_response ?? null,
      response_match_mode: opts.response_match_mode ?? ("json_subset" as MatchMode),
      // Validate logs on every case so each run exercises both the API and the CloudHub log
      // endpoint; error cases assert the APIkit router's error type, 2xx/401 assert nothing
      // specific (still fetched, so a missing/blank log endpoint surfaces).
      validate_logs: true,
      expected_log_strings: expectedLogStrings(opts.expected_status, method, url),
      log_source: "anypoint",
    }));
    this.categories[category] = (this.categories[category] ?? 0) + 1;
  }

  build(): void {
    const paths = this.spec.paths ?? {};
    const methods = ["get", "post", "put", "patch", "delete"];
    for (const [path, item] of Object.entries<Json>(paths)) {
      if (!item || typeof item !== "object") continue;
      for (const method of methods) {
        const op = item[method];
        if (op && typeof op === "object") this.buildOperation(path, method.toUpperCase(), op);
      }
    }
  }

  /** Generic per-operation coverage: a positive case plus one negative per validation rule. */
  private buildOperation(path: string, method: string, op: Json): void {
    const params: Json[] = (op.parameters ?? []).map((p: Json) => ({ ...p, schema: deref(this.spec, p.schema ?? {}) }));
    const pathParams = params.filter((p) => p.in === "path");
    const queryParams = params.filter((p) => p.in === "query");
    const responses: Json = op.responses ?? {};
    const successStatus = firstStatus(responses, "2") ?? 200;
    const hasJsonBody = !!op.requestBody?.content?.["application/json"];
    const secured = nonEmpty(op.security ?? this.spec.security);
    const name = String(op.summary ?? op.operationId ?? `${method} ${path}`);

    const baseUrl = applyPathParams(path, pathParams);
    const query = validQueryString(queryParams);
    const posUrl = baseUrl + query;

    let baseline: Json | undefined;
    let headers: Json = {};
    if (hasJsonBody) {
      const [schema, example] = this.requestBody(op);
      baseline = (example ? structuredClone(example) : sampleValue(this.spec, schema)) as Json;
      headers = { ...JSON_HEADERS };
    }
    const echo = baseline && ["POST", "PUT", "PATCH"].includes(method) ? structuredClone(baseline) : undefined;

    // Positive case (first documented 2xx).
    const success = this.successExpected(op, echo);
    this.add("positive", `${name} — valid request → ${successStatus}`, method, posUrl,
      { headers, body: baseline, expected_status: successStatus,
        expected_response: success?.expected, response_match_mode: success?.mode });

    // Query-parameter constraint negatives: omit each required one, and violate each value constraint.
    for (const qp of queryParams) {
      if (qp.required === true) {
        const others = queryParams.filter((p) => p.name !== qp.name && p.required === true);
        this.add("query_validation", `${name} — missing required query '${qp.name}' → 400`, method,
          baseUrl + validQueryString(others),
          { headers, body: baseline, expected_status: 400, expected_response: this.errorExpected(400) });
      }
      for (const [label, value] of schemaNegatives(qp.name, qp.schema)) {
        const qParams: Record<string, unknown> = {};
        for (const p of queryParams) {
          if (p.required === true) {
            qParams[p.name] = validValue(p.schema);
          }
        }
        qParams[qp.name] = value;
        this.add("query_validation", `${name} — ${label} → 400`, method,
          baseUrl + "?" + urlencode(qParams),
          { headers, body: baseline, expected_status: 400, expected_response: this.errorExpected(400) });
      }
    }

    // Path-parameter constraint negatives.
    for (const pp of pathParams) {
      for (const [label, value] of schemaNegatives(pp.name, pp.schema)) {
        this.add("path_validation", `${name} — ${label} → 400`, method,
          applyPathParams(path, pathParams, { [pp.name]: String(value) }) + query,
          { headers, body: baseline, expected_status: 400, expected_response: this.errorExpected(400) });
      }
    }

    // Request-body validation negatives (all folded into 400; Mulesoft cannot return 422).
    if (hasJsonBody && baseline) {
      const [schema] = this.requestBody(op);
      for (const field of (schema.required ?? []) as string[]) {
        const body = structuredClone(baseline);
        delete body[field];
        this.add("body_validation", `${name} — missing required '${field}' → ${BODY_VALIDATION_STATUS}`, method, posUrl,
          { headers: { ...JSON_HEADERS }, body, expected_status: BODY_VALIDATION_STATUS, expected_response: this.errorExpected(BODY_VALIDATION_STATUS) });
      }
      for (const [field, rawSchema] of Object.entries<Json>(schema.properties ?? {})) {
        const pschema = deref(this.spec, rawSchema);
        const negs = pschema.type === "array" ? arrayNegatives(field, pschema) : schemaNegatives(field, pschema);
        for (const [label, value] of negs) {
          const body = structuredClone(baseline);
          body[field] = value;
          this.add("body_validation", `${name} — ${label} → ${BODY_VALIDATION_STATUS}`, method, posUrl,
            { headers: { ...JSON_HEADERS }, body, expected_status: BODY_VALIDATION_STATUS, expected_response: this.errorExpected(BODY_VALIDATION_STATUS) });
        }
      }
      if (schema.additionalProperties === false) {
        const body = structuredClone(baseline);
        body.unexpectedField = "x";
        this.add("body_validation", `${name} — unexpected extra field (additionalProperties:false) → ${BODY_VALIDATION_STATUS}`, method, posUrl,
          { headers: { ...JSON_HEADERS }, body, expected_status: BODY_VALIDATION_STATUS, expected_response: this.errorExpected(BODY_VALIDATION_STATUS) });
      }
      // Deliberately malformed JSON, sent verbatim as a raw (non-JSON) cell.
      this.add("bad_request", `${name} — malformed JSON body → 400`, method, posUrl,
        { headers: { ...JSON_HEADERS }, body: '{"name": "Broken", "sku": }', expected_status: 400, expected_response: this.errorExpected(400) });
      this.add("media_type", `${name} — wrong Content-Type text/plain → 415`, method, posUrl,
        { headers: { "Content-Type": "text/plain" }, body: baseline, expected_status: 415, expected_response: this.errorExpected(415) });
    }

    // Not-found for a GET addressed by a path parameter.
    if (method === "GET" && pathParams.length > 0) {
      const bogus: Record<string, string> = {};
      for (const pp of pathParams) bogus[pp.name] = "Nonexistent-ZZZ-000";
      this.add("not_found", `${name} — nonexistent resource → 404`, method, applyPathParams(path, pathParams, bogus) + query,
        { headers, body: baseline, expected_status: 404, expected_response: this.errorExpected(404) });
    }

    // Auth negative when the operation (or the API) declares security.
    if (secured) {
      this.add("auth", `${name} — invalid credentials → 401`, method, posUrl,
        { headers: { ...headers, Authorization: "Bearer invalid-token" }, body: baseline, expected_status: 401, expected_response: this.errorExpected(401) });
    }
  }

  private requestBody(op: Json): [Json, unknown] {
    const content = op.requestBody?.content?.["application/json"] ?? {};
    const schema = deref(this.spec, content.schema ?? {});
    return [schema, content.example];
  }
}

// --- schema helpers --------------------------------------------------------------------

function basePath(spec: Json): string | null {
  const servers = spec.servers ?? [];
  if (servers.length && servers[0].url) return servers[0].url;
  return null;
}

/** First response status (as int) whose code starts with `prefix` ("2" for success), in spec order. */
function firstStatus(responses: Json, prefix: string): number | null {
  for (const code of Object.keys(responses)) {
    if (String(code).startsWith(prefix)) return parseInt(code, 10);
  }
  return null;
}

function nonEmpty(security: unknown): boolean {
  return Array.isArray(security) && security.length > 0;
}

/** Substitute `{name}` segments with a valid (or overridden) value, url-quoted. */
function applyPathParams(path: string, pathParams: Json[], overrides: Record<string, string> = {}): string {
  const byName = new Map(pathParams.map((p) => [p.name, p]));
  return path.replace(/\{([^}]+)\}/g, (_m, key: string) => {
    const value = key in overrides ? overrides[key] : String(validValue(byName.get(key)?.schema ?? {}));
    return quote(value);
  });
}

/** Query string of just the required params, each set to a valid value (empty string if none). */
function validQueryString(queryParams: Json[]): string {
  const required = queryParams.filter((p) => p.required === true);
  if (!required.length) return "";
  const params: Record<string, unknown> = {};
  for (const p of required) params[p.name] = validValue(p.schema);
  return "?" + urlencode(params);
}

/** A schema-conformant valid value: explicit example, else first enum, else a type-based default. */
function validValue(schema: Json): unknown {
  if (schema.example !== undefined) return String(schema.example).replace(/%20/g, " ");
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
  switch (schema.type) {
    case "integer":
    case "number":
      return schema.minimum ?? 1;
    case "boolean":
      return true;
    default:
      return "sample";
  }
}

function resolveRef(spec: Json, ref: string): any {
  let node: any = spec;
  for (const part of ref.replace(/^#\//, "").split("/")) node = node[part];
  return node;
}

function deref(spec: Json, node: any): any {
  if (node && typeof node === "object" && !Array.isArray(node) && "$ref" in node) {
    return deref(spec, resolveRef(spec, node.$ref));
  }
  return node;
}

function errorSchema(spec: Json): Json {
  for (const pathItem of Object.values<Json>(spec.paths ?? {})) {
    if (!pathItem || typeof pathItem !== "object") continue;
    for (const op of Object.values<Json>(pathItem)) {
      if (!op || typeof op !== "object") continue;
      for (const [status, rawResp] of Object.entries<Json>(op.responses ?? {})) {
        if (!(String(status).startsWith("4") || String(status).startsWith("5"))) continue;
        const resp = deref(spec, rawResp);
        let schema = resp.content?.["application/json"]?.schema ?? {};
        schema = deref(spec, schema);
        if (schema.properties) return schema;
      }
    }
  }
  return {};
}

function successSchema(spec: Json, op: Json): Json {
  for (const [status, rawResp] of Object.entries<Json>(op.responses ?? {})) {
    if (!String(status).startsWith("2")) continue;
    const resp = deref(spec, rawResp);
    const schema = resp.content?.["application/json"]?.schema ?? {};
    return deref(spec, schema);
  }
  return {};
}

/**
 * A single-node `<<any>>` template for an array success body. When the item schema declares
 * `required` fields it yields `[{ field: "<<any>>", … }]` (only those fields); otherwise — no
 * declared required fields, or scalar items — it yields `["<<any>>"]` (each element accepted).
 */
function arrayAnyTemplate(spec: Json, schema: Json): unknown[] {
  const items = deref(spec, schema.items ?? {});
  const required: string[] = items.required ?? [];
  if (required.length) {
    const node: Json = {};
    for (const field of required) node[field] = ANY;
    return [node];
  }
  return [ANY];
}

function schemaNegatives(name: string, schema: Json): [string, unknown][] {
  const out: [string, unknown][] = [];
  if ("enum" in schema) out.push([`${name} not in allowed enum`, "__INVALID_ENUM__"]);
  if ("pattern" in schema) out.push([`${name} violates pattern ${schema.pattern}`, "!bad!"]);
  if ("minLength" in schema && schema.minLength > 1) {
    const k = schema.minLength;
    out.push([`${name} below minLength ${k}`, "x".repeat(k - 1)]);
  }
  if ("maxLength" in schema) {
    const k = schema.maxLength;
    out.push([`${name} above maxLength ${k}`, "x".repeat(k + 1)]);
  }
  if ("minimum" in schema) out.push([`${name} below minimum ${schema.minimum}`, schema.minimum - 1]);
  if ("maximum" in schema) out.push([`${name} above maximum ${schema.maximum}`, schema.maximum + 1]);
  return out;
}

function arrayNegatives(name: string, schema: Json): [string, unknown][] {
  const out: [string, unknown][] = [];
  const items = schema.items ?? {};
  if ("maxItems" in schema) {
    const k = schema.maxItems;
    out.push([`${name} exceeds maxItems ${k}`, Array.from({ length: k + 1 }, (_, i) => `tag${i}`)]);
  }
  if (schema.uniqueItems) out.push([`${name} contains duplicate items`, ["dup", "dup"]]);
  if ("maxLength" in items) {
    const k = items.maxLength;
    out.push([`${name} item exceeds maxLength ${k}`, ["x".repeat(k + 1)]]);
  }
  return out;
}

/**
 * A schema-valid sample request body. Generates a concrete value for EVERY required field
 * (recursing into nested objects and arrays), so the positive case sends a complete valid payload
 * and each negative case can drop/override exactly one field. Uses a field's `example` when given,
 * else synthesizes by type (enum → first; numbers respect bounds; strings honour common formats and
 * min/maxLength). Optional fields are omitted (a minimal valid body).
 */
function sampleValue(spec: Json, schema: Json): unknown {
  schema = deref(spec, schema);
  if (schema.example !== undefined) return structuredClone(schema.example);
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
  switch (schema.type) {
    case "array": {
      const count = Math.max(Number(schema.minItems ?? 1), 1);
      return Array.from({ length: count }, () => sampleValue(spec, schema.items ?? {}));
    }
    case "integer":
    case "number":
      if (typeof schema.minimum === "number") return schema.minimum;
      if (typeof schema.maximum === "number") return schema.maximum;
      return 1;
    case "boolean":
      return true;
    case "string":
      return sampleString(schema);
    case "object":
      return sampleObject(spec, schema);
    default:
      return schema.properties || schema.required ? sampleObject(spec, schema) : "sample";
  }
}

function sampleObject(spec: Json, schema: Json): Json {
  const props: Json = schema.properties ?? {};
  const out: Json = {};
  for (const field of (schema.required ?? []) as string[]) {
    out[field] = sampleValue(spec, props[field] ?? {});
  }
  return out;
}

function sampleString(schema: Json): string {
  switch (schema.format) {
    case "date": return "2024-01-01";
    case "date-time": return "2024-01-01T00:00:00Z";
    case "email": return "user@example.com";
    case "uuid": return "00000000-0000-0000-0000-000000000000";
    case "uri": return "https://example.com";
  }
  let s = "sample";
  if (typeof schema.minLength === "number" && s.length < schema.minLength) s = s.padEnd(schema.minLength, "x");
  if (typeof schema.maxLength === "number" && s.length > schema.maxLength) s = s.slice(0, schema.maxLength);
  return s;
}

// --- URL helpers (match urllib: urlencode + quote) -------------------------------------

function urlencode(params: Record<string, unknown>): string {
  return Object.entries(params)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
}

function quote(s: string): string {
  // urllib.parse.quote keeps "/" safe by default; our segments contain none, so plain encode.
  return encodeURIComponent(s);
}

// --- sheet writing ---------------------------------------------------------------------

export function writeSheet(basePath: string | null, cases: TestCase[], logsFetchUrl?: string | null): Uint8Array {
  const aoa: unknown[][] = [
    ["Basepath", basePath ?? ""],
    // CloudHub log-fetch URL — auto-filled from deployments_base_url + the spec's deployment id
    // when available; otherwise blank for the user to fill. Required to validate anypoint logs.
    ["application_logs_fetch_url", logsFetchUrl ?? ""],
    ["Auth"],
    [...SHEET_COLUMNS],
    ...cases.map(caseToRow),
  ];
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "tests");
  return XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array;
}

function caseToRow(c: TestCase): unknown[] {
  return [
    c.test_id,
    c.description ?? "",
    c.method,
    c.url,
    Object.keys(c.headers).length ? JSON.stringify(c.headers) : "",
    bodyCell(c.body),
    c.auth_required ? "Yes" : "No",
    c.expected_status ?? "",
    c.expected_response != null ? JSON.stringify(c.expected_response) : "",
    c.response_match_mode,
    c.validate_logs ? "Yes" : "No",
    c.expected_log_strings.join("||"),
    c.log_match_mode,
    c.log_source,
  ];
}

function bodyCell(body: unknown): string {
  if (body === null || body === undefined) return "";
  if (typeof body === "string") return body;
  return JSON.stringify(body);
}
