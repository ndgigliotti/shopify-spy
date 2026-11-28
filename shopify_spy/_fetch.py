"""Transport layer: one HTTP request in, one response out.

Fetchers know nothing about robots.txt, throttling, retries or redirects; the
session layer (session.py) handles those. A fetch is always a single hop.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import http.cookiejar
import ipaddress
import json
import logging
import re
import ssl
import urllib.parse
import urllib.request
import warnings
import zlib
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self

from shopify_spy._utils import canonicalize_url

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("shopify_spy")

# Abort responses larger than this, raw or decompressed; warn above WARN_SIZE
MAX_SIZE = 1024 * 1024 * 1024
WARN_SIZE = 32 * 1024 * 1024

# Only these are advertised, so anything else in Content-Encoding is unexpected
ACCEPT_ENCODING = "gzip, deflate"

# The schemes httpx.Proxy accepts
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_ENV_PROXY_PREFIX = "proxy from the environment (HTTPS_PROXY, HTTP_PROXY or ALL_PROXY): "


@dataclass(frozen=True)
class Identity:
    """What a request presents itself as: a user agent and matching headers."""

    user_agent: str | None
    headers: Mapping[str, str] = field(default_factory=dict)
    # A curl_cffi browser profile, for transports that impersonate (CurlCffiFetcher)
    impersonate: str | None = None

    @property
    def name(self) -> str:
        """The profile, else the user agent: what a refusal is remembered by."""
        return self.impersonate or self.user_agent or ""


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    identity: Identity
    timeout: float
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    # Seconds to response headers (CurlCffiFetcher: to the first body byte, or to
    # the complete response when there is no body; curl_cffi has no headers hook)
    latency: float = 0.0

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body)

    def json_or_none(self) -> Any:
        """The body as JSON, or None if it isn't JSON."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None

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


class FetchInvalidURL(FetchError):
    """The URL can't be requested (control characters, a bad host, ...)."""


class Fetcher(Protocol):
    capabilities: FetcherCapabilities

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def fetch(self, request: Request) -> Response: ...


FetcherFactory = Callable[[], Fetcher]


def decompress(body: bytes, wbits: int, max_size: int | None = None) -> bytes:
    """Inflate zlib/gzip/raw-deflate data, refusing output larger than max_size (MAX_SIZE).

    Concatenated gzip members (allowed by RFC 1952) are all decoded.
    """
    if max_size is None:
        max_size = MAX_SIZE
    out = bytearray()
    data = body
    while True:
        d = zlib.decompressobj(wbits)
        out += d.decompress(data, max_size + 1 - len(out))
        if len(out) > max_size or d.unconsumed_tail:
            raise FetchTooLarge(f"decompressed body exceeded {max_size} bytes")
        data = d.unused_data
        if not (d.eof and wbits & 16 and data.strip(b"\0")):
            return bytes(out)


def _decode(body: bytes, encoding: str) -> bytes:
    """Undo a Content-Encoding (a comma-separated list, applied in order).

    gzip and deflate are decoded; at the first other token (servers send things
    like "utf-8") the body is returned as it is at that point, as Scrapy did.
    """
    for token in reversed([t.strip().lower() for t in encoding.split(",") if t.strip()]):
        if token in ("gzip", "x-gzip"):
            body = decompress(body, zlib.MAX_WBITS | 16)
        elif token == "deflate":
            try:
                body = decompress(body, zlib.MAX_WBITS)
            except zlib.error:
                body = decompress(body, -zlib.MAX_WBITS)
        elif token != "identity":
            logger.debug("Leaving body undecoded: unknown Content-Encoding %r", token)
            return body
    return body


