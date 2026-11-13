"""HTTP hardening: security headers on every response, and a same-origin check on
state-changing API requests.

The session cookie is SameSite=Strict, which already stops cross-*site* requests. Other
ports and subdomains of the same site still count as "same site", so unsafe API requests
are also required to come from this origin.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(self), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Called by other servers (Google push, plugin webhooks); authenticated by their own token.
WEBHOOK = re.compile(r"^/api/plugins/[^/]+/webhook$")


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def _cross_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return True
    origin = request.headers.get("origin")
    if not origin:
        return False  # not a browser (curl, scripts): the cookie rules still apply
    allowed = {request.headers.get("host", "")}
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        allowed.add(forwarded.split(",")[0].strip())
    public = request.app.state.c.config.config.server.public_url
    if public:
        allowed.add(urlsplit(public).netloc)
    return urlsplit(origin).netloc not in allowed


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def harden(request: Request, call_next):
        path = request.url.path
        if (
            request.method in UNSAFE
            and path.startswith("/api/")
            and not WEBHOOK.match(path)
            and _cross_origin(request)
        ):
            response = JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
        else:
            response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if is_https(request):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response
