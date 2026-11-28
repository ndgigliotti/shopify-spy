"""Tests for the transport layer: plain CurlCffiFetcher requests, URL and proxy rules,
FakeFetcher and ReplayFetcher."""

import asyncio
import base64
import gzip
import http.server
import json
import socket
import threading
import time
import zlib

import pytest

from shopify_spy._fetch import (
    DEFAULT_IMPERSONATE,
    IMPERSONATE_PROFILES,
    CurlCffiFetcher,
    FakeFetcher,
    FetchConnectionError,
    FetchIncomplete,
    FetchInvalidURL,
    FetchTimeout,
    FetchTLSError,
    FetchTooLarge,
    Identity,
    ReplayFetcher,
    Request,
    Response,
    Seq,
    _decode,
    _split_url,
    ca_locations,
    check_proxy_support,
    decompress,
    encode_proxy_userinfo,
    env_proxy_for,
    impersonate_rotation,
    load_corpus,
    redact_url,
    uses_proxy,
    validate_impersonate,
    validate_proxy,
)

PAYLOAD = b'{"products": []}' * 20


def make_request(url: str, *, ua: str | None = "test-agent/1.0", timeout: float = 2.0) -> Request:
    return Request(
        method="GET",
        url=url,
        identity=Identity(user_agent=ua, headers={"Accept": "application/json"}),
        timeout=timeout,
    )


# --- Local HTTP server ------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self):
        path = self.path
        if path == "/redirect":
            self._send(301, b"moved", {"Location": "/target"})
        elif path == "/echo":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self._send(200, body, {"Content-Type": "application/json"})
        elif path == "/gzip":
            self._send(200, gzip.compress(PAYLOAD), {"Content-Encoding": "gzip"})
        elif path == "/deflate":
            self._send(200, zlib.compress(PAYLOAD), {"Content-Encoding": "deflate"})
        elif path == "/deflate-raw":
            c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            self._send(200, c.compress(PAYLOAD) + c.flush(), {"Content-Encoding": "deflate"})
        elif path == "/br":
            self._send(200, b"\x0b\x02\x80hello\x03", {"Content-Encoding": "br"})
        elif path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Length", "50")
            self.end_headers()
            for _ in range(50):
                self.wfile.write(b"x")
                time.sleep(0.02)
        elif path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.close_connection = True
        elif path == "/slow-headers":
            time.sleep(0.25)
            self._send(200, b"ok")
        elif path == "/mixed-case":
            self._send(200, b"ok", {"X-Custom-Header": "Value", "Content-Type": "text/plain"})
        else:
            self._send(404, b"not found")


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


async def fetch(url: str, **kwargs) -> Response:
    async with asyncio.timeout(5):
        async with CurlCffiFetcher(None, trust_env=False) as fetcher:
            return await fetcher.fetch(make_request(url, **kwargs))


# --- Plain requests ---------------------------------------------------------------------


async def test_redirect_is_returned_not_followed(server):
    response = await fetch(f"{server}/redirect")
    assert response.status == 301
    assert response.location == "/target"
    assert response.url == f"{server}/redirect"
    assert response.body == b"moved"


async def test_identity_user_agent_and_headers_applied(server):
    headers = (await fetch(f"{server}/echo", ua="my-agent/2.0")).json()
    assert headers["user-agent"] == "my-agent/2.0"
    assert headers["accept"] == "application/json"
    assert headers["accept-encoding"] == "gzip, deflate, br"
    assert not {"sec-ch-ua", "upgrade", "http2-settings"} & set(headers)


@pytest.mark.parametrize("path", ["/gzip", "/deflate", "/deflate-raw"])
async def test_compressed_bodies_are_decoded(server, path):
    response = await fetch(f"{server}{path}")
    assert response.status == 200
    assert response.body == PAYLOAD


def test_content_encoding_list_is_decoded_in_order():
    assert _decode(gzip.compress(b"payload"), "identity, gzip") == b"payload"


def test_concatenated_gzip_members_are_all_decoded():
    body = gzip.compress(b"first ") + gzip.compress(b"second")
    assert decompress(body, zlib.MAX_WBITS | 16) == b"first second"


async def test_invalid_url_is_a_fetch_error():
    with pytest.raises(FetchInvalidURL):
        await fetch("https://shop.test/bad\x7fpath")


