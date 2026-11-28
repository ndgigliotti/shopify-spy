"""CurlCffiFetcher's contract against local servers: the same as HttpxFetcher's.

A local server can't check the TLS fingerprint, only what the fetcher does with
redirects, cookies, sizes, encodings, deadlines, errors and proxies.
"""

import asyncio
import base64
import gzip
import hashlib
import http.server
import json
import selectors
import socket
import socketserver
import ssl
import threading
import time
from types import SimpleNamespace

import pytest
import trustme
from cryptography import x509

from shopify_spy import _fetch
from shopify_spy._api import _PausedClockLoop
from shopify_spy._fetch import (
    IMPERSONATE_PROFILES,
    CurlCffiFetcher,
    FetchConnectionError,
    FetchIncomplete,
    FetchInvalidURL,
    FetchTimeout,
    FetchTLSError,
    FetchTooLarge,
    HttpxFetcher,
    Identity,
    Request,
)
from shopify_spy._policy import DEFAULT_HEADERS
from tests.conftest import CA_VARIABLES, _RecordingProxyHandler

pytestmark = [
    pytest.mark.e2e,
    # On Windows this covers the Proactor warning; everywhere, the per-request
    # "https over https proxy" warning (test_https_proxy_receives_connect)
    pytest.mark.filterwarnings("error::curl_cffi.utils.CurlCffiWarning"),
]

PAYLOAD = b'{"products": []}' * 20
BROTLI_HELLO = b"\x0b\x02\x80hello\x03"  # "hello", brotli-encoded
DEAD_PROXY = "http://127.0.0.1:9"
SOCKS_NAME = "store.socks-test.invalid"  # only the SOCKS proxy below can resolve it
# How long the endless handlers keep going if the client never hangs up
ENDLESS = 10


def make_request(
    url: str, *, timeout: float = 3.0, headers: dict[str, str] | None = None
) -> Request:
    return Request(
        method="GET",
        url=url,
        identity=Identity(user_agent="pool-agent/1.0", headers=dict(DEFAULT_HEADERS)),
        timeout=timeout,
        headers=headers or {},
    )


# --- Local servers ------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def _write_forever(self, chunk: bytes, pause: float) -> None:
        deadline = time.monotonic() + ENDLESS
        while time.monotonic() < deadline:
            self.wfile.write(chunk)
            self.wfile.flush()
            time.sleep(pause)

    def _route(self):
        if self.path in STATIC:
            self._send(*STATIC[self.path])
        elif self.path.startswith("/delayed-body/"):
            self._delayed_body(float(self.path.rsplit("/", 1)[1]))
        else:
            handlers = {
                "/echo": self._echo,
                "/endless": self._endless,
                "/trickle": self._trickle,
                "/no-headers": self._no_headers,
                "/truncated": self._truncated,
            }
            handlers.get(self.path, self._not_found)()

    def _not_found(self):
        self._send(404, b"not found")

    def _echo(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self._send(200, body, {"Content-Type": "application/json"})

    def _endless(self):
        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._write_forever(b"x" * 16384, 0)

    def _trickle(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        self._write_forever(b"x", 0.1)

    def _no_headers(self):
        time.sleep(ENDLESS)

    def _truncated(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"x" * 10)
        self.close_connection = True

    def _delayed_body(self, delay: float):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.flush()
        time.sleep(delay)
        self.wfile.write(b"ok")


# path -> (status, body, headers)
STATIC = {
    "/ok": (200, b"ok"),
    "/redirect": (302, b"", {"Location": "/target"}),
    "/set-cookie": (200, b"ok", {"Set-Cookie": "session=secret; Path=/"}),
    "/big": (200, b"x" * 5000),
    "/gzip": (200, gzip.compress(PAYLOAD), {"Content-Encoding": "gzip"}),
    "/br": (200, BROTLI_HELLO, {"Content-Encoding": "br"}),
    "/utf-8": (200, b"as sent", {"Content-Encoding": "utf-8"}),
    "/gzip-bomb": (200, gzip.compress(b"\0" * 5000), {"Content-Encoding": "gzip"}),
    # Unknown token first: libcurl refuses it, so the raw path decodes the gzip
    "/gzip-bomb-unknown": (200, gzip.compress(b"\0" * 5000), {"Content-Encoding": "utf-8, gzip"}),
    "/gzip-unknown": (200, gzip.compress(PAYLOAD), {"Content-Encoding": "utf-8, gzip"}),
    "/mixed-case": (200, b"ok", {"X-Custom-Header": "Value", "Content-Type": "text/plain"}),
}


def _serve(handler, *, tls: SimpleNamespace | None = None, **attrs):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    for name, value in attrs.items():
        setattr(httpd, name, value)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls.cert, tls.key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture(scope="module")
def server():
    httpd = _serve(Handler, paths=[])
    yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}", paths=httpd.paths)
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="session")
def tls(tmp_path_factory):
    """A throwaway CA and a 127.0.0.1 certificate it signed, as PEM file paths."""
    ca = trustme.CA()
    issued = ca.issue_cert("127.0.0.1", "localhost")
    directory = tmp_path_factory.mktemp("tls")
    paths = SimpleNamespace(
        ca=str(directory / "ca.pem"),
        cert=str(directory / "cert.pem"),
        key=str(directory / "key.pem"),
    )
    ca.cert_pem.write_to_path(paths.ca)
    issued.cert_chain_pems[0].write_to_path(paths.cert)
    issued.private_key_pem.write_to_path(paths.key)
    return paths


