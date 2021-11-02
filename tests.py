import subprocess
from shopify_spy.spiders.shopify import get_sitemap_url

def test_contracts():
    subprocess.run(["scrapy", "check", "shopify_spider"], check=True)

def test_get_sitemap_url():
    inputs = [
        "www.mollyjogger.com",
        "mollyjogger.com",
        "https://mollyjogger.com",
        "https://www.mollyjogger.com",
    ]
    correct = [
        "https://www.mollyjogger.com/sitemap.xml",
        "https://mollyjogger.com/sitemap.xml",
        "https://mollyjogger.com/sitemap.xml",
        "https://www.mollyjogger.com/sitemap.xml",
    ]
    outputs = [get_sitemap_url(x) for x in inputs]
    for out, corr in zip(outputs, correct):
        assert out == corr