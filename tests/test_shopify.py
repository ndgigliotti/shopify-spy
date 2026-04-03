import json
import subprocess
import sys
from unittest.mock import Mock

import pytest
from scrapy.exceptions import CloseSpider

from shopify_spy.spiders.shopify import (
    ShopifySpider,
    extract_data,
    get_bulk_products_url,
    get_sitemap_url,
    next_bulk_page_url,
)


@pytest.mark.integration
def test_contracts():
    """Integration test that hits real Shopify endpoints via Scrapy contracts."""
    subprocess.run([sys.executable, "-m", "scrapy", "check", "shopify_spider"], check=True)


# --- get_sitemap_url tests ---


def test_get_sitemap_url():
    inputs = [
        "https://www.example.com",
        "https://www.example.com/",
        "https://www.example.com/products/big_fancy_table",
        "https://www.example.com/products/big_fancy_table/",
    ]
    expected = ["https://www.example.com/sitemap.xml"] * 4

    for url, answer in zip(inputs, expected):
        assert get_sitemap_url(url) == answer


def test_get_sitemap_url_no_scheme():
    """URLs without scheme should default to https."""
    assert get_sitemap_url("www.example.com") == "https://www.example.com/sitemap.xml"
    assert get_sitemap_url("example.com") == "https://example.com/sitemap.xml"


# --- extract_data tests ---


def test_extract_data():
    mock_response = Mock()
    mock_response.text = '{"product": {"title": "Test Product", "price": 100}}'
    mock_response.request.url = "https://www.example.com/products/test.json"

    result = extract_data(mock_response)

    assert result["product"] == {"title": "Test Product", "price": 100}
    assert result["url"] == "https://www.example.com/products/test.json"
    assert result["store"] == "www.example.com"


# --- sitemap_filter tests ---


def test_sitemap_filter():
    spider = ShopifySpider(url="https://www.example.com")

    entries = [
        {"loc": "https://www.example.com/products/item1"},
        {"loc": "https://www.example.com/collections/sale"},
        {"loc": "https://www.example.com/pages/about"},
    ]

    result = list(spider.sitemap_filter(iter(entries)))

    assert result[0]["loc"] == "https://www.example.com/products/item1.json"
    assert result[1]["loc"] == "https://www.example.com/collections/sale.json"
    assert result[2]["loc"] == "https://www.example.com/pages/about"  # unchanged


# --- spider __init__ tests ---


def test_spider_init_with_url_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://www.store1.com\n\nhttps://www.store2.com\n")

    spider = ShopifySpider(url_file=str(url_file))

    assert spider.sitemap_urls == [
        "https://www.store1.com/sitemap.xml",
        "https://www.store2.com/sitemap.xml",
    ]


def test_spider_init_with_collections():
    spider = ShopifySpider(url="https://www.example.com", products=False, collections=True)

    assert spider.sitemap_rules == [("/collections/", "parse_collection")]
    assert spider.sitemap_urls == ["https://www.example.com/sitemap.xml"]


def test_spider_init_default():
    spider = ShopifySpider(url="https://www.example.com")

    assert spider.sitemap_rules == [("/products/", "parse_product")]
    assert spider.images_enabled is True


def test_spider_init_no_url():
    spider = ShopifySpider()

    assert spider.sitemap_urls == []


# --- parse methods tests (unit tests complement contract integration tests) ---


def test_parse_product():
    spider = ShopifySpider(url="https://www.example.com", images=True)

    mock_response = Mock()
    mock_response.text = '{"product": {"title": "Test", "images": [{"src": "http://img1.jpg"}]}}'
    mock_response.request.url = "https://www.example.com/products/test.json"

    results = list(spider.parse_product(mock_response))

    assert len(results) == 1
    assert results[0]["product"]["title"] == "Test"
    assert results[0]["url"] == "https://www.example.com/products/test.json"
    assert results[0]["store"] == "www.example.com"
    assert results[0]["image_urls"] == ["http://img1.jpg"]


