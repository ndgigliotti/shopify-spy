import json
import urllib.parse
from collections.abc import Generator
from typing import Any

import scrapy
from scrapy.http import Response

from shopify_spy.utils import as_bool, find_all_values

COMMON_COLLECTION_PATHS = ["shop", "store", "products", "collections"]


class SquarespaceSpider(scrapy.Spider):
    """Spider for scraping Squarespace stores using the ?format=json endpoint.

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
        **kwargs: Any,
    ) -> None:
        """Initialize spider with store URL(s).

        Args:
            url: Complete URL of target Squarespace store.
            url_file: Path to text file with one URL per line.
            collection_path: Shop collection path (e.g. "shop", "store").
                Tries common paths if not specified.
            images: Whether to extract image URLs.
        """
        if url:
            self._store_urls = [get_base_url(url)]
        elif url_file:
            with open(url_file) as f:
                self._store_urls = [get_base_url(s) for line in f if (s := line.strip())]
        else:
            self._store_urls = []

        self._collection_paths = (
            [collection_path.strip("/")] if collection_path else list(COMMON_COLLECTION_PATHS)
        )
        self.images_enabled = as_bool(images)

        super().__init__(*args, **kwargs)

    def start_requests(self) -> Generator[scrapy.Request, None, None]:
        for store_url in self._store_urls:
            for path in self._collection_paths:
                url = f"{store_url}/{path}?format=json"
                yield scrapy.Request(url, callback=self.parse_collection)

    def parse_collection(self, response: Response) -> Generator[scrapy.Request, None, None]:
        """Parse collection JSON and yield a request for each product."""
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
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
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            return

        if "item" not in data:
            return

        data["url"] = response.request.url
        data["store"] = urllib.parse.urlparse(response.request.url).netloc

        if self.images_enabled:
            data["image_urls"] = list(find_all_values("assetUrl", data["item"]))
        else:
            data["image_urls"] = []

        yield data


def get_base_url(url: str) -> str:
    """Return scheme + netloc of a URL, defaulting to https if no scheme."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"https://{url}")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
