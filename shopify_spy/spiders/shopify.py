# -*- coding: utf-8 -*-
import re
import json
import scrapy
import urllib

PRODUCT_URL = r"/products/"
COLLECTION_URL = r"/collections/"


class ShopifySpider(scrapy.spiders.SitemapSpider):
    """Sitemap-based spider for scraping Shopify stores.

    Usage examples:
    scrapy crawl ShopifySpider -a url=https://www.example.com/
    scrapy crawl ShopifySpider -a url_file=shopify_spy\\resources\\targets.txt

    If no parameters are provided, the spider does nothing.
    """
    name = 'ShopifySpider'
    sitemap_rules = [
        (PRODUCT_URL, "parse"),
        (COLLECTION_URL, "parse")
    ]

    def __init__(self, *args, url=None, url_file=None, **kwargs):
        """Constructs spider with sitemap_urls determined by url or url_file.

        Keyword arguments:
        url -- complete URL of target Shopify store (default None)
        url_file -- path to text file with one URL per line (default None)

        If no parameters are provided, sitemap_urls is left empty.
        """
        super().__init__(*args, **kwargs)
        if url:
            self.sitemap_urls = [get_sitemap_url(url)]
        elif url_file:
            with open(url_file) as f:
                self.sitemap_urls = [get_sitemap_url(x) for x in f.readlines()]
        else:
            self.sitemap_urls = []

    def sitemap_filter(self, entries):
        """Modifies links to reach data files."""
        for entry in entries:
            valid_type = any([
                re.search(PRODUCT_URL, entry["loc"]),
                re.search(COLLECTION_URL, entry["loc"])
            ])
            if valid_type:
                entry["loc"] = entry["loc"] + ".json"
            yield entry

    def parse(self, response):
        """Yields item data plus source URL and store domain."""
        data = json.loads(response.text)
        data["url"] = response.request.url
        data["store"] = urllib.parse.urlparse(response.request.url).netloc
        yield data


def get_sitemap_url(url):
    """Infers sitemap URL from given URL."""
    url = urllib.parse.urlparse(url)
    url = ["https", url.netloc, "/sitemap.xml"] + [None]*3
    return urllib.parse.urlunparse(url)
