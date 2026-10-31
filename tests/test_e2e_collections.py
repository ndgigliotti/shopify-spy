"""End-to-end: run the real `spy` CLI against a local store past Shopify's bulk limit.

The store has 26,200 products. /products.json serves the first 25,000 in 100 full
pages (page 101 answers 400, like Shopify), collections hold 950 more, and the last
250 are only in the sitemap. Run with: uv run pytest -m e2e
"""

import http.server
import json
import subprocess
import sys
import threading
import urllib.parse
from collections import Counter

import pytest

from tests.test_e2e_diagnosis import _free_port

pytestmark = pytest.mark.e2e

PAGE = 250
CATALOG = 26_200
BULK = list(range(25_000))
LISTS = {
    "/collections/all/products.json": list(range(25_000, 25_500)),
    # Its first page repeats bulk products; the next two add 350 new ones
    "/collections/big/products.json": list(range(PAGE)) + list(range(25_500, 25_850)),
    "/collections/small/products.json": list(range(25_850, 25_950)),
}
COLLECTIONS = [
    {"handle": "small", "products_count": 100},
    {"handle": "big", "products_count": 600},
    {"handle": "empty", "products_count": 0},
]
SITEMAP_ONLY = range(25_950, CATALOG)


def product(i: int) -> dict:
    return {"id": i, "handle": f"p{i}", "title": f"Product {i}", "variants": [{"id": i}]}


class _Store(http.server.BaseHTTPRequestHandler):
    hits: Counter

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._send(200, json.dumps(data).encode())

    def _xml(self, tag, child, locs):
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        entries = "".join(f"<{child}><loc>{loc}</loc></{child}>" for loc in locs)
        body = f'<?xml version="1.0"?><{tag} xmlns="{ns}">{entries}</{tag}>'
        self._send(200, body.encode(), "application/xml")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        page = int(dict(urllib.parse.parse_qsl(url.query)).get("page", 1))
        path = url.path
        self.hits[path if path.startswith("/products/") else f"{path}?page={page}"] += 1
        base = f"http://{self.headers['Host']}"

        def window(ids):
            return [product(i) for i in ids[(page - 1) * PAGE : page * PAGE]]

        if path == "/products.json":
            if page > 100:
                return self._send(400)
            return self._json({"products": window(BULK)})
        if path in LISTS:
            return self._json({"products": window(LISTS[path])})
        if path == "/collections.json":
            return self._json({"collections": COLLECTIONS if page == 1 else []})
        if path == "/sitemap.xml":
            children = [f"{base}/sitemap_products_{n}.xml" for n in (1, 2)]
            return self._xml("sitemapindex", "sitemap", children)
        if path.startswith("/sitemap_products_"):
            half = range(13_100) if "_1." in path else range(13_100, CATALOG)
            return self._xml("urlset", "url", [f"{base}/products/p{i}" for i in half])
        if path.startswith("/products/") and path.endswith(".json"):
            return self._json({"product": product(int(path[len("/products/p") : -5]))})
        return self._send(404)


@pytest.fixture
def store():
    handler = type("Store", (_Store,), {"hits": Counter()})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler.hits
    server.shutdown()


def test_capped_store_is_scraped_through_collections_then_the_sitemap(tmp_path, store):
    url, hits = store
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopify_spy.cli",
            "scrape",
            url,
            "--output",
            str(tmp_path),
            "--no-throttle",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    status = json.loads(next(tmp_path.glob("status/*_status.json")).read_text())
    assert status["urls"] == [
        {"url": url, "items": CATALOG, "status": status["urls"][0]["status"], "code": "ok"}
    ]
    items = [
        json.loads(line)
        for path in tmp_path.glob("data/*.jsonl")
        for line in path.read_text().splitlines()
    ]
    handles = [item["product"]["handle"] for item in items]
    assert len(handles) == CATALOG
    assert set(handles) == {f"p{i}" for i in range(CATALOG)}  # every product, once

    assert hits["/products.json?page=100"] == 1
    assert hits["/products.json?page=101"] == 0  # a full page 100 is the limit
    assert hits["/collections/all/products.json?page=2"] == 1
    assert hits["/collections.json?page=1"] == 1
    assert [hits[f"/collections/big/products.json?page={n}"] for n in (1, 2, 3)] == [1, 1, 1]
    assert hits["/collections/small/products.json?page=1"] == 1
    assert hits["/collections/empty/products.json?page=1"] == 0
    # Only the products no list had are fetched one by one
    fetched = {path for path in hits if path.startswith("/products/")}
    assert fetched == {f"/products/p{i}.json" for i in SITEMAP_ONLY}
