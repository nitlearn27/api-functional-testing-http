"""generate_test_suite fills application_logs_fetch_url from the spec's deployment id.

Offline: the deployments base is injected via monkeypatch (never read from a real .env), so the
test is deterministic. Mirrors worker/test/suite-generator-logurl.test.ts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from api_log_test_mcp.config import AnypointSettings
from api_log_test_mcp.tools import suite_generator
from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import _deployment_logs_url, generate_test_suite

BASE = (
    "https://anypoint.mulesoft.com/amc/application-manager/api/v2/"
    "organizations/ORG/environments/ENV/deployments"
)
ID = "351c3653-f9db-4a6a-864a-624f7b5eaa91"

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Employees", "version": "1.0"},
    "servers": [
        {
            "url": "https://employee-api-impl.example.cloudhub.io/api",
            "description": f"Production server deployed in CloudHub with id {ID}",
        }
    ],
    "paths": {
        "/employees": {"get": {"summary": "List", "responses": {"200": {"description": "OK"}}}}
    },
}


def test_deployment_logs_url_helper():
    assert _deployment_logs_url(SPEC, BASE) == f"{BASE}/{ID}"
    assert _deployment_logs_url(SPEC, BASE + "/") == f"{BASE}/{ID}"  # trailing slash trimmed
    assert _deployment_logs_url(SPEC, None) is None  # no base configured
    assert _deployment_logs_url({"servers": [{"description": "no id here"}]}, BASE) is None


def _patch_base(monkeypatch, base: str | None):
    monkeypatch.setattr(
        suite_generator,
        "get_anypoint_settings",
        lambda: AnypointSettings(deployments_base_url=base),
    )


def test_generate_fills_logs_url_when_base_configured(tmp_path: Path, monkeypatch):
    _patch_base(monkeypatch, BASE)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    out = tmp_path / "suite.xlsx"
    summary = generate_test_suite(str(spec_path), str(out))

    assert summary["application_logs_fetch_url"] == f"{BASE}/{ID}"
    suite = read_test_suite(str(out))
    assert suite.application_logs_fetch_url == f"{BASE}/{ID}"


def test_generate_leaves_logs_url_blank_without_base(tmp_path: Path, monkeypatch):
    _patch_base(monkeypatch, None)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    out = tmp_path / "suite.xlsx"
    summary = generate_test_suite(str(spec_path), str(out))

    assert summary["application_logs_fetch_url"] is None
    suite = read_test_suite(str(out))
    assert suite.application_logs_fetch_url is None