def _is_cert_error(exc: BaseException) -> bool:
    """True if certificate verification failed somewhere in the exception's causes.

    Other TLS failures (handshake resets, protocol mismatches) are ordinary
    connection errors and get retried.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


def redact_url(url: str) -> str:
    """The URL with its credentials masked: ``USER:***@`` or, with no password, ``***@``.

    A lone username is masked too, since some proxy gateways take a token there.
    Never raises; a URL without ``@`` comes back unchanged.
    """
    if "@" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or not parts.netloc:
        # No parseable netloc (e.g. scheme-less "u:secret@host:8080"): mask
        # everything up to the last "@"
        return "***" + url[url.rindex("@") :]
    if "@" not in parts.netloc:
        # An unencoded "/", "?" or "#" in a password moves the "@" past the
        # netloc, where it can't be told apart from a path or query "@": mask
        # everything between "//" and the last "@"
        start = url.index("//") + 2
        return url[:start] + "***" + url[url.rindex("@") :]
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, sep, _ = userinfo.partition(":")
    masked = f"{user}:***" if sep else "***"
    return urllib.parse.urlunsplit(parts._replace(netloc=f"{masked}@{hostport}"))


_USERINFO_ESCAPES = str.maketrans({"/": "%2F", "?": "%3F", "#": "%23"})


def encode_proxy_userinfo(url: str) -> str:
    """``url`` with any ``/``, ``?`` or ``#`` in its credentials percent-encoded.

    Unencoded, they end the host part, so ``http://tok?en@gw:8080`` would go to host
    ``tok``. A proxy URL has no use for a path or query, so everything between
    ``//`` and the last ``@`` is taken as the credentials.
    """
    scheme, sep, rest = url.partition("://")
    userinfo, at, hostport = rest.rpartition("@")
    if not (sep and at):
        return url
    return f"{scheme}://{userinfo.translate(_USERINFO_ESCAPES)}@{hostport}"


def validate_proxy(url: str) -> str:
    """Return ``url`` if it is a usable proxy URL, else raise ValueError (redacted).

    Credentials may be typed unencoded (see encode_proxy_userinfo).
    """
    try:
        parts = urllib.parse.urlsplit(encode_proxy_userinfo(url))
        valid = parts.scheme in PROXY_SCHEMES and bool(parts.hostname)
        parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "proxy must be an http://, https://, socks5:// or socks5h:// URL with a host, "
            f"got {redact_url(url)!r}"
        )
    return url


def _env_proxy_mounts() -> dict[str, str | None]:
    """The proxy mounts httpx builds from the environment: pattern -> proxy, None to bypass.

    Mirrors httpx._utils.get_environment_proxies (private, so not imported);
    test_fetch's parity tests pin this to the installed httpx.
    """
    info = urllib.request.getproxies()
    mounts: dict[str, str | None] = {}
    for scheme in ("http", "https", "all"):
        if value := info.get(scheme):
            mounts[f"{scheme}://"] = value if "://" in value else f"http://{value}"
    hosts = [host.strip() for host in info.get("no", "").split(",")]
    if "*" in hosts:
        return {}
    mounts.update((_no_proxy_pattern(host), None) for host in hosts if host)
    return mounts


def _no_proxy_pattern(host: str) -> str:
    if "://" in host:
        return host
    try:
        version = ipaddress.ip_address(host.split("/")[0]).version
    except ValueError:
        version = None
    if version == 4 or host.lower() == "localhost":
        return f"all://{host}"
    if version == 6:
        return f"all://[{host}]"
    # Unlike urllib's rule, ".example.com" bypasses subdomains but not example.com
    return f"all://*{host}"


@dataclass(frozen=True)
class _URLPattern:
    """Mirrors httpx._utils.URLPattern, which orders and matches proxy mounts."""

    scheme: str
    host_regex: re.Pattern[str] | None
    port: int | None
    priority: tuple[int, int, int]

    @classmethod
    def parse(cls, pattern: str) -> _URLPattern:
        import httpx

        url = httpx.URL(pattern)
        scheme = "" if url.scheme == "all" else url.scheme
        host = "" if url.host == "*" else url.host
        if not host:
            regex = None
        elif host.startswith("*."):
            regex = re.compile(rf"^.+\.{re.escape(host[2:])}$")
        elif host.startswith("*"):
            regex = re.compile(rf"^(.+\.)?{re.escape(host[1:])}$")
        else:
            regex = re.compile(rf"^{re.escape(host)}$")
        priority = (0 if url.port is not None else 1, -len(host), -len(scheme))
        return cls(scheme, regex, url.port, priority)

    def matches(self, url: httpx.URL) -> bool:
        return (
            (not self.scheme or self.scheme == url.scheme)
            and (self.host_regex is None or bool(self.host_regex.match(url.host)))
            and (self.port is None or self.port == url.port)
        )


def env_proxy_for(url: str) -> str | None:
    """The proxy httpx would use for ``url`` from the environment, or None to go direct.

    A URL httpx can't parse gets None: no request goes anywhere for it.
    """
    import httpx

    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, UnicodeError, ValueError):
        return None
    mounts = []
    for key, proxy in _env_proxy_mounts().items():
        try:
            mounts.append((_URLPattern.parse(key), proxy))
        except (httpx.InvalidURL, UnicodeError, ValueError):
            continue  # httpx itself refuses to build a client for it
    # Stable, like httpx's sort, so equal priorities keep the environment's order
    for pattern, proxy in sorted(mounts, key=lambda mount: mount[0].priority):
        if pattern.matches(target):
            return proxy
    return None


def uses_proxy(url: str, proxy: str | None) -> bool:
    """Whether a request to ``url`` goes through a proxy, explicit or from the environment."""
    return proxy is not None or env_proxy_for(url) is not None


# The browser profiles a store rotates through on refusals, starting with the first.
# Aliases curl_cffi resolves to its newest desktop profile of that browser; each is
# accepted by every curl_cffi in the supported range (0.16.3 to 0.17). "edge" is left
# out: it resolves to Edge 101, from 2022.
IMPERSONATE_PROFILES = ("chrome", "safari", "firefox")
DEFAULT_IMPERSONATE = IMPERSONATE_PROFILES[0]


def impersonate_rotation(start: str) -> tuple[str, ...]:
    """The profiles a store tries in turn, starting with ``start``.

    A pool profile starts the pool at its place; any other comes first, followed by
    the pool without the profile it resolves to (``chrome150`` for ``chrome`` when
    that is curl_cffi's newest Chrome).
    """
    pool = IMPERSONATE_PROFILES
    if start in pool:
        i = pool.index(start)
        return pool[i:] + pool[:i]
    from curl_cffi.requests.impersonate import resolve_latest_browser_type

    same = resolve_latest_browser_type(start)
    return (start, *(p for p in pool if resolve_latest_browser_type(p) != same))


def validate_impersonate(target: str) -> str:
    """Return ``target`` if the installed curl_cffi can impersonate it, else raise ValueError.

    Mirrors how curl_cffi resolves a target when it builds a request: a native
    libcurl target (after aliases like "chrome") or a named fingerprint. Its
    BrowserTypeLiteral lists targets it can't use (safari18_4) and misses some it
    can (tor).
    """
    from curl_cffi.fingerprints import FingerprintManager
    from curl_cffi.requests.impersonate import resolve_latest_browser_type
    from curl_cffi.requests.utils import NATIVE_TARGET_NAMES

    native = resolve_latest_browser_type(target) in NATIVE_TARGET_NAMES
    if not (native or target in FingerprintManager.load_fingerprints()):
        raise ValueError(
            f"unknown impersonate target {target!r}; use a curl_cffi browser such as "
            "chrome, firefox or safari, or a pinned profile such as chrome150"
        )
    return target


def check_proxy_support(proxy: str | None) -> None:
    """Raise ValueError now for a proxy httpx would reject when building its client.

    An explicit ``proxy`` gets the full validate_proxy check. Environment proxies
    get only the checks httpx applies itself, so setups that work today keep
    working (httpx accepts a hostless ``HTTP_PROXY=http://``, for instance). The
    exception is unencoded credentials, which httpx would send to the wrong host.
    """
    if proxy is not None:
        validate_proxy(proxy)
        return
    import httpx

    for value in filter(None, _env_proxy_mounts().values()):
        # httpx parses these itself, so they aren't encoded for it (see
        # encode_proxy_userinfo); stricter than httpx, which would misroute them
        if encode_proxy_userinfo(value) != value:
            raise ValueError(
                f"{_ENV_PROXY_PREFIX}percent-encode any /, ? or # in the credentials "
                f"(%2F, %3F, %23), got {redact_url(value)!r}"
            )
        try:
            scheme = httpx.URL(value).scheme
        except httpx.InvalidURL as exc:
            raise ValueError(f"{_ENV_PROXY_PREFIX}{exc}") from None
        if scheme not in PROXY_SCHEMES:
            raise ValueError(f"{_ENV_PROXY_PREFIX}unknown scheme in {redact_url(value)!r}")
    # httpx fails to build its client on these; env_proxy_for would skip them, so
    # CurlCffiFetcher would proxy what the user meant to send direct
    for pattern in _env_proxy_mounts():
        try:
            _URLPattern.parse(pattern)
        except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
            host = pattern.removeprefix("all://").removeprefix("*")
            raise ValueError(f"NO_PROXY in the environment: bad entry {host!r} ({exc})") from None


class HttpxFetcher:
    """Default transport: httpx over HTTP/1.1, one hop per fetch.

    An explicit ``proxy`` replaces any proxy from the environment (NO_PROXY included).
    """

    capabilities = FetcherCapabilities()

    def __init__(
        self, *, verify: bool = True, trust_env: bool = True, proxy: str | None = None
    ) -> None:
        self._verify = verify
        self._trust_env = trust_env
        self._proxy = None if proxy is None else encode_proxy_userinfo(proxy)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        import httpx  # imported here: it's slow to import and only needed for real HTTP

        self._client = httpx.AsyncClient(
            http1=True,
            http2=False,
            verify=self._verify,
            trust_env=self._trust_env,
            proxy=self._proxy,
            follow_redirects=False,
            headers={"Accept-Encoding": ACCEPT_ENCODING},
            timeout=None,  # a total deadline is enforced per fetch instead
            # Never store cookies: the client is shared by every store in a run
            # (a raw CookieJar: httpx copies a Cookies object into a default jar)
            cookies=http.cookiejar.CookieJar(
                http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            ),
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
        import httpx

        assert self._client is not None
        loop = asyncio.get_running_loop()
        start = loop.time()  # the loop clock excludes time the sync API is paused
        try:
            req = self._client.build_request(
                request.method, request.url, headers=headers, content=request.body
            )
        except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
            raise FetchInvalidURL(f"{request.url!r}: {exc}") from exc
        try:
            resp = await self._client.send(req, stream=True)
        except httpx.TimeoutException as exc:
            raise FetchTimeout(str(exc) or "timeout") from exc
        except httpx.TransportError as exc:
            if _is_cert_error(exc):
                raise FetchTLSError(str(exc) or repr(exc)) from exc
            if isinstance(exc, httpx.RemoteProtocolError):
                raise FetchIncomplete(str(exc) or repr(exc)) from exc
            raise FetchConnectionError(str(exc) or repr(exc)) from exc
        latency = loop.time() - start
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
        if len(body) > WARN_SIZE:
            logger.warning("Large response (%d bytes) from %s", len(body), request.url)
        return Response(
            url=str(resp.url),
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            body=body,
            latency=latency,
        )


# httpx's default max_connections; curl_cffi's own default of 10 would queue
# requests the run's concurrency limits allow
MAX_CURL_CLIENTS = 100

# curl_cffi warns on every https:// request through an https:// proxy; ours are
# deliberate, and its advice (use http://) would change how the proxy is reached
_HTTPS_PROXY_WARNING = "Make sure you are using https over https proxy"


class _UndecodableBody(Exception):
    """libcurl refused a Content-Encoding before writing any body."""


def _socks_on_proxy(proxy: str) -> str:
    """``socks5://`` as ``socks5h://``: httpx resolves names on the proxy for both."""
    scheme, sep, rest = proxy.partition("://")
    return f"socks5h://{rest}" if sep and scheme.lower() == "socks5" else proxy


def _scrub(message: str, proxy: str | None) -> str:
    return message.replace(proxy, redact_url(proxy)) if proxy else message


class CurlCffiFetcher:
    """Transport that presents a browser profile's TLS and HTTP/2 fingerprint and
    headers, through curl_cffi. One hop per fetch.

    It keeps HttpxFetcher's contract: no redirects followed, no cookies kept, the
    MAX_SIZE cap, the same errors, a total deadline on the loop clock, and proxies
    chosen by httpx's rules (an explicit ``proxy`` replaces the environment's).
    Each request uses ``Request.identity.impersonate``, or ``impersonate`` when that
    is None. The profile brings its own user agent and headers, so the identity's
    are never applied; ``Request.headers`` override the profile's.
    """

    capabilities = FetcherCapabilities(applies_identity_headers=False, can_impersonate=True)

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        *,
        verify: bool = True,
        proxy: str | None = None,
    ) -> None:
        self._impersonate = impersonate
        self._verify = verify
        self._proxy = None if proxy is None else encode_proxy_userinfo(proxy)
        # One session per (profile, raw): a session's connections keep the TLS and
        # HTTP/2 settings of the profile that opened them, so profiles never share
        # one. Raw sessions read bodies libcurl can't decode; all open on first use
        self._sessions: dict[tuple[str, bool], Any] = {}
        self._curls: list[Any] = []
        self._entered = False

    async def __aenter__(self) -> Self:
        from curl_cffi.utils import CurlCffiWarning

        # Installed on every entry: a caller's catch_warnings may have reset the
        # filters since. Never removed, since that could drop someone else's entry
        warnings.filterwarnings("ignore", message=_HTTPS_PROXY_WARNING, category=CurlCffiWarning)
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        for session in self._sessions.values():
            await session.close()
        for acurl in self._curls:  # a session doesn't close an AsyncCurl it was given
            await acurl.close()
        self._sessions = {}
        self._curls = []
        self._entered = False

    def _session(self, profile: str, raw: bool = False) -> Any:
        from curl_cffi import AsyncCurl, CurlOpt
        from curl_cffi.requests import AsyncSession
        from curl_cffi.utils import CurlCffiWarning

        session = self._sessions.get((profile, raw))
        if session is not None:
            return session
        # On a Windows Proactor loop curl_cffi warns that it runs select() in a
        # thread and to change the loop policy, which scrape() and the CLI own; the
        # thread hands control back to the loop, so callbacks stay on it
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CurlCffiWarning)
            acurl = AsyncCurl()
        self._curls.append(acurl)
        session = self._sessions[profile, raw] = AsyncSession(
            async_curl=acurl,
            max_clients=MAX_CURL_CLIENTS,
            impersonate=profile,
            verify=self._verify,
            discard_cookies=True,  # the session is shared by every store in a run
            allow_redirects=False,
            # Every request sets its proxy, empty for direct, so libcurl never reads
            # the proxy environment; NOPROXY "" stops it applying NO_PROXY to that
            curl_options={
                CurlOpt.NOPROXY: "",
                **({CurlOpt.HTTP_CONTENT_DECODING: 0} if raw else {}),
            },
        )
        return session

    async def fetch(self, request: Request) -> Response:
        import httpx

        assert self._entered, "use CurlCffiFetcher as an async context manager"
        profile = request.identity.impersonate or self._impersonate
        try:
            # httpx's rule, so both fetchers refuse the same URLs; curl_cffi would
            # percent-encode control characters and send them
            scheme = httpx.URL(request.url).scheme
        except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
            raise FetchInvalidURL(f"{request.url!r}: {exc}") from exc
        if scheme not in ("http", "https"):
            # libcurl would read file:// and open dict://, ftp:// and the rest;
            # httpx raises UnsupportedProtocol, a transport error
            raise FetchConnectionError(f"unsupported protocol in {request.url!r}")
        proxy = self._proxy or env_proxy_for(request.url)
        if proxy:
            proxy = _socks_on_proxy(proxy)
        # proxies={"all": ""} sets an empty CURLOPT_PROXY: direct, whatever the
        # environment says (proxy="" would count as unset)
        route: dict[str, Any] = {"proxy": proxy} if proxy else {"proxies": {"all": ""}}
        try:
            # timeout=None on the request: libcurl's timers run on the wall clock,
            # which keeps going while scrape() is paused
            async with asyncio.timeout(request.timeout):
                try:
                    return await self._attempt(profile, False, request, route, proxy)
                except _UndecodableBody:
                    # Every request the adapters make is a GET, so resending is safe
                    return await self._attempt(profile, True, request, route, proxy)
        except TimeoutError as exc:
            raise FetchTimeout(f"no complete response within {request.timeout}s") from exc

    async def _attempt(
        self, profile: str, raw: bool, request: Request, route: dict[str, Any], proxy: str | None
    ) -> Response:
        from curl_cffi.curl import CURL_WRITEFUNC_ERROR
        from curl_cffi.requests.exceptions import RequestException

        session = self._session(profile, raw)
        loop = asyncio.get_running_loop()
        chunks: list[bytes] = []
        size = 0
        first_byte: float | None = None

        def on_chunk(chunk: bytes) -> int:
            nonlocal size, first_byte
            if first_byte is None:
                first_byte = loop.time()
            size += len(chunk)
            if size > MAX_SIZE:
                return CURL_WRITEFUNC_ERROR  # aborts the transfer at once
            chunks.append(chunk)
            return len(chunk)

        start = loop.time()  # the loop clock excludes time the sync API is paused
        try:
            resp = await session.request(
                request.method,
                request.url,
                headers=dict(request.headers) or None,
                data=request.body,
                content_callback=on_chunk,
                allow_redirects=False,
                timeout=None,
                **route,
            )
        except RequestException as exc:
            if size > MAX_SIZE:
                raise FetchTooLarge(f"response exceeded {MAX_SIZE} bytes") from exc
            raise _map_curl_error(
                exc, received=first_byte is not None, raw=raw, proxy=proxy
            ) from exc
        latency = (loop.time() if first_byte is None else first_byte) - start
        body = b"".join(chunks)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if raw:
            try:
                body = _decode(body, headers.get("content-encoding", ""))
            except zlib.error as exc:
                raise FetchIncomplete(f"corrupt compressed body: {exc}") from exc
        if len(body) > WARN_SIZE:
            logger.warning("Large response (%d bytes) from %s", len(body), request.url)
        return Response(
            url=str(resp.url), status=resp.status_code, headers=headers, body=body, latency=latency
        )


