"""FastMCP server entry point.

Exactly three tools: ``create_test_suite_from_schema`` (OpenAPI in → suite),
``create_test_suite_from_application`` (Mule app folder → suite, reading flows + the bundled
schema), and ``run_test_suite`` (run a suite .xlsx, write a separate results file). The underlying
building blocks (HTTP runner, response matcher, log snapshot/validate, auth) stay importable from
``tools/`` for these three to use, but are deliberately not exposed as MCP tools.
"""

from __future__ import annotations

import threading
import time
from typing import Any
import uuid

from fastmcp import FastMCP

from .tools import orchestrate as _orchestrate
from .tools import suite_generator as _suite_generator

mcp = FastMCP("api-log-test-mcp")

# In-memory background job tracking to prevent MCP timeouts in Claude Code
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _update_job_progress(job_id: str, total_expected_seconds: float) -> None:
    """Increment progress percentage and update detail string in real-time."""
    start_time = time.time()
    while True:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or job["status"] in ("complete", "error"):
                break

            elapsed = time.time() - start_time
            # Progress starts at 10% and goes up to 95% based on elapsed time relative to expected seconds
            pct = 10 + int((elapsed / total_expected_seconds) * 85)
            pct = min(95, max(10, pct))

            job["progress_percent"] = pct

            # Dynamic detail strings to indicate activity
            if pct < 20:
                job["detail"] = "Sending HTTP requests..."
            elif pct < 85:
                job["detail"] = (
                    f"Waiting for CloudHub log propagation "
                    f"({int(elapsed)}s/{int(total_expected_seconds)}s)..."
                )
            else:
                job["detail"] = "Downloading logs and validating expected strings..."

        time.sleep(1.0)


def _run_job_async(job_id: str, suite_path: str) -> None:
    try:
        report, run_at, results_path = _orchestrate.run_and_record(suite_path)
        # Serialize SuiteReport model to dict so it crosses the MCP boundary cleanly
        report_dict = report.model_dump() if hasattr(report, "model_dump") else (
            report.dict() if hasattr(report, "dict") else report
        )
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "complete",
                "progress_percent": 100,
                "run_at": run_at,
                "results_path": results_path,
                "report": report_dict,
                "error": None,
                "detail": "Run complete — results workbook ready.",
            }
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "error",
                "progress_percent": 100,
                "run_at": None,
                "results_path": None,
                "report": None,
                "error": str(e),
                "detail": f"Run failed: {e}",
            }


@mcp.tool
def create_test_suite_from_schema(
    schema_path: str, output_path: str | None = None
) -> dict[str, Any]:
    """Create a runnable .xlsx test suite from an OpenAPI 3.0 schema. Does NOT run the tests.

    Walks every path × method and builds comprehensive coverage — a positive case per operation
    plus one negative per validation rule, including the schema's **query params and header params**
    (required ones are sent; omitting/violating them yields a 400) and request-body rules. Writes
    the suite next to the schema as ``<stem>_suite.xlsx`` (or ``output_path``) and returns a summary
    (``output_path``, ``base_path``, ``case_count``, ``cases_by_category``). Run it with
    ``run_test_suite``.
    """
    return _suite_generator.generate_test_suite(schema_path, output_path)


@mcp.tool
def create_test_suite_from_application(
    app_root: str, output_path: str | None = None
) -> dict[str, Any]:
    """Create a .xlsx test suite from a MuleSoft app's root folder.

    Combines flow logic and OpenAPI schema validation. Reads ``src/main/mule/*.xml`` and builds
    cases from the flow logic (base path, endpoints, entry/exit loggers, DataWeave responses,
    choices/branches, and error-handler mappings) and combines them with the bundled OpenAPI
    schema (query, header, path parameter, and body validations) extracted from
    ``target/repository/**/*-oas.zip`` or ``~/.m2``. If no schema is found, it falls back to
    flow-only test cases. Writes ``<app-name>_suite.xlsx`` (or ``output_path``); run it with
    ``run_test_suite``.
    """
    return _suite_generator.create_test_suite_from_application(app_root, output_path)


@mcp.tool
def run_test_suite(
    suite_path: str | None = None, job_id: str | None = None
) -> dict[str, Any]:
    """Run a test-suite .xlsx against its Basepath and monitor execution in the background.

    To START a new run: provide ``suite_path`` (e.g. "path/to/suite_suite.xlsx"). This starts a background job
    and immediately returns a ``job_id``.
    To CHECK status: provide only the ``job_id`` (leave ``suite_path`` blank) to check if the job is running, complete, or failed.
    """
    if job_id:
        if suite_path:
            return {
                "error": (
                    "Do not specify both suite_path and job_id. "
                    "Provide only job_id to check the status of an active run."
                )
            }
        with _jobs_lock:
            if job_id not in _jobs:
                return {"status": "not_found", "error": f"Job ID '{job_id}' not found."}
            return _jobs[job_id]

    if not suite_path:
        return {"error": "Provide suite_path to start a run, or job_id to check the status of one."}

    # Pre-parse the suite to check if log validation is enabled (to estimate total run duration)
    try:
        suite = _orchestrate.read_test_suite(suite_path)
        has_logs = any(c.validate_logs for c in suite.cases)
    except Exception as e:
        return {"error": f"Could not read suite spreadsheet: {e}"}

    new_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[new_id] = {
            "status": "running",
            "progress_percent": 5,
            "run_at": None,
            "results_path": None,
            "report": None,
            "error": None,
            "detail": "Initializing HTTP requests...",
        }

    # Start executing the suite in a daemon thread so it does not block the MCP stdio connection
    t = threading.Thread(target=_run_job_async, args=(new_id, suite_path), daemon=True)
    t.start()

    # Estimate progress duration based on config settings
    from .config import get_settings
    settings = get_settings()
    
    # We estimate log check takes wait time + 15 seconds (typical network latency/retries)
    expected_secs = (settings.propagation_wait_seconds + 15.0) if has_logs else 2.0

    # Start real-time progress updater
    t_progress = threading.Thread(
        target=_update_job_progress, args=(new_id, expected_secs), daemon=True
    )
    t_progress.start()

    return {
        "status": "running",
        "job_id": new_id,
        "progress_percent": 5,
        "detail": (
            f"Run started. Executing HTTP requests and waiting ~{int(expected_secs)}s "
            "for log propagation and verification. Poll status using this tool with job_id."
        ),
        "next_check_seconds": 15,
    }


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
