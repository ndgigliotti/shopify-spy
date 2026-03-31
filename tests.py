import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import scrapy
from typer.testing import CliRunner

from shopify_spy.cli import Platform, app, apply_cli_overrides, get_urls, run_spider
from shopify_spy.config import (
    OUTPUT_FORMATS,
    Config,
    OutputConfig,
    ScrapeConfig,
    create_default_config,
    load_config,
    load_config_from_file,
)
from shopify_spy.spiders.shopify import ShopifySpider, extract_data, get_sitemap_url
from shopify_spy.spiders.woocommerce import WooCommerceSpider, get_api_url, next_page_url
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


@pytest.mark.integration
def test_woocommerce_contracts():
    """Integration test that hits real WooCommerce endpoints via Scrapy contracts."""
    subprocess.run([sys.executable, "-m", "scrapy", "check", "woocommerce_spider"], check=True)


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
    assert "--platform" in output


def test_cli_scrape_help_platform_values():
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    output = strip_ansi(result.stdout)
    assert "shopify" in output
    assert "woocommerce" in output


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
    assert config.scrape.platform == "shopify"
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
  platform: woocommerce
  products: false
  collections: true
  images: false
output:
  dir: ./custom-output
network:
  concurrent_requests: 8
""")
    config = load_config_from_file(config_file)
    assert config.scrape.platform == "woocommerce"
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
        platform=Platform.woocommerce,
        products=False,
        collections=True,
        images=None,  # should not override
        output=Path("/custom"),
        format=None,
        concurrent=4,
        throttle=False,
        limit=10,
        user_agent="MyBot/1.0",
    )
    assert overridden.scrape.platform == "woocommerce"
    assert overridden.scrape.products is False
    assert overridden.scrape.collections is True
    assert overridden.scrape.images is False  # unchanged (default)
    assert overridden.scrape.limit == 10
    assert overridden.output.dir == Path("/custom")
    assert overridden.network.concurrent_requests == 4
    assert overridden.network.user_agent == "MyBot/1.0"
    assert overridden.throttle.enabled is False


def test_apply_cli_overrides_none_values():
    """Test that None values don't override config."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=None,
        products=None,
        collections=None,
        images=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        limit=None,
        user_agent=None,
    )
    assert overridden.scrape.platform == "shopify"  # default
    assert overridden.scrape.products is True
    assert overridden.scrape.collections is False
    assert overridden.scrape.limit is None
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
    """All format values are accepted."""
    for fmt in ("json", "jsonl", "csv", "xml", "tsv", "sqlite", "parquet"):
        config = OutputConfig(format=fmt)
        assert config.format == fmt


def test_output_config_invalid_format():
    """Invalid format values are rejected."""
    with pytest.raises(Exception):
        OutputConfig(format="msgpack")


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
        platform=None,
        products=None,
        collections=None,
        images=None,
        output=None,
        format="csv",
        concurrent=None,
        throttle=None,
        limit=None,
        user_agent=None,
    )
    assert overridden.output.format == "csv"


def test_apply_cli_overrides_format_none():
    """format=None preserves config default."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=None,
        products=None,
        collections=None,
        images=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        limit=None,
        user_agent=None,
    )
    assert overridden.output.format == "jsonl"


def test_output_formats_mapping():
    """OUTPUT_FORMATS maps format names to (scrapy_format, file_ext) tuples."""
    assert OUTPUT_FORMATS["json"] == ("json", ".json")
    assert OUTPUT_FORMATS["jsonl"] == ("jsonlines", ".jsonl")
    assert OUTPUT_FORMATS["csv"] == ("csv", ".csv")
    assert OUTPUT_FORMATS["xml"] == ("xml", ".xml")
    assert OUTPUT_FORMATS["tsv"] == ("tsv", ".tsv")
    assert OUTPUT_FORMATS["sqlite"] == ("sqlite", ".db")
    assert OUTPUT_FORMATS["parquet"] == ("parquet", ".parquet")


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


# --- Limit tests ---


def test_scrape_config_limit_default():
    """Default limit is None (no limit)."""
    config = Config()
    assert config.scrape.limit is None


def test_scrape_config_limit_valid():
    """Positive integer limit is accepted."""
    from shopify_spy.config import ScrapeConfig

    assert ScrapeConfig(limit=1).limit == 1
    assert ScrapeConfig(limit=100).limit == 100


def test_scrape_config_limit_invalid():
    """Zero or negative limit is rejected."""
    from pydantic import ValidationError

    from shopify_spy.config import ScrapeConfig

    with pytest.raises(ValidationError):
        ScrapeConfig(limit=0)
    with pytest.raises(ValidationError):
        ScrapeConfig(limit=-5)


def test_load_config_with_limit(tmp_path):
    """YAML with limit loads correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scrape:\n  limit: 25\n")
    config = load_config_from_file(config_file)
    assert config.scrape.limit == 25


