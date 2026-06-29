"""Verify the FastMCP server registers the full tool contract."""

import asyncio

from api_log_test_mcp.server import mcp

EXPECTED_TOOLS = {
    # Primary surface — the three user-facing tools.
    "create_test_suite_from_schema",
    "create_test_suite_from_application",
    "run_test_suite",
    # Low-level building blocks the primary tools use.
    "read_test_suite",
    "assert_response",
    "snapshot_logs",
    "validate_logs",
    "get_auth_token",
    "call_api",
    "run_suite",
}


def test_all_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
