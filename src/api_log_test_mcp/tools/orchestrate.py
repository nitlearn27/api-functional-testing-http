"""run_suite: execute a full suite end-to-end and emit a report.

Batched flow (so a rate-limited remote log backend is downloaded only once per run):
  1. For every case: build URL, [attach auth], call_api (stamps a correlation id),
     assert the response.
  2. If any case has ``validate_logs=Yes``: wait for log propagation, then take ONE snapshot
     per distinct ``log_source``, validate each such case's ``expected_log_strings`` against it
     (filtered by that case's correlation id), and merge the result in.
  3. Discard snapshots.

Flags gate the credentialed work: ``auth_required=no`` -> no OAuth token; ``validate_logs=no``
-> no log download/validation. Each case is isolated so one failure never aborts the run.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin

from ..config import Settings, get_settings
from ..models import CaseReport, SuiteReport, TestCase
from .auth import get_auth_token
from .http_runner import ApiCallError, call_api
from .logs import discard_snapshot, snapshot_logs, validate_logs
from .response import assert_case_response
from .results_writer import write_results
from .suite import read_test_suite


@dataclass
class _CaseRun:
    """A case's request-phase outcome, carried into the log-validation phase."""

    case: TestCase
    report: CaseReport
    correlation_id: str | None


def run_suite(suite_path: str, retain_snapshots: bool = False) -> SuiteReport:
    """Run every case in the suite at ``suite_path`` and return an aggregate report."""
    suite = read_test_suite(suite_path)
    settings = get_settings()
    report = SuiteReport(parse_errors=suite.parse_errors)

    # Phase 1: requests + response assertions.
    runs = [_run_request(case, suite.base_path) for case in suite.cases]

    # Phase 2: log validation (one snapshot per distinct source, reused across cases).
    active_snapshots: list[str] = []
    try:
        _validate_logs_phase(runs, settings, active_snapshots)
    finally:
        if not retain_snapshots:
            for sid in active_snapshots:
                discard_snapshot(sid)

    report.cases = [r.report for r in runs]
    report.total = len(report.cases)
    report.passed = sum(1 for c in report.cases if c.passed)
    report.failed = report.total - report.passed
    return report


def run_and_record(suite_path: str, retain_snapshots: bool = False) -> tuple[SuiteReport, str]:
    """Run the suite and append a timestamped results block to the sheet.

    Use this (rather than bare ``run_suite``) so every test run is recorded into the sheet.
    """
    report = run_suite(suite_path, retain_snapshots)
    run_at = write_results(suite_path, report)
    return report, run_at


def _run_request(case: TestCase, base_path: str | None) -> _CaseRun:
    """Do the call + response assertion for one case (no log validation yet)."""
    # Generate the correlation id up front so it is recorded even if the request fails.
    correlation_id = f"{case.test_id}-{uuid.uuid4().hex[:12]}"
    try:
        url = urljoin(base_path or "", case.url) if base_path else case.url

        headers = dict(case.headers)
        if case.auth_required:
            headers["Authorization"] = f"Bearer {get_auth_token()}"

        response = call_api(
            case.method, url, headers=headers, body=case.body, correlation_id=correlation_id,
        )
        response_assert = assert_case_response(case, response)
        report = CaseReport(
            test_id=case.test_id,
            passed=response_assert.passed,
            correlation_id=correlation_id,
            actual_status=response.status,
            expected_status=case.expected_status,
            response_assert=response_assert,
        )
        return _CaseRun(case=case, report=report, correlation_id=correlation_id)
    except ApiCallError as exc:
        return _failed_run(case, correlation_id, f"request failed: {exc}")
    except NotImplementedError as exc:
        return _failed_run(case, correlation_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - any case error must not abort the suite
        return _failed_run(case, correlation_id, f"{type(exc).__name__}: {exc}")


def _failed_run(case: TestCase, correlation_id: str, error: str) -> _CaseRun:
    report = CaseReport(test_id=case.test_id, passed=False, correlation_id=correlation_id,
                        expected_status=case.expected_status, error=error)
    return _CaseRun(case=case, report=report, correlation_id=correlation_id)


def _validate_logs_phase(
    runs: list[_CaseRun], settings: Settings, active_snapshots: list[str]
) -> None:
    """Snapshot once per distinct log_source and validate each opted-in case against it."""
    log_runs = [r for r in runs if r.case.validate_logs and r.correlation_id]
    if not log_runs:
        return

    if settings.propagation_wait_seconds > 0:
        time.sleep(settings.propagation_wait_seconds)

    # Group by source so each backend is downloaded only once.
    by_source: dict[str, list[_CaseRun]] = {}
    for r in log_runs:
        by_source.setdefault(r.case.log_source, []).append(r)

    for source_name, group in by_source.items():
        try:
            sid = snapshot_logs(settings, log_source=source_name)
            active_snapshots.append(sid)
        except Exception as exc:  # noqa: BLE001 - attribute the failure to every case in group
            for r in group:
                r.report.error = (r.report.error or "") + f" log snapshot failed: {exc}"
                r.report.passed = False
            continue

        for r in group:
            lv = validate_logs(
                sid, r.correlation_id, r.case.expected_log_strings, r.case.log_match_mode,
                correlation_fallback=settings.log_correlation_fallback,
            )
            r.report.log_validation = lv
            if not lv.passed:
                r.report.passed = False