async def test_total_deadline_applies_to_trickled_body(server):
    start = time.monotonic()
    with pytest.raises(FetchTimeout):
        await fetch(f"{server}/trickle", timeout=0.3)
    assert time.monotonic() - start < 0.9


async def test_truncated_body_raises_incomplete(server):
    with pytest.raises(FetchIncomplete):
        await fetch(f"{server}/truncated")


async def test_connection_refused_raises_connection_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(FetchConnectionError) as excinfo:
        # Windows retries a refused connect for about 2s before reporting it
        await fetch(f"http://127.0.0.1:{port}/", timeout=4.0)
    assert not isinstance(excinfo.value, FetchTLSError)


async def test_latency_measured_to_headers_when_headers_are_slow(server):
    response = await fetch(f"{server}/slow-headers")
    assert response.latency >= 0.2


async def test_response_headers_lowercased(server):
    response = await fetch(f"{server}/mixed-case")
    assert response.headers["x-custom-header"] == "Value"
    assert all(name == name.lower() for name in response.headers)


def test_decompress_bomb_guard():
    bomb = gzip.compress(b"\0" * 100_000)
    with pytest.raises(FetchTooLarge):
        decompress(bomb, zlib.MAX_WBITS | 16, max_size=1000)


def test_decompress_within_limit():
    data = b"a" * 1000
    assert decompress(gzip.compress(data), zlib.MAX_WBITS | 16, max_size=1000) == data


# --- Proxies -------------------------------------------------------------------------

DEAD_PROXY = "http://127.0.0.1:9"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://USER:PASS@gw:8000", "http://USER:***@gw:8000"),
        ("http://token@gw:8000", "http://***@gw:8000"),
        ("socks5://u:p@[::1]:1080/x", "socks5://u:***@[::1]:1080/x"),
        ("http://gw:8000", "http://gw:8000"),
        ("http://gw:8000/?session=a@b", "http://***@b"),
        ("http://user:pa#ss@host:8080", "http://***@host:8080"),
        ("http://user:p/ss@host:8080", "http://***@host:8080"),
        ("http://tok?en@host:8080", "http://***@host:8080"),
        ("http://u:p@gw:8000/?session=a@b", "http://u:***@gw:8000/?session=a@b"),
        ("not a url", "not a url"),
    ],
)
def test_redact_url(url, expected):
    assert redact_url(url) == expected


@pytest.mark.parametrize("url", ["u:secret@host:8080", "http://u:secret@[bad:8080", "@secret@h"])
def test_redact_url_without_parseable_netloc_hides_userinfo(url):
    assert "secret" not in redact_url(url)


@pytest.mark.parametrize(
    "url",
    ["ftp://h:1", "h:8080", "http://", "http://h:notaport", "http://h:99999", "socks4://h:1"],
)
def test_validate_proxy_rejects(url):
    with pytest.raises(ValueError, match="proxy must be an http://"):
        validate_proxy(url)


@pytest.mark.parametrize(
    "url", ["http://h:8080", "https://u:p@h", "socks5://h:1080", "socks5h://h:1080"]
)
def test_validate_proxy_accepts(url):
    assert validate_proxy(url) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://tok?en@gw:8080", "http://tok%3Fen@gw:8080"),
        ("http://u:p/s#s@gw:8080", "http://u:p%2Fs%23s@gw:8080"),
        ("http://u:p@ss@gw:8080", "http://u:p@ss@gw:8080"),
        ("http://gw:8080", "http://gw:8080"),
    ],
)
def test_encode_proxy_userinfo(url, expected):
    assert encode_proxy_userinfo(url) == expected
    assert validate_proxy(url) == url


def test_fetcher_encodes_proxy_credentials():
    assert CurlCffiFetcher(proxy="http://tok?en@gw:8080")._proxy == "http://tok%3Fen@gw:8080"


