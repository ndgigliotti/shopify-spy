import json
from unittest.mock import Mock

import pytest
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse, TextResponse

from shopify_spy.spiders.squarespace import (
    COMMON_COLLECTION_PATHS,
    SquarespaceSpider,
    _filter_image_urls,
    get_base_url,
)

# --- get_base_url tests ---


def test_get_base_url():
    inputs = [
        "https://www.example.com",
        "https://www.example.com/",
        "https://www.example.com/shop",
        "https://www.example.com/shop/p/some-product",
    ]
    expected = ["https://www.example.com"] * 4

    for url, answer in zip(inputs, expected):
        assert get_base_url(url) == answer


def test_get_base_url_no_scheme():
    assert get_base_url("example.squarespace.com") == "https://example.squarespace.com"
    assert get_base_url("www.example.com") == "https://www.example.com"


# --- SquarespaceSpider __init__ tests ---


def test_squarespace_spider_init_url():
    spider = SquarespaceSpider(url="https://example.squarespace.com")
    assert spider._store_urls == ["https://example.squarespace.com"]
    assert spider._collection_path is None
    assert spider.images_enabled is True
    assert spider.limit is None
    assert spider._item_count == 0


def test_squarespace_spider_init_url_no_scheme():
    spider = SquarespaceSpider(url="example.squarespace.com")
    assert spider._store_urls == ["https://example.squarespace.com"]


def test_squarespace_spider_init_url_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://store1.squarespace.com\n\nhttps://store2.squarespace.com\n")
    spider = SquarespaceSpider(url_file=str(url_file))
    assert spider._store_urls == [
        "https://store1.squarespace.com",
        "https://store2.squarespace.com",
    ]


def test_squarespace_spider_init_collection_path():
    spider = SquarespaceSpider(url="https://example.com", collection_path="store")
    assert spider._collection_path == "store"


def test_squarespace_spider_init_collection_path_strips_slashes():
    spider = SquarespaceSpider(url="https://example.com", collection_path="/shop/")
    assert spider._collection_path == "shop"


def test_squarespace_spider_init_no_images():
    spider = SquarespaceSpider(url="https://example.com", images=False)
    assert spider.images_enabled is False


def test_squarespace_spider_init_no_url():
    spider = SquarespaceSpider()
    assert spider._store_urls == []


def test_squarespace_spider_init_limit():
    spider = SquarespaceSpider(url="https://example.com", limit=10)
    assert spider.limit == 10


def test_squarespace_spider_init_limit_string():
    spider = SquarespaceSpider(url="https://example.com", limit="5")
    assert spider.limit == 5


# --- start tests ---


def _collect_start(spider):
    import asyncio

    async def _inner():
        return [r async for r in spider.start()]

    return asyncio.run(_inner())


def test_squarespace_start_with_collection_path():
    spider = SquarespaceSpider(url="https://example.com", collection_path="shop")
    requests = _collect_start(spider)
    assert len(requests) == 1
    assert requests[0].url == "https://example.com/shop?format=json"
    assert requests[0].callback == spider.parse_collection


def test_squarespace_start_auto_discovery():
    spider = SquarespaceSpider(url="https://example.com")
    requests = _collect_start(spider)
    assert len(requests) == 1
    assert requests[0].url == "https://example.com"
    assert requests[0].callback == spider.discover_collections


def test_squarespace_start_multiple_stores():
    spider = SquarespaceSpider(collection_path="shop")
    spider._store_urls = ["https://store1.com", "https://store2.com"]
    requests = _collect_start(spider)
    assert len(requests) == 2
    assert requests[0].url == "https://store1.com/shop?format=json"
    assert requests[1].url == "https://store2.com/shop?format=json"


# --- discover_collections tests ---


def _make_html_response(url, body):
    return HtmlResponse(url=url, body=body.encode(), request=Mock(url=url))


def test_discover_collections_from_nav():
    spider = SquarespaceSpider(url="https://example.com")
    html = """
    <html><body>
    <nav data-content-field="navigation">
      <a href="/shop-brine">Shop</a>
      <a href="/blog-brine">Blog</a>
      <a href="/about">About</a>
    </nav>
    </body></html>
    """
    response = _make_html_response("https://example.com", html)
    requests = list(spider.discover_collections(response))

    assert len(requests) == 3
    assert requests[0].url == "https://example.com/shop-brine?format=json"
    assert requests[1].url == "https://example.com/blog-brine?format=json"
    assert requests[2].url == "https://example.com/about?format=json"