@pytest.fixture(scope="module")
def tls_server(tls):
    httpd = _serve(Handler, tls=tls, paths=[])
    yield f"https://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("client closed")
        data += chunk
    return data


class _Socks5Handler(socketserver.BaseRequestHandler):
    """No-auth SOCKS5 CONNECT that records (address type, host, port) and tunnels.

    SOCKS_NAME resolves to 127.0.0.1 here and nowhere else.
    """

    def handle(self):
        client = self.request
        _, methods = _recv_exact(client, 2)
        _recv_exact(client, methods)
        client.sendall(b"\x05\x00")
        _, _, _, atyp = _recv_exact(client, 4)
        if atyp == 3:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode()
        elif atyp == 1:
            host = socket.inet_ntoa(_recv_exact(client, 4))
        else:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        port = int.from_bytes(_recv_exact(client, 2), "big")
        self.server.records.append((atyp, host, port))  # type: ignore[attr-defined]
        upstream = socket.create_connection(("127.0.0.1" if host == SOCKS_NAME else host, port))
        client.sendall(b"\x05\x00\x00\x01" + bytes(6))
        with upstream, selectors.DefaultSelector() as sel:
            sel.register(client, selectors.EVENT_READ, upstream)
            sel.register(upstream, selectors.EVENT_READ, client)
            while True:
                for key, _ in sel.select(timeout=ENDLESS):
                    data = key.fileobj.recv(65536)  # type: ignore[union-attr]
                    if not data:
                        return
                    key.data.sendall(data)


@pytest.fixture
def socks_proxy():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Socks5Handler)
    srv.daemon_threads = True
    srv.records = []  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=srv.server_address[1], records=srv.records)  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()


async def fetch(url: str, transport: str = "curl", **kwargs) -> _fetch.Response:
    timeout = kwargs.pop("timeout", 3.0)
    headers = kwargs.pop("headers", None)
    fetcher = CurlCffiFetcher("chrome", **kwargs) if transport == "curl" else HttpxFetcher(**kwargs)
    async with fetcher:
        return await fetcher.fetch(make_request(url, timeout=timeout, headers=headers))


# --- HTTP contract -------------------------------------------------------------------------


async def test_redirect_is_returned_not_followed(server):
    response = await fetch(f"{server.url}/redirect")
    assert response.status == 302
    assert response.location == "/target"
    assert response.url == f"{server.url}/redirect"


async def test_cookies_are_never_sent_back(server):
    async with CurlCffiFetcher("chrome") as fetcher:
        await fetcher.fetch(make_request(f"{server.url}/set-cookie"))
        headers = (await fetcher.fetch(make_request(f"{server.url}/echo"))).json()
    assert "cookie" not in headers


async def test_profile_headers_replace_identity(server):
    headers = (await fetch(f"{server.url}/echo")).json()
    assert "Chrome/" in headers["user-agent"]
    assert "pool-agent" not in headers["user-agent"]
    for name, value in DEFAULT_HEADERS.items():
        assert headers.get(name.lower()) != value


