import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from scrapy.http import TextResponse
from typer.testing import CliRunner

from shopify_spy.cli import app, apply_cli_overrides, get_urls
from shopify_spy.config import (
    OUTPUT_FORMATS,
    Config,
    OutputConfig,
    create_default_config,
    load_config,
    load_config_from_file,
)
from shopify_spy.spiders.headless import HeadlessSpider
from shopify_spy.spiders.shopify import ShopifySpider, extract_data, get_sitemap_url
from shopify_spy.utils import as_bool, find_all_values, uri_params

runner = CliRunner()

# Pattern to strip ANSI escape codes from Rich output
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


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


# --- as_bool tests ---


def test_as_bool():
    pos_inputs = ["y", "yes", "t", "T", "TRue", "on", "ON", "1", True, 1]
    neg_inputs = ["n", "NO", "f", "false", "OfF", "0", "null", "na", "nan", False, 0, None]

    for value in pos_inputs:
        assert as_bool(value) is True

    for value in neg_inputs:
        assert as_bool(value) is False


def test_as_bool_invalid():
    with pytest.raises(ValueError, match="Could not interpret"):
        as_bool("maybe")

    with pytest.raises(ValueError, match="Could not interpret"):
        as_bool("invalid")


# --- uri_params tests ---


def test_uri_params():
    mock_spider = Mock()
    mock_spider.name = "test_spider"

    params = {"key": "value", "time": "2026-01-01"}
    result = uri_params(params, mock_spider)

    assert result == {"key": "value", "time": "2026-01-01", "spider_name": "test_spider"}


def test_find_all_values():
    nested = {
        "product": {
            "title": "Test",
            "images": [
                {"src": "http://img1.jpg", "alt": "Image 1"},
                {"src": "http://img2.jpg", "alt": "Image 2"},
            ],
            "featured_image": {"src": "http://featured.jpg"},
        }
    }

    result = list(find_all_values("src", nested))
    assert result == ["http://img1.jpg", "http://img2.jpg", "http://featured.jpg"]

    # Empty case
    assert list(find_all_values("nonexistent", nested)) == []

    # Simple dict
    assert list(find_all_values("key", {"key": "value"})) == ["value"]

    # List at root
    assert list(find_all_values("x", [{"x": 1}, {"x": 2}])) == [1, 2]


def test_find_all_values_empty_containers():
    """Test with empty dicts and lists."""
    assert list(find_all_values("key", {})) == []
    assert list(find_all_values("key", [])) == []
    assert list(find_all_values("key", {"a": {}, "b": []})) == []


def test_find_all_values_none_values():
    """Test handling of None values."""
    # None as a value should be returned
    assert list(find_all_values("key", {"key": None})) == [None]
    # None in a list should be skipped (not iterable)
    assert list(find_all_values("key", [None, {"key": "found"}])) == ["found"]


def test_find_all_values_nested_lists():
    """Test deeply nested list structures."""
    data = [[{"x": 1}], [{"x": 2}, {"x": 3}]]
    assert list(find_all_values("x", data)) == [1, 2, 3]


def test_find_all_values_value_is_container():
    """Test when the value itself is a dict or list."""
    data = {"items": [1, 2, 3], "meta": {"nested": "value"}}
    assert list(find_all_values("items", data)) == [[1, 2, 3]]
    assert list(find_all_values("meta", data)) == [{"nested": "value"}]


def test_find_all_values_duplicate_keys():
    """Test finding same key at multiple nesting levels."""
    data = {
        "id": "outer",
        "child": {
            "id": "inner",
            "grandchild": {"id": "deepest"},
        },
    }
    assert list(find_all_values("id", data)) == ["outer", "inner", "deepest"]


def test_find_all_values_primitives_at_root():
    """Test that primitives at root return empty (not iterable)."""
    assert list(find_all_values("key", "string")) == []
    assert list(find_all_values("key", 123)) == []
    assert list(find_all_values("key", None)) == []
    assert list(find_all_values("key", True)) == []


def test_find_all_values_mixed_types():
    """Test with various value types."""
    data = {
        "string": "text",
        "number": 42,
        "float": 3.14,
        "bool": True,
        "null": None,
    }
    assert list(find_all_values("string", data)) == ["text"]
    assert list(find_all_values("number", data)) == [42]
    assert list(find_all_values("float", data)) == [3.14]
    assert list(find_all_values("bool", data)) == [True]
    assert list(find_all_values("null", data)) == [None]


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