def test_parse_product_no_images():
    spider = ShopifySpider(url="https://www.example.com", images=False)

    mock_response = Mock()
    mock_response.text = '{"product": {"title": "Test", "images": [{"src": "http://img1.jpg"}]}}'
    mock_response.request.url = "https://www.example.com/products/test.json"

    results = list(spider.parse_product(mock_response))

    assert results[0]["image_urls"] == []


def test_parse_collection():
    spider = ShopifySpider(url="https://www.example.com", collections=True, images=True)

    mock_response = Mock()
    mock_response.text = '{"collection": {"title": "Sale", "image": {"src": "http://img.jpg"}}}'
    mock_response.request.url = "https://www.example.com/collections/sale.json"

    results = list(spider.parse_collection(mock_response))

    assert len(results) == 1
    assert results[0]["collection"]["title"] == "Sale"
    assert results[0]["url"] == "https://www.example.com/collections/sale.json"
    assert results[0]["image_urls"] == ["http://img.jpg"]


def test_parse_collection_no_images():
    spider = ShopifySpider(url="https://www.example.com", collections=True, images=False)

    mock_response = Mock()
    mock_response.text = '{"collection": {"title": "Sale", "image": {"src": "http://img.jpg"}}}'
    mock_response.request.url = "https://www.example.com/collections/sale.json"

    results = list(spider.parse_collection(mock_response))

    assert results[0]["image_urls"] == []


# --- Limit tests ---


def _make_response(i: int) -> Mock:
    mock = Mock()
    mock.text = f'{{"product": {{"title": "Product {i}"}}}}'
    mock.request.url = f"https://www.example.com/products/p{i}.json"
    return mock


def test_spider_limit_exact_count():
    """Spider yields exactly N items when limit is set."""
    spider = ShopifySpider(url="https://www.example.com", limit=2)

    assert len(list(spider.parse_product(_make_response(1)))) == 1
    assert len(list(spider.parse_product(_make_response(2)))) == 1
    with pytest.raises(CloseSpider):
        list(spider.parse_product(_make_response(3)))


def test_spider_limit_string_param():
    """limit passed as a string (e.g. from scrapy CLI -a limit=3) is coerced to int."""
    spider = ShopifySpider(url="https://www.example.com", limit="3")
    assert spider.limit == 3


def test_spider_no_limit():
    """Spider yields all items when limit is None."""
    spider = ShopifySpider(url="https://www.example.com", limit=None)

    for i in range(20):
        assert len(list(spider.parse_product(_make_response(i)))) == 1


# --- Bulk URL helper tests ---


def test_get_bulk_products_url():
    assert (
        get_bulk_products_url("https://www.example.com")
        == "https://www.example.com/products.json?limit=250&page=1"
    )


def test_get_bulk_products_url_page():
    assert (
        get_bulk_products_url("https://www.example.com", page=3)
        == "https://www.example.com/products.json?limit=250&page=3"
    )


def test_get_bulk_products_url_no_scheme():
    assert (
        get_bulk_products_url("www.example.com")
        == "https://www.example.com/products.json?limit=250&page=1"
    )


def test_next_bulk_page_url():
    url = "https://www.example.com/products.json?limit=250&page=1"
    assert next_bulk_page_url(url) == "https://www.example.com/products.json?limit=250&page=2"


def test_next_bulk_page_url_preserves_limit():
    url = "https://www.example.com/products.json?limit=250&page=5"
    result = next_bulk_page_url(url)
    assert "limit=250" in result
    assert "page=6" in result


# --- Bulk response helper ---


def _make_bulk_response(products, page=1, store="www.example.com"):
    """Create a mock response for the bulk /products.json endpoint."""
    mock = Mock()
    mock.text = json.dumps({"products": products})
    mock.url = f"https://{store}/products.json?limit=250&page={page}"
    mock.request.url = mock.url
    return mock


