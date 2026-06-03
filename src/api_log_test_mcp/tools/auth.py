"""get_auth_token — Phase 3 stub.

Will perform the OAuth2 client-credentials flow with in-process caching and
refresh-on-expiry, sourcing the client id/secret from the env vars named in config (never
the test sheet). Not implemented in the start-now slice.
"""

from __future__ import annotations


def get_auth_token() -> str:
    raise NotImplementedError(
        "get_auth_token is a Phase 3 stub (OAuth2 client-credentials); blocked on Gate-0 "
        "OAuth details."
    )
