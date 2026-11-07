"""Origin, token, and request-size checks for the loopback API."""
import hmac
from typing import Any

from fastapi.responses import JSONResponse

from supertravel_server.conversations import MAX_BYTES

ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]


class LocalAccessMiddleware:
    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin-1") not in ALLOWED_ORIGINS:
            await JSONResponse(status_code=403, content={"detail": {"code": "origin_denied", "message": "请求来源不受允许。"}})(scope, receive, send)
            return
        public_health = scope["path"] == "/api/health" and scope["method"] == "GET"
        authorization = headers.get(b"authorization", b"")
        if not public_health and not hmac.compare_digest(authorization, b"Bearer " + self.token):
            await JSONResponse(status_code=401, headers={"WWW-Authenticate": "Bearer"}, content={"detail": {"code": "unauthorized", "message": "需要有效的本地应用令牌。"}})(scope, receive, send)
            return
        if scope["method"] in {"POST", "PUT"}:
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BYTES:
                    await JSONResponse(status_code=413, content={"detail": {"code": "request_too_large", "message": "请求超过 2 MiB 限制。"}})(scope, receive, send)
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            delivered = False

            async def replay_body() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay_body, send)
            return
        await self.app(scope, receive, send)
