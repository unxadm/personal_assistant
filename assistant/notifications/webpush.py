"""Web Push channel (RFC 8030 delivery, RFC 8291 encryption, RFC 8292 VAPID).

Implemented directly on ``cryptography`` + ``httpx``. Payloads are end-to-end encrypted
for the browser, so the push service (Google, Mozilla, Apple) cannot read them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select

from ..core.config import AppConfig
from ..core.db import Database
from ..core.models import PushSubscription
from ..core.security import SecretBox
from ..core.settings_store import SettingsStore
from ..plugins.sdk import Notification
from .service import Channel, ChannelError

log = logging.getLogger(__name__)

VAPID_KEY = "webpush.vapid_private_key"
MAX_BODY = 1200
TTL_SECONDS = 24 * 3600


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt_payload(payload: bytes, p256dh: bytes, auth: bytes) -> bytes:
    """Encrypt ``payload`` for a subscription (aes128gcm content coding, one record)."""
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    sender = ec.generate_private_key(ec.SECP256R1())
    sender_public = _public_bytes(sender.public_key())
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + p256dh + sender_public, 32)
    salt = os.urandom(16)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(key).encrypt(nonce, payload + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(sender_public)]) + sender_public
    return header + ciphertext


def vapid_header(private_key: ec.EllipticCurvePrivateKey, endpoint: str, contact: str) -> str:
    """``Authorization`` header value proving the push comes from this server."""
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 3600,
        "sub": contact,
    }
    signing_input = (
        b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        + "."
        + b64u(json.dumps(claims, separators=(",", ":")).encode())
    )
    r, s = decode_dss_signature(private_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    public = b64u(_public_bytes(private_key.public_key()))
    return f"vapid t={signing_input}.{b64u(signature)}, k={public}"


class VapidKeys:
    """The server's VAPID key pair: one for everyone, created on first use."""

    def __init__(self, settings: SettingsStore):
        self._settings = settings
        self._key: ec.EllipticCurvePrivateKey | None = None

    async def private_key(self) -> ec.EllipticCurvePrivateKey:
        if self._key is None:
            pem = await self._settings.get(VAPID_KEY)
            if pem is None:
                key = ec.generate_private_key(ec.SECP256R1())
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode()
                await self._settings.set(VAPID_KEY, pem, secret=True)
            loaded = serialization.load_pem_private_key(pem.encode(), password=None)
            assert isinstance(loaded, ec.EllipticCurvePrivateKey)
            self._key = loaded
        return self._key

    async def public_key(self) -> str:
        """The ``applicationServerKey`` browsers subscribe with."""
        return b64u(_public_bytes((await self.private_key()).public_key()))


class WebPushChannel(Channel):
    """One user's devices. A browser belongs to one account at a time: subscribing from it
    while signed in as someone else moves it, so the previous user's alerts stop going there."""

    name = "web_push"
    label = "Browser / phone push"

    def __init__(
        self,
        db: Database,
        box: SecretBox,
        vapid: VapidKeys,
        http: httpx.AsyncClient,
        get_config: Callable[[], AppConfig],
        user_id: int,
    ):
        self._db = db
        self._box = box
        self._vapid = vapid
        self._http = http
        self._get_config = get_config
        self._user_id = user_id

    # -- keys and subscriptions -------------------------------------------------------

    async def public_key(self) -> str:
        return await self._vapid.public_key()

    async def subscribe(self, subscription: dict[str, Any], label: str = "") -> None:
        endpoint = str(subscription.get("endpoint") or "")
        keys = subscription.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("not a valid push subscription")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64u_decode(keys["p256dh"]))
        except Exception:
            raise ValueError("not a valid push subscription") from None
        data = self._box.encrypt(
            json.dumps({"endpoint": endpoint, "p256dh": keys["p256dh"], "auth": keys["auth"]})
        )
        digest = hashlib.sha256(endpoint.encode()).hexdigest()
        async with self._db.session() as s:
            row = await s.scalar(select(PushSubscription).where(PushSubscription.endpoint_hash == digest))
            if row is None:
                s.add(
                    PushSubscription(
                        user_id=self._user_id, endpoint_hash=digest, data=data, label=label[:200]
                    )
                )
            else:
                row.user_id, row.data, row.label = self._user_id, data, label[:200]
            await s.commit()

    async def unsubscribe(self, endpoint: str) -> None:
        digest = hashlib.sha256(endpoint.encode()).hexdigest()
        async with self._db.session() as s:
            await s.execute(
                delete(PushSubscription).where(
                    PushSubscription.user_id == self._user_id,
                    PushSubscription.endpoint_hash == digest,
                )
            )
            await s.commit()

    async def _subscriptions(self) -> list[tuple[int, dict[str, str]]]:
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(PushSubscription).where(PushSubscription.user_id == self._user_id)
                )
            ).all()
        return [(r.id, json.loads(self._box.decrypt(r.data))) for r in rows]

    async def devices(self) -> list[dict[str, Any]]:
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(PushSubscription)
                    .where(PushSubscription.user_id == self._user_id)
                    .order_by(PushSubscription.id)
                )
            ).all()
        return [{"id": r.id, "label": r.label, "endpoint_hash": r.endpoint_hash} for r in rows]

    # -- channel ----------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        if not self._get_config().notifications.web_push.enabled:
            return {"ready": False, "detail": "turned off"}
        async with self._db.session() as s:
            count = len(
                (
                    await s.scalars(
                        select(PushSubscription.id).where(
                            PushSubscription.user_id == self._user_id
                        )
                    )
                ).all()
            )
        if not count:
            return {"ready": False, "detail": "no device has notifications turned on"}
        return {"ready": True, "detail": f"{count} device{'s' if count != 1 else ''}"}

    async def send(self, notification: Notification) -> None:
        cfg = self._get_config()
        contact = (
            cfg.notifications.web_push.contact
            or cfg.server.public_url
            or "mailto:webpush@localhost.invalid"
        )
        key = await self._vapid.private_key()
        payload = json.dumps(
            {
                "id": notification.id,
                "title": notification.title[:200],
                "body": notification.body[:MAX_BODY],
                "url": notification.url,
                "ref": notification.ref,
                "actions": notification.actions[:2],
                "urgency": notification.urgency,
            }
        ).encode()
        sent, errors, gone = 0, [], []
        for sub_id, sub in await self._subscriptions():
            body = encrypt_payload(payload, b64u_decode(sub["p256dh"]), b64u_decode(sub["auth"]))
            try:
                response = await self._http.post(
                    sub["endpoint"],
                    content=body,
                    headers={
                        "Authorization": vapid_header(key, sub["endpoint"], contact),
                        "Content-Encoding": "aes128gcm",
                        "Content-Type": "application/octet-stream",
                        "TTL": str(TTL_SECONDS),
                        "Urgency": "high" if notification.urgency == "high" else "normal",
                    },
                    timeout=20.0,
                )
            except httpx.HTTPError as e:
                errors.append(type(e).__name__)
                continue
            if response.status_code in (404, 410):
                gone.append(sub_id)  # the browser unsubscribed
            elif response.status_code >= 400:
                errors.append(f"HTTP {response.status_code}")
            else:
                sent += 1
        if gone:
            async with self._db.session() as s:
                await s.execute(
                    delete(PushSubscription).where(
                        PushSubscription.user_id == self._user_id, PushSubscription.id.in_(gone)
                    )
                )
                await s.commit()
        if not sent:
            raise ChannelError("; ".join(errors) or "no active device")
