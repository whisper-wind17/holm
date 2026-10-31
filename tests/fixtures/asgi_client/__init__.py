"""An in-process ASGI client for tests, with no HTTP client dependency.

Starlette's own ``TestClient`` requires ``httpx``, which TDD 4.1 lists only as
the Gateway's Runtime HTTP client, not as part of the application test stack
(M1-01A). Nothing about these tests
needs a real socket: the ASGI application is a coroutine, so the client below
speaks the ASGI protocol to it directly.

It deliberately keeps the exception that Starlette's server error middleware
re-raises after responding, so a test can assert both halves of the contract:
the caller received a sanitized body, and the process still saw the failure.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    MutableMapping,
)
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
_CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"
_CSRF_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

type Message = MutableMapping[str, Any]
type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
# The ASGI application itself is annotated as a coroutine function rather than
# a plain awaitable so that asyncio.create_task accepts it without a cast.
type AsgiApp = Callable[[Scope, Receive, Send], Coroutine[Any, Any, None]]


class NoConfiguredCredentialRows:
    """Explicit test composition for databases known to contain no credential rows."""

    async def check(self) -> None:
        return None


NO_CONFIGURED_CREDENTIAL_ROWS: Final[NoConfiguredCredentialRows] = NoConfiguredCredentialRows()


class LifespanError(RuntimeError):
    """The application did not complete the ASGI lifespan handshake."""


class SessionCookies(dict[str, str]):
    """Cookies from a test login, including the CSRF value its client must mirror."""

    __slots__ = ("csrf_token",)

    def __init__(self, values: Mapping[str, str], *, csrf_token: str) -> None:
        super().__init__(values)
        self.csrf_token = csrf_token


@dataclass(frozen=True, slots=True)
class AsgiResponse:
    """One complete HTTP response captured from the ASGI send channel."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    server_error: BaseException | None = field(default=None)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.text)


class AsgiStreamResponse:
    """An open response whose body can be read incrementally and disconnected."""

    __slots__ = (
        "_closed",
        "_disconnect",
        "_messages",
        "_task",
        "_timeout_seconds",
        "headers",
        "status",
    )

    def __init__(
        self,
        *,
        start: Message,
        messages: asyncio.Queue[Message],
        disconnect: asyncio.Event,
        task: asyncio.Task[None],
        timeout_seconds: float,
    ) -> None:
        self.status = int(start["status"])
        self.headers = tuple(
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in start.get("headers", ())
        )
        self._messages = messages
        self._disconnect = disconnect
        self._task = task
        self._timeout_seconds = timeout_seconds
        self._closed = False

    async def read_chunk(self) -> bytes:
        """Read exactly one ASGI body message without collecting the stream."""
        message = await asyncio.wait_for(self._messages.get(), self._timeout_seconds)
        if message["type"] != "http.response.body":
            raise RuntimeError(f"unexpected streaming ASGI message: {message['type']}")
        return bytes(message.get("body", b""))

    async def close(self) -> None:
        """Actively signal client disconnect and stop the application task."""
        if self._closed:
            return
        self._closed = True
        self._disconnect.set()
        try:
            await asyncio.wait_for(self._task, self._timeout_seconds)
        except TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def __aenter__(self) -> AsgiStreamResponse:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()


