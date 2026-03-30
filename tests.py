import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from typer.testing import CliRunner

from shopify_spy.cli import app, apply_cli_overrides, get_urls, run_spider
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
from shopify_spy.spiders.squarespace import (
    COMMON_COLLECTION_PATHS,
    SquarespaceSpider,
    get_base_url,
)
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
        output=Path("/custom"),
        format=None,
        concurrent=4,
        throttle=False,
        limit=10,
        user_agent="MyBot/1.0",
    )
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


# --- SquarespaceSpider: get_base_url tests ---


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


# --- SquarespaceSpider: __init__ tests ---


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


# --- SquarespaceSpider: start_requests tests ---


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


# --- SquarespaceSpider: parse_collection tests ---


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


# --- SquarespaceSpider: parse_product tests ---


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