def test_discover_collections_deduplicates():
    spider = SquarespaceSpider(url="https://example.com")
    html = """
    <html><body>
    <nav data-content-field="navigation">
      <a href="/shop">Shop</a>
      <a href="/shop">Shop Again</a>
    </nav>
    </body></html>
    """
    response = _make_html_response("https://example.com", html)
    requests = list(spider.discover_collections(response))

    assert len(requests) == 1


def test_discover_collections_skips_external_and_anchors():
    spider = SquarespaceSpider(url="https://example.com")
    html = """
    <html><body>
    <nav data-content-field="navigation">
      <a href="https://other.com/page">External</a>
      <a href="#section">Anchor</a>
      <a href="mailto:a@b.com">Email</a>
      <a href="/shop">Shop</a>
    </nav>
    </body></html>
    """
    response = _make_html_response("https://example.com", html)
    requests = list(spider.discover_collections(response))

    assert len(requests) == 1
    assert requests[0].url == "https://example.com/shop?format=json"


def test_discover_collections_falls_back_to_common_paths():
    spider = SquarespaceSpider(url="https://example.com")
    html = "<html><body><p>No navigation here</p></body></html>"
    response = _make_html_response("https://example.com", html)
    requests = list(spider.discover_collections(response))

    urls = [r.url for r in requests]
    assert len(urls) == len(COMMON_COLLECTION_PATHS)
    assert "https://example.com/shop?format=json" in urls
    assert "https://example.com/store?format=json" in urls


# --- parse_collection tests ---


def _make_json_response(url, data):
    body = json.dumps(data).encode()
    return TextResponse(url=url, body=body, request=Mock(url=url))


def test_squarespace_parse_collection():
    spider = SquarespaceSpider(url="https://example.com")
    response = _make_json_response(
        "https://example.com/shop?format=json",
        {
            "collection": {"urlId": "shop"},
            "items": [
                {"fullUrl": "/shop/p/product-one", "title": "Product One"},
                {"fullUrl": "/shop/p/product-two", "title": "Product Two"},
            ],
        },
    )
    requests = list(spider.parse_collection(response))

    assert len(requests) == 2
    assert requests[0].url == "https://example.com/shop/p/product-one?format=json"
    assert requests[1].url == "https://example.com/shop/p/product-two?format=json"


def test_squarespace_parse_collection_no_items():
    spider = SquarespaceSpider(url="https://example.com")
    response = _make_json_response(
        "https://example.com/shop?format=json",
        {"collection": {"urlId": "shop"}, "items": []},
    )
    requests = list(spider.parse_collection(response))

    assert requests == []


def test_squarespace_parse_collection_no_items_key():
    """Response without 'items' key (e.g. wrong path returned non-product JSON)."""
    spider = SquarespaceSpider(url="https://example.com")
    response = _make_json_response(
        "https://example.com/about?format=json",
        {"page": {"title": "About"}},
    )
    requests = list(spider.parse_collection(response))

    assert requests == []


def test_squarespace_parse_collection_invalid_json():
    spider = SquarespaceSpider(url="https://example.com")
    response = HtmlResponse(
        url="https://example.com/shop?format=json",
        body=b"<html><body>Not JSON</body></html>",
        request=Mock(url="https://example.com/shop?format=json"),
    )
    requests = list(spider.parse_collection(response))

    assert requests == []


def test_squarespace_parse_collection_skips_items_without_full_url():
    spider = SquarespaceSpider(url="https://example.com")
    response = _make_json_response(
        "https://example.com/shop?format=json",
        {
            "collection": {"urlId": "shop"},
            "items": [
                {"fullUrl": "/shop/p/good-product", "title": "Good Product"},
                {"title": "No URL Product"},
            ],
        },
    )
    requests = list(spider.parse_collection(response))

    assert len(requests) == 1
    assert requests[0].url == "https://example.com/shop/p/good-product?format=json"


# --- parse_product tests ---