# --- parse_products_json tests ---


def test_parse_products_json_yields_items():
    spider = ShopifySpider(url="https://www.example.com")
    products = [
        {"handle": "item-1", "title": "Item 1", "images": [{"src": "http://img1.jpg"}]},
        {"handle": "item-2", "title": "Item 2", "images": []},
    ]
    response = _make_bulk_response(products)

    results = list(
        spider.parse_products_json(response, sitemap_url="https://www.example.com/sitemap.xml")
    )
    items = [r for r in results if isinstance(r, dict)]

    assert len(items) == 2
    assert items[0]["product"]["title"] == "Item 1"
    assert items[0]["url"] == "https://www.example.com/products/item-1.json"
    assert items[0]["store"] == "www.example.com"
    assert items[1]["product"]["handle"] == "item-2"


def test_parse_products_json_images():
    spider = ShopifySpider(url="https://www.example.com", images=True)
    products = [{"handle": "p", "images": [{"src": "http://img.jpg"}]}]
    response = _make_bulk_response(products)

    items = [r for r in spider.parse_products_json(response, "x") if isinstance(r, dict)]
    assert items[0]["image_urls"] == ["http://img.jpg"]


def test_parse_products_json_no_images():
    spider = ShopifySpider(url="https://www.example.com", images=False)
    products = [{"handle": "p", "images": [{"src": "http://img.jpg"}]}]
    response = _make_bulk_response(products)

    items = [r for r in spider.parse_products_json(response, "x") if isinstance(r, dict)]
    assert items[0]["image_urls"] == []


def test_parse_products_json_pagination():
    """250 products triggers a next-page request."""
    spider = ShopifySpider(url="https://www.example.com")
    products = [{"handle": f"p{i}"} for i in range(250)]
    response = _make_bulk_response(products)

    results = list(
        spider.parse_products_json(response, sitemap_url="https://www.example.com/sitemap.xml")
    )
    requests = [r for r in results if not isinstance(r, dict)]

    assert len(requests) == 1
    assert "page=2" in requests[0].url


def test_parse_products_json_no_pagination_partial():
    """Fewer than 250 products means no next-page request."""
    spider = ShopifySpider(url="https://www.example.com")
    products = [{"handle": f"p{i}"} for i in range(10)]
    response = _make_bulk_response(products)

    results = list(
        spider.parse_products_json(response, sitemap_url="https://www.example.com/sitemap.xml")
    )
    requests = [r for r in results if not isinstance(r, dict)]

    assert len(requests) == 0


def test_parse_products_json_collections_sitemap_after_last_page():
    """When collections are enabled, sitemap is requested after the last bulk page."""
    spider = ShopifySpider(url="https://www.example.com", collections=True)
    products = [{"handle": "p1"}]
    response = _make_bulk_response(products)
    sitemap_url = "https://www.example.com/sitemap.xml"

    results = list(spider.parse_products_json(response, sitemap_url=sitemap_url))
    requests = [r for r in results if not isinstance(r, dict)]

    assert len(requests) == 1
    assert requests[0].url == sitemap_url


def test_parse_products_json_json_error_fallback():
    """Non-JSON response triggers sitemap fallback."""
    spider = ShopifySpider(url="https://www.example.com")
    response = Mock()
    response.text = "<html>Not JSON</html>"
    response.url = "https://www.example.com/products.json?limit=250&page=1"
    sitemap_url = "https://www.example.com/sitemap.xml"

    results = list(spider.parse_products_json(response, sitemap_url=sitemap_url))

    assert len(results) == 1
    assert results[0].url == sitemap_url


