import random
import subprocess

from shopify_spy.spiders.shopify import get_sitemap_url
from shopify_spy.utils import as_bool


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


def test_as_bool():
    pos_inputs = [
        "y",
        "yes",
        "t",
        "T",
        "TRue",
        "on",
        "ON",
        "1",
        True,
        1,
    ]
    neg_inputs = [
        "n",
        "NO",
        "f",
        "false",
        "OfF",
        "0",
        "null",
        "na",
        "nan",
        False,
        0,
        None,
    ]
    pos_correct = [True] * len(pos_inputs)
    neg_correct = [False] * len(neg_inputs)
    queue = list(zip(pos_inputs, pos_correct)) + list(zip(neg_inputs, neg_correct))
    random.shuffle(queue)
    for input, answer in queue:
        assert as_bool(input) is answer
