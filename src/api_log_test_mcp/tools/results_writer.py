"""Write a SuiteReport back into the suite sheet as a timestamped RESULTS block.

Each call appends a new block below any existing content:

    RESULTS — run <YYYY-MM-DD HH:MM:SS>
    test_id | status | actual_status | expected_status | detail
    <one row per case>

The parser stops reading cases at the first ``RESULTS`` marker (see ``tools/suite.py``), so
stacked blocks never interfere with re-parsing the suite.

Safety: a timestamped backup is taken first, and after saving the case-definition region is
re-read and compared to its pre-write state. If the save altered it (e.g. the known
numbers-parser smart-quote round-trip glitch), the file is restored from the backup and a
:class:`ResultsWriteError` is raised — the sheet is never left corrupted.
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from ..models import CaseReport, SuiteReport
from .suite import RESULTS_MARKER, _as_str, _find_header_row, _load_rows

RESULTS_HEADER = [
    "test_id", "status", "actual_status", "expected_status", "correlation_id", "detail",
]


class ResultsWriteError(Exception):
    """Writing the results block failed or would have corrupted the sheet."""


def write_results(path: str, report: SuiteReport, run_at: str | None = None) -> str:
    """Append a timestamped results block to the suite at ``path``. Returns the run timestamp."""
    run_at = run_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_path = Path(path)
    suffix = file_path.suffix.lower()

    block = _build_block(report, run_at)

    backup = file_path.with_name(
        f"{file_path.stem}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}{file_path.suffix}"
    )
    shutil.copy2(file_path, backup)

    before = _case_region(file_path)
    if suffix == ".numbers":
        _append_numbers(file_path, block)
    elif suffix in {".xlsx", ".xlsm"}:
        _append_xlsx(file_path, block)
    else:
        backup.unlink(missing_ok=True)
        raise ResultsWriteError(f"unsupported file type for write-back: {suffix}")

    after = _case_region(file_path)
    if after != before:
        shutil.copy2(backup, file_path)  # restore the untouched original
        raise ResultsWriteError(
            f"save altered the test-definition rows; restored from backup ({backup.name})"
        )

    backup.unlink(missing_ok=True)  # verified intact — the backup is no longer needed
    return run_at


# --- block construction ----------------------------------------------------------------


def _build_block(report: SuiteReport, run_at: str) -> list[list[str]]:
    summary = f"RESULTS — run {run_at}  (passed {report.passed}/{report.total})"
    rows: list[list[str]] = [[summary], list(RESULTS_HEADER)]
    for case in report.cases:
        rows.append([
            case.test_id,
            "PASS" if case.passed else "FAIL",
            "" if case.actual_status is None else str(case.actual_status),
            "" if case.expected_status is None else str(case.expected_status),
            case.correlation_id or "",
            _detail(case),
        ])
    return rows


def _detail(case: CaseReport) -> str:
    if case.error:
        return case.error
    parts: list[str] = []
    ra = case.response_assert
    if ra is not None and not ra.passed:
        if not ra.status_ok:
            parts.append("status mismatch")
        missing = [d.path for d in ra.diffs if d.message == "missing key"]
        if missing:
            parts.append("missing keys: " + ", ".join(missing))
        mismatched = [d.path for d in ra.diffs if d.message == "value mismatch"]
        if mismatched:
            parts.append("value mismatch: " + ", ".join(mismatched))
    lv = case.log_validation
    if lv is not None:
        if lv.missing:
            parts.append("missing logs: " + ", ".join(lv.missing))
        elif lv.used_fallback:
            parts.append("logs ok (whole-log fallback)")
        else:
            parts.append("logs ok")
    if not parts:
        mode = ra.mode if ra is not None else ""
        return f"response matched ({mode})" if case.passed else "did not match"
    return "; ".join(parts)


# --- writers ---------------------------------------------------------------------------


def _append_numbers(file_path: Path, block: list[list[str]]) -> None:
    from numbers_parser import Document

    doc = Document(str(file_path))
    sheet = next((s for s in doc.sheets if s.name.lower() == "tests"), doc.sheets[0])
    table = sheet.tables[0]
    start = table.num_rows
    for _ in range(1 + len(block)):  # +1 leaves a blank separator row
        table.add_row()
    for i, line in enumerate(block):
        for c, val in enumerate(line):
            table.write(start + 1 + i, c, val)
    doc.save(str(file_path))


def _append_xlsx(file_path: Path, block: list[list[str]]) -> None:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=file_path)
    sheet = next(
        (workbook[name] for name in workbook.sheetnames if name.lower() == "tests"),
        workbook.active,
    )
    sheet.append([])  # blank separator
    for line in block:
        sheet.append(line)
    workbook.save(file_path)


# --- verification ----------------------------------------------------------------------


def _case_region(file_path: Path) -> list[list[str]]:
    """The rows from the top through the last test-case row (before any RESULTS marker).

    Comparing this region before/after the write detects any save-time corruption of the
    test definitions.
    """
    rows = _load_rows(file_path)
    header_idx = _find_header_row(rows)
    if header_idx is None:
        return [[_as_str(c) or "" for c in r] for r in rows]

    end = len(rows)
    for i in range(header_idx + 1, len(rows)):
        first = _as_str(rows[i][0]) if rows[i] else None
        if first and first.lower().startswith(RESULTS_MARKER):
            end = i
            break

    region = [_normalize_row(r) for r in rows[:end]]
    # Drop trailing blank rows so an added separator row before the results block doesn't
    # register as a change to the test definitions.
    while region and not region[-1]:
        region.pop()
    return region


def _normalize_row(row: list) -> list[str]:
    """Row as trimmed strings with trailing empties removed.

    Trailing-cell trimming matters because appending a wider results block pads the existing
    (narrower) definition rows with empty cells — that is not a content change.
    """
    cells = [_as_str(c) or "" for c in row]
    while cells and cells[-1] == "":
        cells.pop()
    return cells
