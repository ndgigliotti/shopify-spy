import pytest
from scrapy.http import TextResponse

from shopify_spy.spiders.headless import HeadlessSpider


def make_response(url: str, html: str) -> TextResponse:
    return TextResponse(url=url, body=html.encode(), encoding="utf-8")


def make_spider() -> HeadlessSpider:
    """Create a HeadlessSpider instance without running __init__."""
    return HeadlessSpider.__new__(HeadlessSpider)


def test_headless_extract_handle():
    spider = make_spider()
    assert spider._extract_handle("https://store.com/products/cool-shirt") == "cool-shirt"
    assert spider._extract_handle("https://store.com/products/tee?variant=1") == "tee"
    assert spider._extract_handle("https://store.com/collections/all") == ""


def test_headless_extract_images_from_json():
    spider = make_spider()
    product = {
        "images": [
            {"src": "https://cdn.shopify.com/img1.jpg"},
            {"src": "https://cdn.shopify.com/img2.jpg"},
        ]
    }
    images = spider._extract_images_from_json(product)
    assert images == ["https://cdn.shopify.com/img1.jpg", "https://cdn.shopify.com/img2.jpg"]


def test_headless_extract_images_from_json_empty():
    spider = make_spider()
    assert spider._extract_images_from_json({}) == []
    assert spider._extract_images_from_json({"images": []}) == []


def test_headless_extract_jsonld():
    spider = make_spider()
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Cool Shirt", "description": "A cool shirt",
     "offers": {"price": "29.99", "priceCurrency": "USD"},
     "brand": {"name": "BrandCo"}}
    </script>
    </head></html>
    """
    response = make_response("https://store.com/products/cool-shirt", html)
    result = spider._extract_jsonld(response)
    assert result is not None
    assert result["product"]["title"] == "Cool Shirt"
    assert result["product"]["vendor"] == "BrandCo"
    assert result["product"]["price"] == "29.99"
    assert result["product"]["currency"] == "USD"


def test_headless_extract_jsonld_no_product():
    spider = make_spider()
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "WebSite", "name": "Store"}'
        "</script></head></html>"
    )
    response = make_response("https://store.com", html)
    assert spider._extract_jsonld(response) is None


def test_headless_extract_jsonld_invalid_json():
    spider = make_spider()
    html = '<html><head><script type="application/ld+json">{invalid json}</script></head></html>'
    response = make_response("https://store.com/products/shirt", html)
    assert spider._extract_jsonld(response) is None


def test_headless_extract_meta_tags():
    spider = make_spider()
    html = """
    <html><head>
    <meta property="og:title" content="Cool Shirt" />
    <meta property="og:description" content="A cool shirt" />
    <meta property="og:image" content="https://cdn.shopify.com/img1.jpg" />
    <meta property="product:price:amount" content="29.99" />
    <meta property="product:price:currency" content="USD" />
    </head></html>
    """
    response = make_response("https://store.com/products/cool-shirt", html)
    result = spider._extract_meta_tags(response)
    assert result is not None
    assert result["product"]["title"] == "Cool Shirt"
    assert result["product"]["price"] == "29.99"
    assert result["product"]["currency"] == "USD"
    assert result["product"]["images"] == ["https://cdn.shopify.com/img1.jpg"]
    assert result["product"]["handle"] == "cool-shirt"


def test_headless_extract_meta_tags_no_title():
    spider = make_spider()
    html = "<html><head></head></html>"
    response = make_response("https://store.com/products/shirt", html)
    assert spider._extract_meta_tags(response) is None


@pytest.mark.asyncio
async def test_headless_spider_products_false():
    """HeadlessSpider with products=False yields no requests."""
    spider = HeadlessSpider(url="https://example.com", products=False)
    requests = [r async for r in spider.start()]
    assert requests == []


@pytest.mark.asyncio
async def test_headless_spider_products_true():
    """HeadlessSpider with products=True (default) yields requests."""
    spider = HeadlessSpider(url="https://example.com", products=True)
    requests = [r async for r in spider.start()]
    assert len(requests) == 1
    assert "products.json" in requests[0].url
