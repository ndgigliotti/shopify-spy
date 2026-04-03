import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from shopify_spy.cli import (
    Platform,
    _diagnose_crawler,
    _finish_reason,
    app,
    apply_cli_overrides,
    get_urls,
    run_spider,
)
from shopify_spy.config import Config, NetworkConfig, OutputConfig, ScrapeConfig

from .conftest import runner, strip_ansi


def _patch_log_dir(tmp_path):
    """Return a patch context manager that redirects _log_dir to tmp_path/logs."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    return patch("shopify_spy.cli._log_dir", return_value=log_dir)


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


def test_cli_scrape_warns_collections_woocommerce():
    """--collections with --platform woocommerce warns about unsupported flag."""
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce", "--collections"])
    output = strip_ansi(result.stdout)
    assert "--collections has no effect with WooCommerce" in output


def test_cli_scrape_warns_no_products_woocommerce():
    """--no-products with --platform woocommerce warns about unsupported flag."""
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce", "--no-products"])
    output = strip_ansi(result.stdout)
    assert "--no-products has no effect with WooCommerce" in output


def test_cli_scrape_no_warn_shopify_collections():
    """--collections with shopify (default) should not produce a warning."""
    result = runner.invoke(app, ["scrape", "--collections"])
    output = strip_ansi(result.stdout)
    assert "--collections has no effect" not in output


def test_cli_scrape_no_warn_woocommerce_without_flags():
    """--platform woocommerce without --collections/--no-products should not warn."""
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce"])
    output = strip_ansi(result.stdout)
    assert "has no effect" not in output


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


def _mock_crawler_process(item_count=1):
    """Create a mock CrawlerProcess whose crawlers report item_count items."""
    mock_process = MagicMock()
    mock_crawler = MagicMock()
    mock_crawler.stats.get_value.side_effect = lambda key, default=0: (
        item_count if key == "item_scraped_count" else default
    )
    mock_process.crawlers = [mock_crawler]
    return mock_process


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
        _patch_log_dir(tmp_path),
    ):
        mock_process = _mock_crawler_process()
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
        _patch_log_dir(tmp_path),
    ):
        mock_process = _mock_crawler_process()
        mock_process_cls.return_value = mock_process
        run_spider(["https://example.com"], config)

    _, kwargs = mock_process.crawl.call_args
    assert kwargs["limit"] == 5


def _make_crawler(stats_dict):
    """Create a mock crawler with get_value backed by a dict."""
    c = MagicMock()
    c.stats.get_value.side_effect = lambda key, default=0: stats_dict.get(key, default)
    return c


def _mock_multi_crawler_process(stats_list):
    """Create a mock CrawlerProcess with one crawler per entry in stats_list."""
    proc = MagicMock()
    proc.crawlers = [_make_crawler(s) for s in stats_list]
    return proc


# --- Per-URL breakdown tests (multi-URL) ---


def test_multi_url_success_shows_per_url_counts(tmp_path, capsys):
    """Success path prints per-URL item counts when multiple URLs are given."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [
        {"item_scraped_count": 10},
        {"item_scraped_count": 5},
    ]
    urls = ["https://store1.com", "https://store2.com"]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        run_spider(urls, config)

    output = capsys.readouterr().out
    assert "Done!" in output
    assert "store1.com: 10 items" in output
    assert "store2.com: 5 items" in output


