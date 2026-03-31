from pathlib import Path
from unittest.mock import MagicMock, patch

from shopify_spy.cli import Platform, app, apply_cli_overrides, get_urls, run_spider
from shopify_spy.config import Config, OutputConfig, ScrapeConfig

from .conftest import runner, strip_ansi

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


def test_cli_scrape_help_shows_limit():
    """--limit flag appears in scrape help."""
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    assert "--limit" in strip_ansi(result.stdout)


# --- CLI helper function tests ---


def test_apply_cli_overrides():
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=Platform.woocommerce,
        products=False,
        collections=True,
        images=None,  # should not override
        headless=True,
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
        platform=None,
        products=None,
        collections=None,
        images=None,
        headless=None,
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
    assert overridden.scrape.headless is False  # unchanged (default)
    assert overridden.output.dir == Path("./output")
    assert overridden.throttle.enabled is True  # default is now True
    assert overridden.network.user_agent is None  # uses Scrapy default


def test_apply_cli_overrides_format():
    """CLI format override is applied."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=None,
        products=None,
        collections=None,
        images=None,
        headless=None,
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
        headless=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        limit=None,
        user_agent=None,
    )
    assert overridden.output.format == "jsonl"


def test_apply_cli_overrides_limit():
    """--limit CLI value is applied."""
    config = Config()
    overridden = apply_cli_overrides(
        config,
        platform=None,
        products=None,
        collections=None,
        images=None,
        headless=None,
        output=None,
        format=None,
        concurrent=None,
        throttle=None,
        limit=5,
        user_agent=None,
    )
    assert overridden.scrape.limit == 5


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


def test_cli_headless_non_shopify_error():
    """--headless with a non-Shopify platform exits with an error."""
    result = runner.invoke(
        app, ["scrape", "https://example.com", "--headless", "--platform", "woocommerce"]
    )
    assert result.exit_code == 1
    output = strip_ansi(result.stdout).lower()
    assert "only supported" in output
    assert "shopify" in output