@pytest.mark.parametrize(
    ("profile", "token"), [("chrome", "Chrome/"), ("safari", "Version/"), ("firefox", "Firefox/")]
)
async def test_each_pool_profile_sends_its_own_desktop_user_agent(server, profile, token):
    assert profile in IMPERSONATE_PROFILES
    request = Request(
        method="GET",
        url=f"{server.url}/echo",
        identity=Identity(user_agent=None, impersonate=profile),
        timeout=3.0,
    )
    async with CurlCffiFetcher("firefox" if profile == "chrome" else "chrome") as fetcher:
        agent = (await fetcher.fetch(request)).json()["user-agent"]
    assert token in agent
    # Shopify answers 429 to every request from a plain Linux desktop user agent (#62)
    assert "Linux" not in agent and "Mobile" not in agent
    assert "Macintosh" in agent or "Windows" in agent


async def test_profiles_never_share_a_connection(server):
    async with CurlCffiFetcher() as fetcher:
        for profile in ("chrome", "safari", "chrome"):
            identity = Identity(user_agent=None, impersonate=profile)
            await fetcher.fetch(Request("GET", f"{server.url}/echo", identity, timeout=3.0))
        assert set(fetcher._sessions) == {("chrome", False), ("safari", False)}


async def test_request_headers_override_the_profile(server):
    headers = (await fetch(f"{server.url}/echo", headers={"Accept-Language": "de"})).json()
    assert headers["accept-language"] == "de"


async def test_response_headers_lowercased(server):
    response = await fetch(f"{server.url}/mixed-case")
    assert response.headers["x-custom-header"] == "Value"
    assert all(name == name.lower() for name in response.headers)


async def test_sixteen_requests_are_in_flight_at_once():
    barrier = threading.Barrier(16, timeout=5)

    class BarrierHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            barrier.wait()  # answers only once all 16 requests have arrived
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    httpd = _serve(BarrierHandler)
    # The server is already listening with socketserver's backlog of 5, so
    # request_queue_size would come too late; macOS drops the extra SYNs and
    # the client's retransmit backoff outlasts the deadline
    httpd.socket.listen(64)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    try:
        start = time.monotonic()
        async with CurlCffiFetcher("chrome") as fetcher:
            responses = await asyncio.gather(
                *(fetcher.fetch(make_request(url, timeout=3)) for _ in range(16))
            )
        assert [r.status for r in responses] == [200] * 16
        assert time.monotonic() - start < 3
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- Sizes, deadlines and latency ----------------------------------------------------------


async def test_body_over_max_size(server, monkeypatch):
    monkeypatch.setattr(_fetch, "MAX_SIZE", 1000)
    with pytest.raises(FetchTooLarge):
        await fetch(f"{server.url}/big")


async def _then_ok(fetcher: CurlCffiFetcher, url: str) -> None:
    response = await fetcher.fetch(make_request(f"{url}/ok"))
    assert (response.status, response.body) == (200, b"ok")


async def test_endless_body_stops_at_max_size(server, monkeypatch):
    monkeypatch.setattr(_fetch, "MAX_SIZE", 100_000)
    async with CurlCffiFetcher("chrome") as fetcher:
        start = time.monotonic()
        with pytest.raises(FetchTooLarge):
            await fetcher.fetch(make_request(f"{server.url}/endless", timeout=3))
        assert time.monotonic() - start < 1
        await _then_ok(fetcher, server.url)


@pytest.mark.parametrize("path", ["/trickle", "/no-headers"])
async def test_total_deadline(server, path):
    async with CurlCffiFetcher("chrome") as fetcher:
        start = time.monotonic()
        with pytest.raises(FetchTimeout):
            await fetcher.fetch(make_request(f"{server.url}{path}", timeout=1))
        assert time.monotonic() - start < 2
        await _then_ok(fetcher, server.url)


async def test_latency_is_time_to_first_body_byte(server):
    response = await fetch(f"{server.url}/delayed-body/0.3")
    assert response.body == b"ok"
    assert response.latency >= 0.3


async def test_latency_without_body_is_time_to_response(server):
    response = await fetch(f"{server.url}/redirect")
    assert 0 < response.latency < 1


