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
    assert result.items and "collection" in result.items[0]


def test_woocommerce_store_live():
    item = spy.scrape(
        "https://porterandyork.com", platform="woocommerce", max_items_per_store=1
    ).first()
    assert item["name"]
    assert item["store"] == "porterandyork.com"
    assert spy.summarize(item).currency