class AsgiTestClient:
    """Drive an ASGI application in the current Event Loop."""

    __slots__ = ("_app", "_timeout_seconds")

    def __init__(self, app: AsgiApp, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._app = app
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the application's startup and shutdown handlers."""
        to_app: asyncio.Queue[Message] = asyncio.Queue()
        from_app: asyncio.Queue[Message] = asyncio.Queue()

        async def receive() -> Message:
            return await to_app.get()

        async def send(message: Message) -> None:
            await from_app.put(message)

        scope: Scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
        }
        task = asyncio.create_task(self._app(scope, receive, send))
        try:
            await self._handshake(task, to_app, from_app, "startup")
            yield
            await self._handshake(task, to_app, from_app, "shutdown")
            await asyncio.wait_for(task, self._timeout_seconds)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _handshake(
        self,
        task: asyncio.Task[None],
        to_app: asyncio.Queue[Message],
        from_app: asyncio.Queue[Message],
        phase: str,
    ) -> None:
        await to_app.put({"type": f"lifespan.{phase}"})
        message = await asyncio.wait_for(from_app.get(), self._timeout_seconds)
        if message["type"] != f"lifespan.{phase}.complete":
            raise LifespanError(f"lifespan {phase} failed: {message['type']}")

    async def get(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> AsgiResponse:
        """Perform one GET request against the application."""
        return await self.request("GET", path, headers=headers, cookies=cookies)

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> AsgiResponse:
        """Perform one request and collect the complete response."""
        body = b"" if json_body is None else json.dumps(dict(json_body)).encode("utf-8")
        effective_headers = {} if headers is None else dict(headers)
        if (
            isinstance(cookies, SessionCookies)
            and method.upper() not in _CSRF_SAFE_METHODS
            and not any(
                name.casefold() == _CSRF_HEADER_NAME.casefold() for name in effective_headers
            )
        ):
            effective_headers[_CSRF_HEADER_NAME] = cookies.csrf_token
        if json_body is not None:
            effective_headers["content-type"] = "application/json"
        scope = _http_scope(method, path, headers=effective_headers, cookies=cookies)

        messages: list[Message] = []
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)

        server_error: BaseException | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._app(scope, receive, send)
        except Exception as error:  # noqa: BLE001 - the caller asserts on it
            # Starlette's ServerErrorMiddleware sends the response and then
            # re-raises so the server can log the failure. Keeping the exception
            # here is what lets a test prove the response stayed sanitized.
            server_error = error

        return _build_response(messages, server_error)

    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> AsgiStreamResponse:
        """Open a bounded incremental response that the caller must close."""
        scope = _http_scope(method, path, headers=headers, cookies=cookies)
        messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=4)
        disconnect = asyncio.Event()
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            await messages.put(message)

        task = asyncio.create_task(self._app(scope, receive, send))
        try:
            start = await asyncio.wait_for(messages.get(), self._timeout_seconds)
            if start["type"] != "http.response.start":
                raise RuntimeError(f"unexpected first streaming ASGI message: {start['type']}")
            return AsgiStreamResponse(
                start=start,
                messages=messages,
                disconnect=disconnect,
                task=task,
                timeout_seconds=self._timeout_seconds,
            )
        except BaseException:
            disconnect.set()
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise


def _http_scope(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None,
    cookies: Mapping[str, str] | None,
) -> Scope:
    parsed = urlsplit(path)
    encoded_headers: list[tuple[bytes, bytes]] = [(b"host", b"asgi-test-client")]
    if headers is not None:
        encoded_headers.extend(
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
        )
    if cookies:
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        encoded_headers.append((b"cookie", cookie_value.encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("utf-8"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": encoded_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("asgi-test-client", 80),
        "state": {},
    }


def _build_response(messages: list[Message], server_error: BaseException | None) -> AsgiResponse:
    start = next(
        (message for message in messages if message["type"] == "http.response.start"),
        None,
    )
    if start is None:
        if server_error is not None:
            raise server_error
        raise RuntimeError("the application sent no http.response.start message")

    body = b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message["type"] == "http.response.body"
    )
    headers = tuple(
        (name.decode("latin-1"), value.decode("latin-1"))
        for name, value in start.get("headers", ())
    )
    return AsgiResponse(
        status=int(start["status"]),
        headers=headers,
        body=body,
        server_error=server_error,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "AsgiApp",
    "AsgiResponse",
    "AsgiStreamResponse",
    "AsgiTestClient",
    "LifespanError",
    "SessionCookies",
]
