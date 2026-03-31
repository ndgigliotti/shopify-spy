import json
from unittest.mock import Mock

from shopify_spy.spiders.squarespace import (
    COMMON_COLLECTION_PATHS,
    SquarespaceSpider,
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
    assert spider._collection_paths == list(COMMON_COLLECTION_PATHS)
    assert spider.images_enabled is True


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
    assert spider._collection_paths == ["store"]


def test_squarespace_spider_init_collection_path_strips_slashes():
    spider = SquarespaceSpider(url="https://example.com", collection_path="/shop/")
    assert spider._collection_paths == ["shop"]


def test_squarespace_spider_init_no_images():
    spider = SquarespaceSpider(url="https://example.com", images=False)
    assert spider.images_enabled is False


def test_squarespace_spider_init_no_url():
    spider = SquarespaceSpider()
    assert spider._store_urls == []


# --- start_requests tests ---


def test_squarespace_start_requests():
    spider = SquarespaceSpider(url="https://example.com", collection_path="shop")
    requests = list(spider.start_requests())
    assert len(requests) == 1
    assert requests[0].url == "https://example.com/shop?format=json"


def test_squarespace_start_requests_default_paths():
    spider = SquarespaceSpider(url="https://example.com")
    requests = list(spider.start_requests())
    urls = [r.url for r in requests]
    assert len(urls) == len(COMMON_COLLECTION_PATHS)
    assert "https://example.com/shop?format=json" in urls
    assert "https://example.com/store?format=json" in urls


def test_squarespace_start_requests_multiple_stores():
    spider = SquarespaceSpider(collection_path="shop")
    spider._store_urls = ["https://store1.com", "https://store2.com"]
    requests = list(spider.start_requests())
    assert len(requests) == 2
    assert requests[0].url == "https://store1.com/shop?format=json"
    assert requests[1].url == "https://store2.com/shop?format=json"


# --- parse_collection tests ---


def test_squarespace_parse_collection():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = json.dumps(
        {
            "collection": {"urlId": "shop"},
            "items": [
                {"fullUrl": "/shop/p/product-one", "title": "Product One"},
                {"fullUrl": "/shop/p/product-two", "title": "Product Two"},
            ],
        }
    )
    mock_response.request.url = "https://example.com/shop?format=json"

    requests = list(spider.parse_collection(mock_response))

    assert len(requests) == 2
    assert requests[0].url == "https://example.com/shop/p/product-one?format=json"
    assert requests[1].url == "https://example.com/shop/p/product-two?format=json"


def test_squarespace_parse_collection_no_items():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = json.dumps({"collection": {"urlId": "shop"}, "items": []})
    mock_response.request.url = "https://example.com/shop?format=json"

    requests = list(spider.parse_collection(mock_response))

    assert requests == []


def test_squarespace_parse_collection_no_items_key():
    """Response without 'items' key (e.g. wrong path returned non-product JSON)."""
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = json.dumps({"page": {"title": "About"}})
    mock_response.request.url = "https://example.com/about?format=json"

    requests = list(spider.parse_collection(mock_response))

    assert requests == []


def test_squarespace_parse_collection_invalid_json():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = "<html><body>Not JSON</body></html>"
    mock_response.request.url = "https://example.com/shop?format=json"

    requests = list(spider.parse_collection(mock_response))

    assert requests == []


def test_squarespace_parse_collection_skips_items_without_full_url():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = json.dumps(
        {
            "collection": {"urlId": "shop"},
            "items": [
                {"fullUrl": "/shop/p/good-product", "title": "Good Product"},
                {"title": "No URL Product"},
            ],
        }
    )
    mock_response.request.url = "https://example.com/shop?format=json"

    requests = list(spider.parse_collection(mock_response))

    assert len(requests) == 1
    assert requests[0].url == "https://example.com/shop/p/good-product?format=json"


# --- parse_product tests ---


def test_squarespace_parse_product():
    spider = SquarespaceSpider(url="https://example.com", images=True)
    mock_response = Mock()
    mock_response.text = json.dumps(
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
        }
    )
    mock_response.request.url = "https://example.com/shop/p/test-bag?format=json"

    results = list(spider.parse_product(mock_response))

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
    mock_response = Mock()
    mock_response.text = json.dumps(
        {
            "item": {
                "title": "Test Bag",
                "assetUrl": "https://images.squarespace-cdn.com/img1.jpg",
            }
        }
    )
    mock_response.request.url = "https://example.com/shop/p/test-bag?format=json"

    results = list(spider.parse_product(mock_response))

    assert results[0]["image_urls"] == []


def test_squarespace_parse_product_no_item_key():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = json.dumps({"collection": {"title": "Shop"}})
    mock_response.request.url = "https://example.com/shop/p/test?format=json"

    results = list(spider.parse_product(mock_response))

    assert results == []


def test_squarespace_parse_product_invalid_json():
    spider = SquarespaceSpider(url="https://example.com")
    mock_response = Mock()
    mock_response.text = "not json"
    mock_response.request.url = "https://example.com/shop/p/test?format=json"

    results = list(spider.parse_product(mock_response))

    assert results == []


def test_squarespace_parse_product_no_asset_url():
    """Items with no assetUrl yield empty image_urls."""
    spider = SquarespaceSpider(url="https://example.com", images=True)
    mock_response = Mock()
    mock_response.text = json.dumps({"item": {"title": "Minimal"}})
    mock_response.request.url = "https://example.com/shop/p/minimal?format=json"

    results = list(spider.parse_product(mock_response))

    assert results[0]["image_urls"] == []
