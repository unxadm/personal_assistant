"""Time-based one-time passwords (RFC 6238, SHA-1, 6 digits, 30 s) and recovery codes.

Implemented on the standard library so the second factor adds no dependency. These are the
parameters every authenticator app supports.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret() -> str:
    """160 random bits, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def counter_at(when: datetime) -> int:
    return int(when.timestamp()) // STEP


def code_at(secret: str, when: datetime) -> str:
    return _hotp(secret, counter_at(when))


def verify(secret: str, code: str, when: datetime, *, after: int = -1, window: int = 1) -> int | None:
    """The counter the code is valid for, or None.

    Accepts the previous and next time step (clock drift). ``after`` is the last counter
    already used: a code is never accepted twice.
    """
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = counter_at(when)
    for counter in range(current - window, current + window + 1):
        if counter > after and hmac.compare_digest(_hotp(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Personal Assistant") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def new_recovery_codes(n: int = 8) -> list[str]:
    """One-time codes for when the phone is gone: ``xxxxx-xxxxx`` (50 bits each)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2))
        for _ in range(n)
    ]


def normalize_recovery(code: str) -> str:
    return code.strip().lower().replace(" ", "")


def hash_recovery(code: str) -> str:
    # High-entropy random values: a plain hash is enough (no slow KDF needed).
    return hashlib.sha256(normalize_recovery(code).encode()).hexdigest()
