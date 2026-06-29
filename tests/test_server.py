"""Verify the FastMCP server exposes exactly the three-tool contract."""

import asyncio

from api_log_test_mcp.server import mcp

EXPECTED_TOOLS = {
    "create_test_suite_from_schema",
    "create_test_suite_from_application",
    "run_test_suite",
}


def test_all_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
