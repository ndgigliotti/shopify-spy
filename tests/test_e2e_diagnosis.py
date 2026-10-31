"""End-to-end diagnosis tests: run the real CLI against local HTTP servers.

Each server port plays one store scenario, so the diagnosis is checked against real
HTTP traffic (robots.txt retries, redirects, fallbacks), not hand-written stats.
Run with: uv run pytest -m e2e
"""

import csv
import http.server
import json
import socket
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

BAIL = 4
DISALLOW_ALL = b"User-agent: *\nDisallow: /\n"


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
    hits: dict

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

    def do_GET(self):  # noqa: C901 - one branch per scenario
        path = self.path.split("?")[0]
        count = self.hits[path] = self.hits.get(path, 0) + 1
        host = self.headers.get("Host", "")
        s = self.scenario

        if s == "robots_disallow":
            return self._send(200, DISALLOW_ALL) if path == "/robots.txt" else self._send(200)
        if s == "robots_redirect_disallow":
            if path == "/robots.txt":
                return self._send(301, headers={"Location": "/real-robots.txt"})
            if path == "/real-robots.txt":
                return self._send(200, DISALLOW_ALL)
            return self._send(200, b"{}")
        if s == "host_redirect_disallow":
            # 127.0.0.1 allows everything but redirects the store to localhost,
            # whose robots.txt disallows everything
            if host.startswith("127.0.0.1"):
                if path == "/robots.txt":
                    return self._send(404)
                port = host.split(":")[1]
                return self._send(301, headers={"Location": f"http://localhost:{port}{self.path}"})
            return self._send(200, DISALLOW_ALL) if path == "/robots.txt" else self._send(200)
        if s == "robots_403_hang":
            return self._send(403) if path == "/robots.txt" else self._hang()
        if s == "robots_404_hang":
            return self._send(404) if path == "/robots.txt" else self._hang()
        if s == "store_403":
            return self._send(404) if path == "/robots.txt" else self._send(403)
        if s == "transient_403_then_hang":
            if path == "/robots.txt":
                return self._send(404)
            if path == "/products.json":
                return self._send(403) if count == 1 else self._send(200, b'{"products": []}')
            return self._hang()
        if s == "products_404_sitemap_hang":
            if path in ("/robots.txt", "/products.json"):
                return self._send(404)
            if path == "/sitemap.xml":
                index = (
                    b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/'
                    b'schemas/sitemap/0.9"><sitemap><loc>http://'
                    + host.encode()
                    + b"/sitemap_products_1.xml</loc></sitemap></sitemapindex>"
                )
                return self._send(200, index, {"Content-Type": "application/xml"})
            return self._hang()
        if s.startswith("woo_variations_"):
            if path == "/robots.txt":
                return self._send(404)
            if "type=variation" not in self.path:
                page = [WOO_VARIABLE] if "page=1&" in self.path else []
                return self._send(200, json.dumps(page).encode())
            if s == "woo_variations_hang":
                return self._hang()
            if s == "woo_variations_403":
                return self._send(403, b"<html>Forbidden</html>")
            assert "parent=1&" in self.path, self.path
            return self._send(200, json.dumps(WOO_VARIATIONS).encode())
        raise AssertionError(f"unknown scenario {s}")


@pytest.fixture
def stores():
    """Start one local server per scenario; yield {scenario: url}."""
    servers = {}

    def start(scenario):
        handler = type(f"H_{scenario}", (_Handler,), {"scenario": scenario, "hits": {}})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[scenario] = server
        return f"http://127.0.0.1:{server.server_address[1]}"

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
}


def test_shopify_diagnosis(tmp_path, stores):
    urls = {scenario: stores(scenario) for scenario in EXPECTED_SHOPIFY}
    statuses = _run(tmp_path, list(urls.values()))
    actual = {scenario: statuses[url] for scenario, url in urls.items()}
    assert actual == EXPECTED_SHOPIFY


def test_woocommerce_single_request_robots_redirect(tmp_path, stores):
    """WooCommerce makes one API request, so robots.txt forbids exactly one request."""
    url = stores("robots_redirect_disallow")
    statuses = _run(tmp_path, [url], "--platform", "woocommerce")
    assert statuses[url] == "blocked by robots.txt"


@pytest.mark.parametrize("scenario", ["woo_variations_403", "woo_variations_hang"])
def test_woocommerce_failed_variation_listing(tmp_path, stores, scenario):
    """A refused or hanging variation listing leaves the store ok, with the listing's
    entries; a hang gives up before --bail."""
    url = stores(scenario)
    statuses = _run(tmp_path, [url], "--platform", "woocommerce")
    assert statuses[url] == "ok"
    [output] = tmp_path.rglob("*.jsonl")
    [item] = [json.loads(line) for line in output.read_text().splitlines()]
    assert item["variations"] == WOO_VARIABLE["variations"]


def test_woocommerce_variation_rows_in_csv(tmp_path, stores):
    """The CLI's CSV gives each variation row its own price, SKU and stock."""
    url = stores("woo_variations_ok")
    statuses = _run(tmp_path, [url], "--platform", "woocommerce", "--format", "csv")
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
