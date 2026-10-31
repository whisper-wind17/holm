"""Normalize Anthropic Base URL aliases before protocol and ledger selection."""

from starlette.types import ASGIApp, Receive, Scope, Send


class AnthropicPathAliasMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in {"/v1/v1/messages", "/v1/v1/models"} or path.startswith("/v1/v1/messages/"):
                scope = dict(scope)
                scope["path"] = path[3:]
                # Keep encoded suffixes intact; the query string is a separate ASGI field.
                scope["raw_path"] = scope.get("raw_path", path.encode("utf-8"))[3:]
        await self._app(scope, receive, send)
