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

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..models import ApiResponse, CaseEvidence, CaseReport, SuiteReport, TestCase
from .auth import get_auth_token
from .http_runner import ApiCallError, call_api
from .logs import (
    correlation_present,
    discard_snapshot,
    matched_log_lines,
    snapshot_logs,
    validate_logs,
)
from .response import assert_case_response
from .results_writer import write_evidence_tabs, write_results
from .suite import read_test_suite


@dataclass
class _CaseRun:
    """A case's request-phase outcome, carried into the log-validation phase."""

    case: TestCase
    report: CaseReport
    correlation_id: str | None
    sent_request: dict | None = None
    response: ApiResponse | None = None
    matched_log_lines: dict[str, list[str]] | None = None


def run_suite(suite_path: str, retain_snapshots: bool = False) -> SuiteReport:
    """Run every case in the suite at ``suite_path`` and return an aggregate report."""
    report, _evidence = _run(suite_path, retain_snapshots)
    return report


def run_and_record(
    suite_path: str, retain_snapshots: bool = False, results_path: str | None = None
) -> tuple[SuiteReport, str, str]:
    """Run the suite and record results into a SEPARATE ``<stem>_results.xlsx`` file.

    Use this (rather than bare ``run_suite``) so every test run is recorded. The **suite file is
    never modified** — results are written to a sibling results workbook (``results_path`` if
    given, else ``_results_path(suite_path)``). The results file is cloned from the suite on first
    use (so it carries the case definitions), then each run appends a timestamped ``RESULTS``
    summary block and overwrites the per-case evidence tabs (latest run only). Returns
    ``(report, run_at, results_path)``.
    """
    report, evidence = _run(suite_path, retain_snapshots)
    out = Path(results_path) if results_path else _results_path(suite_path)
    if not out.exists():
        shutil.copy2(suite_path, out)  # seed the results file with the suite's case definitions
    run_at = write_results(str(out), report)
    write_evidence_tabs(str(out), evidence, run_at)
    return report, run_at, str(out)


def _results_path(suite_path: str) -> Path:
    """Sibling results workbook for ``suite_path``: ``<stem>_results<suffix>``.

    A trailing ``_suite`` in the stem is replaced (so ``foo_suite.xlsx`` -> ``foo_results.xlsx``);
    otherwise ``_results`` is appended (``foo.xlsx`` -> ``foo_results.xlsx``).
    """
    p = Path(suite_path)
    stem = p.stem[: -len("_suite")] if p.stem.endswith("_suite") else p.stem
    return p.with_name(f"{stem}_results{p.suffix}")


def _run(suite_path: str, retain_snapshots: bool = False) -> tuple[SuiteReport, list[CaseEvidence]]:
    """Run every case and return both the aggregate report and per-case evidence."""
    import os
    os.environ["_CURRENT_SUITE_PATH"] = suite_path

    suite = read_test_suite(suite_path)
    settings = get_settings()
    report = SuiteReport(parse_errors=suite.parse_errors)

    # Phase 1: requests + response assertions.
    runs = [_run_request(case, suite.base_path) for case in suite.cases]

    # Phase 2: log validation (one snapshot per distinct source, reused across cases).
    active_snapshots: list[str] = []
    try:
        _validate_logs_phase(runs, settings, active_snapshots, suite.application_logs_fetch_url)
    finally:
        if not retain_snapshots:
            for sid in active_snapshots:
                discard_snapshot(sid)

    report.cases = [r.report for r in runs]
    report.total = len(report.cases)
    report.passed = sum(1 for c in report.cases if c.passed)
    report.failed = report.total - report.passed
    return report, [_build_evidence(r) for r in runs]


def _build_evidence(run: _CaseRun) -> CaseEvidence:
    """Flatten a finished ``_CaseRun`` into the self-contained evidence record for its tab."""
    case, rep = run.case, run.report
    ra, lv, resp = rep.response_assert, rep.log_validation, run.response
    req = run.sent_request or {}
    return CaseEvidence(
        test_id=case.test_id,
        description=case.description,
        passed=rep.passed,
        error=rep.error,
        method=req.get("method"),
        url=req.get("url"),
        request_headers=req.get("headers") or {},
        request_body=req.get("body"),
        actual_status=rep.actual_status,
        expected_status=rep.expected_status,
        latency_ms=resp.latency_ms if resp else None,
        match_mode=ra.mode if ra else None,
        response_passed=ra.passed if ra else None,
        response_diffs=ra.diffs if ra else [],
        expected_response=case.expected_response,
        actual_body=resp.body if resp else None,
        validated_logs=case.validate_logs,
        logs_passed=lv.passed if lv else None,
        log_source=case.log_source if case.validate_logs else None,
        correlation_id=rep.correlation_id,
        expected_log_strings=case.expected_log_strings,
        matched_logs=lv.matched if lv else [],
        missing_logs=lv.missing if lv else [],
        used_fallback=lv.used_fallback if lv else False,
        lines_considered=lv.lines_considered if lv else 0,
        matched_log_lines=run.matched_log_lines or {},
    )


