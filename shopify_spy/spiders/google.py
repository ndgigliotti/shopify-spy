# -*- coding: utf-8 -*-
import re
import scrapy
import bs4

RE_MYSHOPIFY = re.compile(r"https?://[\w\d\-]+\.myshopify\.com/?")


class GoogleSpider(scrapy.Spider):
    name = 'GoogleSpider'
    allowed_domains = ['google.com']
    custom_settings = {
        "ROBOTSTXT_OBEY": False,
    }

    def __init__(self, query=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_urls = [get_search_url(query)]

    def parse(self, response):
        soup = bs4.BeautifulSoup(response.text, "lxml")
        urls = [x["href"] for x in soup.find_all("a", href=RE_MYSHOPIFY)]
        urls = [RE_MYSHOPIFY.search(x)[0] for x in urls]
        yield from [{"url": x} for x in urls]
        next_ = soup.find("a", attrs={"aria-label": "Next page", "href": True})
        if next_:
            yield scrapy.Request("https://www.google.com" + next_["href"])


def get_search_url(query, site="myshopify.com"):
    terms = "+".join(query.split())
    return "https://www.google.com/search?q={terms}+site:{site}".format(**locals())