def test_paused_clock_excludes_time_outside_the_loop(server):
    # Driven the way scrape() drives its loop: the clock stops between
    # run_until_complete calls, so the 2s sleep must not count
    loop = _PausedClockLoop()
    try:
        fetcher = CurlCffiFetcher("chrome")
        loop.run_until_complete(fetcher.__aenter__())
        task = loop.create_task(
            fetcher.fetch(make_request(f"{server.url}/delayed-body/0.5", timeout=1))
        )
        loop.run_until_complete(asyncio.sleep(0.1))
        time.sleep(2)
        response = loop.run_until_complete(task)
        assert response.body == b"ok"
        assert response.latency < 1
        loop.run_until_complete(fetcher.__aexit__(None, None, None))
    finally:
        loop.close()


# --- Encodings -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/gzip", PAYLOAD),
        ("/br", b"hello"),
        ("/utf-8", b"as sent"),  # unknown token: passed through, as HttpxFetcher does
        ("/gzip-unknown", PAYLOAD),
    ],
)
async def test_bodies_are_decoded(server, path, expected):
    response = await fetch(f"{server.url}{path}")
    assert (response.status, response.body) == (200, expected)


@pytest.mark.parametrize("path", ["/gzip-bomb", "/gzip-bomb-unknown"])
async def test_decoded_body_over_max_size(server, monkeypatch, path):
    monkeypatch.setattr(_fetch, "MAX_SIZE", 1000)
    with pytest.raises(FetchTooLarge):
        await fetch(f"{server.url}{path}")


# --- Errors and TLS ------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_connection_refused():
    with pytest.raises(FetchConnectionError) as info:
        # Windows retries a refused connect for about 2s before reporting it
        await fetch(f"http://127.0.0.1:{_free_port()}/", timeout=4)
    assert not isinstance(info.value, FetchTLSError)


async def test_unknown_host():
    with pytest.raises(FetchConnectionError):
        await fetch("http://no-such-host.invalid/", timeout=5)


async def test_invalid_url_never_reaches_the_server(server):
    before = list(server.paths)
    with pytest.raises(FetchInvalidURL):
        await fetch(f"{server.url}/bad\x7fpath")
    assert server.paths == before


@pytest.mark.parametrize(
    "url", ["file:///etc/hostname", "dict://127.0.0.1:1/", "ftp://127.0.0.1:1/"]
)
async def test_non_http_scheme_is_refused(url):
    with pytest.raises(FetchConnectionError, match="unsupported protocol"):
        await fetch(url)


async def test_truncated_body(server):
    with pytest.raises(FetchIncomplete):
        await fetch(f"{server.url}/truncated")


async def test_self_signed_certificate(tls_server):
    with pytest.raises(FetchTLSError):
        await fetch(f"{tls_server}/ok")
    response = await fetch(f"{tls_server}/ok", verify=False)
    assert (response.status, response.body) == (200, b"ok")


def _der(tag: int, body: bytes) -> bytes:
    size = len(body)
    if size < 0x80:
        return bytes([tag, size]) + body
    length = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + body


def _subject_hash(pem: bytes) -> str:
    """OpenSSL's ``x509 -subject_hash``, the file name a CA directory looks up.

    The hash of the subject's canonical form: each RDN as a DER SET of its
    attributes, values as lowercased UTF8Strings with whitespace collapsed.
    """
    out = b""
    for rdn in x509.load_pem_x509_certificate(pem).subject.rdns:
        attributes = []
        for attribute in rdn:
            first, second, *rest = (int(p) for p in attribute.oid.dotted_string.split("."))
            oid = bytes([40 * first + second])
            for arc in rest:
                chunk = [arc & 0x7F]
                while arc := arc >> 7:
                    chunk.append(0x80 | (arc & 0x7F))
                oid += bytes(reversed(chunk))
            value = " ".join(str(attribute.value).split()).lower().encode()
            attributes.append(_der(0x30, _der(0x06, oid) + _der(0x0C, value)))
        out += _der(0x31, b"".join(sorted(attributes)))
    return f"{int.from_bytes(hashlib.sha1(out).digest()[:4], 'little'):08x}"


def _ca_dir(directory, pem: bytes) -> str:
    """Laid out as OpenSSL's c_rehash would; BoringSSL reads it the same way."""
    directory.mkdir(exist_ok=True)
    (directory / f"{_subject_hash(pem)}.0").write_bytes(pem)
    return str(directory)


