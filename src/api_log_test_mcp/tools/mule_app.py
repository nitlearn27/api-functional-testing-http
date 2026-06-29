"""Read a MuleSoft application's source folder — flow logic + its bundled OpenAPI schema.

Used by ``create_test_suite_from_application``. ``parse_mule_app`` reads what the flows define
(base path, APIkit endpoints, per-flow loggers, ``choice`` branches, the DataWeave success response
each flow emits, and the global error-handler's actual error envelope). ``locate_oas`` additionally
finds + extracts the app's **bundled OpenAPI schema** (an Exchange dependency resolved into
``target/repository`` at build time) — used to build *valid request structures* (body/headers/query)
so the flow cases can actually call the API, and for schema validation coverage. Stdlib only.
"""

from __future__ import annotations

import glob
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# Mule XML namespaces (the default namespace is `core`).
NS = {
    "core": "http://www.mulesoft.org/schema/mule/core",
    "http": "http://www.mulesoft.org/schema/mule/http",
    "apikit": "http://www.mulesoft.org/schema/mule/mule-apikit",
    "ee": "http://www.mulesoft.org/schema/mule/ee/core",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
# An APIkit implementation flow is named "<method>:\<path>[:<contentType>]:<config>".
_FLOW_NAME = re.compile(r"^(get|post|put|patch|delete|head|options):\\", re.IGNORECASE)


@dataclass
class Branch:
    """One ``choice`` branch: a ``payload.<field> == '<value>'`` guard (or otherwise) + logger."""

    method: str
    path: str
    field: str | None  # None => the <otherwise> branch
    value: str | None
    logger: str


@dataclass
class MuleApp:
    base_path: str | None = None
    endpoints: list[tuple[str, str]] = field(default_factory=list)  # (METHOD, /path)
    flow_loggers: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # Top-level string fields the flow's DataWeave set-payload emits (the mock success response).
    flow_responses: dict[tuple[str, str], dict] = field(default_factory=dict)
    branches: list[Branch] = field(default_factory=list)
    error_envelope: dict[int, dict] = field(default_factory=dict)  # status -> response body


def parse_mule_app(app_root: str) -> MuleApp:
    """Parse every ``src/main/mule/*.xml`` under ``app_root`` into a :class:`MuleApp`."""
    root = Path(app_root)
    app = MuleApp()
    mule_files = sorted((root / "src" / "main" / "mule").glob("*.xml"))
    for xml_path in mule_files:
        tree = ET.parse(xml_path)
        mule = tree.getroot()
        if app.base_path is None:
            app.base_path = _base_path(mule)
        for flow in mule.findall("core:flow", NS):
            _parse_flow(flow, app)
    return app


def _base_path(mule: ET.Element) -> str | None:
    """``http://<host>:<port><listener-base>`` from the listener config + the `/api/*` listener."""
    conn = mule.find(".//http:listener-config/http:listener-connection", NS)
    host = (conn.get("host") if conn is not None else None) or "localhost"
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    port = (conn.get("port") if conn is not None else None) or "8081"
    # The non-console listener path (e.g. "/api/*"); strip the trailing wildcard.
    listener_base = ""
    for listener in mule.findall(".//http:listener", NS):
        path = listener.get("path") or ""
        if path and not path.startswith("/console"):
            listener_base = re.sub(r"/\*?$", "", path)
            break
    return f"http://{host}:{port}{listener_base}"


def _parse_flow(flow: ET.Element, app: MuleApp) -> None:
    name = flow.get("name", "")
    if name and flow.find("core:error-handler", NS) is not None:
        _parse_error_handler(flow.find("core:error-handler", NS), app)
    if not _FLOW_NAME.match(name):
        return
    method, path = _flow_endpoint(name)
    if not method:
        return
    app.endpoints.append((method, path))
    # Entry/exit loggers are the flow's direct <logger> children (not the ones inside a <choice>).
    msgs = [lg.get("message", "") for lg in flow.findall("core:logger", NS) if lg.get("message")]
    if msgs:
        app.flow_loggers[(method, path)] = msgs
    response = _impl_flow_response(flow)
    if response:
        app.flow_responses[(method, path)] = response
    for choice in flow.findall("core:choice", NS):
        _parse_choice(choice, method, path, app)


def _impl_flow_response(flow: ET.Element) -> dict | None:
    """Top-level string fields of the flow's first DataWeave ``set-payload`` (the mock response).

    Isolates only the **first** balanced ``{ … }`` object (the payload) — so a trailing
    ``as Object {encoding: …, mediaType: …}`` coercion is ignored — then takes that object's own
    ``key: "value"`` pairs, skipping nested objects (e.g. ``address``). Non-string fields ignored.
    """
    sp = flow.find(".//ee:set-payload", NS)
    if sp is None or not sp.text:
        return None
    body = sp.text.split("---", 1)[-1]  # the DataWeave body, after the `---` separator
    start = body.find("{")
    if start == -1:
        return None
    depth = 0
    end = start
    for i in range(start, len(body)):  # find the matching close of the first object
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    inner_depth = 0
    top: list[str] = []
    for ch in body[start + 1 : end]:  # inside the first object, depth-0 chars only
        if ch == "{":
            inner_depth += 1
        elif ch == "}":
            inner_depth -= 1
        elif inner_depth == 0:
            top.append(ch)
    out = {key: value for key, value in re.findall(r'(\w+)\s*:\s*"([^"]*)"', "".join(top))}
    return out or None


def _flow_endpoint(name: str) -> tuple[str, str]:
    """Map an APIkit flow name to (METHOD, /path), e.g. ``post:\\patients:application\\json:c``."""
    parts = name.split(":")
    if len(parts) < 3 or parts[0].lower() not in _HTTP_METHODS:
        return "", ""
    method = parts[0].upper()
    path = parts[1].replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return method, path


def _parse_choice(choice: ET.Element, method: str, path: str, app: MuleApp) -> None:
    """Pull simple ``payload.<field> == '<value>'`` whens + the otherwise, each with its logger."""
    for when in choice.findall("core:when", NS):
        logger = when.find("core:logger", NS)
        if logger is None or not logger.get("message"):
            continue
        m = re.search(r"payload\.(\w+)\s*==\s*'([^']*)'", when.get("expression", ""))
        if m:
            app.branches.append(
                Branch(method, path, m.group(1), m.group(2), logger.get("message", ""))
            )
    otherwise = choice.find("core:otherwise", NS)
    if otherwise is not None:
        logger = otherwise.find("core:logger", NS)
        if logger is not None and logger.get("message"):
            app.branches.append(Branch(method, path, None, None, logger.get("message", "")))


def _parse_error_handler(handler: ET.Element, app: MuleApp) -> None:
    """Map each APIKIT error type's HTTP status to the literal JSON body the app returns."""
    for oep in handler.findall("core:on-error-propagate", NS):
        status = _set_variable_int(oep, "httpStatus")
        body = _set_payload_object(oep)
        if status is not None and body is not None:
            app.error_envelope[status] = body


def _set_variable_int(oep: ET.Element, var_name: str) -> int | None:
    for sv in oep.findall(".//ee:set-variable", NS):
        if sv.get("variableName") == var_name and sv.text and sv.text.strip().isdigit():
            return int(sv.text.strip())
    return None


def _set_payload_object(oep: ET.Element) -> dict | None:
    """Extract the ``{message: "…"}`` from the on-error DataWeave set-payload (literal map only)."""
    sp = oep.find(".//ee:set-payload", NS)
    if sp is None or not sp.text:
        return None
    out: dict[str, str] = {}
    for key, value in re.findall(r'(\w+)\s*:\s*"([^"]*)"', sp.text):
        out[key] = value
    return out or None


def locate_oas(app_root: str) -> Path | None:
    """Find the app's bundled OpenAPI YAML and return a path to the extracted file (or None).

    The schema is an Anypoint Exchange dependency resolved into ``target/repository`` at build time;
    falls back to ``~/.m2/repository`` using the Exchange coords in the ``apikit:config`` ``api=``
    attribute. Returns the path to the ``.yaml`` unpacked from the ``*-oas.zip`` (under the app's
    ``target/`` so it stays inside the project), or None when no schema is on disk.
    """
    root = Path(app_root)
    zips = sorted(
        glob.glob(str(root / "target" / "repository" / "**" / "*-oas.zip"), recursive=True)
    )
    if not zips:
        zips = _m2_oas_zips(root)
    for zip_path in zips:
        yaml_path = _extract_oas_yaml(Path(zip_path), root)
        if yaml_path is not None:
            return yaml_path
    return None


def _m2_oas_zips(root: Path) -> list[str]:
    """OAS zips in ~/.m2 for the Exchange coords parsed from the apikit:config ``api=`` attr."""
    coords = _apikit_api_coords(root)
    if not coords:
        return []
    group_id, artifact_id, version = coords
    m2 = Path.home() / ".m2" / "repository" / group_id / artifact_id / version
    return sorted(glob.glob(str(m2 / "*-oas.zip")))


def _apikit_api_coords(root: Path) -> tuple[str, str, str] | None:
    """Parse ``api="resource::<group>:<artifact>:<version>:oas:zip:<file>"`` from the Mule XML."""
    for xml_path in (root / "src" / "main" / "mule").glob("*.xml"):
        cfg = ET.parse(xml_path).getroot().find(".//apikit:config", NS)
        if cfg is None:
            continue
        m = re.match(r"resource::([^:]+):([^:]+):([^:]+):", cfg.get("api", ""))
        if m:
            return m.group(1), m.group(2), m.group(3)
    return None


def _extract_oas_yaml(zip_path: Path, app_root: Path) -> Path | None:
    dest = app_root / "target" / "_oas_extracted"
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".yaml", ".yml"))]
        if not names:
            return None
        dest.mkdir(parents=True, exist_ok=True)
        zf.extract(names[0], dest)
        return dest / names[0]
