"""Transport layer: one HTTP request in, one response out.

Fetchers know nothing about robots.txt, throttling, retries or redirects; the
session layer (session.py) handles those. A fetch is always a single hop.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Self

import httpx

from shopify_spy.utils import canonicalize_url

# Abort downloads larger than this (decoded bytes); warn above WARN_SIZE
MAX_SIZE = 1024 * 1024 * 1024
WARN_SIZE = 32 * 1024 * 1024

# Only these are advertised, so anything else in Content-Encoding is unexpected
ACCEPT_ENCODING = "gzip, deflate"


@dataclass(frozen=True)
class Identity:
    """What a request presents itself as: a user agent and matching headers."""

    user_agent: str | None
    headers: Mapping[str, str] = field(default_factory=dict)
    impersonate: str | None = None  # for TLS-impersonating transports later


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    identity: Identity
    timeout: float
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    cookies: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    latency: float = 0.0  # seconds to response headers
    set_cookies: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


@dataclass(frozen=True)
class FetcherCapabilities:
    applies_identity_headers: bool = True  # False for browsers that bring their own
    can_impersonate: bool = False
    renders_js: bool = False


class FetchError(Exception):
    """A request failed below HTTP: no usable response was received."""


class FetchTimeout(FetchError):
    pass


class FetchConnectionError(FetchError):
    """DNS failure, refused or reset connection."""


class FetchTLSError(FetchConnectionError):
    """Certificate verification failed."""


class FetchTooLarge(FetchError):
    pass


class FetchIncomplete(FetchError):
    """The connection closed before the body was complete."""


class FetchBadEncoding(FetchError):
    """The response used a Content-Encoding that wasn't requested."""


class Fetcher(Protocol):
    capabilities: FetcherCapabilities

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def fetch(self, request: Request) -> Response: ...


FetcherFactory = Callable[[], Fetcher]


def decompress(body: bytes, wbits: int, max_size: int = MAX_SIZE) -> bytes:
    """Inflate zlib/gzip/raw-deflate data, refusing output larger than max_size."""
    d = zlib.decompressobj(wbits)
    out = d.decompress(body, max_size + 1)
    if len(out) > max_size or d.unconsumed_tail:
        raise FetchTooLarge(f"decompressed body exceeded {max_size} bytes")
    return out


