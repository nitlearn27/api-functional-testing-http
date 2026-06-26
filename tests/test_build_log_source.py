"""build_log_source: the anypoint log URL comes from the suite, not .env (offline)."""

import pytest

from api_log_test_mcp.config import Settings
from api_log_test_mcp.logsource.anypoint_source import AnypointLogSource
from api_log_test_mcp.tools.logs import build_log_source

URL = "https://logs.example.test/deployments/abc"


def test_anypoint_uses_suite_url():
    source = build_log_source("anypoint", Settings(), application_logs_fetch_url=URL)
    assert isinstance(source, AnypointLogSource)
    # The suite URL is injected into the (otherwise creds-only) Anypoint settings.
    assert source._settings.application_logs_fetch_url == URL


def test_anypoint_requires_url():
    for missing in (None, ""):
        with pytest.raises(ValueError, match="application_logs_fetch_url"):
            build_log_source("anypoint", Settings(), application_logs_fetch_url=missing)
