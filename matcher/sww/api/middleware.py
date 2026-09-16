"""Loopback-only guard: reject drive-by writes, DNS rebinding and huge bodies."""
from __future__ import annotations

from fastapi.responses import JSONResponse

from .. import config


class BodyTooLarge(Exception):
    pass


class LocalOnlyMiddleware:
    """A page on any other localhost port shares this one's cookie jar, so the
    service checks Host, Origin and an explicit client header rather than
    assuming "local means trusted"."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode().lower()
        origin = headers.get(b"origin", b"").decode()
        mutation = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if host not in config.ALLOWED_HOSTS or (origin and origin not in config.ALLOWED_ORIGINS):
            return await JSONResponse({"detail": "Only the local SWW page can access this service."}, 403)(scope, receive, send)
        if mutation and headers.get(b"x-sww-client") != b"1":
            return await JSONResponse({"detail": "Missing X-SWW-Client header."}, 403)(scope, receive, send)
        try:
            if int(headers.get(b"content-length", b"0")) > config.MAX_BODY_BYTES:
                raise BodyTooLarge
        except (ValueError, BodyTooLarge):
            return await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)

        size = 0
        too_large = False
        sent_error = False

        async def limited_receive():
            nonlocal size, too_large
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > config.MAX_BODY_BYTES:
                    too_large = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message):
            nonlocal sent_error
            if too_large:
                if not sent_error:
                    sent_error = True
                    await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ])
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:
            if not sent_error:
                await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)
