"""End-to-end generic platform: the real CLI against local stores whose sitemaps list
product pages with schema.org Product JSON-LD (#93).

Run with: uv run pytest -m e2e
"""

import csv
import http.server
import json
import subprocess
import sys
import threading
import urllib.parse

import pytest

from shopify_spy._generic import FETCH_CONCURRENCY, PROBE_PAGES
from tests.test_e2e_diagnosis import _free_port

pytestmark = pytest.mark.e2e

IN_STOCK = "https://schema.org/InStock"
OUT_OF_STOCK = "https://schema.org/OutOfStock"
CLIENT_RENDERED_PAGES = 60


def _offer(price, availability=IN_STOCK, **extra):
    return {
        "@type": "Offer",
        "price": price,
        "priceCurrency": "USD",
        "availability": availability,
        **extra,
    }


def _simple(slug, host):
    return {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": slug.title(),
        "sku": f"SKU-{slug}",
        "brand": {"@type": "Brand", "name": "Acme"},
        "image": f"http://{host}/img/{slug}.jpg",
        "offers": _offer("19.90"),
    }


def _socks(host):
    """A Product whose list of offers is its variants."""
    return {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Socks",
        "image": f"http://{host}/img/socks.jpg",
        "offers": [
            _offer("5.00", sku="SOCK-S", name="Small"),
            _offer("6.00", OUT_OF_STOCK, sku="SOCK-L", name="Large"),
        ],
    }


def _tee(host):
    """Google's variant markup: a ProductGroup with hasVariant Products."""

    def variant(size, price, availability):
        return {
            "@type": "Product",
            "name": f"Tee {size}",
            "sku": f"TEE-{size}",
            "size": size,
            "offers": _offer(price, availability),
        }

    return {
        "@context": "https://schema.org/",
        "@type": "ProductGroup",
        "name": "Tee",
        "productGroupID": "TEE",
        "url": f"http://{host}/products/tee",
        "variesBy": ["https://schema.org/size"],
        "hasVariant": [variant("S", "20.00", IN_STOCK), variant("L", "24.00", OUT_OF_STOCK)],
    }


def _html(ld=None):
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    return f"<html><head>{script}</head><body><div id='app'></div></body></html>".encode()


def _urlset(host, paths):
    locs = "".join(f"<url><loc>http://{host}{p}</loc></url>" for p in paths)
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    ).encode()


def _index(host, paths):
    locs = "".join(
        f"<sitemap><loc>http://{host}{p.replace('&', '&amp;')}</loc></sitemap>" for p in paths
    )
    return (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</sitemapindex>"
    ).encode()


def _sitemaps(scenario, host):
    """The scenario's sitemap documents by request path (query included)."""
    if scenario == "robots_sitemap":
        return {
            "/sitemaps/index.xml": _index(host, ["/product-sitemap.xml", "/page-sitemap.xml"]),
            "/product-sitemap.xml": _urlset(
                host,
                ["/products/hat", "/products/socks", "/products/tee", "/blog/hello"],
            ),
            "/page-sitemap.xml": _urlset(host, ["/about"]),
        }
    if scenario == "sitemap_xml":
        return {"/sitemap.xml": _urlset(host, ["/products/hat", "/products/cap"])}
    if scenario == "xmlsitemap_php":
        # BigCommerce's children are the same script with a type and page query
        return {
            "/xmlsitemap.php": _index(
                host,
                [
                    "/xmlsitemap.php?type=products&page=1",
                    "/xmlsitemap.php?type=categories&page=1",
                ],
            ),
            "/xmlsitemap.php?type=products&page=1": _urlset(
                host, ["/products/hat", "/products/cap"]
            ),
            "/xmlsitemap.php?type=categories&page=1": _urlset(host, ["/shop-all"]),
        }
    if scenario == "client_rendered":
        pages = [f"/products/item-{i}" for i in range(CLIENT_RENDERED_PAGES)]
        return {"/sitemap.xml": _urlset(host, pages)}
    if scenario == "page_503":
        return {
            "/sitemap.xml": _urlset(host, ["/products/hat", "/products/cap", "/products/scarf"])
        }
    return {}  # no_sitemap


def _page(scenario, slug, host):
    if scenario == "client_rendered":
        return _html()
    if slug == "socks":
        return _html(_socks(host))
    if slug == "tee":
        return _html(_tee(host))
    return _html(_simple(slug, host))


