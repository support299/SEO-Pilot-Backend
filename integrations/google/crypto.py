"""
AES-256-GCM encryption for OAuth tokens at rest, and HMAC signing for the
OAuth `state` parameter. Deliberately mirrors the scheme in the old Next.js
app's src/lib/google/crypto.ts — same algorithm, same envelope shape
philosophy — just re-implemented in Python. No Django imports: this module
doesn't know or care what web framework calls it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STATE_TTL_SECONDS = 15 * 60


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt_secret(payload: dict, secret: str) -> str:
    key = _derive_key(secret)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, json.dumps(payload).encode("utf-8"), None)
    return _b64url_encode(nonce + ciphertext)


def decrypt_secret(envelope: str, secret: str) -> dict:
    key = _derive_key(secret)
    raw = _b64url_decode(envelope)
    nonce, ciphertext = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


@dataclass
class OAuthStatePayload:
    business_id: str
    user_id: str
    issued_at: float
    nonce: str


def sign_oauth_state(business_id: str, user_id: str, secret: str) -> str:
    payload = {
        "business_id": business_id,
        "user_id": user_id,
        "issued_at": time.time(),
        "nonce": _b64url_encode(os.urandom(9)),
    }
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
    )