def test_unencoded_environment_credentials_are_rejected(clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", "http://u:sec?ret@gw:8080")
    with pytest.raises(ValueError, match="percent-encode") as info:
        check_proxy_support(None)
    assert "sec" not in str(info.value)


def test_validate_proxy_error_is_redacted():
    with pytest.raises(ValueError) as info:
        validate_proxy("ftp://u:secret@h:1")
    assert "secret" not in str(info.value)
    assert "ftp://u:***@h:1" in str(info.value)


@pytest.mark.parametrize(
    ("env", "url", "expected"),
    [
        ({}, "https://a.com", False),
        ({"HTTPS_PROXY": "http://gw:8080"}, "https://a.com", True),
        ({"HTTPS_PROXY": "http://gw:8080"}, "http://a.com", False),
        ({"ALL_PROXY": "socks5://gw:1080"}, "http://a.com", True),
        ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": "a.com"}, "https://a.com", False),
        ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": "*"}, "https://a.com", False),
        # 0.5's rule, not urllib's: a leading dot bypasses subdomains only
        ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": ".a.com"}, "https://a.com", True),
        ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": ".a.com"}, "https://www.a.com", False),
    ],
)
def test_uses_proxy_from_environment(clean_proxy_env, env, url, expected):
    for name, value in env.items():
        clean_proxy_env.setenv(name, value)
    assert uses_proxy(url, None) is expected


# Recorded from httpx 0.28.1 before it was removed; regenerating needs it reinstalled.
URL_PARTS_CASES = [
    ("https://shop.test/bad\x7fpath", None),
    ("https://shop.test/\x00", None),
    ("https://shop.test/?q=\x01", None),
    ("https://shop.test/#\x7f", None),
    ("https://exa\x7fmple.com/", None),
    ("http://\xad.com/", None),
    # IDNA 2003 and 2008 disagree on these, so they're refused rather than guessed
    ("https://stra\xdfe.de/", None),
    ("https://a\u200d.com/", None),
    ("https://shop.test/a b", ("https", "shop.test", None)),
    ("https://Shop.Test:443/x", ("https", "shop.test", None)),
    ("HTTPS://Shop.Test:443/x", ("https", "shop.test", 443)),
    ("HTTP://a:80/", ("http", "a", 80)),
    ("hTTp://a:80/", ("http", "a", 80)),
    ("http://a:80/", ("http", "a", None)),
    ("http://a:+80/", ("http", "a", None)),
    ("http://h: 8080", ("http", "h", 8080)),
    ("http://h:8080 ", ("http", "h", 8080)),
    ("http://a:-1/", ("http", "a", -1)),
    ("http://a:1_000/", ("http", "a", 1000)),
    ("http://a:\u0663/", ("http", "a", 3)),
    ("http://a: /", None),
    (" http://a/", ("", "", None)),
    ("\xa0http://a/", ("", "", None)),
    ("ht tp://a/", ("", "", None)),
    ("http://a:8080/", ("http", "a", 8080)),
    ("http://a:99999/", ("http", "a", 99999)),
    ("http://a:x/", None),
    ("http://h:notaport", None),
    ("http://[::1]:80/", ("http", "::1", None)),
    ("http://[::1/", None),
    ("all://[::1", None),
    ("http://[zz]/", None),
    ("http://999.1.1.1/", None),
    ("http://münchen.de/", ("http", "xn--mnchen-3ya.de", None)),
    ("http://xn--mnchen-3ya.de/", ("http", "xn--mnchen-3ya.de", None)),
    ("http://MÜNCHEN.de/", ("http", "xn--mnchen-3ya.de", None)),
    ("all://*münchen.de", None),
    ("all://*.münchen.de", None),
    ("http://a_b.com/", ("http", "a_b.com", None)),
    ("http://-a.com/", ("http", "-a.com", None)),
    ("http://a..b/", ("http", "a..b", None)),
    ("http://127.1/", ("http", "127.1", None)),
    ("http://u:p@h/", ("http", "h", None)),
    ("http://u@p@h:81/", ("http", "h", 81)),
    ("file:///etc/x", ("file", "", None)),
    ("dict://h:1/", ("dict", "h", 1)),
    ("h:8080", ("h", "", None)),
    ("http://", ("http", "", None)),
    ("http://:8080", ("http", "", 8080)),
    ("http://a.com:/", ("http", "a.com", None)),
    ("all://*example.com", ("all", "*example.com", None)),
    ("all://*.a.com", ("all", "*.a.com", None)),
    ("all://10.0.0.0/8", ("all", "10.0.0.0", None)),
    ("::x", None),
    ("//h:81/x", ("", "h", 81)),
    ("a" * 65537, None),
    ("http://a b.com/", ("http", "a%20b.com", None)),
    ("http://a^b/", ("http", "a%5Eb", None)),
    ("http://[::FFFF:1]/", ("http", "::FFFF:1", None)),
]

URL_PARTS_IDS = [ascii(url)[:40] for url, _ in URL_PARTS_CASES]
GW = "http://gw:8080"
ENV_PROXY_CASES = [
    ({}, "https://example.com", None),
    ({"HTTPS_PROXY": GW, "NO_PROXY": ".example.com"}, "https://example.com", GW),
    ({"HTTPS_PROXY": GW, "NO_PROXY": ".example.com"}, "https://www.example.com", None),
    ({"HTTPS_PROXY": GW, "NO_PROXY": "example.com"}, "https://www.example.com", None),
    ({"HTTPS_PROXY": GW, "NO_PROXY": "example.com"}, "https://wwwexample.com", GW),
    ({"HTTPS_PROXY": GW, "NO_PROXY": "EXAMPLE.com"}, "https://example.com", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "127.0.0.1"}, "http://127.0.0.1:8000/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "127.0.0.1"}, "http://127.0.0.2/", GW),
    ({"HTTP_PROXY": GW, "NO_PROXY": "10.0.0.0/8"}, "http://10.0.0.0/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "::1"}, "http://[::1]:8000/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "localhost"}, "http://localhost:8000/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "localhost"}, "http://www.localhost/", GW),
    ({"HTTP_PROXY": GW, "NO_PROXY": "example.com:8080"}, "http://example.com:8080/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "example.com:8080"}, "http://example.com/", GW),
    ({"HTTP_PROXY": GW, "NO_PROXY": "http://example.com"}, "http://example.com/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "http://example.com"}, "https://example.com/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": "other.com, *"}, "http://example.com/", None),
    ({"HTTP_PROXY": GW, "NO_PROXY": " , example.com"}, "http://example.com/", None),
    ({"HTTPS_PROXY": GW}, "http://example.com/", None),
    ({"HTTPS_PROXY": GW}, "https://example.com/", GW),
    ({"HTTP_PROXY": "gw:3128"}, "http://example.com/", "http://gw:3128"),
    ({"ALL_PROXY": "socks5://gw:1080"}, "http://example.com/", "socks5://gw:1080"),
    ({"ALL_PROXY": "socks5://gw:1080", "HTTPS_PROXY": GW}, "https://example.com/", GW),
    (
        {"ALL_PROXY": "socks5://gw:1080", "HTTPS_PROXY": GW},
        "http://example.com/",
        "socks5://gw:1080",
    ),
]

ENV_CASES = [
    ({"HTTPS_PROXY": "socks5://h:1080"}, False),
    ({"ALL_PROXY": "socks5://h:1080", "NO_PROXY": "*"}, False),
    ({"HTTPS_PROXY": "socks5://h:1080", "NO_PROXY": "*"}, False),
    ({"HTTPS_PROXY": "socks5://h:1080", "NO_PROXY": "example.com, *"}, False),
    ({"HTTP_PROXY": "http://"}, False),
    ({"HTTP_PROXY": "http://:8080"}, False),
    ({"HTTPS_PROXY": "h:8080"}, False),
    ({"HTTPS_PROXY": "http://h:notaport"}, True),
    ({"HTTPS_PROXY": "socks4://h:1"}, True),
    ({"HTTPS_PROXY": "http://h:99999"}, False),
    ({"ALL_PROXY": "socks5h://h:1080"}, False),
    ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": "[::1"}, True),
    ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": "::1, 10.0.0.0/8, .example.com"}, False),
    ({"HTTPS_PROXY": "http://gw:8080", "NO_PROXY": "münchen.de"}, True),
]


def _env_id(env: dict[str, str], *rest: str) -> str:
    return ",".join([*(f"{k}={v}" for k, v in env.items()), *rest])


ENV_PROXY_IDS = [_env_id(env, url) for env, url, _ in ENV_PROXY_CASES]
ENV_IDS = [_env_id(env) for env, _ in ENV_CASES]


@pytest.mark.parametrize(("url", "expected"), URL_PARTS_CASES, ids=URL_PARTS_IDS)
def test_split_url_matches_recorded(url, expected):
    if expected is None:
        with pytest.raises(ValueError):
            _split_url(url)
    else:
        assert _split_url(url) == expected


@pytest.mark.parametrize(("env", "url", "expected"), ENV_PROXY_CASES, ids=ENV_PROXY_IDS)
def test_env_proxy_for_matches_recorded(clean_proxy_env, env, url, expected):
    for name, value in env.items():
        clean_proxy_env.setenv(name, value)
    assert env_proxy_for(url) == expected


@pytest.mark.parametrize(
    ("no_proxy", "url", "expected"),
    [
        (".example.com", "https://example.com", GW),
        (".example.com", "https://www.example.com", None),
        ("example.com", "https://www.example.com", None),
        ("example.com", "https://wwwexample.com", GW),
    ],
)
def test_env_proxy_for_no_proxy_names(clean_proxy_env, no_proxy, url, expected):
    clean_proxy_env.setenv("HTTPS_PROXY", GW)
    clean_proxy_env.setenv("NO_PROXY", no_proxy)
    assert env_proxy_for(url) == expected


def test_env_proxy_for_unparseable_url_goes_direct(clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", GW)
    assert env_proxy_for("https://exa\x7fmple.com/") is None


def test_impersonate_profiles_are_current_desktop_browsers():
    """Pinned: the aliases resolve to each browser's newest desktop profile on every
    supported curl_cffi (checked on 0.16.3 and 0.17.0); "edge" is Edge 101."""
    from curl_cffi.requests.impersonate import resolve_latest_browser_type

    assert IMPERSONATE_PROFILES == ("chrome", "safari", "firefox")
    assert DEFAULT_IMPERSONATE == "chrome"
    resolved = [resolve_latest_browser_type(p) for p in IMPERSONATE_PROFILES]
    assert len(set(resolved)) == len(resolved)
    for profile, target in zip(IMPERSONATE_PROFILES, resolved, strict=True):
        assert validate_impersonate(profile) == profile
        assert target.startswith(profile)
        assert not target.endswith(("_android", "_ios"))


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("chrome", ("chrome", "safari", "firefox")),
        ("safari", ("safari", "firefox", "chrome")),
        ("firefox", ("firefox", "chrome", "safari")),
        ("chrome146", ("chrome146", "chrome", "safari", "firefox")),
    ],
)
def test_impersonate_rotation(start, expected):
    assert impersonate_rotation(start) == expected


def test_impersonate_rotation_skips_the_alias_a_pinned_profile_resolves_to():
    from curl_cffi.requests.impersonate import resolve_latest_browser_type

    newest = resolve_latest_browser_type("safari")
    assert impersonate_rotation(newest) == (newest, "chrome", "firefox")


def test_validate_impersonate_matches_what_curl_cffi_can_use():
    from typing import get_args

    from curl_cffi.requests import Session
    from curl_cffi.requests.exceptions import ImpersonateError, RequestException
    from curl_cffi.requests.impersonate import BrowserTypeLiteral

    def usable(target: str) -> bool:
        # Port 1 refuses the connection, after curl_cffi has applied the target
        with Session() as session:
            try:
                session.get("http://127.0.0.1:1/", impersonate=target, timeout=5)
            except ImpersonateError:
                return False
            except RequestException:
                return True
        raise AssertionError("port 1 accepted a connection")

    targets = {*get_args(BrowserTypeLiteral), "tor"}
    assert {"chrome", "firefox", "safari", "edge", "chrome150", "tor145"} <= targets
    for target in targets:
        if usable(target):
            assert validate_impersonate(target) == target
        else:
            with pytest.raises(ValueError, match="unknown impersonate target"):
                validate_impersonate(target)


@pytest.mark.parametrize("target", ["nosuchbrowser", "", "Chrome", "chrome "])
def test_validate_impersonate_rejects(target):
    with pytest.raises(ValueError, match="chrome, firefox or safari") as info:
        validate_impersonate(target)
    assert repr(target) in str(info.value)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, (None, None)),
        ({"SSL_CERT_FILE": "a", "REQUESTS_CA_BUNDLE": "b", "CURL_CA_BUNDLE": "c"}, ("a", None)),
        ({"REQUESTS_CA_BUNDLE": "b", "CURL_CA_BUNDLE": "c"}, ("b", None)),
        ({"CURL_CA_BUNDLE": "c"}, ("c", None)),
        ({"SSL_CERT_FILE": "", "CURL_CA_BUNDLE": "c"}, ("c", None)),
        ({"SSL_CERT_DIR": "d"}, (None, "d")),
        ({"SSL_CERT_FILE": "a", "SSL_CERT_DIR": "d"}, ("a", "d")),
    ],
)
def test_ca_locations_precedence(ca_env, tmp_path, env, expected):
    for name in "abc":
        (tmp_path / name).write_text("")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "89abcdef.0").write_text("-----BEGIN CERTIFICATE-----\n")
    for name, value in env.items():
        ca_env.setenv(name, value and str(tmp_path / value))
    assert ca_locations() == tuple(value and str(tmp_path / value) for value in expected)
    assert ca_locations(trust_env=False) == (None, None)


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
@pytest.mark.parametrize("missing", [True, False])
def test_unreadable_ca_file_names_the_variable(ca_env, tmp_path, variable, missing):
    """A missing file, or a directory, never quietly falls back to another bundle."""
    location = str(tmp_path / "nonexistent.pem") if missing else str(tmp_path)
    ca_env.setenv(variable, location)
    with pytest.raises(ValueError, match=f"^{variable} in the environment: can't read"):
        ca_locations()
    assert ca_locations(trust_env=False) == (None, None)


