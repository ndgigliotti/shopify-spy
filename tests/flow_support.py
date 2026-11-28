"""Shared helpers for flow tests that drive the public API against FakeFetcher."""

from __future__ import annotations

import base64
import json
from typing import Any
from xml.sax.saxutils import escape

import shopify_spy as spy
from shopify_spy import _bigcommerce
from shopify_spy._fetch import FakeFetcher
from shopify_spy._magento import get_api_url

STORE = "https://shop.test"
WOO_API = "/wp-json/wc/store/v1/products"

# Fast defaults: no throttle, no retry backoff, failures left to run.result
FAST = {"throttle": False, "_backoff": False, "on_failure": "ignore"}


def bulk_url(page: int, store: str = STORE) -> str:
    return f"{store}/products.json?limit=250&page={page}"


def collection_url(handle: str, page: int, store: str = STORE) -> str:
    return f"{store}/collections/{handle}/products.json?limit=250&page={page}"


def woo_url(page: int, store: str = STORE) -> str:
    return f"{store}{WOO_API}?per_page=100&page={page}&orderby=id&order=asc"


def magento_url(page: int, page_size: int = 100, store: str = STORE) -> str:
    return get_api_url(store, page, page_size)


def bigcommerce_api(store: str = STORE) -> str:
    return _bigcommerce.get_api_url(store)


def bc_token(**claims: Any) -> str:
    """An unsigned three-part JWT shaped like a BigCommerce Storefront API token."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    payload = {"iss": "BC", "sub": "BC", "cors": [STORE], "eat": 2_000_000_000, **claims}
    return f"{part({'alg': 'ES256', 'typ': 'JWT'})}.{part(payload)}.c2lnbmF0dXJl"


def product(i: int | str, **extra: Any) -> dict[str, Any]:
    return {
        "id": i,
        "handle": f"p{i}",
        "title": f"Product {i}",
        "images": [{"src": f"https://cdn.test/{i}.jpg"}],
        "variants": [{"id": 1, "price": "10.00"}],
        **extra,
    }


def products(n: int, start: int = 0) -> dict[str, Any]:
    return {"products": [product(i) for i in range(start, start + n)]}


def urlset(*locs: str) -> tuple[int, str, dict[str, str]]:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{escape(loc)}</loc></url>" for loc in locs)
        + "</urlset>"
    )
    return 200, body, {"Content-Type": "application/xml"}


def sitemapindex(*locs: str) -> tuple[int, str, dict[str, str]]:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{escape(loc)}</loc></sitemap>" for loc in locs)
        + "</sitemapindex>"
    )
    return 200, body, {"Content-Type": "application/xml"}


def product_page(*nodes: Any) -> tuple[int, str, dict[str, str]]:
    """An HTML page with each node in its own JSON-LD script."""
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(node)}</script>' for node in nodes
    )
    body = f"<html><head>{scripts}</head><body></body></html>"
    return 200, body, {"Content-Type": "text/html"}


def redirect(location: str, status: int = 301) -> tuple[int, str, dict[str, str]]:
    return status, "", {"Location": location}


def scrape(
    routes: dict[Any, Any] | FakeFetcher, urls: Any = STORE, **options: Any
) -> tuple[list[dict[str, Any]], spy.RunResult, FakeFetcher]:
    """Run a sync scrape to completion; return (items, result, fetcher)."""
    fetcher = routes if isinstance(routes, FakeFetcher) else FakeFetcher(routes)
    run = spy.scrape(urls, fetcher_factory=lambda: fetcher, **{**FAST, **options})
    items = list(run)
    return items, run.result, fetcher


def requested(fetcher: FakeFetcher) -> list[str]:
    return [r.url for r in fetcher.requests if not r.url.endswith("/robots.txt")]