@pytest.mark.parametrize("transport", ["curl", "httpx"])
@pytest.mark.parametrize("variable", CA_VARIABLES)
async def test_ca_from_the_environment_is_trusted(
    tls, tls_server, tmp_path, ca_env, transport, variable
):
    """Set after import, as a company proxy's CA would be."""
    pem = open(tls.ca, "rb").read()
    location = _ca_dir(tmp_path / "ca", pem) if variable == "SSL_CERT_DIR" else tls.ca
    ca_env.setenv(variable, location)
    response = await fetch(f"{tls_server}/ok", transport)
    assert (response.status, response.body) == (200, b"ok")


@pytest.mark.parametrize("transport", ["curl", "httpx"])
async def test_ca_file_comes_first(tls, tls_server, tmp_path, ca_env, transport):
    (tmp_path / "other.pem").write_bytes(trustme.CA().cert_pem.bytes())
    ca_env.setenv("SSL_CERT_FILE", str(tmp_path / "other.pem"))
    ca_env.setenv("REQUESTS_CA_BUNDLE", tls.ca)
    with pytest.raises(FetchTLSError):
        await fetch(f"{tls_server}/ok", transport)
    ca_env.setenv("SSL_CERT_FILE", "")
    assert (await fetch(f"{tls_server}/ok", transport)).status == 200


@pytest.mark.parametrize("transport", ["curl", "httpx"])
async def test_ca_file_and_directory_are_both_trusted(tls, tls_server, tmp_path, ca_env, transport):
    pem = open(tls.ca, "rb").read()
    (tmp_path / "other.pem").write_bytes(trustme.CA().cert_pem.bytes())
    ca_env.setenv("SSL_CERT_FILE", str(tmp_path / "other.pem"))
    ca_env.setenv("SSL_CERT_DIR", _ca_dir(tmp_path / "ca", pem))
    assert (await fetch(f"{tls_server}/ok", transport)).status == 200


@pytest.mark.parametrize("transport", ["curl", "httpx"])
@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
async def test_ca_from_the_environment_replaces_the_default(
    tls_server, tmp_path, ca_env, transport, variable
):
    """Without the server's CA, the default trust store doesn't fill in, and
    ``verify=False`` still skips verification."""
    other = trustme.CA().cert_pem.bytes()
    (tmp_path / "other.pem").write_bytes(other)
    location = (
        _ca_dir(tmp_path / "dir", other)
        if variable == "SSL_CERT_DIR"
        else str(tmp_path / "other.pem")
    )
    ca_env.setenv(variable, location)
    with pytest.raises(FetchTLSError):
        await fetch(f"{tls_server}/ok", transport)
    assert (await fetch(f"{tls_server}/ok", transport, verify=False)).status == 200


@pytest.mark.parametrize("transport", ["curl", "httpx"])
@pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "SSL_CERT_DIR"])
async def test_unusable_ca_location_fails_the_same_way(
    tls_server, tmp_path, ca_env, transport, variable
):
    """A missing file, or a directory with no hashed certificate."""
    location = tmp_path if variable == "SSL_CERT_DIR" else tmp_path / "nonexistent.pem"
    ca_env.setenv(variable, str(location))
    with pytest.raises(ValueError, match=f"^{variable} in the environment"):
        await fetch(f"{tls_server}/ok", transport)


@pytest.mark.parametrize("transport", ["curl", "httpx"])
async def test_trust_env_off_ignores_the_ca_environment(
    tls, tls_server, tmp_path, ca_env, transport
):
    ca_env.setenv("SSL_CERT_FILE", tls.ca)
    ca_env.setenv("SSL_CERT_DIR", _ca_dir(tmp_path / "ca", open(tls.ca, "rb").read()))
    with pytest.raises(FetchTLSError):
        await fetch(f"{tls_server}/ok", transport, trust_env=False)


# --- Proxies -------------------------------------------------------------------------------


async def test_explicit_proxy_replaces_the_environment(server, clean_proxy_env, recording_proxy):
    proxy = recording_proxy()
    for name in ("HTTP_PROXY", "http_proxy"):
        clean_proxy_env.setenv(name, DEAD_PROXY)
    clean_proxy_env.setenv("NO_PROXY", "127.0.0.1")
    response = await fetch(f"{server.url}/echo", proxy=proxy.url)
    assert response.json() == {"proxied": True}
    assert proxy.records[0][:2] == ("GET", f"{server.url}/echo")


