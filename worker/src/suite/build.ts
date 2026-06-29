/**
 * buildSuiteFromCases — render a runnable .xlsx suite from model-analyzed test cases.
 *
 * The companion to generate.ts: instead of the server parsing an OpenAPI spec, the model
 * (client-side) analyzes the source app (e.g. a MuleSoft project's flows) and supplies the
 * distilled cases. This module only renders them into the canonical sheet — the same format
 * read_test_suite / run_suite consume — and resolves the CloudHub log-fetch URL. Nothing here
 * inspects the app; that heavy lifting stays in the model.
 */
import type { LogMatchMode, MatchMode, TestCase } from "../models.js";
import { makeTestCase } from "../models.js";
import { joinDeploymentUrl, writeSheet } from "./generate.js";

/** One model-supplied case: the TestCase fields, all optional except the request essentials. */
export interface CaseInput {
  test_id?: string;
  description?: string;
  method: string;
  url: string;
  headers?: Record<string, unknown>;
  body?: unknown;
  auth_required?: boolean;
  expected_status: number;
  expected_response?: unknown;
  response_match_mode?: MatchMode;
  ignore_paths?: string[];
  validate_logs?: boolean;
  expected_log_strings?: string[];
  log_match_mode?: LogMatchMode;
  log_source?: string;
}

export interface BuildCasesInput {
  cases: CaseInput[];
  base_path?: string | null;
  // Either a ready log-fetch URL, or a deployment id the worker joins onto deployments_base_url.
  application_logs_fetch_url?: string | null;
  deployment_id?: string | null;
}

export interface BuildSummary {
  base_path: string | null;
  case_count: number;
}

/** Drop keys whose value is `undefined` so they don't shadow makeTestCase's defaults on spread. */
function defined<T extends object>(obj: T): Partial<T> {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined)) as Partial<T>;
}

export function buildSuiteFromCases(
  input: BuildCasesInput,
  deploymentsBaseUrl?: string,
): { summary: BuildSummary; bytes: Uint8Array; application_logs_fetch_url: string | null } {
  const cases: TestCase[] = input.cases.map((c, i) =>
    makeTestCase({
      ...defined(c),
      test_id: c.test_id ?? `TC-${String(i + 1).padStart(3, "0")}`,
    }),
  );

  // Prefer an explicit log URL; otherwise build <deployments_base_url>/<deployment_id>. Either
  // way it lands in the sheet metadata so anypoint log validation has a target (blank if neither).
  const logsFetchUrl =
    input.application_logs_fetch_url ?? joinDeploymentUrl(deploymentsBaseUrl, input.deployment_id);

  const basePath = input.base_path ?? null;
  const bytes = writeSheet(basePath, cases, logsFetchUrl);
  return {
    summary: { base_path: basePath, case_count: cases.length },
    bytes,
    application_logs_fetch_url: logsFetchUrl,
  };
}
