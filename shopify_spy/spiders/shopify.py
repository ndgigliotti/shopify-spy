# -*- coding: utf-8 -*-
import json
import re
import urllib

import nested_lookup as nl
import scrapy
from scrapy_playwright.page import PageMethod
from shopify_spy.utils import as_bool

class ShopifySpider(scrapy.spiders.SitemapSpider):
    name = "shopify_spider"

    custom_settings = {
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler": 543,
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",  # Optional: set Playwright browser type (default is "chromium")
    }

    def __init__(
        self,
        *args,
        url=None,
        url_file=None,
        products=True,
        collections=False,
        images=True,
        **kwargs,
    ):
        if url:
            self.sitemap_urls = [get_sitemap_url(url)]
        elif url_file:
            with open(url_file) as f:
                self.sitemap_urls = [get_sitemap_url(x) for x in f.readlines()]
        else:
            self.sitemap_urls = []

        self.sitemap_rules = []
        if as_bool(products):
            self.sitemap_rules.append(("/products/", "parse_product"))
        if as_bool(collections):
            self.sitemap_rules.append(("/collections/", "parse_collection"))
        self.images_enabled = as_bool(images)

        super().__init__(*args, **kwargs)

    def sitemap_filter(self, entries):
        for entry in entries:
            if re.search(r"/products/|/collections/", entry["loc"]):
                entry["loc"] = entry["loc"] + ".json"
            yield entry

    async def parse_product(self, response):
        page = await response.follow(response.url, PageMethod.BROWSER)
        rendered_body = await page.content()
        await page.close()

        data = extract_data_from_rendered_body(rendered_body)
        if self.images_enabled:
            image_urls = nl.nested_lookup("src", data)
        else:
            image_urls = []

        data["image_urls"] = image_urls
        yield data

    async def parse_collection(self, response):
        page = await response.follow(response.url, PageMethod.BROWSER)
        rendered_body = await page.content()
        await page.close()

        data = extract_data_from_rendered_body(rendered_body)
        if self.images_enabled:
            image_urls = nl.nested_lookup("src", data)
        else:
            image_urls = []

        data["image_urls"] = image_urls
        yield data


def extract_data_from_rendered_body(rendered_body):
    """Deserializes rendered HTML and returns item and metadata as dict."""
    # Process the rendered HTML and extract data
    # This depends on the structure of the HTML and the data you need to extract.
    # For example, you might use BeautifulSoup to parse the HTML.
    # data = ...
    return data


def get_sitemap_url(url):
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        raise ValueError(f"Scheme not specified in URL: {url}")
    sitemap_url = [parsed.scheme, parsed.netloc, "sitemap.xml", None, None, None]
    return urllib.parse.urlunparse(sitemap_url)