async def test_environment_proxy_is_used(server, clean_proxy_env, recording_proxy):
    proxy = recording_proxy()
    # Uppercase only: libcurl itself ignores HTTP_PROXY, httpx doesn't
    clean_proxy_env.setenv("HTTP_PROXY", proxy.url)
    clean_proxy_env.setenv("NO_PROXY", "other.example")
    response = await fetch(f"{server.url}/echo")
    assert response.json() == {"proxied": True}
    assert len(proxy.records) == 1


async def test_trust_env_off_ignores_the_environment_proxy(server, clean_proxy_env):
    clean_proxy_env.setenv("HTTP_PROXY", DEAD_PROXY)
    response = await fetch(f"{server.url}/ok", trust_env=False)
    assert (response.status, response.body) == (200, b"ok")


async def test_no_proxy_host_goes_direct(server, clean_proxy_env):
    # libcurl would read http_proxy on its own and fail on the dead port
    for name in ("HTTP_PROXY", "http_proxy"):
        clean_proxy_env.setenv(name, DEAD_PROXY)
    clean_proxy_env.setenv("NO_PROXY", "127.0.0.1")
    response = await fetch(f"{server.url}/ok")
    assert (response.status, response.body) == (200, b"ok")


async def test_no_proxy_variables_go_direct(server, clean_proxy_env):
    response = await fetch(f"{server.url}/ok")
    assert (response.status, response.body) == (200, b"ok")


async def test_https_with_bypassed_environment_proxy(tls_server, clean_proxy_env):
    # The empty proxy for direct requests comes with HTTPPROXYTUNNEL=1 for https://
    for name in ("HTTPS_PROXY", "https_proxy"):
        clean_proxy_env.setenv(name, DEAD_PROXY)
    clean_proxy_env.setenv("NO_PROXY", "127.0.0.1")
    response = await fetch(f"{tls_server}/ok", verify=False)
    assert (response.status, response.body) == (200, b"ok")


async def test_https_proxy_receives_connect(clean_proxy_env, tls):
    httpd = _serve(_RecordingProxyHandler, tls=tls, records=[])
    proxy = f"https://127.0.0.1:{httpd.server_address[1]}"
    try:
        # The proxy's certificate is checked even with verify=False, as in httpx;
        # curl_cffi takes a CA bundle path as verify, which trusts the test CA here
        with pytest.raises(FetchConnectionError) as info:  # the proxy's 502 to CONNECT
            await fetch("https://store.example:8443/", proxy=proxy, verify=tls.ca)
        assert not isinstance(info.value, FetchTLSError)
        assert httpd.records[0][:2] == ("CONNECT", "store.example:8443")
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_proxy_credentials_reach_the_proxy(server, clean_proxy_env, recording_proxy):
    proxy = recording_proxy()
    url = proxy.url.replace("http://", "http://us?er:pa/ss@")
    await fetch(f"{server.url}/echo", proxy=url)
    assert proxy.records[0][2] == "Basic " + base64.b64encode(b"us?er:pa/ss").decode()


async def test_proxy_credentials_stay_out_of_errors(server, clean_proxy_env):
    with pytest.raises(FetchConnectionError) as info:
        await fetch(f"{server.url}/ok", proxy="http://user:hunter2@127.0.0.1:9")
    assert "hunter2" not in str(info.value)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
@pytest.mark.parametrize("via", ["proxy", "ALL_PROXY"])
async def test_socks_proxy_resolves_the_name(server, clean_proxy_env, socks_proxy, scheme, via):
    proxy = f"{scheme}://127.0.0.1:{socks_proxy.port}"
    port = int(server.url.rsplit(":", 1)[1])
    kwargs = {}
    if via == "proxy":
        kwargs["proxy"] = proxy
    else:
        clean_proxy_env.setenv("ALL_PROXY", proxy)
    response = await fetch(f"http://{SOCKS_NAME}:{port}/ok", **kwargs)
    assert (response.status, response.body) == (200, b"ok")
    assert socks_proxy.records == [(3, SOCKS_NAME, port)]
