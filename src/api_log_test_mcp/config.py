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

_ANYPOINT_KEYS = (
    "application_logs_fetch_url",
    "token_endpoint",
    "client_id",
    "client_secret",
    "grant_type",
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
    propagation_wait_seconds: float = 10.0
    # When True, log validation falls back to the whole snapshot if no lines match the
    # request's correlation id. When False, only correlation-matched lines are considered.
    log_correlation_fallback: bool = True


class AnypointSettings(BaseSettings):
    """Anypoint platform credentials + log URL.

    These are read **without** the ``ALT_`` prefix because the user's ``.env`` uses the plain
    lowercase key names below. Secrets live only here (from env/.env), never in the test sheet.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    application_logs_fetch_url: str | None = None
    token_endpoint: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    grant_type: str = "client_credentials"


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
