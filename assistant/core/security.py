"""Encryption at rest, password hashing, session tokens, and log redaction."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken

_hasher = PasswordHasher()


class SecretBox:
    """Symmetric encryption for secrets stored in the database.

    The key comes from ``PA_SECRET_KEY`` (any string; hashed into a key) or, if unset,
    from ``<data_dir>/secret.key`` which is generated on first run with mode 0600.
    """

    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_env_or_file(cls, data_dir: Path, env_value: str | None = None) -> SecretBox:
        env_value = env_value if env_value is not None else os.environ.get("PA_SECRET_KEY")
        if env_value:
            digest = hashlib.sha256(env_value.encode()).digest()
            return cls(base64.urlsafe_b64encode(digest))
        key_file = data_dir / "secret.key"
        if key_file.exists():
            return cls(key_file.read_bytes().strip())
        data_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("cannot decrypt secret: wrong PA_SECRET_KEY or secret.key?") from e


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (Argon2Error, InvalidHashError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mask_secret(value: str | None) -> str | None:
    """Representation of a secret that is safe to show in the UI."""
    if not value:
        return None
    return "…" + value[-4:] if len(value) > 8 else "…"


_REDACTIONS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret|authorization)\"?\s*[:=]\s*\"?)[^\s\",}]{4,}"),
    re.compile(r"()\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(/bot)\d+:[A-Za-z0-9_\-]{20,}"),  # Telegram bot token in a URL
    # Query strings in access logs: webhook tokens, OAuth codes and state.
    re.compile(r"(?i)([?&](?:token|code|state|key|access_token|refresh_token)=)[^&\s\"']+"),
]


def redact(text: str) -> str:
    for pattern in _REDACTIONS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs credentials from log records before they are emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    # httpx logs full request URLs at INFO; keep it quieter.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def restrict_permissions(data_dir: Path) -> None:
    """Keep the data directory (database, key, uploads) private to the service user."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        key = data_dir / "secret.key"
        if key.exists():
            os.chmod(key, 0o600)
    except OSError:
        logging.getLogger(__name__).warning("could not restrict permissions on %s", data_dir)