def test_apply_cli_overrides_limit():
    """--limit CLI value is applied."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=None,
        products=None,
        collections=None,
        images=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        limit=5,
        user_agent=None,
    )
    assert overridden.scrape.limit == 5


def test_cli_scrape_help_shows_limit():
    """--limit flag appears in scrape help."""
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    assert "--limit" in strip_ansi(result.stdout)


def _make_response(i: int) -> Mock:
    mock = Mock()
    mock.text = f'{{"product": {{"title": "Product {i}"}}}}'
    mock.request.url = f"https://www.example.com/products/p{i}.json"
    return mock


def test_spider_limit_exact_count():
    """Spider yields exactly N items when limit is set."""
    from scrapy.exceptions import CloseSpider

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


def test_run_spider_passes_limit_to_crawl(tmp_path):
    """run_spider passes limit to the spider via process.crawl()."""
    config = Config(scrape=ScrapeConfig(limit=5), output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_process_cls,
    ):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        run_spider(["https://example.com"], config)

    _, kwargs = mock_process.crawl.call_args
    assert kwargs["limit"] == 5


# --- WooCommerce get_api_url tests ---


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


# --- WooCommerce next_page_url tests ---


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


# --- WooCommerceSpider limit tests ---


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


def test_run_spider_passes_limit_to_woocommerce(tmp_path):
    """run_spider passes limit to WooCommerce spider via process.crawl()."""
    config = Config(
        scrape=ScrapeConfig(platform="woocommerce", limit=10),
        output=OutputConfig(dir=tmp_path),
    )

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_process_cls,
    ):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        run_spider(["https://store.com"], config)

    _, kwargs = mock_process.crawl.call_args
    assert kwargs["limit"] == 10


def test_woocommerce_parse_product_no_images_field():
    spider = WooCommerceSpider(url="https://store.com", images=True)

    mock_response = Mock()
    mock_response.text = json.dumps([{"id": 2, "name": "No-image product", "images": []}])
    mock_response.request.url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"

    results = list(spider.parse(mock_response))
    items = [r for r in results if isinstance(r, dict)]
    assert items[0]["image_urls"] == []


# --- TSV exporter tests ---


def test_tsv_exporter_tab_separated(tmp_path):
    """TsvItemExporter writes tab-separated output with a header row."""
    from shopify_spy.exporters import TsvItemExporter

    output_file = tmp_path / "test.tsv"
    with open(output_file, "wb") as f:
        exporter = TsvItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"url": "https://example.com", "store": "example.com"})
        exporter.finish_exporting()

    content = output_file.read_text()
    lines = content.strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == "url\tstore"
    assert lines[1] == "https://example.com\texample.com"


def test_tsv_exporter_multiple_items(tmp_path):
    """TsvItemExporter handles multiple items."""
    from shopify_spy.exporters import TsvItemExporter

    output_file = tmp_path / "test.tsv"
    with open(output_file, "wb") as f:
        exporter = TsvItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"a": "1", "b": "2"})
        exporter.export_item({"a": "3", "b": "4"})
        exporter.finish_exporting()

    content = output_file.read_text()
    lines = content.strip().splitlines()
    assert len(lines) == 3
    assert lines[1] == "1\t2"
    assert lines[2] == "3\t4"


# --- SQLite exporter tests ---


def test_sqlite_exporter_creates_table(tmp_path):
    """SqliteItemExporter creates an items table with correct columns."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"url": "https://example.com", "store": "example.com"})
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    cursor = conn.execute("SELECT * FROM items")
    cols = [desc[0] for desc in cursor.description]
    conn.close()

    assert cols == ["url", "store"]


def test_sqlite_exporter_inserts_items(tmp_path):
    """SqliteItemExporter inserts multiple rows."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"id": 1, "name": "Widget"})
        exporter.export_item({"id": 2, "name": "Gadget"})
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    rows = conn.execute("SELECT * FROM items").fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0] == (1, "Widget")
    assert rows[1] == (2, "Gadget")


def test_sqlite_exporter_json_serializes_nested(tmp_path):
    """SqliteItemExporter JSON-serializes dict and list values."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item(
            {
                "url": "https://example.com",
                "product": {"title": "Test", "price": 100},
                "tags": ["sale", "new"],
            }
        )
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    rows = conn.execute("SELECT * FROM items").fetchall()
    cols = [desc[0] for desc in conn.execute("SELECT * FROM items").description]
    conn.close()

    assert len(rows) == 1
    product_idx = cols.index("product")
    tags_idx = cols.index("tags")
    assert json.loads(rows[0][product_idx]) == {"title": "Test", "price": 100}
    assert json.loads(rows[0][tags_idx]) == ["sale", "new"]


def test_sqlite_exporter_empty(tmp_path):
    """SqliteItemExporter creates a valid database even with no items."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()

    assert tables == []


# --- Parquet exporter tests ---


def test_parquet_exporter_writes_table(tmp_path):
    """ParquetItemExporter writes a readable Parquet file."""
    pq = pytest.importorskip("pyarrow.parquet")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"url": "https://example.com", "store": "example.com"})
        exporter.export_item({"url": "https://other.com", "store": "other.com"})
        exporter.finish_exporting()

    table = pq.read_table(str(output_file))
    assert table.num_rows == 2
    assert table.column("url")[0].as_py() == "https://example.com"
    assert table.column("store")[1].as_py() == "other.com"


def test_parquet_exporter_json_serializes_nested(tmp_path):
    """ParquetItemExporter JSON-serializes dict and list values."""
    pq = pytest.importorskip("pyarrow.parquet")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.export_item(
            {
                "url": "https://example.com",
                "product": {"title": "Test"},
                "tags": ["a", "b"],
            }
        )
        exporter.finish_exporting()

    table = pq.read_table(str(output_file))
    assert table.num_rows == 1
    assert json.loads(table.column("product")[0].as_py()) == {"title": "Test"}
    assert json.loads(table.column("tags")[0].as_py()) == ["a", "b"]


def test_parquet_exporter_empty(tmp_path):
    """ParquetItemExporter handles no items gracefully."""
    pytest.importorskip("pyarrow")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.finish_exporting()

    # File should be empty (no data written)
    assert output_file.stat().st_size == 0