# --- CLI tests ---


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Scrape product and collection data" in strip_ansi(result.stdout)


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "shopify-spy" in strip_ansi(result.stdout)


def test_cli_scrape_help():
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    output = strip_ansi(result.stdout)
    assert "--products" in output
    assert "--no-products" in output
    assert "--url-file" in output


def test_cli_init_help():
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--force" in strip_ansi(result.stdout)


def test_cli_init_creates_file(tmp_path):
    config_file = tmp_path / "test-config.yaml"
    result = runner.invoke(app, ["init", str(config_file)])
    assert result.exit_code == 0
    assert config_file.exists()
    assert "scrape:" in config_file.read_text()


def test_cli_init_refuses_overwrite(tmp_path):
    config_file = tmp_path / "test-config.yaml"
    config_file.write_text("existing content")
    result = runner.invoke(app, ["init", str(config_file)])
    assert result.exit_code == 1
    assert "already exists" in strip_ansi(result.stdout)


def test_cli_init_force_overwrite(tmp_path):
    config_file = tmp_path / "test-config.yaml"
    config_file.write_text("existing content")
    result = runner.invoke(app, ["init", "--force", str(config_file)])
    assert result.exit_code == 0
    assert "scrape:" in config_file.read_text()


def test_cli_scrape_no_url():
    """Test that scrape fails when no URL provided (non-interactive)."""
    result = runner.invoke(app, ["scrape"])
    assert result.exit_code == 1
    assert "No URLs provided" in strip_ansi(result.stdout)


# --- Config loading tests ---


def test_config_defaults():
    config = Config()
    assert config.scrape.products is True
    assert config.scrape.collections is False
    assert config.scrape.images is False
    assert config.output.dir == Path("./output")
    assert config.network.concurrent_requests == 16
    assert config.throttle.enabled is True
    assert config.throttle.start_delay == 1.0


def test_config_images_dir():
    config = Config()
    assert config.output.images_dir == Path("./output/images")


def test_load_config_from_file(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
scrape:
  products: false
  collections: true
  images: false
output:
  dir: ./custom-output
network:
  concurrent_requests: 8
""")
    config = load_config_from_file(config_file)
    assert config.scrape.products is False
    assert config.scrape.collections is True
    assert config.scrape.images is False
    assert config.output.dir == Path("./custom-output")
    assert config.network.concurrent_requests == 8


def test_load_config_empty_file(tmp_path):
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("")
    config = load_config_from_file(config_file)
    assert config.scrape.products is True  # default


def test_load_config_with_explicit_path(tmp_path):
    config_file = tmp_path / "my-config.yaml"
    config_file.write_text("scrape:\n  products: false")
    config = load_config(config_file)
    assert config.scrape.products is False


def test_load_config_no_file():
    config = load_config(Path("/nonexistent/path.yaml"))
    assert config.scrape.products is True  # default


def test_create_default_config(tmp_path):
    config_file = tmp_path / "new-config.yaml"
    created = create_default_config(config_file)
    assert created.exists()
    content = created.read_text()
    assert "scrape:" in content
    assert "output:" in content
    assert "network:" in content


# --- CLI helper function tests ---


def test_apply_cli_overrides():
    config = Config()
    overridden = apply_cli_overrides(
        config,
        products=False,
        collections=True,
        images=None,  # should not override
        headless=True,
        output=Path("/custom"),
        format=None,
        concurrent=4,
        throttle=False,
        user_agent="MyBot/1.0",
    )
    assert overridden.scrape.products is False
    assert overridden.scrape.collections is True
    assert overridden.scrape.images is False  # unchanged (default)
    assert overridden.scrape.headless is True
    assert overridden.output.dir == Path("/custom")
    assert overridden.network.concurrent_requests == 4
    assert overridden.network.user_agent == "MyBot/1.0"
    assert overridden.throttle.enabled is False


def test_apply_cli_overrides_none_values():
    """Test that None values don't override config."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        products=None,
        collections=None,
        images=None,
        headless=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        user_agent=None,
    )
    assert overridden.scrape.products is True
    assert overridden.scrape.collections is False
    assert overridden.scrape.headless is False  # unchanged (default)
    assert overridden.output.dir == Path("./output")
    assert overridden.throttle.enabled is True  # default is now True
    assert overridden.network.user_agent is None  # uses Scrapy default