def _map_curl_error(exc: Any, *, received: bool, raw: bool, proxy: str | None) -> Exception:
    """The Fetch* error (or _UndecodableBody) for a curl_cffi RequestException."""
    from curl_cffi.const import CurlECode
    from curl_cffi.requests import exceptions as cx

    message = _scrub(str(exc) or repr(exc), proxy)
    code = getattr(exc, "code", None)
    if isinstance(exc, cx.Timeout):
        return FetchTimeout(message)
    if isinstance(exc, cx.CertificateVerifyError):
        return FetchTLSError(message)
    if isinstance(exc, (cx.InvalidURL, cx.InvalidSchema, cx.MissingSchema)):
        return FetchInvalidURL(message)
    if code == CurlECode.BAD_CONTENT_ENCODING and not received:
        return FetchIncomplete(message) if raw else _UndecodableBody(message)
    # A failure once the body has started is a body-read error, as in HttpxFetcher
    incomplete = (cx.IncompleteRead, cx.ChunkedEncodingError, cx.ContentDecodingError)
    if received or code == CurlECode.PARTIAL_FILE or isinstance(exc, incomplete):
        return FetchIncomplete(message)
    return FetchConnectionError(message)


# Transports for tests and for replaying recorded traffic


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


# Exceptions for the transport errors a recorded corpus can contain, by kind
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
    """Serves responses from a recorded corpus (tests/fixtures/corpus).

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
