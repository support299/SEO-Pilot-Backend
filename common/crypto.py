"""
AES-256-GCM encryption for any secret-at-rest payload (OAuth tokens,
WordPress application passwords, ...). Originally lived under
integrations/google/ since Google was the first integration that needed it,
but it has no Google-specific logic — every integration that stores a
credential should use this one implementation rather than each rolling its
own. No Django imports: callers pass in the secret key rather than this
module reading settings directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


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
