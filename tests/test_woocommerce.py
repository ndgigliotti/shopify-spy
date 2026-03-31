import asyncio
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest
import scrapy

from shopify_spy.spiders.woocommerce import WooCommerceSpider, get_api_url, next_page_url


@pytest.mark.integration
def test_woocommerce_contracts():
    """Integration test that hits real WooCommerce endpoints via Scrapy contracts."""
    subprocess.run([sys.executable, "-m", "scrapy", "check", "woocommerce_spider"], check=True)


# --- get_api_url tests ---


def test_get_api_url_basic():
    assert get_api_url("https://store.com") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


def test_get_api_url_page():
    assert get_api_url("https://store.com", page=3) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=3"
    )


def test_get_api_url_strips_path():
    assert get_api_url("https://store.com/some/path/") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


def test_get_api_url_no_scheme():
    assert get_api_url("store.com") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


# --- next_page_url tests ---


def test_next_page_url():
    url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    assert next_page_url(url) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=2"
    )


def test_next_page_url_increments():
    url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=5"
    assert next_page_url(url) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=6"
    )


# --- WooCommerceSpider __init__ tests ---


def test_woocommerce_spider_init_url():
    spider = WooCommerceSpider(url="https://store.com")
    assert spider._store_urls == ["https://store.com"]
    assert spider.images_enabled is True


def test_woocommerce_spider_init_url_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://store1.com\n\nhttps://store2.com\n")
    spider = WooCommerceSpider(url_file=str(url_file))
    assert spider._store_urls == ["https://store1.com", "https://store2.com"]


def test_woocommerce_spider_init_no_url():
    spider = WooCommerceSpider()
    assert spider._store_urls == []


def test_woocommerce_spider_init_images_false():
    spider = WooCommerceSpider(url="https://store.com", images=False)
    assert spider.images_enabled is False


# --- WooCommerceSpider start tests ---


def _collect_start(spider):
    async def _inner():
        return [r async for r in spider.start()]

    return asyncio.run(_inner())


def test_woocommerce_spider_start():
    spider = WooCommerceSpider(url="https://store.com")
    requests = _collect_start(spider)
    assert len(requests) == 1
    assert requests[0].url == ("https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1")


def test_woocommerce_spider_start_multiple():
    spider = WooCommerceSpider()
    spider._store_urls = ["https://store1.com", "https://store2.com"]
    requests = _collect_start(spider)
    assert len(requests) == 2
    assert "store1.com" in requests[0].url
    assert "store2.com" in requests[1].url


# --- WooCommerceSpider parse tests ---

_PRODUCT_JSON = json.dumps(
    [
        {
            "id": 1,
            "name": "Widget",
            "slug": "widget",
            "permalink": "https://store.com/product/widget/",
            "sku": "WGT-001",
            "prices": {"price": "1000", "currency_code": "USD", "currency_minor_unit": 2},
            "images": [
                {"id": 10, "src": "https://store.com/img/widget.jpg"},
                {"id": 11, "src": "https://store.com/img/widget-2.jpg"},
            ],
            "categories": [],
        }
    ]
)


def test_woocommerce_parse_yields_product():
    spider = WooCommerceSpider(url="https://store.com", images=True)

    mock_response = Mock()
    mock_response.text = _PRODUCT_JSON
    mock_response.request.url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"

    results = list(spider.parse(mock_response))
    items = [r for r in results if isinstance(r, dict)]
    requests = [r for r in results if isinstance(r, scrapy.Request)]

    assert len(items) == 1
    assert items[0]["id"] == 1
    assert items[0]["name"] == "Widget"
    assert items[0]["store"] == "store.com"
    assert items[0]["image_urls"] == [
        "https://store.com/img/widget.jpg",
        "https://store.com/img/widget-2.jpg",
    ]
    assert len(requests) == 1
    assert "page=2" in requests[0].url


def test_woocommerce_parse_no_images():
    spider = WooCommerceSpider(url="https://store.com", images=False)

    mock_response = Mock()
    mock_response.text = _PRODUCT_JSON
    mock_response.request.url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"

    results = list(spider.parse(mock_response))
    items = [r for r in results if isinstance(r, dict)]

    assert items[0]["image_urls"] == []


def test_woocommerce_parse_empty_stops_pagination():
    spider = WooCommerceSpider(url="https://store.com")

    mock_response = Mock()
    mock_response.text = "[]"
    mock_response.request.url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=5"

    results = list(spider.parse(mock_response))
    assert results == []


# --- WooCommerce limit tests ---


_THREE_PRODUCTS_JSON = json.dumps(
    [{"id": i, "name": f"Product {i}", "images": []} for i in range(1, 4)]
)


def _woo_response(text: str, page: int = 1) -> Mock:
    mock = Mock()
    mock.text = text
    mock.request.url = f"https://store.com/wp-json/wc/store/v1/products?per_page=100&page={page}"
    return mock


def test_woocommerce_limit_stops_after_n():
    """WooCommerce spider yields exactly N items when limit is set."""
    from scrapy.exceptions import CloseSpider

    spider = WooCommerceSpider(url="https://store.com", limit=2)

    items = []
    with pytest.raises(CloseSpider):
        for result in spider.parse(_woo_response(_THREE_PRODUCTS_JSON)):
            if isinstance(result, dict):
                items.append(result)

    assert len(items) == 2
    assert spider._item_count == 2


def test_woocommerce_limit_raises_closespider():
    """WooCommerce spider raises CloseSpider when limit is already exhausted."""
    from scrapy.exceptions import CloseSpider

    spider = WooCommerceSpider(url="https://store.com", limit=1)

    items = []
    with pytest.raises(CloseSpider):
        for result in spider.parse(_woo_response(_THREE_PRODUCTS_JSON)):
            if isinstance(result, dict):
                items.append(result)
    assert len(items) == 1

    # Next call should raise immediately
    with pytest.raises(CloseSpider):
        list(spider.parse(_woo_response(_THREE_PRODUCTS_JSON)))


def test_woocommerce_no_limit():
    """WooCommerce spider yields all items when limit is None."""
    spider = WooCommerceSpider(url="https://store.com", limit=None)

    results = list(spider.parse(_woo_response(_THREE_PRODUCTS_JSON)))
    items = [r for r in results if isinstance(r, dict)]

    assert len(items) == 3
    assert spider._item_count == 3


def test_woocommerce_limit_string_param():
    """limit passed as string (e.g. from scrapy CLI) is coerced to int."""
    spider = WooCommerceSpider(url="https://store.com", limit="5")
    assert spider.limit == 5


def test_woocommerce_parse_product_no_images_field():
    spider = WooCommerceSpider(url="https://store.com", images=True)

    mock_response = Mock()
    mock_response.text = json.dumps([{"id": 2, "name": "No-image product", "images": []}])
    mock_response.request.url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"

    results = list(spider.parse(mock_response))
    items = [r for r in results if isinstance(r, dict)]
    assert items[0]["image_urls"] == []
