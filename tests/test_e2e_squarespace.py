"""End-to-end squarespace platform: the real CLI against local Squarespace-like
stores, with robots.txt obeyed (product pages) and ignored (``?format=json``).

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

from tests.test_e2e_diagnosis import _free_port

pytestmark = pytest.mark.e2e

# The rules every Squarespace site serves, trimmed; the Sitemap line is added per host
SQUARESPACE_ROBOTS = """\
User-agent: *
Disallow: /config
Disallow: /api/
Disallow: /*?format=json
Disallow: /*&format=json
Disallow: /*?format=page-context
Disallow: /*&format=page-context
Disallow: /*?format=json-pretty
Disallow: /*&format=json-pretty
"""
SLUGS = ["tee", "mug", "cap"]
OTHER_PAGES = ["/blog/post-1", "/shop", "/about"]
# Unconfirmed: no live collection with a non-null pagination was found
NEXT_PAGE = "/shop?offset=123"


def _variant(vid, attributes, price, *, sale=None, quantity=3):
    variant = {
        "id": vid,
        "sku": f"SKU-{vid}",
        "attributes": attributes,
        "onSale": sale is not None,
        "priceMoney": {"currency": "USD", "value": price},
        "stock": {"quantity": quantity, "unlimited": False},
    }
    if sale is not None:
        variant["salePriceMoney"] = {"currency": "USD", "value": sale}
    return variant


def _record(slug):
    if slug == "tee":
        variants = [
            _variant("tee-s", {"Color": "Red", "Size": "S"}, "20.00", sale="15.00"),
            _variant("tee-m", {"Color": "Red", "Size": "M"}, "20.00", quantity=0),
        ]
        content = {"variantOptionOrdering": ["Color", "Size"], "variants": variants}
    else:
        content = {"variants": [_variant(f"{slug}-1", {}, "12.00")]}
    return {
        "id": f"id-{slug}",
        "title": slug.title(),
        "urlId": slug,
        "fullUrl": f"/shop/p/{slug}",
        "assetUrl": f"https://images.test/{slug}.jpg",
        "structuredContent": content,
    }


def _collection(slugs, pagination=None):
    body = {"collection": {"pageSize": 2}, "items": [_record(s) for s in slugs]}
    return json.dumps(body | {"pagination": pagination}).encode()


def _html(ld=None):
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    return f"<html><head>{script}</head><body></body></html>".encode()


def _product(slug, host):
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": slug.title(),
        "url": f"http://{host}/shop/p/{slug}",
        "image": f"http://{host}/img/{slug}.jpg",
        "offers": {"@type": "Offer", "price": "12.00", "priceCurrency": "USD"},
    }


def _urlset(host, paths):
    locs = "".join(f"<url><loc>http://{host}{p}</loc></url>" for p in paths)
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    ).encode()


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
            if s == "disallow_all":
                return self._send(200, b"User-agent: *\nDisallow: /\n", "text/plain")
            robots = f"{SQUARESPACE_ROBOTS}Sitemap: http://{host}/sitemap.xml\n"
            return self._send(200, robots.encode(), "text/plain")
        if path == "/sitemap.xml":
            if s == "no_sitemap":
                return self._send(404)
            pages = [*OTHER_PAGES, *(f"/shop/p/{slug}" for slug in SLUGS)]
            return self._send(200, _urlset(host, pages), "application/xml")
        if self.path == "/shop?format=json":
            if s in ("html_json", "html_json_no_product"):
                return self._send(200, _html())
            next_page = {"nextPage": True, "nextPageUrl": NEXT_PAGE, "pageSize": 2}
            return self._send(200, _collection(SLUGS[:2], next_page), "application/json")
        if self.path == f"{NEXT_PAGE}&format=json":
            return self._send(200, _collection(SLUGS[2:], {"nextPage": False}), "application/json")
        if path.startswith("/shop/p/"):
            slug = path.removeprefix("/shop/p/")
            if s == "html_json_no_product":
                return self._send(200, _html())
            return self._send(200, _html(_product(slug, host)))
        if path in OTHER_PAGES:
            return self._send(200, _html())
        return self._send(404)


@pytest.fixture
def stores():
    """Start one local store per scenario; returns its URL and its request log."""
    servers = []

    def start(scenario):
        handler = type(f"S_{scenario}", (_Handler,), {"scenario": scenario, "hits": []})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", handler.hits

    yield start
    for server in servers:
        server.shutdown()


def _scrape(out, urls, *options):
    out.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopify_spy.cli",
            "scrape",
            *urls,
            "--platform",
            "squarespace",
            "--format",
            "csv",
            "--output",
            str(out),
            "--no-throttle",
            "--bail",
            "30",
            "--quiet",
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=out,
    )
    status_files = list(out.glob("status/*_status.json"))
    assert status_files, result.stdout + result.stderr
    status = json.loads(status_files[0].read_text())
    rows = []
    for csv_file in out.rglob("*.csv"):
        rows += list(csv.DictReader(csv_file.open(newline="", encoding="utf-8")))
    return result, status, rows


def _run(tmp_path, name, scenarios, stores, *options):
    started = {scenario: stores(scenario) for scenario in scenarios}
    urls = {scenario: url for scenario, (url, _) in started.items()}
    hits = {scenario: log for scenario, (_, log) in started.items()}
    result, status, rows = _scrape(tmp_path / name, list(urls.values()), *options)
    entries = {e["url"]: e for e in status["urls"]}
    actual = {
        s: (entries[u]["code"], entries[u]["status"], entries[u]["items"]) for s, u in urls.items()
    }
    return result, actual, hits, urls, rows


def _rows_by_store(rows, url):
    return [r for r in rows if r["store"] == url.removeprefix("http://")]


def test_cli_reads_product_pages_with_robots_txt_obeyed(tmp_path, stores):
    expected = {
        "store": ("ok", "ok", 3),
        "no_sitemap": ("not_found", "404 Not Found", 0),
        "disallow_all": ("robots_blocked", "blocked by robots.txt", 0),
    }
    result, actual, hits, urls, rows = _run(tmp_path, "obeyed", expected, stores)
    assert actual == expected, result.stderr

    for log in hits.values():
        assert not [h for h in log if "format=" in h]
    assert not [h for h in hits["store"] if h in OTHER_PAGES]
    assert sorted(h for h in hits["store"] if "/p/" in h) == sorted(f"/shop/p/{s}" for s in SLUGS)

    store_rows = _rows_by_store(rows, urls["store"])
    assert len(store_rows) == 3
    assert {(r["platform"], r["source"]) for r in store_rows} == {("squarespace", "product_page")}
    assert {r["html_url"] for r in store_rows} == {f"{urls['store']}/shop/p/{s}" for s in SLUGS}


def test_cli_reads_format_json_with_robots_txt_ignored(tmp_path, stores):
    expected = {
        "store": ("ok", "ok", 3),
        "html_json": ("ok", "ok", 3),
        "html_json_no_product": ("error", "failed", 0),
    }
    result, actual, hits, urls, rows = _run(
        tmp_path, "ignored", expected, stores, "--ignore-robots"
    )
    assert actual == expected, result.stderr

    # Both collection pages, followed through pagination, and no product page
    store_hits = hits["store"]
    assert store_hits.count("/shop?format=json") == 1
    assert store_hits.count(f"{NEXT_PAGE}&format=json") == 1
    assert not [h for h in store_hits if "/p/" in h]

    store_rows = _rows_by_store(rows, urls["store"])
    assert {(r["platform"], r["source"]) for r in store_rows} == {("squarespace", "format_json")}
    assert sorted(r["handle"] for r in store_rows) == ["cap", "mug", "tee", "tee"]
    tee = [r for r in store_rows if r["handle"] == "tee"]
    assert [r["options"] for r in tee] == ["Color, Size", "Color, Size"]
    assert [r["variant_title"] for r in tee] == ["Red / S", "Red / M"]
    assert [(r["price"], r["compare_at_price"]) for r in tee] == [("15.00", "20.00"), ("20.00", "")]
    assert [r["available"] for r in tee] == ["true", "false"]
    assert {r["html_url"] for r in tee} == {f"{urls['store']}/shop/p/tee"}

    # HTML at ?format=json: the listed product pages are read instead, each once
    fallback_hits = hits["html_json"]
    assert sorted(h for h in fallback_hits if "/p/" in h) == sorted(f"/shop/p/{s}" for s in SLUGS)
    assert fallback_hits.count("/sitemap.xml") == 1
    fallback_rows = _rows_by_store(rows, urls["html_json"])
    assert len(fallback_rows) == 3
    assert {r["source"] for r in fallback_rows} == {"product_page"}
