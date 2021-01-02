# -*- coding: utf-8 -*-
import re
import json
import urllib
from distutils.util import strtobool
import scrapy


class ShopifySpider(scrapy.spiders.SitemapSpider):
    r"""Sitemap-based spider for scraping Shopify stores.

    Usage examples:
    scrapy crawl ShopifySpider -a url=https://www.example.com/
    scrapy crawl ShopifySpider -a url_file=shopify_spy\resources\targets.txt

    If no urls are provided, the spider does nothing.
    """
    name = "ShopifySpider"
    custom_settings = {
        "ITEM_PIPELINES": {"scrapy.pipelines.images.ImagesPipeline": 1}
    }

    def __init__(self, *args, url=None, url_file=None, products=True,
                 collections=False, images=True, **kwargs):
        """Constructs spider with sitemap_urls determined by url or url_file.

        Keyword arguments:
        url -- complete URL of target Shopify store (default None)
        url_file -- path to text file with one URL per line (default None)
        products -- scrape products (default True)
        collections -- scrape collections (default False)
        images -- scrape images (default True)

        If no urls are provided, sitemap_urls is left empty.
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
        """Yields product data plus image_urls if appropriate."""
        data = extract_data(response)
        if self.images_enabled:
            image_urls = [x.get("src") for x in
                          data.get("product", {}).get("images", [])]
            image_urls = list(filter(None, image_urls))
            if image_urls:
                data["image_urls"] = image_urls
        yield data

    def parse_collection(self, response):
        """Yields collection data plus image_urls if appropriate."""
        data = extract_data(response)
        if self.images_enabled:
            image = data.get("collection", {}).get("image")
            if image and image.get("src"):
                data["image_urls"] = [image["src"]]
        yield data


def extract_data(response):
    """Returns item data plus source URL and store domain."""
    data = json.loads(response.text)
    data["url"] = response.request.url
    data["store"] = urllib.parse.urlparse(response.request.url).netloc
    return data


def get_sitemap_url(url):
    """Infers sitemap URL from given URL."""
    url = urllib.parse.urlparse(url)
    url = ["https", url.netloc, "/sitemap.xml"] + [None]*3
    return urllib.parse.urlunparse(url)
