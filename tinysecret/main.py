"""App assembly: web UI on /, MCP (streamable HTTP) on /mcp, one process."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import constant_time_equals
from .mcp_server import build_mcp
from .store import SecretStore
from .web import WebUI

log = logging.getLogger("tinysecret")
STATIC_DIR = str(Path(__file__).parent / "static")

SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
     b"form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
]


class TinySecretApp:
    """Routes /mcp to the MCP app (behind a Bearer token) and everything else to the web UI."""

    def __init__(self, web: ASGIApp, mcp_app: ASGIApp, mcp_token: str):
        self.web = web
        self.mcp_app = mcp_app
        self.mcp_token = mcp_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.mcp_app(scope, receive, send)  # starts the MCP session manager
            return
        if scope["type"] != "http":
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", [])
                           if h[0].lower() not in {k for k, _ in SECURITY_HEADERS}]
                message["headers"] = headers + SECURITY_HEADERS
            await send(message)

        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            if not self._bearer_ok(scope):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Bearer realm="tinysecret"'})
                await resp(scope, receive, send_with_headers)
                return
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
            await self.mcp_app(scope, receive, send_with_headers)
            return
        await self.web(scope, receive, send_with_headers)

    def _bearer_ok(self, scope: Scope) -> bool:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                value = v.decode("latin-1")
                if value[:7].lower() == "bearer ":
                    return constant_time_equals(value[7:].strip(), self.mcp_token)
        return False


def create_app(*, secrets_dir: str, ui_password: str, mcp_token: str,
               puid: int | None = None, pgid: int | None = None,
               public_url: str = "", cookie_secure: bool = False) -> TinySecretApp:
    if not ui_password:
        raise ValueError("UI_PASSWORD is required.")
    if len(mcp_token) < 16:
        raise ValueError("MCP_TOKEN is required and must be at least 16 characters.")
    store = SecretStore(secrets_dir, puid=puid, pgid=pgid)
    web = WebUI(store, ui_password, cookie_secure=cookie_secure)
    web_app = Starlette(routes=web.routes(STATIC_DIR))
    mcp = build_mcp(store, public_url)
    # Stateless + JSON: no server-side MCP sessions to lose on restart.
    # DNS-rebinding checks are off because every /mcp request needs the Bearer token anyway.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                      json_response=True, host="0.0.0.0")
    app = TinySecretApp(web_app, mcp_app, mcp_token)
    app.store = store  # handy for tests
    app.web_ui = web
    return app


def _int_env(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    return int(v) if v else None


def app_from_env() -> TinySecretApp:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return create_app(
            secrets_dir=os.environ.get("SECRETS_DIR", "/secrets"),
            ui_password=os.environ.get("UI_PASSWORD", ""),
            mcp_token=os.environ.get("MCP_TOKEN", ""),
            puid=_int_env("PUID"),
            pgid=_int_env("PGID"),
            public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
            cookie_secure=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        )
    except ValueError as e:
        log.error("%s", e)
        sys.exit(2)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8790"))
    app = app_from_env()
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=False,
                server_header=False, log_config=None)


if __name__ == "__main__":
    main()
