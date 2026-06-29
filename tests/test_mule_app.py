"""parse_mule_app / locate_oas against the sample app in resources/. Offline (file I/O only)."""

from __future__ import annotations

from pathlib import Path

from api_log_test_mcp.tools.mule_app import parse_mule_app

APP = str(Path(__file__).parent.parent / "resources" / "test-enroll-impl4")


def test_parses_base_path_endpoints_and_loggers():
    app = parse_mule_app(APP)
    assert (
        app.base_path == "http://localhost:8081/api"
    )  # listener host:port + /api (0.0.0.0->localhost)
    assert ("GET", "/patients") in app.endpoints
    assert ("POST", "/patients") in app.endpoints
    assert app.flow_loggers[("GET", "/patients")] == ["Start GET", "End GET"]
    assert app.flow_loggers[("POST", "/patients")] == ["Start POST", "End POST"]


def test_parses_choice_branches():
    app = parse_mule_app(APP)
    male = next(b for b in app.branches if b.field == "gender" and b.value == "male")
    assert male.logger == "first flow for male"
    otherwise = next(b for b in app.branches if b.field is None)
    assert otherwise.logger == "flow for female"


def test_parses_error_envelope():
    app = parse_mule_app(APP)
    # The app returns its own {message: ...} envelope (not the OAS {code, message, details}).
    assert app.error_envelope[400] == {"message": "Bad request"}
    assert app.error_envelope[404] == {"message": "Resource not found"}
    assert app.error_envelope[405] == {"message": "Method not allowed"}
    assert app.error_envelope[415] == {"message": "Unsupported media type"}


def test_parses_flow_dataweave_response():
    app = parse_mule_app(APP)
    # Top-level string fields from the GET flow's DataWeave set-payload (the mock response).
    get = app.flow_responses[("GET", "/patients")]
    assert get["patientId"] == "PAT-00123"
    assert get["firstName"] == "John"
    # Nested object fields (address.street/city/...) are skipped, not flattened to the top level.
    assert "street" not in get
    post = app.flow_responses[("POST", "/patients")]
    assert post["patientId"] == "PAT-00456"
    assert post["message"] == "Patient enrolled successfully."
    # The trailing `as Object {encoding: …, mediaType: …}` coercion is NOT part of the response.
    assert "encoding" not in post and "mediaType" not in post
    assert "encoding" not in get and "mediaType" not in get