def test_squarespace_parse_product():
    spider = SquarespaceSpider(url="https://example.com", images=True)
    response = _make_json_response(
        "https://example.com/shop/p/test-bag?format=json",
        {
            "item": {
                "title": "Test Bag",
                "priceCents": 15600,
                "assetUrl": "https://images.squarespace-cdn.com/featured.jpg",
                "items": [
                    {"assetUrl": "https://images.squarespace-cdn.com/gallery1.jpg"},
                    {"assetUrl": "https://images.squarespace-cdn.com/gallery2.jpg"},
                ],
            }
        },
    )
    results = list(spider.parse_product(response))

    assert len(results) == 1
    item = results[0]
    assert item["item"]["title"] == "Test Bag"
    assert item["url"] == "https://example.com/shop/p/test-bag?format=json"
    assert item["store"] == "example.com"
    assert "https://images.squarespace-cdn.com/featured.jpg" in item["image_urls"]
    assert "https://images.squarespace-cdn.com/gallery1.jpg" in item["image_urls"]
    assert "https://images.squarespace-cdn.com/gallery2.jpg" in item["image_urls"]


def test_squarespace_parse_product_no_images():
    spider = SquarespaceSpider(url="https://example.com", images=False)
    response = _make_json_response(
        "https://example.com/shop/p/test-bag?format=json",
        {
            "item": {
                "title": "Test Bag",
                "assetUrl": "https://images.squarespace-cdn.com/img1.jpg",
            }
        },
    )
    results = list(spider.parse_product(response))

    assert results[0]["image_urls"] == []


def test_squarespace_parse_product_no_item_key():
    spider = SquarespaceSpider(url="https://example.com")
    response = _make_json_response(
        "https://example.com/shop/p/test?format=json",
        {"collection": {"title": "Shop"}},
    )
    results = list(spider.parse_product(response))

    assert results == []


def test_squarespace_parse_product_invalid_json():
    spider = SquarespaceSpider(url="https://example.com")
    response = HtmlResponse(
        url="https://example.com/shop/p/test?format=json",
        body=b"not json",
        request=Mock(url="https://example.com/shop/p/test?format=json"),
    )
    results = list(spider.parse_product(response))

    assert results == []


def test_squarespace_parse_product_no_asset_url():
    """Items with no assetUrl yield empty image_urls."""
    spider = SquarespaceSpider(url="https://example.com", images=True)
    response = _make_json_response(
        "https://example.com/shop/p/minimal?format=json",
        {"item": {"title": "Minimal"}},
    )
    results = list(spider.parse_product(response))

    assert results[0]["image_urls"] == []


# --- limit tests ---


def test_squarespace_limit_exact_count():
    spider = SquarespaceSpider(url="https://example.com", limit=2)

    r1 = _make_json_response(
        "https://example.com/shop/p/p1?format=json",
        {"item": {"title": "Product 1"}},
    )
    r2 = _make_json_response(
        "https://example.com/shop/p/p2?format=json",
        {"item": {"title": "Product 2"}},
    )
    r3 = _make_json_response(
        "https://example.com/shop/p/p3?format=json",
        {"item": {"title": "Product 3"}},
    )

    assert len(list(spider.parse_product(r1))) == 1
    assert len(list(spider.parse_product(r2))) == 1
    with pytest.raises(CloseSpider):
        list(spider.parse_product(r3))


def test_squarespace_no_limit():
    spider = SquarespaceSpider(url="https://example.com", limit=None)
    for i in range(20):
        r = _make_json_response(
            f"https://example.com/shop/p/p{i}?format=json",
            {"item": {"title": f"Product {i}"}},
        )
        assert len(list(spider.parse_product(r))) == 1


# --- _filter_image_urls tests ---


def test_filter_image_urls_keeps_good_urls():
    urls = [
        "https://images.squarespace-cdn.com/content/v1/abc/img.jpg",
        "https://images.squarespace-cdn.com/content/v1/abc/img.png",
    ]
    assert _filter_image_urls(iter(urls)) == urls


def test_filter_image_urls_removes_trailing_slash():
    urls = [
        "https://static1.squarespace.com/static/abc/def/ghi/123456/",
        "https://images.squarespace-cdn.com/good.jpg",
    ]
    result = _filter_image_urls(iter(urls))
    assert result == ["https://images.squarespace-cdn.com/good.jpg"]


def test_filter_image_urls_removes_non_strings():
    urls = [None, 123, "https://images.squarespace-cdn.com/good.jpg"]
    result = _filter_image_urls(iter(urls))
    assert result == ["https://images.squarespace-cdn.com/good.jpg"]


def test_filter_image_urls_empty():
    assert _filter_image_urls(iter([])) == []