def _decode(body: bytes, encoding: str) -> bytes:
    """Decode a gzip/deflate body; raise FetchBadEncoding for anything else."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "x-gzip"):
        return decompress(body, zlib.MAX_WBITS | 16)
    if encoding == "deflate":
        try:
            return decompress(body, zlib.MAX_WBITS)
        except zlib.error:
            return decompress(body, -zlib.MAX_WBITS)
    raise FetchBadEncoding(f"unsupported Content-Encoding: {encoding}")


def _is_tls_error(exc: BaseException) -> bool:
    text = repr(exc).lower()
    return "certificate" in text or "ssl" in text


class HttpxFetcher:
    """Default transport: httpx over HTTP/1.1, one hop per fetch."""

    capabilities = FetcherCapabilities()

    def __init__(self, *, verify: bool = True, trust_env: bool = True) -> None:
        self._verify = verify
        self._trust_env = trust_env
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            http1=True,
            http2=False,
            verify=self._verify,
            trust_env=self._trust_env,
            follow_redirects=False,
            headers={"Accept-Encoding": ACCEPT_ENCODING},
            timeout=None,  # a total deadline is enforced per fetch instead
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, request: Request) -> Response:
        assert self._client is not None, "use HttpxFetcher as an async context manager"
        headers = {**request.identity.headers, **request.headers}
        if request.identity.user_agent:
            headers["User-Agent"] = request.identity.user_agent
        try:
            async with asyncio.timeout(request.timeout):
                return await self._fetch(request, headers)
        except TimeoutError as exc:
            raise FetchTimeout(f"no complete response within {request.timeout}s") from exc

    async def _fetch(self, request: Request, headers: dict[str, str]) -> Response:
        assert self._client is not None
        start = time.monotonic()
        req = self._client.build_request(
            request.method,
            request.url,
            headers=headers,
            content=request.body,
            cookies=dict(request.cookies) or None,
        )
        try:
            resp = await self._client.send(req, stream=True)
        except httpx.TimeoutException as exc:
            raise FetchTimeout(str(exc) or "timeout") from exc
        except httpx.TransportError as exc:
            if _is_tls_error(exc):
                raise FetchTLSError(str(exc) or repr(exc)) from exc
            if isinstance(exc, httpx.RemoteProtocolError):
                raise FetchIncomplete(str(exc) or repr(exc)) from exc
            raise FetchConnectionError(str(exc) or repr(exc)) from exc
        latency = time.monotonic() - start
        try:
            chunks = []
            size = 0
            async for chunk in resp.aiter_raw():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_SIZE:
                    raise FetchTooLarge(f"response exceeded {MAX_SIZE} bytes")
            raw = b"".join(chunks)
        except httpx.TimeoutException as exc:
            raise FetchTimeout(str(exc) or "timeout") from exc
        except httpx.TransportError as exc:
            raise FetchIncomplete(str(exc) or repr(exc)) from exc
        finally:
            await resp.aclose()
        try:
            body = _decode(raw, resp.headers.get("content-encoding", ""))
        except zlib.error as exc:
            raise FetchIncomplete(f"corrupt compressed body: {exc}") from exc
        return Response(
            url=str(resp.url),
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            body=body,
            latency=latency,
            set_cookies={c.name: c.value or "" for c in resp.cookies.jar},
        )


# --- Test and parity transports -----------------------------------------------------------


class Seq(list):
    """A sequence of FakeFetcher specs served in order; the last one repeats."""


class FakeFetcher:
    """In-memory transport for tests.

    ``routes`` maps a URL (canonicalized) or a ``(method, url)`` key to a response
    spec, a ``Seq`` of specs consumed in order, or a callable taking the Request
    (sync or async). A spec is a Response, an exception instance to raise, a
    ``(status, body)`` / ``(status, body, headers)`` tuple, or a dict/list (served
    as JSON with status 200). Unknown URLs get ``default_status``.
    """

    capabilities = FetcherCapabilities()

    def __init__(self, routes: Mapping[Any, Any] | None = None, default_status: int = 404):
        self.routes: dict[str, Any] = {}
        for key, spec in (routes or {}).items():
            method, url = key if isinstance(key, tuple) else ("GET", key)
            self.routes[f"{method} {canonicalize_url(url)}"] = spec
        self.default_status = default_status
        self.requests: list[Request] = []
        self._calls: dict[str, int] = defaultdict(int)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    def count(self, url: str, method: str = "GET") -> int:
        return self._calls[f"{method} {canonicalize_url(url)}"]

    async def fetch(self, request: Request) -> Response:
        self.requests.append(request)
        key = f"{request.method} {canonicalize_url(request.url)}"
        index = self._calls[key]
        self._calls[key] += 1
        spec = self.routes.get(key)
        if spec is None:
            return Response(request.url, self.default_status, {}, b"")
        if isinstance(spec, Seq):
            spec = spec[min(index, len(spec) - 1)]
        if callable(spec) and not isinstance(spec, type):
            spec = spec(request)
            if asyncio.iscoroutine(spec):
                spec = await spec
        return _to_response(request, spec)


def _to_response(request: Request, spec: Any) -> Response:
    if isinstance(spec, Response):
        return spec
    if isinstance(spec, BaseException):
        raise spec
    if isinstance(spec, tuple):
        status, body, *rest = spec
        headers = {k.lower(): v for k, v in (rest[0] if rest else {}).items()}
        if isinstance(body, str):
            body = body.encode()
        elif not isinstance(body, bytes):
            body = json.dumps(body).encode()
        return Response(request.url, status, headers, body)
    if isinstance(spec, (dict, list)):
        return Response(
            request.url, 200, {"content-type": "application/json"}, json.dumps(spec).encode()
        )
    raise TypeError(f"bad FakeFetcher spec: {spec!r}")


# Recorded transport errors, by name, shared with the Scrapy parity middlewares
RECORDED_ERRORS: dict[str, type[FetchError]] = {
    "timeout": FetchTimeout,
    "connection": FetchConnectionError,
    "dns": FetchConnectionError,
    "tls": FetchTLSError,
    "incomplete": FetchIncomplete,
}


def load_corpus(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load a recorded corpus: {"METHOD canonical-url": [outcome, ...]}."""
    raw = Path(path).read_bytes()
    if str(path).endswith(".gz"):
        raw = gzip.decompress(raw)
    return json.loads(raw)["entries"]


class ReplayFetcher:
    """Serves responses from a recorded corpus (see shopify_spy._parity).

    Each key's outcomes are replayed in order; the last one repeats. Outcomes are
    raw as recorded, so bodies are decoded here like HttpxFetcher does.
    """

    capabilities = FetcherCapabilities()

    def __init__(self, corpus: Mapping[str, list[dict[str, Any]]]):
        self.corpus = corpus
        self.misses: list[str] = []
        self._calls: dict[str, int] = defaultdict(int)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def fetch(self, request: Request) -> Response:
        key = f"{request.method} {canonicalize_url(request.url)}"
        outcomes = self.corpus.get(key)
        if not outcomes:
            self.misses.append(key)
            return Response(request.url, 404, {"x-replay-miss": "1"}, b"")
        index = self._calls[key]
        self._calls[key] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if "error" in outcome:
            raise RECORDED_ERRORS.get(outcome["error"], FetchConnectionError)(outcome["error"])
        headers = {k.lower(): v for k, v in outcome["headers"].items()}
        body = _decode(base64.b64decode(outcome["body"]), headers.get("content-encoding", ""))
        return Response(request.url, outcome["status"], headers, body)
