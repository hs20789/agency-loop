"""Host/Origin checks, security headers and the workspace cookie (ARCHITECTURE §7.1)."""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agency_loop.web import views

COOKIE = "al_ws"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", CSP),
)


class SecurityMiddleware:
    """Rejects a foreign Host (400, DNS rebinding) and a cross-site POST/DELETE (403) before routing,
    and adds the security headers (plus no-store on /api/*) to every response."""

    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.hosts = frozenset(allowed_hosts)
        self.origins = frozenset(f"http://{host}" for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = _HEADERS + ((("Cache-Control", "no-store"),) if scope["path"].startswith("/api/") else ())
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in extra:
                    headers[name] = value
            await send(message)

        headers = Headers(scope=scope)
        if headers.get("host") not in self.hosts:
            response = views.error("bad_host", 400)
        elif scope["method"] not in ("GET", "HEAD") and headers.get("origin") not in self.origins:
            response = views.error("bad_origin", 403)
        else:
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:
                # Starlette's own 500 is sent outside this middleware, without these headers. Re-raised for the log.
                if not started:
                    await views.error("internal", 500)(scope, receive, send_with_headers)
                raise
            return
        await response(scope, receive, send_with_headers)


def set_workspace_cookie(response: Response, token: str) -> None:
    # Session cookie: the server-side TTL decides expiry. Not Secure: the MVP is served over plain HTTP.
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")


def clear_workspace_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, httponly=True, samesite="strict", path="/")
