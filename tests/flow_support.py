"""Shared helpers for flow tests that drive the public API against FakeFetcher."""

from __future__ import annotations

from typing import Any

import shopify_spy as spy
from shopify_spy._fetch import FakeFetcher

STORE = "https://shop.test"
WOO_API = "/wp-json/wc/store/v1/products"

# Fast defaults: no throttle, no retry backoff, failures left to run.result
FAST = {"throttle": False, "_backoff": False, "on_failure": "ignore"}


def bulk_url(page: int, store: str = STORE) -> str:
    return f"{store}/products.json?limit=250&page={page}"


def woo_url(page: int, store: str = STORE) -> str:
    return f"{store}{WOO_API}?per_page=100&page={page}"


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
        + "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
        + "</urlset>"
    )
    return 200, body, {"Content-Type": "application/xml"}


def sitemapindex(*locs: str) -> tuple[int, str, dict[str, str]]:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
        + "</sitemapindex>"
    )
    return 200, body, {"Content-Type": "application/xml"}


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
