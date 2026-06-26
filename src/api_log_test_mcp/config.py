"""Configuration loading.

Settings come from environment variables (prefix ``ALT_``) with an optional TOML file.
Credentials are referenced by *env var name*, never stored inline and never read from the
test sheet.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Credentials + the deployments base URL. The per-suite log-fetch URL is read from the test
# sheet, not from .env; ``deployments_base_url`` is the ".../deployments" base that
# generate_test_suite appends "/<deployment-id>" to (the id comes from the spec's server
# description) to build that per-suite URL.
_ANYPOINT_KEYS = (
    "token_endpoint",
    "client_id",
    "client_secret",
    "grant_type",
    "deployments_base_url",
)


class LogBackend(StrEnum):
    """Which LogSource implementation the server uses."""

    FILE = "file"
    ANYPOINT = "anypoint"


class Settings(BaseSettings):
    """Server configuration.

    Phase 1-2 only need ``log_backend`` (defaults to the mock file backend) and the file
    path. The target-API and Anypoint fields are declared now so the contract is stable, but
    are unused until Phases 3-4.
    """

    model_config = SettingsConfigDict(
        env_prefix="ALT_",
        env_file=".env",
        extra="ignore",
    )

    # --- Log backend selection (Phase 2) ---
    log_backend: LogBackend = LogBackend.FILE
    file_log_path: str | None = None

    # --- Target API (Phase 3) ---
    base_url: str | None = None
    token_url: str | None = None
    oauth_client_id_env: str = "ALT_OAUTH_CLIENT_ID"
    oauth_client_secret_env: str = "ALT_OAUTH_CLIENT_SECRET"
    oauth_scopes: str | None = None

    # --- Behaviour defaults ---
    # CloudHub takes ~a minute to surface a request's logs, so wait before the first fetch.
    propagation_wait_seconds: float = 60.0
    # If a case's correlation-id logs aren't in the snapshot yet, re-download up to
    # ``log_fetch_max_retries`` more times, waiting ``log_fetch_retry_wait_seconds`` between.
    log_fetch_max_retries: int = 3
    log_fetch_retry_wait_seconds: float = 60.0
    # When True, log validation falls back to the whole snapshot if no lines match the
    # request's correlation id. When False, only correlation-matched lines are considered.
    # (The orchestrated run path forces strict correlation scoping regardless of this flag.)
    log_correlation_fallback: bool = True


class AnypointSettings(BaseSettings):
    """Anypoint platform credentials (+ the log URL, injected from the suite at runtime).

    Credentials are read **without** the ``ALT_`` prefix because the user's ``.env`` uses the
    plain lowercase key names below. Secrets live only here (from env/.env), never in the test
    sheet. ``application_logs_fetch_url`` is **not** read from env — it is populated per run from
    the suite sheet's metadata (see ``tools/logs.build_log_source``); the default ``None`` here
    just keeps the field on the model.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    application_logs_fetch_url: str | None = None
    token_endpoint: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    grant_type: str = "client_credentials"
    # The CloudHub deployments base (".../environments/<ENV>/deployments"); generate_test_suite
    # appends "/<deployment-id>" parsed from the spec's server description to build the per-suite
    # application_logs_fetch_url. Read from .env, not from the sheet.
    deployments_base_url: str | None = None


def get_settings() -> Settings:
    """Load settings from environment / .env."""
    return Settings()


def _read_env_file(path: str = ".env") -> dict[str, str]:
    """Parse a .env file tolerantly, accepting both ``KEY=VALUE`` and ``KEY:VALUE``.

    The separator is the first ``=`` or ``:`` (keys never contain either), so values that
    themselves contain ``:`` (e.g. ``https://...``) are preserved.
    """
    values: dict[str, str] = {}
    file_path = Path(path)
    if not file_path.exists():
        return values
    for raw in file_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        candidates = [i for i in (line.find("="), line.find(":")) if i != -1]
        if not candidates:
            continue
        idx = min(candidates)
        key, value = line[:idx].strip(), line[idx + 1 :].strip()
        if key:
            values[key] = value
    return values


def get_anypoint_settings() -> AnypointSettings:
    """Load Anypoint credentials/URL from environment / .env.

    Real environment variables take precedence over the .env file. The .env is read with a
    tolerant parser so either ``=`` or ``:`` separators work.
    """
    file_vals = _read_env_file()
    merged = {
        key: os.environ.get(key, os.environ.get(key.upper(), file_vals.get(key)))
        for key in _ANYPOINT_KEYS
    }
    return AnypointSettings(**{k: v for k, v in merged.items() if v is not None})