def _join_url(base_path: str | None, url: str) -> str:
    """Join a suite base path with a case url, preserving the base subpath.

    ``urljoin`` discards the base subpath when ``url`` starts with ``/`` (it treats it as
    host-absolute), so ``.../api`` + ``/products`` would wrongly become ``.../products``.
    A fully-qualified case url (``http(s)://...``) is used as-is.
    """
    if url.startswith(("http://", "https://")) or not base_path:
        return url
    return f"{base_path.rstrip('/')}/{url.lstrip('/')}"


def _run_request(case: TestCase, base_path: str | None) -> _CaseRun:
    """Do the call + response assertion for one case (no log validation yet)."""
    # Generate the correlation id up front so it is recorded even if the request fails.
    correlation_id = f"{case.test_id}-{uuid.uuid4().hex[:12]}"
    try:
        url = _join_url(base_path, case.url)

        headers = dict(case.headers)
        if case.auth_required:
            headers["Authorization"] = f"Bearer {get_auth_token()}"

        sent_request = {"method": case.method, "url": url, "headers": headers, "body": case.body}
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
        return _CaseRun(case=case, report=report, correlation_id=correlation_id,
                        sent_request=sent_request, response=response)
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
    runs: list[_CaseRun],
    settings: Settings,
    active_snapshots: list[str],
    application_logs_fetch_url: str | None,
) -> None:
    """Snapshot once per distinct log_source and validate each opted-in case against it."""
    log_runs = [r for r in runs if r.case.validate_logs and r.correlation_id]
    if not log_runs:
        return

    # CloudHub needs time to surface a request's logs, so wait before the first fetch.
    if settings.propagation_wait_seconds > 0:
        time.sleep(settings.propagation_wait_seconds)

    # Group by source so each backend is downloaded only once.
    by_source: dict[str, list[_CaseRun]] = {}
    for r in log_runs:
        by_source.setdefault(r.case.log_source, []).append(r)

    for source_name, group in by_source.items():
        correlation_ids = [r.correlation_id for r in group if r.correlation_id]
        try:
            sid = _snapshot_with_retry(
                settings, source_name, correlation_ids, application_logs_fetch_url
            )
            active_snapshots.append(sid)
        except Exception as exc:  # noqa: BLE001 - attribute the failure to every case in group
            for r in group:
                r.report.error = (r.report.error or "") + f" log snapshot failed: {exc}"
                r.report.passed = False
            continue

        for r in group:
            # A log line counts only if it carries this case's correlation id AND the expected
            # message — never a whole-log message-only match against unrelated runs. So both
            # validation and evidence are strictly correlation-scoped (no fallback).
            lv = validate_logs(
                sid, r.correlation_id, r.case.expected_log_strings, r.case.log_match_mode,
                correlation_fallback=False,
            )
            r.report.log_validation = lv
            if not lv.passed:
                r.report.passed = False
            # Capture the actual matching lines as evidence (blank if none carry the corr id).
            r.matched_log_lines = matched_log_lines(
                sid, r.correlation_id, r.case.expected_log_strings, r.case.log_match_mode,
                correlation_fallback=False,
            )


def _snapshot_with_retry(
    settings: Settings,
    source_name: str,
    correlation_ids: list[str],
    application_logs_fetch_url: str | None,
) -> str:
    """Download a log snapshot, retrying until every correlation id's logs have surfaced.

    CloudHub publishes a request's logs with a lag, so after the initial fetch we re-download
    up to ``log_fetch_max_retries`` more times (waiting ``log_fetch_retry_wait_seconds`` between
    tries) until each case's correlation id appears. Intermediate snapshots are discarded. The
    most recent snapshot id is returned even if some ids never showed up (those cases then fail
    honestly rather than blocking forever).
    """
    sid = snapshot_logs(
        settings, log_source=source_name,
        application_logs_fetch_url=application_logs_fetch_url,
    )
    for _ in range(max(0, settings.log_fetch_max_retries)):
        if all(correlation_present(sid, cid) for cid in correlation_ids):
            break
        if settings.log_fetch_retry_wait_seconds > 0:
            time.sleep(settings.log_fetch_retry_wait_seconds)
        discard_snapshot(sid)
        sid = snapshot_logs(
        settings, log_source=source_name,
        application_logs_fetch_url=application_logs_fetch_url,
    )
    return sid