@pytest.mark.parametrize("layout", ["missing", "empty", "unhashed"])
@pytest.mark.parametrize("with_file", [False, True])
def test_ca_directory_without_a_hashed_certificate_is_an_error(ca_env, tmp_path, layout, with_file):
    """Never left for curl to back with libcurl's built-in bundle."""
    directory = tmp_path / "dir"
    if layout != "missing":
        directory.mkdir()
    if layout == "unhashed":
        (directory / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    if with_file:
        (tmp_path / "ca.pem").write_text("")
        ca_env.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    ca_env.setenv("SSL_CERT_DIR", str(directory))
    with pytest.raises(ValueError, match="^SSL_CERT_DIR in the environment: no CA certificate"):
        ca_locations()
    assert ca_locations(trust_env=False) == (None, None)


async def _curl_ca(**kwargs) -> dict:
    from curl_cffi import CurlOpt

    names = {
        CurlOpt.CAINFO: "cainfo",
        CurlOpt.CAPATH: "capath",
        CurlOpt.PROXY_CAINFO: "proxy_cainfo",
        CurlOpt.PROXY_CAPATH: "proxy_capath",
    }
    async with CurlCffiFetcher("chrome", **kwargs) as fetcher:
        options = fetcher._session("chrome").curl_options
    return {names[key]: value for key, value in options.items() if key in names}


def _both(cafile, capath) -> dict:
    return {"cainfo": cafile, "capath": capath, "proxy_cainfo": cafile, "proxy_capath": capath}


async def test_curl_fetcher_reads_the_ca_file_on_entry(ca_env, tmp_path):
    """Set after import; the file stands in as the unused directory."""
    cafile = str(tmp_path / "file.pem")
    (tmp_path / "file.pem").write_text("")
    ca_env.setenv("REQUESTS_CA_BUNDLE", cafile)
    assert await _curl_ca() == _both(cafile, cafile)
    (tmp_path / "89abcdef.0").write_text("-----BEGIN CERTIFICATE-----\n")
    ca_env.setenv("SSL_CERT_DIR", str(tmp_path))
    assert await _curl_ca() == _both(cafile, str(tmp_path))


async def test_curl_fetcher_reads_the_ca_directory_on_entry(ca_env, tmp_path):
    """A hashed certificate from the directory stands in as the unused file."""
    (tmp_path / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    (tmp_path / "0123abcd.r0").write_text("-----BEGIN X509 CRL-----\n")
    (tmp_path / "89abcdef.0").write_text("-----BEGIN CERTIFICATE-----\n")
    ca_env.setenv("SSL_CERT_DIR", str(tmp_path))
    assert await _curl_ca() == _both(str(tmp_path / "89abcdef.0"), str(tmp_path))


async def test_curl_sessions_opened_later_keep_the_ca_read_on_entry(ca_env, tmp_path):
    """Whatever the environment says mid-run, as for the sessions opened first."""
    from curl_cffi import CurlOpt

    for name in ("a.pem", "b.pem"):
        (tmp_path / name).write_text("")
    ca_env.setenv("SSL_CERT_FILE", str(tmp_path / "a.pem"))
    async with CurlCffiFetcher("chrome") as fetcher:
        ca_env.setenv("SSL_CERT_FILE", str(tmp_path / "b.pem"))
        for profile, raw in (("chrome", False), ("safari", False), ("chrome", True)):
            options = fetcher._session(profile, raw).curl_options
            assert options[CurlOpt.CAINFO] == str(tmp_path / "a.pem")


@pytest.mark.parametrize("kwargs", [{}, {"trust_env": False}])
async def test_curl_session_uses_certifi_otherwise(ca_env, kwargs):
    import certifi

    if kwargs:
        ca_env.setenv("SSL_CERT_FILE", "/ca/file.pem")
        ca_env.setenv("SSL_CERT_DIR", "/ca/dir")
    assert await _curl_ca(**kwargs) == _both(certifi.where(), certifi.where())


async def test_curl_session_without_verification_sets_no_ca(ca_env):
    ca_env.setenv("SSL_CERT_FILE", "/ca/file.pem")
    assert await _curl_ca(verify=False) == {}


def test_explicit_proxy_is_always_used(clean_proxy_env):
    clean_proxy_env.setenv("NO_PROXY", "*")
    assert uses_proxy("https://a.com", "http://gw:8080")


def test_explicit_proxy_skips_the_environment(clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", "socks4://h:1")
    check_proxy_support("http://h:1")


@pytest.mark.parametrize(("env", "raises"), ENV_CASES, ids=ENV_IDS)
def test_environment_check_matches_recorded(clean_proxy_env, env, raises):
    for name, value in env.items():
        clean_proxy_env.setenv(name, value)
    if raises:
        with pytest.raises(ValueError):
            check_proxy_support(None)
    else:
        check_proxy_support(None)


async def test_environment_socks_proxy_builds_a_client(clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", "socks5://h:1080")
    check_proxy_support(None)
    async with CurlCffiFetcher():
        pass


async def test_explicit_proxy_beats_environment(clean_proxy_env, recording_proxy):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        clean_proxy_env.setenv(name, DEAD_PROXY)
    proxy = recording_proxy()
    async with asyncio.timeout(5):
        async with CurlCffiFetcher(None, proxy=proxy.url) as fetcher:
            response = await fetcher.fetch(make_request("http://shop.test/products.json"))
    assert response.json() == {"proxied": True}
    assert proxy.records == [("GET", "http://shop.test/products.json", None)]


async def test_environment_proxy_is_used_without_option(clean_proxy_env, recording_proxy):
    proxy = recording_proxy()
    clean_proxy_env.setenv("HTTP_PROXY", proxy.url)
    async with asyncio.timeout(5):
        async with CurlCffiFetcher(None) as fetcher:
            await fetcher.fetch(make_request("http://shop.test/robots.txt"))
    assert proxy.records == [("GET", "http://shop.test/robots.txt", None)]


async def test_proxy_407_on_connect_hides_credentials(clean_proxy_env, recording_proxy):
    proxy = recording_proxy(reject_connect=True)
    url = f"http://u:secret@127.0.0.1:{proxy.port}"
    async with asyncio.timeout(5):
        async with CurlCffiFetcher(None, proxy=url) as fetcher:
            with pytest.raises(FetchConnectionError) as info:
                await fetcher.fetch(make_request("https://shop.test/"))
    assert "407" in str(info.value)
    assert "secret" not in str(info.value)
    [(command, _, auth)] = proxy.records
    assert command == "CONNECT"
    assert auth is not None and auth.startswith("Basic ")


# --- FakeFetcher ----------------------------------------------------------------------


async def test_fake_fetcher_seq_served_in_order_last_repeats():
    fetcher = FakeFetcher({"https://a.com/x": Seq([(500, ""), (200, "ok")])})
    statuses = [(await fetcher.fetch(make_request("https://a.com/x"))).status for _ in range(3)]
    assert statuses == [500, 200, 200]
    assert fetcher.count("https://a.com/x") == 3


async def test_fake_fetcher_sync_and_async_callables():
    async def async_route(request):
        return (201, request.url)

    fetcher = FakeFetcher(
        {
            "https://a.com/sync": lambda request: (200, request.identity.user_agent),
            "https://a.com/async": async_route,
        }
    )
    sync_resp = await fetcher.fetch(make_request("https://a.com/sync", ua="ua-x"))
    async_resp = await fetcher.fetch(make_request("https://a.com/async"))
    assert sync_resp.body == b"ua-x"
    assert (async_resp.status, async_resp.text) == (201, "https://a.com/async")


async def test_fake_fetcher_tuple_with_headers():
    fetcher = FakeFetcher({"https://a.com/r": (302, b"", {"Location": "/next"})})
    response = await fetcher.fetch(make_request("https://a.com/r"))
    assert response.status == 302
    assert response.headers == {"location": "/next"}


async def test_fake_fetcher_dict_and_list_served_as_json():
    fetcher = FakeFetcher({"https://a.com/d": {"a": 1}, "https://a.com/l": [1, 2]})
    d = await fetcher.fetch(make_request("https://a.com/d"))
    lst = await fetcher.fetch(make_request("https://a.com/l"))
    assert (d.status, d.json(), d.headers["content-type"]) == (200, {"a": 1}, "application/json")
    assert lst.json() == [1, 2]


async def test_fake_fetcher_exception_spec_raises():
    fetcher = FakeFetcher({"https://a.com/x": FetchTimeout("slow")})
    with pytest.raises(FetchTimeout):
        await fetcher.fetch(make_request("https://a.com/x"))


async def test_fake_fetcher_default_404_and_count():
    fetcher = FakeFetcher()
    response = await fetcher.fetch(make_request("https://a.com/missing"))
    assert response.status == 404
    assert fetcher.count("https://a.com/missing") == 1
    assert fetcher.count("https://a.com/other") == 0
    assert [r.url for r in fetcher.requests] == ["https://a.com/missing"]


async def test_fake_fetcher_routes_are_canonicalized():
    fetcher = FakeFetcher({"https://A.com/p?b=2&a=1": {"ok": True}})
    response = await fetcher.fetch(make_request("https://a.com/p?a=1&b=2#frag"))
    assert response.status == 200


# --- ReplayFetcher and load_corpus ----------------------------------------------------


def outcome(status: int, body: bytes, headers: dict | None = None) -> dict:
    return {"status": status, "headers": headers or {}, "body": base64.b64encode(body).decode()}


async def test_replay_outcomes_in_order_last_repeats():
    fetcher = ReplayFetcher(
        {"GET https://a.com/x": [outcome(503, b"busy"), outcome(200, b"ok", {"X-A": "1"})]}
    )
    first, second, third = [await fetcher.fetch(make_request("https://a.com/x")) for _ in range(3)]
    assert (first.status, first.body) == (503, b"busy")
    assert (second.status, second.body, second.headers) == (200, b"ok", {"x-a": "1"})
    assert third.status == 200


@pytest.mark.parametrize(
    "name, exc",
    [("timeout", FetchTimeout), ("tls", FetchTLSError), ("incomplete", FetchIncomplete)],
)
async def test_replay_recorded_errors_raise_mapped_exception(name, exc):
    fetcher = ReplayFetcher({"GET https://a.com/x": [{"error": name}]})
    with pytest.raises(exc):
        await fetcher.fetch(make_request("https://a.com/x"))


async def test_replay_unknown_error_is_connection_error():
    fetcher = ReplayFetcher({"GET https://a.com/x": [{"error": "weird"}]})
    with pytest.raises(FetchConnectionError):
        await fetcher.fetch(make_request("https://a.com/x"))


async def test_replay_miss_recorded_and_returns_404():
    fetcher = ReplayFetcher({})
    response = await fetcher.fetch(make_request("https://a.com/nope?b=1&a=2"))
    assert response.status == 404
    assert fetcher.misses == ["GET https://a.com/nope?a=2&b=1"]


async def test_replay_gzip_body_decoded():
    recorded = outcome(200, gzip.compress(PAYLOAD), {"Content-Encoding": "gzip"})
    fetcher = ReplayFetcher({"GET https://a.com/x": [recorded]})
    response = await fetcher.fetch(make_request("https://a.com/x"))
    assert response.body == PAYLOAD


@pytest.mark.parametrize("name", ["corpus.json", "corpus.json.gz"])
def test_load_corpus(tmp_path, name):
    entries = {"GET https://a.com/x": [outcome(200, b"ok")]}
    raw = json.dumps({"entries": entries}).encode()
    path = tmp_path / name
    path.write_bytes(gzip.compress(raw) if name.endswith(".gz") else raw)
    assert load_corpus(path) == entries