def test_get_urls_single_url():
    urls = get_urls(["https://example.com"], None)
    assert urls == ["https://example.com"]


def test_get_urls_multiple_urls():
    urls = get_urls(["https://store1.com", "https://store2.com"], None)
    assert urls == ["https://store1.com", "https://store2.com"]


def test_get_urls_from_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://store1.com\n\nhttps://store2.com\n")
    urls = get_urls(None, url_file)
    assert urls == ["https://store1.com", "https://store2.com"]


def test_get_urls_empty():
    """Test that empty input returns empty list (non-interactive)."""
    urls = get_urls(None, None)
    assert urls == []


# --- Output format tests ---


def test_output_config_default_format():
    """Default output format is jsonl."""
    config = OutputConfig()
    assert config.format == "jsonl"


def test_output_config_valid_formats():
    """All four format values are accepted."""
    for fmt in ("json", "jsonl", "csv", "xml"):
        config = OutputConfig(format=fmt)
        assert config.format == fmt


def test_output_config_invalid_format():
    """Invalid format values are rejected."""
    with pytest.raises(Exception):
        OutputConfig(format="parquet")


def test_load_config_with_format(tmp_path):
    """YAML with explicit format loads correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("output:\n  format: csv\n")
    config = load_config_from_file(config_file)
    assert config.output.format == "csv"


def test_load_config_without_format(tmp_path):
    """Missing format in YAML defaults to jsonl."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("output:\n  dir: ./data\n")
    config = load_config_from_file(config_file)
    assert config.output.format == "jsonl"


def test_apply_cli_overrides_format():
    """CLI format override is applied."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        products=None,
        collections=None,
        images=None,
        headless=None,
        output=None,
        format="csv",
        concurrent=None,
        throttle=None,
        user_agent=None,
    )
    assert overridden.output.format == "csv"


def test_apply_cli_overrides_format_none():
    """format=None preserves config default."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        products=None,
        collections=None,
        images=None,
        headless=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        user_agent=None,
    )
    assert overridden.output.format == "jsonl"


def test_output_formats_mapping():
    """OUTPUT_FORMATS maps format names to (scrapy_format, file_ext) tuples."""
    assert OUTPUT_FORMATS["json"] == ("json", ".json")
    assert OUTPUT_FORMATS["jsonl"] == ("jsonlines", ".jsonl")
    assert OUTPUT_FORMATS["csv"] == ("csv", ".csv")
    assert OUTPUT_FORMATS["xml"] == ("xml", ".xml")


def test_cli_scrape_help_shows_format():
    """--format flag appears in scrape help."""
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    output = strip_ansi(result.stdout)
    assert "--format" in output
    assert "-F" in output


def test_default_config_includes_format(tmp_path):
    """init output contains format: jsonl."""
    config_file = tmp_path / "test-config.yaml"
    runner.invoke(app, ["init", str(config_file)])
    content = config_file.read_text()
    assert "format: jsonl" in content


# --- HeadlessSpider unit tests ---


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


# --- CLI headless tests ---


def test_cli_scrape_help_shows_headless():
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    assert "--headless" in strip_ansi(result.stdout)


@patch("shopify_spy.cli.run_spider")
def test_cli_headless_flag(mock_run_spider):
    """--headless sets headless=True in config passed to run_spider."""
    result = runner.invoke(app, ["scrape", "https://example.com", "--headless"])
    assert result.exit_code == 0
    config = mock_run_spider.call_args[0][1]
    assert config.scrape.headless is True


@patch("shopify_spy.cli.run_spider")
def test_cli_headless_collections_warning(mock_run_spider):
    """--headless with --collections prints a warning explaining why."""
    result = runner.invoke(app, ["scrape", "https://example.com", "--headless", "--collections"])
    assert result.exit_code == 0
    output = strip_ansi(result.stdout).lower()
    assert "collections" in output
    assert "not supported" in output


def test_cli_headless_no_products_error():
    """--headless with --no-products exits with an error since nothing can be scraped."""
    result = runner.invoke(app, ["scrape", "https://example.com", "--headless", "--no-products"])
    assert result.exit_code == 1
    output = strip_ansi(result.stdout).lower()
    assert "nothing to scrape" in output


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
