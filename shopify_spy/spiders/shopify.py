import json
import urllib.parse
from collections.abc import AsyncGenerator, Generator, Iterator
from typing import Any

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response

from shopify_spy.utils import as_bool, find_all_values, load_store_urls, normalize_url


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
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize spider by inferring sitemap URLs.

        Args:
            url: Complete URL of target Shopify store.
            url_file: Path to text file with one URL per line.
            products: Whether to scrape products.
            collections: Whether to scrape collections.
            images: Whether to scrape images.
            limit: Stop after yielding this many items total.
        """
        self._store_urls = load_store_urls(url, url_file)
        self.sitemap_urls = [get_sitemap_url(u) for u in self._store_urls]

        self._products_enabled = as_bool(products)
        self._collections_enabled = as_bool(collections)
        self.sitemap_rules: list[tuple[str, str]] = []
        if self._products_enabled:
            self.sitemap_rules.append(("/products/", "parse_product"))
        if self._collections_enabled:
            self.sitemap_rules.append(("/collections/", "parse_collection"))
        self.images_enabled = as_bool(images)
        self.limit = int(limit) if limit is not None else None
        self._item_count = 0

        self._bulk_product_stores: set[str] = set()

        super().__init__(*args, **kwargs)

    async def start(self) -> AsyncGenerator[scrapy.Request]:
        """Try bulk /products.json first, defer sitemap for fallback/collections."""
        for store_url in self._store_urls:
            sitemap_url = get_sitemap_url(store_url)
            if self._products_enabled:
                bulk_url = get_bulk_products_url(store_url)
                yield scrapy.Request(
                    bulk_url,
                    callback=self.parse_products_json,
                    cb_kwargs={"sitemap_url": sitemap_url},
                    errback=self._bulk_errback,
                    meta={"sitemap_url": sitemap_url},
                )
            else:
                yield scrapy.Request(sitemap_url, callback=self._parse_sitemap)

    def parse_products_json(
        self, response: Response, sitemap_url: str
    ) -> Generator[dict[str, Any] | scrapy.Request]:
        """Parse bulk /products.json endpoint with sitemap fallback."""
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            self.logger.info(
                "Bulk products.json returned non-JSON for %s, falling back to sitemap",
                response.url,
            )
            yield scrapy.Request(sitemap_url, callback=self._parse_sitemap)
            return

        products = data.get("products")
        if not isinstance(products, list) or not products:
            self.logger.info(
                "Bulk products.json empty for %s, falling back to sitemap",
                response.url,
            )
            yield scrapy.Request(sitemap_url, callback=self._parse_sitemap)
            return

        parsed = urllib.parse.urlparse(response.url)
        store = parsed.netloc
        self._bulk_product_stores.add(store)
        params = urllib.parse.parse_qs(parsed.query)
        page = params.get("page", ["1"])[0]
        self.logger.info("Bulk: %d products from %s (page %s)", len(products), store, page)

        base_url = f"{parsed.scheme}://{store}/products/"
        for product in products:
            if self.limit is not None and self._item_count >= self.limit:
                raise CloseSpider("item_limit")

            handle = product.get("handle", "")
            item = {
                "product": product,
                "url": f"{base_url}{handle}.json",
                "store": store,
                "image_urls": list(find_all_values("src", product)) if self.images_enabled else [],
            }
            self._item_count += 1
            yield item

        if len(products) == 250:
            yield scrapy.Request(
                next_bulk_page_url(response.url),
                callback=self.parse_products_json,
                cb_kwargs={"sitemap_url": sitemap_url},
                errback=self._bulk_errback,
                meta={"sitemap_url": sitemap_url},
            )
        elif self._collections_enabled:
            yield scrapy.Request(sitemap_url, callback=self._parse_sitemap)

    def _bulk_errback(self, failure: Any) -> Generator[scrapy.Request]:
        """Bulk products.json failed; fall back to sitemap."""
        request = failure.request
        sitemap_url = request.meta["sitemap_url"]
        self.logger.info(
            "Bulk products.json request failed for %s, falling back to sitemap",
            request.url,
        )
        yield scrapy.Request(sitemap_url, callback=self._parse_sitemap)

    def sitemap_filter(self, entries: Iterator[dict[str, Any]]) -> Generator[dict[str, Any]]:
        """Modify links to reach JSON data files, skipping bulk-covered products."""
        for entry in entries:
            loc = entry["loc"]
            if "/products/" in loc:
                store = urllib.parse.urlparse(loc).netloc
                if store in self._bulk_product_stores:
                    continue
                entry["loc"] = loc + ".json"
            elif "/collections/" in loc:
                entry["loc"] = loc + ".json"
            yield entry

    def parse_product(self, response: Response) -> Generator[dict[str, Any]]:
        """Yield product data.

        @url https://www.snowdevil.ca/products/a-frame-1.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes product image_urls
        """
        if self.limit is not None and self._item_count >= self.limit:
            raise CloseSpider("item_limit")

        data = extract_data(response)

        if self.images_enabled:
            image_urls = list(find_all_values("src", data))
        else:
            image_urls = []

        data["image_urls"] = image_urls
        self._item_count += 1
        yield data

    def parse_collection(self, response: Response) -> Generator[dict[str, Any]]:
        """Yield collection data.

        @url https://www.snowdevil.ca/collections/2011-winter-sale.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes collection image_urls
        """
        if self.limit is not None and self._item_count >= self.limit:
            raise CloseSpider("item_limit")

        data = extract_data(response)

        if self.images_enabled:
            image_urls = list(find_all_values("src", data))
        else:
            image_urls = []

        data["image_urls"] = image_urls
        self._item_count += 1
        yield data


def extract_data(response: Response) -> dict[str, Any]:
    """Deserialize JSON and return item with metadata."""
    data: dict[str, Any] = json.loads(response.text)
    data["url"] = response.request.url
    data["store"] = urllib.parse.urlparse(response.request.url).netloc
    return data


def get_sitemap_url(url: str) -> str:
    """Infer sitemap URL from given URL, normalizing if needed."""
    parsed = normalize_url(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "sitemap.xml", "", "", ""))


def get_bulk_products_url(store_url: str, page: int = 1) -> str:
    """Build bulk products JSON URL for the given store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"limit": 250, "page": page})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/products.json", "", query, ""))


def next_bulk_page_url(url: str) -> str:
    """Increment the page parameter in a bulk products URL."""
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params["page"] = str(int(params.get("page", 1)) + 1)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(params), "")
    )
