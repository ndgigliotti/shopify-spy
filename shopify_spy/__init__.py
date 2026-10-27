"""Scrape product and collection data from Shopify and WooCommerce stores.

Scripts::

    import shopify_spy as spy

    run = spy.scrape(["a.com", "b.com"], on_failure="warn")
    spy.write_items(run, "products.jsonl")
    for store in run.result.failed:
        print(store.input_url, store.status, store.hint)

Async code and notebooks::

    async for item in spy.scrape_async("a.com"):
        ...
"""

from shopify_spy.api import (
    AsyncScrapeRun,
    ScrapeOptions,
    ScrapeRun,
    ScrapeWarning,
    scrape,
    scrape_async,
)
from shopify_spy.export import ItemWriter, write_items, write_status
from shopify_spy.results import RunResult, ScrapeFailed, StoreResult, StoreStatus
from shopify_spy.summary import ItemSummary, summarize
from shopify_spy.types import ShopifyCollectionItem, ShopifyProductItem, WooCommerceProductItem
from shopify_spy.utils import read_url_file

__all__ = [
    "AsyncScrapeRun",
    "ItemSummary",
    "ItemWriter",
    "RunResult",
    "ScrapeFailed",
    "ScrapeOptions",
    "ScrapeRun",
    "ScrapeWarning",
    "ShopifyCollectionItem",
    "ShopifyProductItem",
    "StoreResult",
    "StoreStatus",
    "WooCommerceProductItem",
    "read_url_file",
    "scrape",
    "scrape_async",
    "summarize",
    "write_items",
    "write_status",
]
