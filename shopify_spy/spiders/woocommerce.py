import json
import urllib.parse
from collections.abc import AsyncGenerator, Generator
from typing import Any

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response

from shopify_spy.utils import as_bool, load_store_urls, normalize_url


class WooCommerceSpider(scrapy.Spider):
    """Paginated API spider for scraping WooCommerce stores.

    Uses the public WooCommerce Store API (no authentication required).

    Usage examples:
    scrapy crawl woocommerce_spider -a url=https://www.example.com/
    scrapy crawl woocommerce_spider -a url_file=resources/urls.txt
    """

    name = "woocommerce_spider"

    def __init__(
        self,
        *args: Any,
        url: str | None = None,
        url_file: str | None = None,
        images: bool | str = True,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize spider with store URL(s).

        Args:
            url: Complete URL of target WooCommerce store.
            url_file: Path to text file with one URL per line.
            images: Whether to collect image URLs.
            limit: Stop after yielding this many items total.
        """
        self._store_urls = load_store_urls(url, url_file)

        self.images_enabled = as_bool(images)
        self.limit = int(limit) if limit is not None else None
        self._item_count = 0
        super().__init__(*args, **kwargs)

    async def start(self) -> AsyncGenerator[scrapy.Request]:
        for url in self._store_urls:
            yield scrapy.Request(get_api_url(url, page=1), callback=self.parse)

    def parse(self, response: Response) -> Generator[dict[str, Any] | scrapy.Request]:
        """Yield products from one page of the Store API and follow pagination.

        @url https://woo.com/wp-json/wc/store/v1/products?per_page=100&page=1
        @returns items 1
        @returns requests 1 1
        @scrapes id name store image_urls
        """
        products: list[dict[str, Any]] = json.loads(response.text)
        if not products:
            return

        store = urllib.parse.urlparse(response.request.url).netloc

        for product in products:
            if self.limit is not None and self._item_count >= self.limit:
                raise CloseSpider("item_limit")

            product["store"] = store
            if self.images_enabled:
                product["image_urls"] = [img["src"] for img in product.get("images", [])]
            else:
                product["image_urls"] = []
            self._item_count += 1
            yield product

        yield scrapy.Request(next_page_url(response.request.url), callback=self.parse)


def get_api_url(store_url: str, page: int = 1) -> str:
    """Build WooCommerce Store API URL for the given store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"per_page": 100, "page": page})
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, "/wp-json/wc/store/v1/products", "", query, "")
    )


def next_page_url(url: str) -> str:
    """Increment the page parameter in a Store API URL."""
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params["page"] = int(params.get("page", 1)) + 1
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(params), "")
    )
