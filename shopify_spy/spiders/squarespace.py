import json
import urllib.parse
from collections.abc import AsyncGenerator, Generator
from typing import Any

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response

from shopify_spy.utils import as_bool, find_all_values, load_store_urls, normalize_url

COMMON_COLLECTION_PATHS = ["shop", "store", "products", "collections"]


class SquarespaceSpider(scrapy.Spider):
    """Spider for scraping Squarespace stores using the ?format=json endpoint.

    Automatically discovers collection pages from site navigation. Falls back
    to probing common paths (shop, store, products, collections) if discovery
    finds nothing.

    Usage examples:
    scrapy crawl squarespace_spider -a url=https://example.squarespace.com
    scrapy crawl squarespace_spider -a url=https://example.com -a collection_path=store
    scrapy crawl squarespace_spider -a url_file=resources/urls.txt
    """

    name = "squarespace_spider"

    def __init__(
        self,
        *args: Any,
        url: str | None = None,
        url_file: str | None = None,
        collection_path: str | None = None,
        images: bool | str = True,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize spider with store URL(s).

        Args:
            url: Complete URL of target Squarespace store.
            url_file: Path to text file with one URL per line.
            collection_path: Shop collection path (e.g. "shop", "store").
                Auto-discovered from site navigation if not specified.
            images: Whether to extract image URLs.
            limit: Stop after yielding this many items total.
        """
        self._store_urls = [get_base_url(u) for u in load_store_urls(url, url_file)]

        self._collection_path: str | None = collection_path.strip("/") if collection_path else None
        self.images_enabled = as_bool(images)
        self.limit = int(limit) if limit is not None else None
        self._item_count = 0

        super().__init__(*args, **kwargs)

    async def start(self) -> AsyncGenerator[scrapy.Request]:
        for store_url in self._store_urls:
            if self._collection_path:
                url = f"{store_url}/{self._collection_path}?format=json"
                yield scrapy.Request(url, callback=self.parse_collection)
            else:
                yield scrapy.Request(store_url, callback=self.discover_collections)

    def discover_collections(self, response: Response) -> Generator[scrapy.Request, None, None]:
        """Extract navigation links from the HTML homepage and probe each for products."""
        store_url = get_base_url(response.request.url)
        nav_links = response.css('nav[data-content-field="navigation"] a::attr(href)').getall()

        # Deduplicate and keep only internal relative paths
        seen: set[str] = set()
        paths: list[str] = []
        for href in nav_links:
            path = href.strip("/")
            if path and not path.startswith(("http", "#", "mailto:")) and path not in seen:
                seen.add(path)
                paths.append(path)

        if not paths:
            paths = list(COMMON_COLLECTION_PATHS)
            self.logger.info("No nav links found, falling back to common paths: %s", paths)

        for path in paths:
            yield scrapy.Request(f"{store_url}/{path}?format=json", callback=self.parse_collection)

    def parse_collection(self, response: Response) -> Generator[scrapy.Request, None, None]:
        """Parse collection JSON and yield a request for each product."""
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return

        items = data.get("items")
        if not items:
            return

        store_url = get_base_url(response.request.url)

        for item in items:
            full_url = item.get("fullUrl", "")
            if not full_url:
                continue
            product_url = f"{store_url}{full_url}?format=json"
            yield scrapy.Request(product_url, callback=self.parse_product)

    def parse_product(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Yield product data."""
        if self.limit is not None and self._item_count >= self.limit:
            raise CloseSpider("item_limit")

        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return

        if "item" not in data:
            return

        data["url"] = response.request.url
        data["store"] = urllib.parse.urlparse(response.request.url).netloc

        if self.images_enabled:
            data["image_urls"] = _filter_image_urls(find_all_values("assetUrl", data["item"]))
        else:
            data["image_urls"] = []

        self._item_count += 1
        yield data


def get_base_url(url: str) -> str:
    """Return scheme + netloc of a URL, defaulting to https if no scheme."""
    parsed = normalize_url(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _filter_image_urls(urls: Generator) -> list[str]:
    """Filter out non-image asset URLs that cause download errors.

    Some Squarespace assetUrl values point to directory-like paths (ending
    with ``/``) which 302 redirect and break the image pipeline.  Keep only
    URLs whose path ends with a file extension.
    """
    return [
        url
        for url in urls
        if isinstance(url, str) and not urllib.parse.urlparse(url).path.endswith("/")
    ]
