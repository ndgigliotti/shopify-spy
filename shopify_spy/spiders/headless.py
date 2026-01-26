"""Playwright-based spider for headless Shopify stores (Hydrogen, etc).

This spider uses browser rendering to scrape stores that don't expose
the traditional /products/*.json endpoints. It works by:
1. Loading the collections page in a real browser
2. Extracting product links from the rendered HTML
3. Visiting each product page and extracting data from JSON-LD or meta tags

Usage:
    scrapy crawl headless_spider -a url=https://hydrogen-store.com/
"""

import json
import re
import urllib.parse
from collections.abc import Generator
from typing import Any

import scrapy
from scrapy.http import Response
from scrapy_playwright.page import PageMethod


class HeadlessSpider(scrapy.Spider):
    """Browser-based spider for JavaScript-rendered Shopify stores."""

    name = "headless_spider"

    custom_settings = {
        # Playwright configuration
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
        # Be respectful
        "DOWNLOAD_DELAY": 2,
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
    }

    def __init__(
        self,
        *args: Any,
        url: str | None = None,
        url_file: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize spider.

        Args:
            url: Base URL of the Shopify store.
            url_file: Path to file with URLs (one per line).
        """
        super().__init__(*args, **kwargs)

        if url:
            self.store_urls = [url]
        elif url_file:
            with open(url_file) as f:
                self.store_urls = [line.strip() for line in f if line.strip()]
        else:
            self.store_urls = []

    def start_requests(self) -> Generator[scrapy.Request, None, None]:
        """Start by visiting the collections page of each store."""
        for store_url in self.store_urls:
            parsed = urllib.parse.urlparse(store_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            # Try /collections/all first (common pattern)
            yield scrapy.Request(
                url=f"{base_url}/collections/all",
                callback=self.parse_collection,
                errback=self.handle_error,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        # Wait for product grid to load
                        PageMethod("wait_for_load_state", "networkidle"),
                    ],
                    "store": parsed.netloc,
                    "base_url": base_url,
                },
            )

    def parse_collection(
        self, response: Response
    ) -> Generator[scrapy.Request | dict[str, Any], None, None]:
        """Parse collection page and extract product links."""
        store = response.meta["store"]
        base_url = response.meta["base_url"]

        # Find product links - common patterns in Shopify themes
        product_links = set()

        # Pattern 1: Standard /products/ URLs
        for href in response.css('a[href*="/products/"]::attr(href)').getall():
            if "/products/" in href and not href.endswith("/products/"):
                product_links.add(href)

        # Pattern 2: Data attributes that might contain product URLs
        for href in response.css("[data-product-url]::attr(data-product-url)").getall():
            product_links.add(href)

        self.logger.info(f"Found {len(product_links)} product links on {response.url}")

        # Visit each product page
        for href in product_links:
            # Normalize URL
            if href.startswith("/"):
                product_url = base_url + href
            elif not href.startswith("http"):
                product_url = base_url + "/" + href
            else:
                product_url = href

            # Remove query params and fragments for cleaner URLs
            product_url = product_url.split("?")[0].split("#")[0]

            yield scrapy.Request(
                url=product_url,
                callback=self.parse_product,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "networkidle"),
                    ],
                    "store": store,
                },
            )

        # Look for pagination
        next_page = response.css(
            'a[rel="next"]::attr(href), '
            "a.next::attr(href), "
            '[aria-label="Next page"]::attr(href), '
            'a[href*="page="]:last-child::attr(href)'
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
                        PageMethod("wait_for_load_state", "networkidle"),
                    ],
                    "store": store,
                    "base_url": base_url,
                },
            )

    def parse_product(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Extract product data from rendered product page."""
        store = response.meta["store"]

        # Strategy 1: JSON-LD (most reliable)
        product_data = self._extract_jsonld(response)

        # Strategy 2: Meta tags
        if not product_data:
            product_data = self._extract_meta_tags(response)

        # Strategy 3: Shopify's window.__SHOPIFY__ object (if available)
        if not product_data:
            product_data = self._extract_shopify_object(response)

        if product_data:
            product_data["url"] = response.url
            product_data["store"] = store
            product_data["source"] = "playwright"

            # Extract image URLs
            product_data["image_urls"] = self._extract_images(response, product_data)

            yield product_data
        else:
            self.logger.warning(f"Could not extract product data from {response.url}")

    def _extract_jsonld(self, response: Response) -> dict[str, Any] | None:
        """Extract product data from JSON-LD script tags."""
        for script in response.css('script[type="application/ld+json"]::text').getall():
            try:
                data = json.loads(script)

                # Handle both single object and array formats
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") == "Product":
                            return self._normalize_jsonld(item)
                elif data.get("@type") == "Product":
                    return self._normalize_jsonld(data)
            except json.JSONDecodeError:
                continue
        return None

    def _normalize_jsonld(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize JSON-LD product data to our format."""
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
                "availability": offers.get("availability"),
                "sku": data.get("sku"),
                "images": images,
            }
        }

    def _extract_meta_tags(self, response: Response) -> dict[str, Any] | None:
        """Extract product data from Open Graph and other meta tags."""
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

    def _extract_shopify_object(self, response: Response) -> dict[str, Any] | None:
        """Try to extract from Shopify's window object."""
        # Look for product JSON in script tags
        for script in response.css("script::text").getall():
            # Pattern: var meta = {"product": {...}}
            match = re.search(r'var\s+meta\s*=\s*(\{.*?"product".*?\});', script, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    if "product" in data:
                        return {"product": data["product"]}
                except json.JSONDecodeError:
                    continue

            # Pattern: window.ShopifyAnalytics.meta.product
            match = re.search(r'"product"\s*:\s*(\{[^}]+\})', script)
            if match:
                try:
                    return {"product": json.loads(match.group(1))}
                except json.JSONDecodeError:
                    continue

        return None

    def _extract_images(self, response: Response, product_data: dict[str, Any]) -> list[str]:
        """Extract all product images from the page."""
        images = set()

        # From product data
        product = product_data.get("product", {})
        for img in product.get("images", []):
            if img:
                images.add(img)

        # From page - product image galleries
        for src in response.css(
            '[class*="product"] img::attr(src), '
            '[class*="gallery"] img::attr(src), '
            "[data-product-image]::attr(src), "
            'img[srcset*="cdn.shopify"]::attr(src)'
        ).getall():
            if src and "cdn.shopify" in src:
                # Normalize Shopify CDN URLs
                images.add(src.split("?")[0])

        return list(images)

    def _extract_handle(self, url: str) -> str:
        """Extract product handle from URL."""
        match = re.search(r"/products/([^/?#]+)", url)
        return match.group(1) if match else ""

    def handle_error(self, failure: Any) -> None:
        """Log errors."""
        self.logger.error(f"Request failed: {failure.request.url} - {failure.value}")