def test_single_url_success_no_per_url_breakdown(tmp_path, capsys):
    """Single-URL success should NOT print a per-URL line."""
    config = Config(output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process([{"item_scraped_count": 7}])
        run_spider(["https://only-one.com"], config)

    output = capsys.readouterr().out
    assert "Done!" in output
    assert "only-one.com" not in output


def test_multi_url_failure_shows_per_url_status(tmp_path, capsys):
    """Failure path prints per-URL diagnostics when multiple URLs are given."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [
        {"downloader/response_status_count/403": 1, "downloader/response_count": 1},
        {"downloader/response_status_count/404": 1, "downloader/response_count": 1},
    ]
    urls = ["https://blocked.com", "https://missing.com"]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        with pytest.raises((SystemExit, typer.Exit)):
            run_spider(urls, config)

    output = capsys.readouterr().out
    assert "blocked.com: 403 Forbidden" in output
    assert "missing.com: 404 Not Found" in output


def test_single_url_failure_no_per_url_breakdown(tmp_path, capsys):
    """Single-URL failure should NOT print a per-URL line."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [{"downloader/response_status_count/403": 1, "downloader/response_count": 1}]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        with pytest.raises((SystemExit, typer.Exit)):
            run_spider(["https://blocked.com"], config)

    output = capsys.readouterr().out
    assert "blocked.com: 403 Forbidden" not in output
    # Aggregate message should still appear
    assert "403" in output


# --- _diagnose_crawler unit tests ---


def test_diagnose_crawler_items():
    c = _make_crawler({"item_scraped_count": 5})
    assert _diagnose_crawler(c, Config()) == "5 items"


def test_diagnose_crawler_403():
    c = _make_crawler({"downloader/response_status_count/403": 2, "downloader/response_count": 2})
    assert _diagnose_crawler(c, Config()) == "403 Forbidden"


def test_diagnose_crawler_404():
    c = _make_crawler({"downloader/response_status_count/404": 1, "downloader/response_count": 1})
    assert _diagnose_crawler(c, Config()) == "404 Not Found"


def test_diagnose_crawler_timed_out():
    c = _make_crawler({"finish_reason": "bail"})
    assert _diagnose_crawler(c, Config()) == "timed out"


def test_diagnose_crawler_robots():
    c = _make_crawler({"downloader/response_count": 1, "robotstxt/response_count": 1})
    config = Config(network=NetworkConfig(respect_robots_txt=True))
    assert _diagnose_crawler(c, config) == "blocked by robots.txt"


def test_diagnose_crawler_no_response():
    c = _make_crawler({})
    assert _diagnose_crawler(c, Config()) == "no response"


def test_diagnose_crawler_zero_items():
    c = _make_crawler({"downloader/response_count": 5})
    assert _diagnose_crawler(c, Config()) == "0 items"


# --- Status file tests ---


def test_status_file_written_on_success(tmp_path):
    """run_spider writes a _status.json file alongside output."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [{"item_scraped_count": 12, "finish_reason": "finished"}]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        run_spider(["https://store.com"], config)

    status_files = list(tmp_path.glob("*_status.json"))
    assert len(status_files) == 1
    data = json.loads(status_files[0].read_text())
    assert data["items_scraped"] == 12
    assert data["finish_reason"] == "finished"
    assert data["duration_seconds"] >= 0
    assert data["log_file"] is not None
    assert len(data["urls"]) == 1
    assert data["urls"][0]["url"] == "https://store.com"
    assert data["urls"][0]["items"] == 12
    assert data["urls"][0]["status"] == "ok"


def test_status_file_written_on_failure(tmp_path):
    """Status file is written even when scraping fails (0 items)."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [{"downloader/response_status_count/403": 1, "downloader/response_count": 1}]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        with pytest.raises((SystemExit, typer.Exit)):
            run_spider(["https://blocked.com"], config)

    status_files = list(tmp_path.glob("*_status.json"))
    assert len(status_files) == 1
    data = json.loads(status_files[0].read_text())
    assert data["items_scraped"] == 0
    assert data["urls"][0]["status"] == "403 Forbidden"


def test_status_file_multi_url(tmp_path):
    """Status file includes per-URL breakdown for multiple URLs."""
    config = Config(output=OutputConfig(dir=tmp_path))
    stats = [
        {"item_scraped_count": 10, "finish_reason": "finished"},
        {"downloader/response_status_count/403": 1, "downloader/response_count": 1},
    ]
    urls = ["https://ok.com", "https://blocked.com"]

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process(stats)
        run_spider(urls, config)

    status_files = list(tmp_path.glob("*_status.json"))
    data = json.loads(status_files[0].read_text())
    assert data["items_scraped"] == 10
    assert data["urls"][0]["status"] == "ok"
    assert data["urls"][1]["status"] == "403 Forbidden"


def test_no_status_file_in_peek_mode(tmp_path):
    """Peek mode does not write a status file."""
    config = Config(output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process([{"item_scraped_count": 1}])
        run_spider(["https://store.com"], config, peek=True, quiet=True)

    status_files = list(tmp_path.glob("*_status.json"))
    assert len(status_files) == 0


# --- Log file tests ---


def test_log_file_setting_configured(tmp_path):
    """run_spider sets LOG_FILE in Scrapy settings."""
    config = Config(output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process([{"item_scraped_count": 1}])
        run_spider(["https://store.com"], config)

    log_file_calls = [call for call in mock_settings.set.call_args_list if call[0][0] == "LOG_FILE"]
    assert len(log_file_calls) == 1
    log_path = log_file_calls[0][0][1]
    assert log_path.endswith(".log")
    assert "shopify_spider" in log_path


def test_no_log_file_in_peek_mode(tmp_path):
    """Peek mode does not set LOG_FILE."""
    config = Config(output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process([{"item_scraped_count": 1}])
        run_spider(["https://store.com"], config, peek=True, quiet=True)

    log_file_calls = [call for call in mock_settings.set.call_args_list if call[0][0] == "LOG_FILE"]
    assert len(log_file_calls) == 0


def test_status_file_log_path_matches(tmp_path):
    """The log_file field in the status JSON matches the LOG_FILE setting."""
    config = Config(output=OutputConfig(dir=tmp_path))

    mock_settings = MagicMock()
    with (
        patch("scrapy.utils.project.get_project_settings", return_value=mock_settings),
        patch("scrapy.crawler.CrawlerProcess") as mock_cls,
        _patch_log_dir(tmp_path),
    ):
        mock_cls.return_value = _mock_multi_crawler_process([{"item_scraped_count": 1}])
        run_spider(["https://store.com"], config)

    # Get LOG_FILE from settings
    log_file_calls = [call for call in mock_settings.set.call_args_list if call[0][0] == "LOG_FILE"]
    log_path = log_file_calls[0][0][1]

    # Get log_file from status JSON
    status_files = list(tmp_path.glob("*_status.json"))
    data = json.loads(status_files[0].read_text())
    assert data["log_file"] == log_path


# --- _finish_reason unit tests ---


def test_finish_reason_normal():
    crawlers = [_make_crawler({"finish_reason": "finished"})]
    assert _finish_reason(crawlers, Config()) == "finished"


def test_finish_reason_bail():
    crawlers = [_make_crawler({"finish_reason": "no_item_timeout"})]
    assert _finish_reason(crawlers, Config()) == "bail"


def test_finish_reason_item_limit():
    config = Config(scrape=ScrapeConfig(limit=5))
    crawlers = [_make_crawler({"finish_reason": "finished", "item_scraped_count": 5})]
    assert _finish_reason(crawlers, config) == "item_limit"


def test_finish_reason_bail_takes_priority():
    """bail takes priority even when some crawlers finished normally."""
    crawlers = [
        _make_crawler({"finish_reason": "finished", "item_scraped_count": 3}),
        _make_crawler({"finish_reason": "no_item_timeout"}),
    ]
    assert _finish_reason(crawlers, Config()) == "bail"
