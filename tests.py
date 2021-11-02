import subprocess
from shopify_spy.spiders.shopify import get_sitemap_url


def test_contracts():
    subprocess.run(["scrapy", "check", "shopify_spider"], check=True)


def test_get_sitemap_url():
    inputs = [
        "https://www.example.com",
        "https://www.example.com/",
        "https://www.example.com/products/big_fancy_table"
        "https://www.example.com/products/big_fancy_table/",
    ]

    correct = ["https://www.example.com/sitemap.xml"] * 4

    for input, answer in zip(inputs, correct):
        assert get_sitemap_url(input) == answer
