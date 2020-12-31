# -*- coding: utf-8 -*-
import re
import json
import scrapy
import urllib

PRODUCT_URL = r"/products/"


class ShopifySpider(scrapy.spiders.SitemapSpider):
    name = 'ShopifySpider'
    # allowed_domains = ["shopify.com"]
    sitemap_rules = [(PRODUCT_URL, "parse_product")]

    def __init__(self, *args, url=None, url_file=None, **kwargs):
        super().__init__(*args, **kwargs)
        if url:
            self.sitemap_urls = [get_sitemap_url(url)]
        elif url_file:
            with open(url_file) as f:
                self.sitemap_urls = [get_sitemap_url(x) for x in f.readlines()]
        else:
            self.sitemap_urls = []

    def sitemap_filter(self, entries):
        for entry in entries:
            if re.search(PRODUCT_URL, entry["loc"]):
                entry["loc"] = entry["loc"] + ".json"
            yield entry

    def parse_product(self, response):
        product = json.loads(response.text)
        product["url"] = response.request.url
        product["store"] = urllib.parse.urlparse(response.request.url).netloc
        yield product


def get_sitemap_url(url):
    url = urllib.parse.urlparse(url)
    url = ["https", url.netloc, "/sitemap.xml"] + [None]*3
    return urllib.parse.urlunparse(url)
