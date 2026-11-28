"""End-to-end diagnosis tests: run the real CLI against local HTTP servers.

Each server port plays one store scenario, so the diagnosis is checked against real
HTTP traffic (robots.txt retries, redirects, fallbacks), not hand-written stats.
Run with: uv run pytest -m e2e
"""

import collections
import csv
import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

pytestmark = pytest.mark.e2e

BAIL = 4
DISALLOW_ALL = b"User-agent: *\nDisallow: /\n"


@pytest.fixture(params=["curl_cffi", "httpx"])
def transport(request):
    """Extra CLI args selecting the transport: the session layer (robots.txt,
    throttling, Crawl-delay, Retry-After, redirects, address checks) must not depend
    on it."""
    return [] if request.param == "curl_cffi" else ["--no-impersonate"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _woo_prices(price, regular):
    return {"price": price, "regular_price": regular, "currency_minor_unit": 2}


WOO_VARIABLE = {
    "id": 1,
    "name": "Steak",
    "permalink": "/product/steak/",
    "sku": "STEAK",
    "is_in_stock": True,
    "prices": _woo_prices("2800", "2800"),
    "attributes": [{"name": "Size", "has_variations": True, "terms": []}],
    "variations": [
        {"id": 2, "attributes": [{"name": "Size", "value": "8 oz"}]},
        {"id": 3, "attributes": [{"name": "Size", "value": "12 oz"}]},
    ],
}
WOO_VARIATIONS = [
    {
        "id": 2,
        "parent": 1,
        "type": "variation",
        "sku": "STEAK-8",
        "is_in_stock": True,
        "prices": _woo_prices("2800", "2800"),
        "attributes": [],
    },
    {
        "id": 3,
        "parent": 1,
        "type": "variation",
        "sku": "STEAK-12",
        "is_in_stock": False,
        "prices": _woo_prices("4000", "5000"),
        "attributes": [],
    },
]


class _Handler(http.server.BaseHTTPRequestHandler):
    scenario = ""
    redirect_to = ""  # redirect_to_loopback: the origin to send every request to
    hits: dict
    agents: list  # user agents in the order first seen
    arrivals: list  # (path with query, monotonic arrival time)

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hang(self):
        time.sleep(BAIL * 4)

    def _record_agent(self):
        agent = self.headers.get("User-Agent", "")
        if agent not in self.agents:
            self.agents.append(agent)

    def _user_agent_rate_limited(self, path, count):
        # Shopify on 2026-11-14 (#62): one user agent is refused for longer than
        # --bail, any other is served
        if path == "/robots.txt":
            return self._send(404)
        if self.headers.get("User-Agent", "") == self.agents[0]:
            return self._send(429, headers={"Retry-After": str(BAIL + 1)})
        page = [{"id": 1, "handle": "p1", "title": "P1"}] if "page=1" in self.path else []
        return self._send(200, json.dumps({"products": page}).encode())

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        count = self.hits[path] = self.hits.get(path, 0) + 1
        self.arrivals.append((self.path, time.monotonic()))
        self._record_agent()
        scenario = SCENARIOS.get(self.scenario)
        if scenario is None:
            raise AssertionError(f"unknown scenario {self.scenario}")
        return scenario(self, path, count)

    def _robots_disallow(self, path, count):
        return self._send(200, DISALLOW_ALL) if path == "/robots.txt" else self._send(200)

    def _robots_redirect_disallow(self, path, count):
        if path == "/robots.txt":
            return self._send(301, headers={"Location": "/real-robots.txt"})
        if path == "/real-robots.txt":
            return self._send(200, DISALLOW_ALL)
        return self._send(200, b"{}")

    def _host_redirect_disallow(self, path, count):
        # 127.0.0.1 allows everything but redirects the store to localhost,
        # whose robots.txt disallows everything
        host = self.headers.get("Host", "")
        if host.startswith("127.0.0.1"):
            if path == "/robots.txt":
                return self._send(404)
            port = host.split(":")[1]
            return self._send(301, headers={"Location": f"http://localhost:{port}{self.path}"})
        return self._send(200, DISALLOW_ALL) if path == "/robots.txt" else self._send(200)

    def _redirect_to_loopback(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        return self._send(302, headers={"Location": f"{self.redirect_to}{self.path}"})

    def _loopback_target(self, path, count):
        return self._send(200, b'{"products": []}')

    def _robots_403_hang(self, path, count):
        return self._send(403) if path == "/robots.txt" else self._hang()

    def _robots_404_hang(self, path, count):
        return self._send(404) if path == "/robots.txt" else self._hang()

    def _store_403(self, path, count):
        return self._send(404) if path == "/robots.txt" else self._send(403)

    def _transient_403_then_hang(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        if path == "/products.json":
            return self._send(403) if count == 1 else self._send(200, b'{"products": []}')
        return self._hang()

    def _products_failed_sitemap_hang(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        if path == "/products.json":
            if self.scenario == "products_429_sitemap_hang":
                return self._send(429, headers={"Retry-After": "0"})
            return self._send(404)
        if path == "/":  # names no myshopify.com domain, so the sitemap comes next
            return self._send(200, b"<html></html>", {"Content-Type": "text/html"})
        if path == "/sitemap.xml":
            index = (
                b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/'
                b'schemas/sitemap/0.9"><sitemap><loc>http://'
                + self.headers.get("Host", "").encode()
                + b"/sitemap_products_1.xml</loc></sitemap></sitemapindex>"
            )
            return self._send(200, index, {"Content-Type": "application/xml"})
        return self._hang()

    def _rate_limited_or_unavailable(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        status = 429 if "rate_limited" in self.scenario else 503
        return self._send(status, headers={"Retry-After": "0"})

    def _woo_fallback_rate_limited(self, path, count):
        if path == "/robots.txt" or path.startswith("/wp-json/wc/store/"):
            return self._send(404)
        return self._send(429, headers={"Retry-After": "0"})

    def _robots_rate_limited(self, path, count):
        # Shopify per IP, from robots.txt on: the Retry-After waits (3 x 3 x 2 s)
        # outlast --bail but don't count against it, so the retries run out first
        return self._send(429, headers={"Retry-After": "2"})

    def _rate_limited_no_retry_after(self, path, count):
        # Stupell (#30): 429s without Retry-After; retries wait until --bail
        return self._send(404) if path == "/robots.txt" else self._send(429)

    def _long_retry_after(self, path, count):
        # A Retry-After longer than we wait before a retry: not retried early
        if path == "/products.json":
            return self._send(429, headers={"Retry-After": "120"})
        return self._send(404)

    def _crawl_delay(self, path, count):
        if path == "/robots.txt":
            if self.scenario == "spaced":
                return self._send(404)
            return self._send(200, b"User-agent: *\nCrawl-delay: 1.5\n")
        if path == "/products.json":
            # A full page 1, so the adapter asks for page 2
            page = []
            if "page=1" in urllib.parse.urlsplit(self.path).query.split("&"):
                page = [{"id": i, "handle": f"p{i}", "title": f"P{i}"} for i in range(250)]
            return self._send(200, json.dumps({"products": page}).encode())
        return self._send(404)

    def _forbidden_then_rate_limited(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        return self._send(403) if count == 1 else self._send(429, headers={"Retry-After": "0"})

    def _woo_variations(self, path, count):
        if path == "/robots.txt":
            return self._send(404)
        if "type=variation" not in self.path:
            page = [WOO_VARIABLE] if "page=1&" in self.path else []
            return self._send(200, json.dumps(page).encode())
        if self.scenario == "woo_variations_hang":
            return self._hang()
        if self.scenario == "woo_variations_403":
            return self._send(403, b"<html>Forbidden</html>")
        assert "parent=1&" in self.path, self.path
        return self._send(200, json.dumps(WOO_VARIATIONS).encode())

    def _headless_myshopify(self, path, count):
        # A headless storefront: no /products.json, but the homepage names the shop's
        # myshopify.com domain (served by _MyshopifyProxy)
        if path == "/":
            body = b'<html><a href="https://acme.myshopify.com/cart">cart</a></html>'
            return self._send(200, body, {"Content-Type": "text/html"})
        return self._send(404)


SCENARIOS = {
    "robots_disallow": _Handler._robots_disallow,
    "robots_redirect_disallow": _Handler._robots_redirect_disallow,
    "host_redirect_disallow": _Handler._host_redirect_disallow,
    "redirect_to_loopback": _Handler._redirect_to_loopback,
    "loopback_target": _Handler._loopback_target,
    "robots_403_hang": _Handler._robots_403_hang,
    "robots_404_hang": _Handler._robots_404_hang,
    "store_403": _Handler._store_403,
    "transient_403_then_hang": _Handler._transient_403_then_hang,
    "products_404_sitemap_hang": _Handler._products_failed_sitemap_hang,
    "products_429_sitemap_hang": _Handler._products_failed_sitemap_hang,
    "headless_myshopify": _Handler._headless_myshopify,
    "rate_limited": _Handler._rate_limited_or_unavailable,
    "unavailable": _Handler._rate_limited_or_unavailable,
    "woo_rate_limited": _Handler._rate_limited_or_unavailable,
    "woo_unavailable": _Handler._rate_limited_or_unavailable,
    "woo_fallback_rate_limited": _Handler._woo_fallback_rate_limited,
    "robots_rate_limited": _Handler._robots_rate_limited,
    "user_agent_rate_limited": _Handler._user_agent_rate_limited,
    "rate_limited_no_retry_after": _Handler._rate_limited_no_retry_after,
    "forbidden_then_rate_limited": _Handler._forbidden_then_rate_limited,
    "long_retry_after": _Handler._long_retry_after,
    "crawl_delay": _Handler._crawl_delay,
    "spaced": _Handler._crawl_delay,
    "woo_variations_ok": _Handler._woo_variations,
    "woo_variations_403": _Handler._woo_variations,
    "woo_variations_hang": _Handler._woo_variations,
}


@pytest.fixture
def stores():
    """Start one local server per scenario; yield {scenario: url}."""
    servers = {}

    def start(scenario):
        attrs = {"scenario": scenario, "hits": {}, "agents": [], "arrivals": []}
        handler = type(f"H_{scenario}", (_Handler,), attrs)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[scenario] = server
        return f"http://127.0.0.1:{server.server_address[1]}"

    start.servers = servers  # type: ignore[attr-defined]
    yield start
    for server in servers.values():
        server.shutdown()


def _run(tmp_path, urls, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopify_spy.cli",
            "scrape",
            *urls,
            "--output",
            str(tmp_path),
            "--bail",
            str(BAIL),
            "--quiet",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        # Run outside the repo, like an installed copy (0.2.x lost its settings this way)
        cwd=tmp_path,
    )
    status_files = list(tmp_path.glob("status/*_status.json"))
    assert status_files, result.stdout + result.stderr
    data = json.loads(status_files[0].read_text())
    return {entry["url"]: entry["status"] for entry in data["urls"]}


EXPECTED_SHOPIFY = {
    "robots_disallow": "blocked by robots.txt",
    "robots_redirect_disallow": "blocked by robots.txt",
    "host_redirect_disallow": "blocked by robots.txt",
    "robots_403_hang": "timed out",
    "robots_404_hang": "timed out",
    "store_403": "403 Forbidden",
    "transient_403_then_hang": "timed out",
    "products_404_sitemap_hang": "timed out",
    "products_429_sitemap_hang": "rate limited (429)",  # a 2xx, then --bail
    "rate_limited": "rate limited (429)",
    "rate_limited_no_retry_after": "rate limited (429)",
    "robots_rate_limited": "rate limited (429)",
    "user_agent_rate_limited": "ok",
    "unavailable": "unavailable (5xx)",
    "forbidden_then_rate_limited": "403 Forbidden",
    "long_retry_after": "rate limited (429)",
}


def test_shopify_diagnosis(tmp_path, stores, transport):
    urls = {scenario: stores(scenario) for scenario in EXPECTED_SHOPIFY}
    statuses = _run(tmp_path, list(urls.values()), *transport)
    actual = {scenario: statuses[url] for scenario, url in urls.items()}
    assert actual == EXPECTED_SHOPIFY
    # The 120 s Retry-After isn't retried after 60 s, nor waited out past --bail
    assert stores.servers["long_retry_after"].RequestHandlerClass.hits["/products.json"] == 1


def test_crawl_delay_spaces_requests_without_throttle(tmp_path, stores, transport):
    """robots.txt Crawl-delay still applies with --no-throttle."""
    url = stores("crawl_delay")
    statuses = _run(tmp_path, [url], "--no-throttle", *transport)
    assert statuses[url] == "ok"
    arrivals = stores.servers["crawl_delay"].RequestHandlerClass.arrivals
    pages = [t for path, t in arrivals if path.startswith("/products.json")]
    assert len(pages) == 2
    assert pages[1] - pages[0] >= 1.4


def test_min_delay_spaces_requests(tmp_path, stores, transport):
    """Default throttling keeps requests to one host --min-delay apart."""
    url = stores("spaced")
    statuses = _run(tmp_path, [url], "--min-delay", "1.5", *transport)
    assert statuses[url] == "ok"
    arrivals = [t for _, t in stores.servers["spaced"].RequestHandlerClass.arrivals]
    assert len(arrivals) == 3  # robots.txt and two pages
    assert all(b - a >= 1.4 for a, b in zip(arrivals, arrivals[1:]))


def test_woocommerce_single_request_robots_redirect(tmp_path, stores):
    """WooCommerce makes one API request, so robots.txt forbids exactly one request."""
    url = stores("robots_redirect_disallow")
    statuses = _run(tmp_path, [url], "--platform", "woocommerce")
    assert statuses[url] == "blocked by robots.txt"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("woo_rate_limited", "rate limited (429)"),
        ("woo_unavailable", "unavailable (5xx)"),
        # Store API gone, and the product-page fallback is rate limited
        ("woo_fallback_rate_limited", "rate limited (429)"),
    ],
)
def test_woocommerce_rate_limited_or_unavailable(tmp_path, stores, scenario, expected):
    """A Store API 429 ends the store; a 503 falls back to product pages, which 503 too.
    Either way the store fails instead of coming up empty."""
    url = stores(scenario)
    statuses = _run(tmp_path, [url], "--platform", "woocommerce")
    assert statuses[url] == expected


@pytest.mark.parametrize("scenario", ["woo_variations_403", "woo_variations_hang"])
def test_woocommerce_failed_variation_listing(tmp_path, stores, scenario):
    """A refused or hanging variation listing leaves the store ok, with the listing's
    entries; a hang gives up before --bail."""
    url = stores(scenario)
    # Several requests to one host before the first item: at the default 1 s minimum
    # they come close to the short e2e --bail on a slow runner
    statuses = _run(tmp_path, [url], "--platform", "woocommerce", "--min-delay", "0")
    assert statuses[url] == "ok"
    [output] = tmp_path.rglob("*.jsonl")
    [item] = [json.loads(line) for line in output.read_text().splitlines()]
    assert item["variations"] == WOO_VARIABLE["variations"]


def test_woocommerce_variation_rows_in_csv(tmp_path, stores):
    """The CLI's CSV gives each variation row its own price, SKU and stock."""
    url = stores("woo_variations_ok")
    statuses = _run(
        tmp_path, [url], "--platform", "woocommerce", "--format", "csv", "--min-delay", "0"
    )
    assert statuses[url] == "ok"
    [output] = tmp_path.rglob("*.csv")
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert [
        (
            r["variant_id"],
            r["variant_title"],
            r["sku"],
            r["price"],
            r["compare_at_price"],
            r["available"],
        )
        for r in rows
    ] == [
        ("2", "8 oz", "STEAK-8", "28.00", "", "true"),
        ("3", "12 oz", "STEAK-12", "40.00", "50.00", "false"),
    ]


class _ForwardingProxy(http.server.BaseHTTPRequestHandler):
    """Relays absolute-form GETs to their target's port on 127.0.0.1, so it can serve
    made-up hostnames, recording (path, Proxy-Authorization)."""

    records: list

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        target = urllib.parse.urlsplit(self.path)
        self.records.append((target.path, self.headers.get("Proxy-Authorization")))
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("proxy-authorization", "proxy-connection", "connection")
        }
        conn = http.client.HTTPConnection("127.0.0.1", target.port, timeout=30)
        try:
            path = target.path + (f"?{target.query}" if target.query else "")
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def forwarding_proxy():
    handler = type("Proxy", (_ForwardingProxy,), {"records": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], handler.records
    server.shutdown()


def _run_verbose(tmp_path, urls, *args):
    """Like _run, at DEBUG with the log inside tmp_path; returns (process, status JSON)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopify_spy.cli",
            "scrape",
            *urls,
            "--output",
            str(tmp_path / "out"),
            "--bail",
            str(BAIL),
            "--verbose",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")},
    )
    status_files = list((tmp_path / "out").glob("status/*_status.json"))
    assert status_files, result.stdout + result.stderr
    return result, json.loads(status_files[0].read_text())


def test_proxy_sees_every_request(tmp_path, stores, forwarding_proxy):
    """--proxy routes every request, robots.txt included, and never prints the password."""
    port, records = forwarding_proxy
    url = stores("woo_variations_ok")
    proxy = f"http://u:secret@127.0.0.1:{port}"
    result, status = _run_verbose(tmp_path, [url], "--platform", "woocommerce", "--proxy", proxy)
    assert {e["url"]: e["status"] for e in status["urls"]} == {url: "ok"}

    hits = stores.servers["woo_variations_ok"].RequestHandlerClass.hits
    assert "/robots.txt" in hits
    assert collections.Counter(path for path, _ in records) == hits
    assert all(auth and auth.startswith("Basic ") for _, auth in records)

    log_text = open(status["log_file"], encoding="utf-8").read()
    assert f"Using proxy http://u:***@127.0.0.1:{port}" in log_text
    for text in (result.stdout, result.stderr, json.dumps(status), log_text):
        assert "secret" not in text


class _MyshopifyProxy(_ForwardingProxy):
    """A forwarding proxy that plays acme.myshopify.com itself."""

    def do_GET(self):  # noqa: N802
        target = urllib.parse.urlsplit(self.path)
        if target.hostname != "acme.myshopify.com":
            return super().do_GET()
        self.records.append((self.path, None))
        products = []
        if target.path == "/products.json" and "page=1" in target.query.split("&"):
            products = [{"id": i, "handle": f"p{i}", "title": f"P{i}"} for i in range(3)]
        status = 200 if target.path == "/products.json" else 404
        body = json.dumps({"products": products}).encode() if status == 200 else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def myshopify_proxy():
    handler = type("Proxy", (_MyshopifyProxy,), {"records": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], handler.records
    server.shutdown()


def test_headless_store_products_come_from_its_myshopify_domain(tmp_path, stores, myshopify_proxy):
    """A 404 on the store's own /products.json is retried on the myshopify.com domain
    its homepage names; items keep the store's own URL."""
    port, records = myshopify_proxy
    url = stores("headless_myshopify")
    # Without --no-throttle the shared slot's first delay (~1 s) can outlast the half
    # of the short e2e --bail the attempt gets
    proxy = f"http://127.0.0.1:{port}"
    _, status = _run_verbose(tmp_path, [url], "--proxy", proxy, "--no-throttle")
    assert status["urls"] == [
        {
            "url": url,
            "items": 3,
            "status": "ok",
            "code": "ok",
            "products_from": "acme.myshopify.com",
        }
    ]
    assert "http://acme.myshopify.com/products.json?limit=250&page=1" in [p for p, _ in records]
    [output] = (tmp_path / "out").rglob("*.jsonl")
    items = [json.loads(line) for line in output.read_text().splitlines()]
    assert [i["url"] for i in items] == [f"{url}/products/p{i}.json" for i in range(3)]
    hits = stores.servers["headless_myshopify"].RequestHandlerClass.hits
    assert "/sitemap.xml" not in hits


def test_non_public_redirect_refused(tmp_path, stores, forwarding_proxy, transport):
    """Through a proxy only literal IPs are checked: a public-looking store's redirect
    to 127.0.0.1 is refused before any request reaches it, robots.txt included."""
    proxy_port, _ = forwarding_proxy
    target = stores("loopback_target")
    port = stores("redirect_to_loopback").rsplit(":", 1)[1]
    stores.servers["redirect_to_loopback"].RequestHandlerClass.redirect_to = target
    url = f"http://store.test:{port}"
    proxy = f"http://127.0.0.1:{proxy_port}"
    _, status = _run_verbose(tmp_path, [url], "--proxy", proxy, *transport)
    assert {e["url"]: e["status"] for e in status["urls"]} == {url: "0 items"}
    assert stores.servers["loopback_target"].RequestHandlerClass.hits == {}
    assert stores.servers["redirect_to_loopback"].RequestHandlerClass.hits
    log_text = open(status["log_file"], encoding="utf-8").read()
    assert f"Refused request to {target}/" in log_text
    assert "127.0.0.1 is not a public address" in log_text