def test_parse_products_json_empty_fallback():
    """Empty products list triggers sitemap fallback."""
    spider = ShopifySpider(url="https://www.example.com")
    response = Mock()
    response.text = '{"products": []}'
    response.url = "https://www.example.com/products.json?limit=250&page=1"
    sitemap_url = "https://www.example.com/sitemap.xml"

    results = list(spider.parse_products_json(response, sitemap_url=sitemap_url))

    assert len(results) == 1
    assert results[0].url == sitemap_url


def test_parse_products_json_limit():
    """Bulk path respects item limit."""
    spider = ShopifySpider(url="https://www.example.com", limit=2)
    products = [{"handle": f"p{i}"} for i in range(5)]
    response = _make_bulk_response(products)

    results = []
    with pytest.raises(CloseSpider):
        for r in spider.parse_products_json(response, sitemap_url="x"):
            results.append(r)

    items = [r for r in results if isinstance(r, dict)]
    assert len(items) == 2


def test_parse_products_json_marks_store_bulk():
    """Successful bulk marks the store in _bulk_product_stores."""
    spider = ShopifySpider(url="https://www.example.com")
    products = [{"handle": "p1"}]
    response = _make_bulk_response(products)

    list(spider.parse_products_json(response, sitemap_url="x"))

    assert "www.example.com" in spider._bulk_product_stores


# --- _bulk_errback tests ---


def test_bulk_errback_yields_sitemap_request():
    spider = ShopifySpider(url="https://www.example.com")
    sitemap_url = "https://www.example.com/sitemap.xml"

    failure = Mock()
    failure.request.url = "https://www.example.com/products.json?limit=250&page=1"
    failure.request.meta = {"sitemap_url": sitemap_url}

    results = list(spider._bulk_errback(failure))

    assert len(results) == 1
    assert results[0].url == sitemap_url


# --- start() tests ---


async def _collect_start(spider):
    """Collect all requests from the async start() generator."""
    results = []
    async for req in spider.start():
        results.append(req)
    return results


async def test_start_products_only():
    """Products enabled: yields bulk request, not sitemap."""
    spider = ShopifySpider(url="https://www.example.com", products=True, collections=False)
    requests = await _collect_start(spider)

    assert len(requests) == 1
    assert "/products.json" in requests[0].url


async def test_start_collections_only():
    """Collections only: yields sitemap request directly."""
    spider = ShopifySpider(url="https://www.example.com", products=False, collections=True)
    requests = await _collect_start(spider)

    assert len(requests) == 1
    assert "sitemap.xml" in requests[0].url


async def test_start_products_and_collections():
    """Both enabled: yields only bulk request (sitemap deferred to after bulk)."""
    spider = ShopifySpider(url="https://www.example.com", products=True, collections=True)
    requests = await _collect_start(spider)

    assert len(requests) == 1
    assert "/products.json" in requests[0].url


# --- sitemap_filter with bulk stores ---


def test_sitemap_filter_skips_bulk_products():
    """Product entries for bulk-succeeded stores are skipped."""
    spider = ShopifySpider(url="https://www.example.com")
    spider._bulk_product_stores.add("www.example.com")

    entries = [
        {"loc": "https://www.example.com/products/item1"},
        {"loc": "https://www.example.com/collections/sale"},
        {"loc": "https://www.example.com/pages/about"},
    ]

    result = list(spider.sitemap_filter(iter(entries)))

    locs = [e["loc"] for e in result]
    assert "https://www.example.com/products/item1" not in locs
    assert "https://www.example.com/products/item1.json" not in locs
    assert "https://www.example.com/collections/sale.json" in locs
    assert "https://www.example.com/pages/about" in locs


def test_sitemap_filter_passes_products_for_non_bulk_store():
    """Product entries for stores without bulk data pass through."""
    spider = ShopifySpider(url="https://www.example.com")
    # _bulk_product_stores is empty

    entries = [{"loc": "https://www.example.com/products/item1"}]
    result = list(spider.sitemap_filter(iter(entries)))

    assert result[0]["loc"] == "https://www.example.com/products/item1.json"
