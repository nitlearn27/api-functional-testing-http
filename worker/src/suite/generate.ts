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

type Json = Record<string, any>;

export interface GenerateSummary {
  base_path: string | null;
  case_count: number;
  cases_by_category: Record<string, number>;
}

export function generateTestSuite(specYaml: string): { summary: GenerateSummary; bytes: Uint8Array } {
  const spec = parseYaml(specYaml) as Json;
  const builder = new SuiteBuilder(spec);
  builder.build();
  const bytes = writeSheet(builder.basePath, builder.cases);
  return {
    summary: { base_path: builder.basePath, case_count: builder.cases.length, cases_by_category: builder.categories },
    bytes,
  };
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

  private successExpected(op: Json, echo?: Json): Json | null {
    const schema = successSchema(this.spec, op);
    const expected: Json = {};
    for (const field of schema.required ?? []) expected[field] = ANY;
    if (echo) Object.assign(expected, echo);
    return Object.keys(expected).length ? expected : null;
  }

  private add(category: string, description: string, method: string, url: string, opts: {
    expected_status: number; body?: unknown; headers?: Json; expected_response?: unknown;
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
      response_match_mode: "json_subset" as MatchMode,
      validate_logs: false,
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
      baseline = (example ? structuredClone(example) : exampleFromSchema(this.spec, schema)) as Json;
      headers = { ...JSON_HEADERS };
    }
    const echo = baseline && ["POST", "PUT", "PATCH"].includes(method) ? structuredClone(baseline) : undefined;

    // Positive case (first documented 2xx).
    this.add("positive", `${name} — valid request → ${successStatus}`, method, posUrl,
      { headers, body: baseline, expected_status: successStatus, expected_response: this.successExpected(op, echo) ?? undefined });

    // Query-parameter constraint negatives.
    for (const qp of queryParams) {
      for (const [label, value] of schemaNegatives(qp.name, qp.schema)) {
        this.add("query_validation", `${name} — ${label} → 400`, method,
          baseUrl + "?" + urlencode({ [qp.name]: value }),
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

function exampleFromSchema(spec: Json, schema: Json): Json {
  const required = new Set<string>(schema.required ?? []);
  const out: Json = {};
  for (const [field, rawSchema] of Object.entries<Json>(schema.properties ?? {})) {
    const pschema = deref(spec, rawSchema);
    if (required.has(field) && "example" in pschema) out[field] = pschema.example;
  }
  return out;
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

function writeSheet(basePath: string | null, cases: TestCase[]): Uint8Array {
  const aoa: unknown[][] = [
    ["Basepath", basePath ?? ""],
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
