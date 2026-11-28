"""Live checks against real stores. Run with: uv run pytest -m integration"""

import pytest

import shopify_spy as spy

pytestmark = pytest.mark.integration


def test_shopify_store_live():
    item = spy.scrape("https://www.snowdevil.ca", max_items_per_store=1).first()
    assert item["product"]["handle"]
    assert item["store"] == "www.snowdevil.ca"
    assert item["url"].endswith(".json")


def test_shopify_collections_live():
    result = spy.scrape(
        "https://www.snowdevil.ca", products=False, collections=True, max_items_per_store=1
    ).collect()
    (item,) = result.items
    handle = item["collection"]["handle"]
    assert item["url"] == f"https://www.snowdevil.ca/collections/{handle}.json"
    assert item["store"] == "www.snowdevil.ca"
    assert result.stores[0].status == "ok"


def test_woocommerce_store_live():
    item = spy.scrape(
        "https://porterandyork.com", platform="woocommerce", max_items_per_store=1
    ).first()
    assert item["name"]
    assert item["store"] == "porterandyork.com"
    assert spy.summarize(item).currency


def test_magento_store_live():
    item = spy.scrape("https://www.vitamix.com", platform="magento", max_items_per_store=1).first()
    assert item["name"]
    assert item["store"] == "www.vitamix.com"
    assert spy.summarize(item).currency


def test_bigcommerce_store_live():
    store = "https://eshopping-industrial-demo.mybigcommerce.com"
    item = spy.scrape(store, platform="bigcommerce", max_items_per_store=1).first()
    assert item["name"]
    assert item["store"] == "eshopping-industrial-demo.mybigcommerce.com"


def test_generic_store_live():
    # A BigCommerce demo store: its /xmlsitemap.php lists product pages with JSON-LD
    url = "https://cornerstone-light-demo.mybigcommerce.com"
    item = spy.scrape(url, platform="generic", max_items_per_store=1).first()
    assert item["name"]
    assert item["store"] == "cornerstone-light-demo.mybigcommerce.com"
    assert item["html_url"].startswith(f"{url}/")
    assert spy.summarize(item).currency


def test_squarespace_store_live():
    # Squarespace's demo store; robots.txt obeyed, so read from its product pages
    url = "https://hester-demo.squarespace.com"
    item = spy.scrape(url, platform="squarespace", max_items_per_store=1).first()
    assert item["name"]
    assert item["platform"] == "squarespace"
    assert item["source"] == "product_page"
    assert item["store"] == "hester-demo.squarespace.com"
    assert "/p/" in item["html_url"]
