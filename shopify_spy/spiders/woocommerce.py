import json
import urllib.parse
from collections.abc import AsyncGenerator, Generator
from typing import Any

import scrapy
from scrapy.http import Response

from shopify_spy.utils import as_bool, normalize_url


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
        **kwargs: Any,
    ) -> None:
        """Initialize spider with store URL(s).

        Args:
            url: Complete URL of target WooCommerce store.
            url_file: Path to text file with one URL per line.
            images: Whether to collect image URLs.
        """
        if url:
            self._store_urls = [url]
        elif url_file:
            with open(url_file) as f:
                self._store_urls = [line.strip() for line in f if line.strip()]
        else:
            self._store_urls = []

        self.images_enabled = as_bool(images)
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
            product["store"] = store
            if self.images_enabled:
                product["image_urls"] = [img["src"] for img in product.get("images", [])]
            else:
                product["image_urls"] = []
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