class _Handler(http.server.BaseHTTPRequestHandler):
    scenario = ""
    hits: list

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="text/html"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.hits.append(self.path)
        path = urllib.parse.urlsplit(self.path).path
        host = self.headers.get("Host", "")
        s = self.scenario
        if path == "/robots.txt":
            if s == "robots_sitemap":
                return self._send(
                    200, b"User-agent: *\nSitemap: /sitemaps/index.xml\n", "text/plain"
                )
            return self._send(404)
        if (doc := _sitemaps(s, host).get(self.path)) is not None:
            return self._send(200, doc, "application/xml")
        if path == "/blog/hello":
            return self._send(200, _html())
        if path.startswith("/products/"):
            slug = path.removeprefix("/products/")
            if s == "page_503" and slug == "scarf":
                return self._send(503)
            return self._send(200, _page(s, slug, host))
        return self._send(404)


@pytest.fixture
def stores():
    """Start one local store per scenario; returns its URL and its request log."""
    servers = []

    def start(scenario):
        handler = type(f"J_{scenario}", (_Handler,), {"scenario": scenario, "hits": []})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", handler.hits

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
            "generic",
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
    (csv_file,) = list(tmp_path.rglob("*.csv"))
    rows = list(csv.DictReader(csv_file.open(newline="", encoding="utf-8")))
    return result, status, rows


EXPECTED = {
    # scenario: (status code, status label, items)
    "robots_sitemap": ("ok", "ok", 3),
    "sitemap_xml": ("ok", "ok", 2),
    "xmlsitemap_php": ("ok", "ok", 2),
    "client_rendered": ("error", "failed", 0),
    "no_sitemap": ("not_found", "404 Not Found", 0),
    "page_503": ("partial", "ok", 2),
}


def test_cli_scrapes_generic_stores(tmp_path, stores):
    started = {scenario: stores(scenario) for scenario in EXPECTED}
    urls = {scenario: url for scenario, (url, _) in started.items()}
    hits = {scenario: log for scenario, (_, log) in started.items()}
    result, status, rows = _scrape(tmp_path, list(urls.values()))
    entries = {e["url"]: e for e in status["urls"]}
    actual = {
        s: (entries[u]["code"], entries[u]["status"], entries[u]["items"]) for s, u in urls.items()
    }
    assert actual == EXPECTED, result.stderr
    assert status["items_scraped"] == 9

    # Only product sitemaps are read, and the relative robots.txt line resolves
    assert "/sitemaps/index.xml" in hits["robots_sitemap"]
    assert "/page-sitemap.xml" not in hits["robots_sitemap"]
    assert "/xmlsitemap.php?type=products&page=1" in hits["xmlsitemap_php"]
    assert "/xmlsitemap.php?type=categories&page=1" not in hits["xmlsitemap_php"]
    pages = [h for h in hits["client_rendered"] if h.startswith("/products/")]
    assert len(pages) <= PROBE_PAGES + FETCH_CONCURRENCY

    # A row per simple product, per offer in a list, and per hasVariant Product
    assert len(rows) == 1 + 2 + 2 + 2 + 2 + 2
    assert {r["platform"] for r in rows} == {"generic"}
    assert {r["kind"] for r in rows} == {"product"}
    by_store: dict = {}
    for row in rows:
        key = f"{row['handle']}/{row['sku']}" if row["variant_title"] else row["handle"]
        by_store.setdefault(row["store"], {})[key] = row
    store = by_store[urls["robots_sitemap"].removeprefix("http://")]
    assert set(store) == {"hat", "socks/SOCK-S", "socks/SOCK-L", "tee/TEE-S", "tee/TEE-L"}

    hat = store["hat"]
    assert (hat["title"], hat["sku"], hat["vendor"]) == ("Hat", "SKU-hat", "Acme")
    assert (hat["price"], hat["currency"], hat["available"]) == ("19.90", "USD", "true")
    assert hat["html_url"] == f"{urls['robots_sitemap']}/products/hat"
    assert hat["image_url"] == f"{urls['robots_sitemap']}/img/hat.jpg"

    small, large = store["socks/SOCK-S"], store["socks/SOCK-L"]
    assert (small["variant_title"], small["price"], small["available"]) == ("Small", "5.00", "true")
    assert (large["variant_title"], large["price"], large["available"]) == (
        "Large",
        "6.00",
        "false",
    )
    assert (small["min_price"], small["max_price"]) == ("5.00", "6.00")

    small, large = store["tee/TEE-S"], store["tee/TEE-L"]
    assert (small["title"], small["options"]) == ("Tee", "size")
    assert (small["variant_title"], small["option1"]) == ("S", "S")
    assert (small["price"], small["available"]) == ("20.00", "true")
    assert (large["variant_title"], large["option1"]) == ("L", "L")
    assert (large["price"], large["available"]) == ("24.00", "false")

    assert set(by_store[urls["page_503"].removeprefix("http://")]) == {"hat", "cap"}
    assert set(by_store[urls["xmlsitemap_php"].removeprefix("http://")]) == {"hat", "cap"}
