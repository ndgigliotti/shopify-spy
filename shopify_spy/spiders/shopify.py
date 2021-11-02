# -*- coding: utf-8 -*-
import re
import warnings
import json
import urllib
from distutils.util import strtobool
import scrapy


class ShopifySpider(scrapy.spiders.SitemapSpider):
    r"""Sitemap-based spider for scraping Shopify stores.

    Usage examples:
    scrapy crawl shopify_spider -a url=https://www.example.com/
    scrapy crawl shopify_spider -a url_file=shopify_spy\resources\targets.txt

    If no urls are provided, the spider does nothing.
    """
    name = "shopify_spider"

    def __init__(
        self,
        *args,
        url=None,
        url_file=None,
        products=True,
        collections=False,
        images=True,
        **kwargs
    ):
        """Constructs spider with sitemap_urls determined by url or url_file.

        Keyword arguments:
        url -- complete URL of target Shopify store (default None)
        url_file -- path to text file with one URL per line (default None)
        products -- scrape products (default True)
        collections -- scrape collections (default False)
        images -- scrape images (default True)
        """
        # Determine starting sitemap URLs
        if url:
            self.sitemap_urls = [get_sitemap_url(url)]
        elif url_file:
            with open(url_file) as f:
                self.sitemap_urls = [get_sitemap_url(x) for x in f.readlines()]
        else:
            self.sitemap_urls = []

        

        # Determine what to scrape
        self.sitemap_rules = []
        if strtobool(str(products)):
            self.sitemap_rules.append(("/products/", "parse_product"))
        if strtobool(str(collections)):
            self.sitemap_rules.append(("/collections/", "parse_collection"))
        self.images_enabled = strtobool(str(images))

        super().__init__(*args, **kwargs)

    def sitemap_filter(self, entries):
        """Modifies links to reach data files."""
        for entry in entries:
            if re.search(r"/products/|/collections/", entry["loc"]):
                entry["loc"] = entry["loc"] + ".json"
            yield entry

    def parse_product(self, response):
        """
        Yields product data.

        @url https://www.mollyjogger.com/products/classic-jones-cap.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes product image_urls
        """
        data = extract_data(response)
        image_urls = []

        # Get image urls
        if self.images_enabled:
            for x in data.get("product", {}).get("images", []):
                url = x.get("src")
                if url:
                    image_urls.append(url)

        # Set special field with image_urls
        data["image_urls"] = image_urls
        yield data

    def parse_collection(self, response):
        """
        Yields collection data.

        @url https://www.denydesigns.com/collections/wall.json
        @returns items 1 1
        @returns requests 0 0
        @scrapes collection image_urls
        """
        data = extract_data(response)
        image_urls = []
        if self.images_enabled:
            url = data.get("collection", {}).get("image", {}).get("src")
            if url:
                image_urls.append(url)
        data["image_urls"] = image_urls
        yield data


def extract_data(response):
    """Deserializes JSON and returns item and metadata as dict."""
    data = json.loads(response.text)
    data["url"] = response.request.url
    data["store"] = urllib.parse.urlparse(response.request.url).netloc
    return data


def get_sitemap_url(url):
    """Infers sitemap URL from given URL."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        raise ValueError(f"Scheme not specified in URL: {url}")
    sitemap_url = [parsed.scheme, parsed.netloc, "sitemap.xml", None, None, None]
    return urllib.parse.urlunparse(sitemap_url)