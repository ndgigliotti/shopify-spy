"""End-to-end WooCommerce fallback: the real CLI against local stores whose Store API
fails, reading product pages instead (#42).

Run with: uv run pytest -m e2e
"""

import csv
import http.server
import json
import subprocess
import sys
import threading

import pytest

from tests.test_e2e_diagnosis import _free_port

pytestmark = pytest.mark.e2e

PRODUCTS = {
    "hat": ("Hat", "19.90", 11),
    "cap": ("Cap", "5.00", 12),
    "scarf": ("Scarf", "42.50", 13),
}


def _page(slug: str, host: str) -> bytes:
    name, price, post_id = PRODUCTS[slug]
    ld = {
        "@context": "https://schema.org/",
        "@graph": [
            {
                "@type": "Product",
                "@id": f"http://{host}/product/{slug}/#product",
                "name": name,
                "sku": f"SKU-{slug}",
                "image": f"http://{host}/img/{slug}.jpg",
                "offers": [
                    {
                        "@type": "Offer",
                        "price": price,
                        "priceCurrency": "USD",
                        "availability": "https://schema.org/InStock",
                    }
                ],
            }
        ],
    }
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f'<body class="single-product postid-{post_id}"><h1>{name}</h1></body></html>'
    ).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    scenario = ""

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="text/html", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, data, headers=None):
        self._send(status, json.dumps(data).encode(), "application/json", headers)

    def do_GET(self):
        path = self.path.split("?")[0]
        host = self.headers.get("Host", "")
        s = self.scenario
        if path == "/robots.txt":
            return self._send(404)
        if path == "/wp-json/wc/store/v1/products":
            if s == "api_401":
                return self._json(401, {"code": "rest_forbidden", "data": {"status": 401}})
            if s == "api_html":
                return self._send(200, b"<html>maintenance</html>")
            return self._send(404)  # api_404, sitemap_only
        if path == "/products.json":  # the Shopify check after a 404
            return self._send(404)
        if path == "/wp-json/wp/v2/product":
            if s == "sitemap_only":
                return self._json(401, {"code": "rest_cannot_read"})
            links = [
                {"id": post_id, "link": f"http://{host}/product/{slug}/"}
                for slug, (_, _, post_id) in PRODUCTS.items()
            ]
            return self._json(200, links, {"X-WP-TotalPages": "1"})
        if path == "/wp-sitemap.xml" and s == "sitemap_only":
            body = (
                '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/'
                f'schemas/sitemap/0.9"><sitemap><loc>http://{host}/wp-sitemap-posts-product-1.xml'
                "</loc></sitemap></sitemapindex>"
            ).encode()
            return self._send(200, body, "application/xml")
        if path == "/wp-sitemap-posts-product-1.xml" and s == "sitemap_only":
            locs = "".join(
                f"<url><loc>http://{host}/product/{slug}/</loc></url>" for slug in PRODUCTS
            )
            body = (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/'
                f'schemas/sitemap/0.9">{locs}</urlset>'
            ).encode()
            return self._send(200, body, "application/xml")
        if path.startswith("/product/"):
            slug = path.strip("/").split("/")[-1]
            if slug in PRODUCTS:
                if s == "page_503" and slug == "scarf":
                    return self._send(503)
                return self._send(200, _page(slug, host))
        return self._send(404)


@pytest.fixture
def stores():
    servers = []

    def start(scenario):
        handler = type(f"H_{scenario}", (_Handler,), {"scenario": scenario})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()


def _scrape(tmp_path, urls):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopify_spy.cli",
            "scrape",
            *urls,
            "--platform",
            "woocommerce",
            "--format",
            "csv",
            "--output",
            str(tmp_path),
            "--no-throttle",
            "--bail",
            "30",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    status_files = list(tmp_path.glob("status/*_status.json"))
    assert status_files, result.stdout + result.stderr
    status = json.loads(status_files[0].read_text())
    (csv_file,) = [p for p in tmp_path.rglob("*.csv")]
    rows = list(csv.DictReader(csv_file.open(newline="", encoding="utf-8")))
    return result, status, rows


EXPECTED = {
    # scenario: (status code, items)
    "api_401": ("ok", 3),
    "api_404": ("ok", 3),
    "api_html": ("ok", 3),
    "sitemap_only": ("ok", 3),
    "page_503": ("partial", 2),
}


def test_cli_falls_back_to_product_pages(tmp_path, stores):
    urls = {scenario: stores(scenario) for scenario in EXPECTED}
    result, status, rows = _scrape(tmp_path, list(urls.values()))
    entries = {e["url"]: e for e in status["urls"]}
    actual = {s: (entries[u]["code"], entries[u]["items"]) for s, u in urls.items()}
    assert actual == EXPECTED, result.stderr
    assert status["items_scraped"] == 14

    # One row per product: every fallback row is marked and carries the page's data
    assert len(rows) == 14
    assert {r["platform"] for r in rows} == {"woocommerce"}
    assert {r["source"] for r in rows} == {"product_page"}
    by_store = {}
    for row in rows:
        by_store.setdefault(row["store"], {})[row["handle"]] = row
    hat = by_store[urls["api_401"].removeprefix("http://")]["hat"]
    assert hat["id"] == "11"
    assert hat["title"] == "Hat"
    assert hat["sku"] == "SKU-hat"
    assert hat["price"] == "19.90"
    assert hat["currency"] == "USD"
    assert hat["available"] == "true"
    assert hat["html_url"] == f"{urls['api_401']}/product/hat/"
    assert set(by_store[urls["page_503"].removeprefix("http://")]) == {"hat", "cap"}
