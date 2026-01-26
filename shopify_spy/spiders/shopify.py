import json
import re
import urllib.parse
from collections.abc import Generator, Iterator
from typing import Any

import scrapy
from scrapy.http import Response

from shopify_spy.utils import as_bool, find_all_values


class ShopifySpider(scrapy.spiders.SitemapSpider):
    """Sitemap-based spider for scraping Shopify stores.

    Usage examples:
    scrapy crawl shopify_spider -a url=https://www.example.com/
    scrapy crawl shopify_spider -a url_file=resources/urls.txt
    """

    name = "shopify_spider"

    def __init__(
        self,
        *args: Any,
        url: str | None = None,
        url_file: str | None = None,
        products: bool | str = True,
        collections: bool | str = False,
        images: bool | str = True,
        **kwargs: Any,
    ) -> None:
        """Initialize spider by inferring sitemap URLs.

        Args:
            url: Complete URL of target Shopify store.
            url_file: Path to text file with one URL per line.
            products: Whether to scrape products.
            collections: Whether to scrape collections.
            images: Whether to scrape images.
        """
        # Determine starting sitemap URLs
        if url:
            self.sitemap_urls = [get_sitemap_url(url)]
        elif url_file:
            with open(url_file) as f:
                self.sitemap_urls = [get_sitemap_url(line.strip()) for line in f if line.strip()]
        else:
            self.sitemap_urls = []

        # Determine what to scrape
        self.sitemap_rules: list[tuple[str, str]] = []
        if as_bool(products):
            self.sitemap_rules.append(("/products/", "parse_product"))
        if as_bool(collections):
            self.sitemap_rules.append(("/collections/", "parse_collection"))
        self.images_enabled = as_bool(images)

        super().__init__(*args, **kwargs)

    def sitemap_filter(self, entries: Iterator[dict[str, Any]]) -> Generator[dict[str, Any]]:
        """Modify links to reach JSON data files."""
        for entry in entries:
            if re.search(r"/products/|/collections/", entry["loc"]):
                entry["loc"] = entry["loc"] + ".json"
            yield entry

    def parse_product(self, response: Response) -> Generator[dict[str, Any]]:
        """Yield product data.

        @url https://www.snowdevil.ca/products/a-frame-1.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes product image_urls
        """
        data = extract_data(response)

        if self.images_enabled:
            image_urls = list(find_all_values("src", data))
        else:
            image_urls = []

        data["image_urls"] = image_urls
        yield data

    def parse_collection(self, response: Response) -> Generator[dict[str, Any]]:
        """Yield collection data.

        @url https://www.snowdevil.ca/collections/2011-winter-sale.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes collection image_urls
        """
        data = extract_data(response)

        if self.images_enabled:
            image_urls = list(find_all_values("src", data))
        else:
            image_urls = []

        data["image_urls"] = image_urls
        yield data


def extract_data(response: Response) -> dict[str, Any]:
    """Deserialize JSON and return item with metadata."""
    data: dict[str, Any] = json.loads(response.text)
    data["url"] = response.request.url
    data["store"] = urllib.parse.urlparse(response.request.url).netloc
    return data


def get_sitemap_url(url: str) -> str:
    """Infer sitemap URL from given URL, normalizing if needed."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        # Assume https if no scheme provided
        parsed = urllib.parse.urlparse(f"https://{url}")
    sitemap_url = (parsed.scheme, parsed.netloc, "sitemap.xml", "", "", "")
    return urllib.parse.urlunparse(sitemap_url)
