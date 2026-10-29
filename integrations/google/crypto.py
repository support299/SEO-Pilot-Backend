"""
HMAC signing for the OAuth `state` parameter. No Django imports: this module
doesn't know or care what web framework calls it.

encrypt_secret/decrypt_secret used to live here but have no Google-specific
logic, so they moved to common/crypto.py (shared by every integration that
stores a credential, e.g. WordPress). Re-exported below so existing imports
(`from integrations.google.crypto import encrypt_secret`) keep working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from common.crypto import decrypt_secret, encrypt_secret

__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "OAuthStatePayload",
    "sign_oauth_state",
    "verify_oauth_state",
    "InvalidOAuthState",
]

STATE_TTL_SECONDS = 15 * 60


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


@dataclass
class OAuthStatePayload:
    business_id: str
    user_id: str
    issued_at: float
    nonce: str
    redirect_uri: str | None = None
    popup: bool = False  # the login ran in a popup (e.g. inside a GoHighLevel iframe)


def sign_oauth_state(business_id: str, user_id: str, secret: str, redirect_uri: str | None = None, popup: bool = False) -> str:
    payload = {
        "business_id": business_id,
        "user_id": user_id,
        "issued_at": time.time(),
        "nonce": _b64url_encode(os.urandom(9)),
    }
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    if popup:
        payload["popup"] = True
    body = _b64url_encode(json.dumps(payload).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


class InvalidOAuthState(Exception):
    pass


def verify_oauth_state(state: str, secret: str) -> OAuthStatePayload:
    try:
        body, signature = state.split(".", 1)
    except ValueError as exc:
        raise InvalidOAuthState("Malformed connection state.") from exc

    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_decode(signature), expected):
        raise InvalidOAuthState("Connection state signature is invalid.")

    payload = json.loads(_b64url_decode(body))
    if time.time() - payload["issued_at"] > STATE_TTL_SECONDS:
        raise InvalidOAuthState("Connection state expired — please try connecting again.")

    return OAuthStatePayload(
        business_id=payload["business_id"],
        user_id=payload["user_id"],
        issued_at=payload["issued_at"],
        nonce=payload["nonce"],
        redirect_uri=payload.get("redirect_uri"),
        popup=bool(payload.get("popup")),
    )
