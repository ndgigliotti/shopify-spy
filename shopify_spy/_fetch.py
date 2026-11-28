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
import os
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

import certifi

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


_URL_RE = re.compile(
    r"(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)?:)?(?://(?P<authority>[^/?#]*))?(?P<path>[^?#]*)"
)
_AUTHORITY_RE = re.compile(r"(?:(?P<userinfo>.*)@)?(?P<host>\[.*\]|[^:@]*):?(?P<port>.*)")
_IPV4_STYLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")
_DEFAULT_PORTS = {"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}
MAX_URL_LENGTH = 65536


def _split_url(url: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` of ``url``, or ValueError for one Shopify Spy refuses.

    These are the rules 0.5 and earlier applied, frozen by URL_PARTS_CASES in
    tests/test_fetch.py, quirks included: ``int()`` parses the port (``+80`` and
    ``99999`` pass) and a default port is dropped only for a lowercase scheme, since
    environment proxies and NO_PROXY entries that worked then must keep working. The
    host is lowercased and in ASCII (IDNA) form; a scheme must start the URL.
    """
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL too long")
    if bad := re.search(r"[\x00-\x1f\x7f]", url):
        raise ValueError(
            f"Invalid non-printable ASCII character in URL, {bad[0]!r} at position {bad.start()}."
        )
    match = _URL_RE.match(url)
    assert match is not None  # every group is optional
    typed_scheme = match["scheme"] or ""
    authority = _AUTHORITY_RE.fullmatch(match["authority"] or "")
    assert authority is not None
    host = _split_host(authority["host"])
    port_text = authority["port"]
    port = None
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"Invalid port: {port_text!r}") from None
        if port == _DEFAULT_PORTS.get(typed_scheme):
            port = None
    has_authority = bool(authority["userinfo"] or host) or port is not None
    if not (typed_scheme or has_authority) and match["path"].startswith(("//", ":")):
        start = "//" if match["path"].startswith("//") else ":"
        raise ValueError(f"Relative URLs cannot have a path starting with {start!r}")
    return typed_scheme.lower(), host, port


def _split_host(host: str) -> str:
    if not host:
        return ""
    if _IPV4_STYLE.fullmatch(host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(f"Invalid IPv4 address: {host!r}") from None
        return host
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError(f"Invalid IPv6 address: {host!r}") from None
        return host[1:-1]
    if host.isascii():
        # Not IDNA-checked: a_b.com, -a.com and *example.com are all accepted
        return re.sub(r"[ <>\[\]^]", lambda c: f"%{ord(c[0]):02X}", host.lower())
    # The stdlib codec would turn *münchen.de into xn--*mnchen-o2a.de, a NO_PROXY
    # pattern that matches nothing, so hosts meant to go direct would be proxied
    if "*" in host:
        raise ValueError(f"Invalid IDNA hostname: {host!r}")
    try:
        encoded = host.lower().encode("idna").decode("ascii")
    except UnicodeError:
        encoded = ""
    # The stdlib codec (IDNA 2003) maps and passes characters such as "+", "_" and
    # spaces that an internationalized name can't hold, and hyphens at a label's ends
    names = host.removesuffix(".").split(".")
    labels = encoded.removesuffix(".").split(".")
    if any(name.startswith("-") or name.endswith("-") for name in names) or not all(
        re.fullmatch(r"[a-z0-9-]+", label) for label in labels
    ):
        raise ValueError(f"Invalid IDNA hostname: {host!r}")
    return encoded


@dataclass(frozen=True)
class _URLPattern:
    """A proxy mount pattern (``all://*.example.com``), ordered and matched by the
    rules 0.5 and earlier used."""

    scheme: str
    host_regex: re.Pattern[str] | None
    port: int | None
    priority: tuple[int, int, int]

    @classmethod
    def parse(cls, pattern: str) -> _URLPattern:
        scheme, host, port = _split_url(pattern)
        scheme = "" if scheme == "all" else scheme
        host = "" if host == "*" else host
        if not host:
            regex = None
        elif host.startswith("*."):
            regex = re.compile(rf"^.+\.{re.escape(host[2:])}$")
        elif host.startswith("*"):
            regex = re.compile(rf"^(.+\.)?{re.escape(host[1:])}$")
        else:
            regex = re.compile(rf"^{re.escape(host)}$")
        priority = (0 if port is not None else 1, -len(host), -len(scheme))
        return cls(scheme, regex, port, priority)

    def matches(self, url: tuple[str, str, int | None]) -> bool:
        scheme, host, port = url
        return (
            (not self.scheme or self.scheme == scheme)
            and (self.host_regex is None or bool(self.host_regex.match(host)))
            and (self.port is None or self.port == port)
        )


def env_proxy_for(url: str) -> str | None:
    """The proxy the environment gives ``url``, or None to go direct.

    A URL _split_url refuses gets None: no request goes anywhere for it.
    """
    try:
        target = _split_url(url)
    except ValueError:
        return None
    mounts = []
    for key, proxy in _env_proxy_mounts().items():
        try:
            mounts.append((_URLPattern.parse(key), proxy))
        except ValueError:
            continue  # check_proxy_support refuses the run before it gets here
    # A stable sort, so equal priorities keep the environment's order
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
    for value in filter(None, _env_proxy_mounts().values()):
        # httpx parses these itself, so they aren't encoded for it (see
        # encode_proxy_userinfo); stricter than httpx, which would misroute them
        if encode_proxy_userinfo(value) != value:
            raise ValueError(
                f"{_ENV_PROXY_PREFIX}percent-encode any /, ? or # in the credentials "
                f"(%2F, %3F, %23), got {redact_url(value)!r}"
            )
        try:
            scheme = _split_url(value)[0]
        except ValueError as exc:
            raise ValueError(f"{_ENV_PROXY_PREFIX}{exc}") from None
        if scheme not in PROXY_SCHEMES:
            raise ValueError(f"{_ENV_PROXY_PREFIX}unknown scheme in {redact_url(value)!r}")
    # httpx fails to build its client on these; env_proxy_for would skip them, so
    # CurlCffiFetcher would proxy what the user meant to send direct
    for pattern in _env_proxy_mounts():
        try:
            _URLPattern.parse(pattern)
        except ValueError as exc:
            host = pattern.removeprefix("all://").removeprefix("*")
            raise ValueError(f"NO_PROXY in the environment: bad entry {host!r} ({exc})") from None


_CA_FILE_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def ca_locations(trust_env: bool = True) -> tuple[str | None, str | None]:
    """The CA file and directory both fetchers trust instead of certifi's bundle.

    Read from the environment when called, and only with ``trust_env``: the file
    from the first of SSL_CERT_FILE, REQUESTS_CA_BUNDLE and CURL_CA_BUNDLE that is
    set, the directory (OpenSSL's hashed layout) from SSL_CERT_DIR. Either one, or
    both together, replaces the default bundle, as httpx does with SSL_CERT_FILE.
    (None, None) means certifi's bundle.

    Raises ValueError, naming the variable, for a file that can't be opened: httpx
    would fail building its client and curl on every request, each its own way.
    So does a directory with no hashed certificate, which httpx would trust nothing
    from and curl would back with libcurl's built-in bundle (see _curl_ca_options).
    """
    if not trust_env:
        return None, None
    cafile = None
    for name in _CA_FILE_VARIABLES:
        if cafile := os.environ.get(name):
            try:
                open(cafile, "rb").close()
            except OSError as exc:
                raise ValueError(
                    f"{name} in the environment: can't read the CA file {cafile!r} "
                    f"({exc.strerror or exc})"
                ) from None
            break
    capath = os.environ.get("SSL_CERT_DIR") or None
    if capath is not None:
        _hashed_cert_in(capath)
    return cafile or None, capath


def _ssl_context(trust_env: bool) -> ssl.SSLContext:
    cafile, capath = ca_locations(trust_env)
    if cafile is None and capath is None:
        cafile = certifi.where()
    return ssl.create_default_context(cafile=cafile, capath=capath)


def _proxy_with(url: str, context: ssl.SSLContext) -> httpx.Proxy:
    import httpx

    # httpcore refuses a context for an http:// proxy, which has no TLS to use it
    https = httpx.URL(url).scheme == "https"
    return httpx.Proxy(url, ssl_context=context if https else None)


class HttpxFetcher:
    """Default transport: httpx over HTTP/1.1, one hop per fetch.

    An explicit ``proxy`` replaces any proxy from the environment (NO_PROXY included).
    CAs come from ca_locations, read on entry, for the store and an https:// proxy.
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

        # An SSLContext, so httpx's own reading of SSL_CERT_FILE/DIR never applies
        context = _ssl_context(self._trust_env) if self._verify else None
        proxy: str | httpx.Proxy | None = self._proxy
        mounts: dict[str, httpx.AsyncBaseTransport | None] | None = None
        if context is not None:
            # httpcore checks an https:// proxy against its own default context
            # unless the Proxy carries one, which httpx never gives the environment's
            if proxy is not None:
                proxy = _proxy_with(proxy, context)
            elif self._trust_env:
                mounts = {
                    pattern: None
                    if url is None
                    else httpx.AsyncHTTPTransport(verify=context, proxy=_proxy_with(url, context))
                    for pattern, url in _env_proxy_mounts().items()
                }
        self._client = httpx.AsyncClient(
            http1=True,
            http2=False,
            verify=context or False,
            trust_env=self._trust_env,
            proxy=proxy,
            mounts=mounts,
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


def _curl_ca_options(trust_env: bool) -> dict[Any, Any]:
    """libcurl options for ca_locations, for the store and the proxy alike.

    Both locations are always set, so neither curl_cffi's import-time CA file nor
    libcurl's built-in CA directory adds to them. An unused one gets a placeholder,
    not NULL: the libcurl in curl_cffi 0.16 (8.21) takes NULL as the built-in default.
    A CA file stands in as the directory (nothing can be found under a file), and
    a certificate from the directory as the file (the directory trusts it anyway).
    """
    from curl_cffi import CurlOpt

    cafile, capath = ca_locations(trust_env)
    if cafile is None:
        cafile = _hashed_cert_in(capath) if capath else certifi.where()
    path = capath or cafile
    return {
        CurlOpt.CAINFO: cafile,
        CurlOpt.CAPATH: path,
        CurlOpt.PROXY_CAINFO: cafile,
        CurlOpt.PROXY_CAPATH: path,
    }


def _hashed_cert_in(directory: str) -> str:
    """A PEM certificate the directory serves under its hashed name.

    Raises ValueError naming SSL_CERT_DIR when there is none, or no directory.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if re.fullmatch(r"[0-9a-f]{8}\.\d+", name):
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as file:
                    if b"CERTIFICATE-----" in file.read(65536):
                        return path
            except OSError:
                continue
    raise ValueError(
        f"SSL_CERT_DIR in the environment: no CA certificate under a hashed name in "
        f"{directory!r} (lay it out with openssl rehash)"
    )


class CurlCffiFetcher:
    """Transport that presents a browser profile's TLS and HTTP/2 fingerprint and
    headers, through curl_cffi. One hop per fetch.

    It keeps HttpxFetcher's contract: no redirects followed, no cookies kept, the
    MAX_SIZE cap, the same errors, a total deadline on the loop clock, and proxies
    chosen by httpx's rules (an explicit ``proxy`` replaces the environment's).
    CAs come from ca_locations, read on entry, as in HttpxFetcher.
    Each request uses ``Request.identity.impersonate``, or ``impersonate`` when that
    is None. The profile brings its own user agent and headers, so the identity's
    are never applied; ``Request.headers`` override the profile's.
    """

    capabilities = FetcherCapabilities(applies_identity_headers=False, can_impersonate=True)

    def __init__(
        self,
        impersonate: str | None = DEFAULT_IMPERSONATE,
        *,
        verify: bool = True,
        trust_env: bool = True,
        proxy: str | None = None,
    ) -> None:
        self._impersonate = impersonate
        if impersonate is None:
            self.capabilities = FetcherCapabilities()
        self._verify = verify
        self._trust_env = trust_env
        self._proxy = None if proxy is None else encode_proxy_userinfo(proxy)
        # One session per (profile, raw): a session's connections keep the TLS and
        # HTTP/2 settings of the profile that opened them, so profiles never share
        # one. Raw sessions read bodies libcurl can't decode; all open on first use
        self._sessions: dict[tuple[str | None, bool], Any] = {}
        self._curls: list[Any] = []
        self._ca_options: dict[Any, Any] = {}
        self._entered = False

    async def __aenter__(self) -> Self:
        from curl_cffi.utils import CurlCffiWarning

        # Installed on every entry: a caller's catch_warnings may have reset the
        # filters since. Never removed, since that could drop someone else's entry
        warnings.filterwarnings("ignore", message=_HTTPS_PROXY_WARNING, category=CurlCffiWarning)
        self._ca_options = _curl_ca_options(self._trust_env) if self._verify else {}
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

    def _session(self, profile: str | None, raw: bool = False) -> Any:
        from curl_cffi import AsyncCurl, CurlHttpVersion, CurlOpt
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
        plain: dict[str, Any] = {}
        version: dict[Any, Any] = {}
        if profile is None:
            # A client default under the identity's headers. HTTP/1.1, as 0.5 and
            # earlier used: over HTTP/2, even with curl's own settings
            # (akamai="3:100;4:65536;2:0|1048510465|0|m,s,a,p") rather than the
            # Chrome ones curl_cffi sends without a profile, Shopify stores answered
            # 429 to a custom user agent that got 200 over HTTP/1.1 (2026-11-28)
            plain = {"headers": {"Accept": "*/*"}}
            version = {CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1}
        session = self._sessions[profile, raw] = AsyncSession(
            async_curl=acurl,
            max_clients=MAX_CURL_CLIENTS,
            impersonate=profile,
            **plain,
            verify=self._verify,
            discard_cookies=True,  # the session is shared by every store in a run
            allow_redirects=False,
            # Every request sets its proxy, empty for direct, so libcurl never reads
            # the proxy environment; NOPROXY "" stops it applying NO_PROXY to that
            curl_options={
                CurlOpt.NOPROXY: "",
                **({CurlOpt.HTTP_CONTENT_DECODING: 0} if raw else {}),
                **version,
                **self._ca_options,
            },
        )
        return session

    async def fetch(self, request: Request) -> Response:
        assert self._entered, "use CurlCffiFetcher as an async context manager"
        profile = request.identity.impersonate or self._impersonate
        try:
            # curl_cffi would percent-encode control characters and send them
            scheme = _split_url(request.url)[0]
        except ValueError as exc:
            raise FetchInvalidURL(f"{request.url!r}: {exc}") from exc
        if scheme not in ("http", "https"):
            # libcurl would read file:// and open dict://, ftp:// and the rest;
            # httpx raises UnsupportedProtocol, a transport error
            raise FetchConnectionError(f"unsupported protocol in {request.url!r}")
        proxy = self._proxy or (env_proxy_for(request.url) if self._trust_env else None)
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
        self,
        profile: str | None,
        raw: bool,
        request: Request,
        route: dict[str, Any],
        proxy: str | None,
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

        sent = dict(request.headers)
        if profile is None:  # no profile brings a user agent or headers
            sent = {**request.identity.headers, **sent}
            if request.identity.user_agent:
                sent["User-Agent"] = request.identity.user_agent
        start = loop.time()  # the loop clock excludes time the sync API is paused
        try:
            resp = await session.request(
                request.method,
                request.url,
                headers=sent or None,
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
