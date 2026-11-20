"""Secrets: API keys and the bootstrap admin token.

Only a BLAKE2b-256 digest of a 256-bit secret is ever stored, so the control database is not a
list of usable credentials. Lookup is by digest equality (the secret carries enough entropy that a
KDF would only slow down the legitimate path, which is a hash lookup, not a comparison), and the
plaintext is returned exactly once, at creation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

API_KEY_PREFIX = "okg_"
ADMIN_TOKEN_PREFIX = "okga_"
_KEY_BYTES = 32  # 256 bits of entropy -> 43 url-safe characters, trimmed to 40


def _urlsafe_token(prefix: str, nbytes: int, trim: int) -> str:
    return prefix + secrets.token_urlsafe(nbytes)[:trim]


def generate_api_key() -> str:
    """A fresh API key: ``okg_`` plus 40 URL-safe characters of 256-bit secret."""

    return _urlsafe_token(API_KEY_PREFIX, _KEY_BYTES, 40)


def generate_admin_token() -> str:
    return _urlsafe_token(ADMIN_TOKEN_PREFIX, _KEY_BYTES, 32)


def hash_secret(secret: str) -> bytes:
    """The stored digest of a secret."""

    return hashlib.blake2b(secret.encode("utf-8"), digest_size=32).digest()


def fingerprint(secret: str) -> str:
    """A short, safe identifier for a key, safe to show in a list."""

    return hash_secret(secret)[:4].hex()


def display_prefix(secret: str) -> str:
    return secret[: len(API_KEY_PREFIX) + 6] + "…"


KEY_SHAPE = re.compile(r"^" + API_KEY_PREFIX + r"[A-Za-z0-9_-]{40}$")


def looks_like_api_key(candidate: str) -> bool:
    """Shape check only: it lets us refuse a non-Grafx credential without a lookup."""

    return bool(KEY_SHAPE.match(candidate or ""))


def constant_equals(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)
