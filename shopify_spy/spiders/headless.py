"""Hybrid spider for Shopify stores with Playwright fallback.

This spider uses a hybrid approach:
1. Tries fast JSON endpoints first (no browser needed)
2. Falls back to Playwright rendering only when JSON fails

This makes it work on both traditional Liquid stores (fast) and
headless Hydrogen stores (slower but works).

Usage:
    scrapy crawl headless_spider -a url=https://any-shopify-store.com/
"""

import json
import re
import urllib.parse
from collections.abc import AsyncGenerator, Generator
from typing import Any

import scrapy
from scrapy.http import Response
from scrapy_playwright.page import PageMethod


class HeadlessSpider(scrapy.Spider):
    """Hybrid spider: tries JSON first, falls back to Playwright."""

    name = "headless_spider"

    custom_settings = {
        "DOWNLOAD_DELAY": 1,  # Sensible default for browser-based scraping
    }

    def __init__(
        self,
        *args: Any,
        url: str | None = None,
        url_file: str | None = None,
        products: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize spider."""
        super().__init__(*args, **kwargs)

        if url:
            self.store_urls = [url]
        elif url_file:
            with open(url_file) as f:
                self.store_urls = [line.strip() for line in f if line.strip()]
        else:
            self.store_urls = []

        self.products = products

        # Track which stores need Playwright (JSON failed)
        self._needs_playwright: set[str] = set()

    async def start(self) -> AsyncGenerator[scrapy.Request, None]:
        """Try products.json first, fall back to Playwright collection scraping."""
        if not self.products:
            return

        for store_url in self.store_urls:
            parsed = urllib.parse.urlparse(store_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            # Try products.json first (fast, works on most stores)
            yield scrapy.Request(
                url=f"{base_url}/products.json?limit=250",
                callback=self.parse_products_json,
                errback=self.fallback_to_playwright,
                meta={
                    "store": parsed.netloc,
                    "base_url": base_url,
                    "page": 1,
                },
                dont_filter=True,
            )

    def parse_products_json(
        self, response: Response
    ) -> Generator[scrapy.Request | dict[str, Any], None, None]:
        """Parse /products.json endpoint (fast path)."""
        store = response.meta["store"]
        base_url = response.meta["base_url"]
        page = response.meta["page"]

        try:
            data = response.json()
            products = data.get("products", [])
        except (json.JSONDecodeError, ValueError):
            # JSON failed, fall back to Playwright
            self.logger.info(f"JSON parsing failed for {store}, using Playwright")
            yield from self._playwright_collection_request(base_url, store)
            return

        if not products and page == 1:
            # No products found, try Playwright
            self.logger.info(f"No products in JSON for {store}, trying Playwright")
            yield from self._playwright_collection_request(base_url, store)
            return

        self.logger.info(f"Found {len(products)} products via JSON on {store} (page {page})")

        # Yield products
        for product in products:
            yield {
                "product": product,
                "url": f"{base_url}/products/{product.get('handle')}",
                "store": store,
                "source": "json",
                "image_urls": self._extract_images_from_json(product),
            }

        # Paginate if we got a full page
        if len(products) == 250:
            yield scrapy.Request(
                url=f"{base_url}/products.json?limit=250&page={page + 1}",
                callback=self.parse_products_json,
                meta={
                    "store": store,
                    "base_url": base_url,
                    "page": page + 1,
                },
            )

    def fallback_to_playwright(self, failure: Any) -> Generator[scrapy.Request, None, None]:
        """Fall back to Playwright when JSON endpoint fails."""
        request = failure.request
        store = request.meta["store"]
        base_url = request.meta["base_url"]

        self.logger.info(f"JSON endpoint failed for {store}, falling back to Playwright")
        self._needs_playwright.add(store)

        yield from self._playwright_collection_request(base_url, store)

    def _playwright_collection_request(
        self, base_url: str, store: str
    ) -> Generator[scrapy.Request, None, None]:
        """Generate Playwright request for collection page."""
        yield scrapy.Request(
            url=f"{base_url}/collections/all",
            callback=self.parse_collection,
            errback=self.handle_error,
            meta={
                "playwright": True,
                "playwright_page_methods": [
                    # Faster than networkidle - just wait for content
                    PageMethod("wait_for_selector", "a[href*='/products/']", timeout=15000),
                ],
                "store": store,
                "base_url": base_url,
            },
            dont_filter=True,
        )

    def parse_collection(
        self, response: Response
    ) -> Generator[scrapy.Request | dict[str, Any], None, None]:
        """Parse collection page and extract product links."""
        store = response.meta["store"]
        base_url = response.meta["base_url"]

        # Find product links
        product_links = set()
        for href in response.css('a[href*="/products/"]::attr(href)').getall():
            if "/products/" in href and not href.endswith("/products/"):
                # Clean the URL
                clean_href = href.split("?")[0].split("#")[0]
                product_links.add(clean_href)

        self.logger.info(f"Found {len(product_links)} product links on {response.url}")

        for href in product_links:
            # Normalize URL
            if href.startswith("/"):
                product_url = base_url + href
            elif not href.startswith("http"):
                product_url = base_url + "/" + href
            else:
                product_url = href

            # Try JSON first for this product
            yield scrapy.Request(
                url=product_url + ".json",
                callback=self.parse_product_json,
                errback=self.fallback_product_to_playwright,
                meta={
                    "store": store,
                    "product_url": product_url,
                },
            )

        # Handle pagination
        next_page = response.css(
            'a[rel="next"]::attr(href), [aria-label="Next page"]::attr(href)'
        ).get()

        if next_page:
            if next_page.startswith("/"):
                next_page = base_url + next_page
            yield scrapy.Request(
                url=next_page,
                callback=self.parse_collection,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", "a[href*='/products/']", timeout=15000),
                    ],
                    "store": store,
                    "base_url": base_url,
                },
            )

    def parse_product_json(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Parse individual product JSON (fast path for product pages)."""
        store = response.meta["store"]
        product_url = response.meta["product_url"]

        try:
            data = response.json()
            product = data.get("product", {})

            if product:
                yield {
                    "product": product,
                    "url": product_url,
                    "store": store,
                    "source": "json",
                    "image_urls": self._extract_images_from_json(product),
                }
        except (json.JSONDecodeError, ValueError):
            # Will be handled by errback
            pass

    def fallback_product_to_playwright(self, failure: Any) -> Generator[scrapy.Request, None, None]:
        """Fall back to Playwright for individual product page."""
        request = failure.request
        store = request.meta["store"]
        product_url = request.meta["product_url"]

        yield scrapy.Request(
            url=product_url,
            callback=self.parse_product_rendered,
            meta={
                "playwright": True,
                "playwright_page_methods": [
                    PageMethod("wait_for_selector", "[class*='product']", timeout=10000),
                ],
                "store": store,
            },
        )

    def parse_product_rendered(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Extract product data from rendered product page (slow path)."""
        store = response.meta["store"]

        # Try multiple extraction strategies
        product_data = (
            self._extract_jsonld(response)
            or self._extract_shopify_object(response)
            or self._extract_meta_tags(response)
        )

        if product_data:
            product_data["url"] = response.url
            product_data["store"] = store
            product_data["source"] = "playwright"
            product_data["image_urls"] = self._extract_images_rendered(response, product_data)
            yield product_data
        else:
            self.logger.warning(f"Could not extract product data from {response.url}")

    def _extract_images_from_json(self, product: dict[str, Any]) -> list[str]:
        """Extract image URLs from JSON product data."""
        images = []
        for img in product.get("images", []):
            if isinstance(img, dict):
                src = img.get("src")
            else:
                src = img
            if src:
                images.append(src)
        return images

    def _extract_jsonld(self, response: Response) -> dict[str, Any] | None:
        """Extract product data from JSON-LD script tags."""
        for script in response.css('script[type="application/ld+json"]::text').getall():
            try:
                data = json.loads(script)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Product":
                        return self._normalize_jsonld(item)
            except json.JSONDecodeError:
                continue
        return None

    def _normalize_jsonld(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize JSON-LD product data."""
        offers = data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        brand = data.get("brand")
        vendor = brand.get("name") if isinstance(brand, dict) else brand

        image = data.get("image")
        images = [image] if isinstance(image, str) else (image or [])

        return {
            "product": {
                "title": data.get("name"),
                "description": data.get("description"),
                "handle": self._extract_handle(data.get("url", "")),
                "vendor": vendor,
                "product_type": data.get("category"),
                "price": offers.get("price"),
                "currency": offers.get("priceCurrency"),
                "sku": data.get("sku"),
                "images": images,
            }
        }

    def _extract_shopify_object(self, response: Response) -> dict[str, Any] | None:
        """Extract from embedded Shopify product JSON."""
        for script in response.css("script::text").getall():
            # Look for product object in various formats
            patterns = [
                r'var\s+meta\s*=\s*(\{.+?"product"\s*:.+?\});',
                r'"product"\s*:\s*(\{"id":\d+.+?\})\s*[,}]',
            ]
            for pattern in patterns:
                match = re.search(pattern, script, re.DOTALL)
                if match:
                    try:
                        text = match.group(1)
                        # Handle the meta object case
                        if "product" in text and not text.startswith('{"id"'):
                            data = json.loads(text)
                            if "product" in data:
                                return {"product": data["product"]}
                        else:
                            return {"product": json.loads(text)}
                    except json.JSONDecodeError:
                        continue
        return None

    def _extract_meta_tags(self, response: Response) -> dict[str, Any] | None:
        """Extract product data from meta tags."""
        title = (
            response.css('meta[property="og:title"]::attr(content)').get()
            or response.css("title::text").get()
        )
        if not title:
            return None

        price = (
            response.css('meta[property="product:price:amount"]::attr(content)').get()
            or response.css('meta[property="og:price:amount"]::attr(content)').get()
        )
        currency = response.css('meta[property="product:price:currency"]::attr(content)').get()
        og_image = response.css('meta[property="og:image"]::attr(content)').get()

        return {
            "product": {
                "title": title,
                "description": response.css('meta[property="og:description"]::attr(content)').get(),
                "handle": self._extract_handle(response.url),
                "price": price,
                "currency": currency,
                "images": [og_image] if og_image else [],
            }
        }

    def _extract_images_rendered(
        self, response: Response, product_data: dict[str, Any]
    ) -> list[str]:
        """Extract images from rendered page."""
        images = set()

        # From product data
        for img in product_data.get("product", {}).get("images", []):
            if img:
                images.add(img)

        # From page
        for src in response.css('img[src*="cdn.shopify"]::attr(src)').getall():
            images.add(src.split("?")[0])

        return list(images)

    def _extract_handle(self, url: str) -> str:
        """Extract product handle from URL."""
        match = re.search(r"/products/([^/?#]+)", url)
        return match.group(1) if match else ""

    def handle_error(self, failure: Any) -> None:
        """Log errors."""
        self.logger.error(f"Request failed: {failure.request.url} - {failure.value}")
