"""Tests for the transport layer: HttpxFetcher, FakeFetcher, ReplayFetcher."""

import asyncio
import base64
import gzip
import http.server
import json
import socket
import ssl
import threading
import time
import zlib

import httpx
import pytest
import respx

from shopify_spy._fetch import (
    ACCEPT_ENCODING,
    FakeFetcher,
    FetchConnectionError,
    FetchIncomplete,
    FetchInvalidURL,
    FetchTimeout,
    FetchTLSError,
    FetchTooLarge,
    HttpxFetcher,
    Identity,
    ReplayFetcher,
    Request,
    Response,
    Seq,
    _is_cert_error,
    decompress,
    load_corpus,
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
        elif path == "/slow-body":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            time.sleep(0.25)
            self.wfile.write(b"ok")
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
        async with HttpxFetcher(trust_env=False) as fetcher:
            return await fetcher.fetch(make_request(url, **kwargs))


# --- HttpxFetcher ---------------------------------------------------------------------


async def test_redirect_is_returned_not_followed(server):
    response = await fetch(f"{server}/redirect")
    assert response.status == 301
    assert response.location == "/target"
    assert response.url == f"{server}/redirect"
    assert response.body == b"moved"


async def test_accept_encoding_is_exactly_gzip_deflate(server):
    headers = (await fetch(f"{server}/echo")).json()
    assert ACCEPT_ENCODING == "gzip, deflate"
    assert headers["accept-encoding"] == "gzip, deflate"


async def test_identity_user_agent_and_headers_applied(server):
    headers = (await fetch(f"{server}/echo", ua="my-agent/2.0")).json()
    assert headers["user-agent"] == "my-agent/2.0"
    assert headers["accept"] == "application/json"


@pytest.mark.parametrize("path", ["/gzip", "/deflate", "/deflate-raw"])
async def test_compressed_bodies_are_decoded(server, path):
    response = await fetch(f"{server}{path}")
    assert response.status == 200
    assert response.body == PAYLOAD


async def test_unknown_content_encoding_passes_body_through(server):
    # Like Scrapy: servers send values such as "utf-8"; the body is left as is
    response = await fetch(f"{server}/br")
    assert response.body == b"\x0b\x02\x80hello\x03"


async def test_content_encoding_list_is_decoded_in_order():
    body = gzip.compress(b"payload")
    with respx.mock:
        respx.get("https://shop.test/x").mock(
            return_value=httpx.Response(
                200, content=body, headers={"Content-Encoding": "identity, gzip"}
            )
        )
        assert (await fetch("https://shop.test/x")).body == b"payload"


def test_concatenated_gzip_members_are_all_decoded():
    body = gzip.compress(b"first ") + gzip.compress(b"second")
    assert decompress(body, zlib.MAX_WBITS | 16) == b"first second"


async def test_invalid_url_is_a_fetch_error():
    with pytest.raises(FetchInvalidURL):
        await fetch("https://shop.test/bad\x7fpath")


def test_only_certificate_failures_are_tls_errors():
    cert = ssl.SSLCertVerificationError("certificate verify failed")
    wrapped = httpx.ConnectError("boom")
    wrapped.__cause__ = cert
    assert _is_cert_error(wrapped)
    reset = httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
    reset.__cause__ = ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")
    assert not _is_cert_error(reset)  # retried as a connection error instead


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
        await fetch(f"http://127.0.0.1:{port}/")
    assert not isinstance(excinfo.value, FetchTLSError)


async def test_latency_measured_to_headers_when_headers_are_slow(server):
    response = await fetch(f"{server}/slow-headers")
    assert response.latency >= 0.2


async def test_latency_excludes_slow_body(server):
    response = await fetch(f"{server}/slow-body")
    assert response.body == b"ok"
    assert response.latency < 0.15


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
