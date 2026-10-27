"""Tests for the CLI (shopify_spy.cli), ported from the Scrapy-era test_cli.py.

Those tests mocked Scrapy's CrawlerProcess and faked its stats. These drive real
scrapes through FakeFetcher routes, so every diagnosis comes from the library's own
accounting. Plan references (§, D) are to .context/httpx-library-rewrite.md.
"""

import asyncio
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from rich.console import Console

import shopify_spy.config as config_module
import shopify_spy.runner as engine
from shopify_spy import cli as cli_module
from shopify_spy.cli import app, apply_cli_overrides, get_urls
from shopify_spy.config import Config, Platform
from shopify_spy.fetch import FakeFetcher, FetchConnectionError, FetchTLSError, Seq
from shopify_spy.results import StoreResult, StoreStatus, run_finish_reason
from shopify_spy.utils import is_valid_url

from .conftest import runner, strip_ansi

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


# --- Route builders ---


def products_url(host: str, page: int = 1) -> str:
    return f"https://{host}/products.json?limit=250&page={page}"


def sitemap_url(host: str) -> str:
    return f"https://{host}/sitemap.xml"


def wc_url(host: str, page: int = 1) -> str:
    return f"https://{host}/wp-json/wc/store/v1/products?per_page=100&page={page}"


def bulk(n: int, start: int = 0) -> dict:
    return {
        "products": [
            {"id": i, "handle": f"p{i}", "title": f"Product {i}"} for i in range(start, start + n)
        ]
    }


def urlset(*locs: str) -> tuple:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
        + "</urlset>"
    )
    return (200, body, {"content-type": "application/xml"})


async def hang(request):
    await asyncio.sleep(60)


def shop(host: str, n: int) -> dict:
    """A Shopify store whose bulk endpoint returns n products on one page."""
    return {products_url(host): bulk(n)}


def forbidden(host: str) -> dict:
    """A store that answers 403 to everything it is asked."""
    return {products_url(host): (403, ""), sitemap_url(host): (403, "")}


def unreachable(host: str, exc: Exception | None = None) -> dict:
    """A store whose connections fail (robots.txt included)."""
    exc = exc or FetchConnectionError("connection refused")
    return {
        f"https://{host}/robots.txt": exc,
        products_url(host): exc,
        sitemap_url(host): exc,
    }


def broken(host: str) -> dict:
    """A store whose request handling raises an unexpected exception."""

    def boom(request):
        raise RuntimeError("boom")

    return {products_url(host): boom}


def empty(host: str) -> dict:
    """A store that answers but has nothing to scrape."""
    return {products_url(host): {"products": []}, sitemap_url(host): urlset()}


# --- Harness ---


@dataclass
class Run:
    result: object
    out: Path
    log_dir: Path
    fetchers: list = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return self.result.exit_code

    @property
    def stdout(self) -> str:
        return strip_ansi(self.result.stdout)

    @property
    def stderr(self) -> str:
        return strip_ansi(self.result.stderr)

    @property
    def requests(self) -> list[str]:
        return [r.url for f in self.fetchers for r in f.requests]

    def status(self) -> dict:
        files = list(self.out.glob("status/*_status.json"))
        assert len(files) == 1, files
        return json.loads(files[0].read_text())

    def data_files(self) -> list[Path]:
        return list(self.out.glob("data/*"))

    def items(self) -> list[dict]:
        (path,) = self.data_files()
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # No ambient config files
    monkeypatch.setattr(config_module, "CONFIG_SEARCH_PATHS", [])
    # Wide consoles so hints aren't wrapped mid-phrase
    monkeypatch.setattr(cli_module, "console", Console(width=300))
    monkeypatch.setattr(cli_module, "err_console", Console(stderr=True, width=300))
    # No retry backoff sleeps (D5); retry counts are unchanged
    original = engine.retry_policy_for
    monkeypatch.setattr(
        engine,
        "retry_policy_for",
        lambda caps, retries, backoff: original(caps, retries, False),
    )


@pytest.fixture
def cli(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(cli_module, "_log_dir", lambda: log_dir)
    out = tmp_path / "out"

    def invoke(routes=None, *args, output=True, throttle=False):
        fetchers = []

        def factory():
            fetcher = FakeFetcher(routes or {})
            fetchers.append(fetcher)
            return fetcher

        monkeypatch.setattr(cli_module, "FETCHER_FACTORY", factory)
        argv = ["scrape", *([] if throttle else ["--no-throttle"]), *args]
        if output:
            argv += ["-o", str(out)]
        result = runner.invoke(app, argv)
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            raise result.exception
        return Run(result, out, log_dir, fetchers)

    return invoke


def merge(*route_maps: dict) -> dict:
    merged = {}
    for routes in route_maps:
        merged.update(routes)
    return merged


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


def test_cli_scrape_help_new_flags():
    """New flags --insecure (D14) and --max-stores (D3) are listed."""
    result = runner.invoke(app, ["scrape", "--help"])
    output = strip_ansi(result.stdout)
    assert "--insecure" in output
    assert "--max-stores" in output


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
    assert "No URLs provided" in strip_ansi(result.stderr)


def test_cli_scrape_warns_collections_woocommerce():
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce", "--collections"])
    assert "--collections has no effect with WooCommerce" in strip_ansi(result.stderr)


def test_cli_scrape_warns_no_products_woocommerce():
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce", "--no-products"])
    assert "--no-products has no effect with WooCommerce" in strip_ansi(result.stderr)


def test_cli_scrape_no_warn_shopify_collections():
    result = runner.invoke(app, ["scrape", "--collections"])
    assert "--collections has no effect" not in strip_ansi(result.stderr)


def test_cli_scrape_no_warn_woocommerce_without_flags():
    result = runner.invoke(app, ["scrape", "--platform", "woocommerce"])
    assert "has no effect" not in strip_ansi(result.stderr)


def test_cli_scrape_help_shows_format():
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    output = strip_ansi(result.stdout)
    assert "--format" in output
    assert "-F" in output


def test_default_config_includes_format(tmp_path):
    config_file = tmp_path / "test-config.yaml"
    runner.invoke(app, ["init", str(config_file)])
    assert "format: jsonl" in config_file.read_text()


def test_cli_scrape_help_shows_limit():
    """--limit is listed, and its help says it is per store (§4.11)."""
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    output = " ".join(strip_ansi(result.stdout).split())
    assert "--limit" in output
    assert "per store" in output


# --- CLI helper function tests ---


def _overrides(config=None, **kwargs):
    base = dict(
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
    base.update(kwargs)
    return apply_cli_overrides(config or Config(), **base)


def test_apply_cli_overrides():
    overridden = _overrides(
        platform=Platform.woocommerce,
        products=False,
        collections=True,
        images=None,  # should not override
        output=Path("/custom"),
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
    overridden = _overrides()
    assert overridden.scrape.platform == "shopify"  # default
    assert overridden.scrape.products is True
    assert overridden.scrape.collections is False
    assert overridden.scrape.limit is None
    assert overridden.output.dir == Path("./output")
    assert overridden.throttle.enabled is True
    assert overridden.network.user_agent is None  # random browser UA


def test_apply_cli_overrides_format():
    assert _overrides(format="csv").output.format == "csv"


def test_apply_cli_overrides_format_none():
    assert _overrides(format=None).output.format == "jsonl"


def test_apply_cli_overrides_limit():
    assert _overrides(limit=5).scrape.limit == 5


def test_apply_cli_overrides_platform_none_keeps_config():
    """§3.10: --platform defaults to None, so the config file's platform wins."""
    config = Config(scrape={"platform": "woocommerce"})
    assert _overrides(config).scrape.platform == "woocommerce"
    assert _overrides(config, platform=Platform.shopify).scrape.platform == "shopify"


def test_insecure_and_max_stores_map_to_kwargs():
    """--insecure (D14) and --max-stores (D3) reach the library options."""
    kwargs = _overrides(insecure=True, max_stores=3).to_kwargs()
    assert kwargs["verify_tls"] is False
    assert kwargs["max_concurrent_stores"] == 3

    defaults = _overrides().to_kwargs()
    assert defaults["verify_tls"] is True
    assert defaults["max_concurrent_stores"] == 16


def test_overrides_map_to_kwargs():
    kwargs = _overrides(
        limit=7, throttle=False, ignore_robots=True, bail=0, concurrent=2
    ).to_kwargs()
    assert kwargs["max_items_per_store"] == 7
    assert kwargs["throttle"] is False
    assert kwargs["respect_robots_txt"] is False
    assert kwargs["bail"] == 0  # 0 = off; the library treats it like None
    assert kwargs["concurrent_requests"] == 2


def test_get_urls_single_url():
    assert get_urls(["https://example.com"], None) == ["https://example.com"]


def test_get_urls_multiple_urls():
    urls = get_urls(["https://store1.com", "https://store2.com"], None)
    assert urls == ["https://store1.com", "https://store2.com"]


def test_get_urls_from_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://store1.com\n\nhttps://store2.com\n")
    assert get_urls(None, url_file) == ["https://store1.com", "https://store2.com"]


def test_get_urls_file_skips_comments(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("# stores\nstore1.com\n  # indented comment\nstore2.com\n")
    assert get_urls(None, url_file) == ["store1.com", "store2.com"]


def test_get_urls_blank_arguments_fall_back_to_file(tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("store.com\n")
    assert get_urls(["", " "], url_file) == ["store.com"]


def test_get_urls_strips_arguments():
    assert get_urls([" store.com ", "  "], None) == ["store.com"]


def test_get_urls_empty():
    assert get_urls(None, None) == []


def test_cli_scrape_flag_conflict_checked_before_urls():
    result = runner.invoke(app, ["scrape", "-v", "-q", "https://[bad"])
    assert result.exit_code == 1
    assert "Cannot use both --verbose and --quiet" in strip_ansi(result.stderr)
    assert "Skipping invalid URL" not in strip_ansi(result.stderr)


def test_cli_scrape_url_file_comments_not_scraped(cli, tmp_path):
    url_file = tmp_path / "urls.txt"
    url_file.write_text("# my stores\nstore.com\n")
    run = cli(shop("store.com", 1), "--url-file", str(url_file))
    assert run.exit_code == 0
    assert [u["url"] for u in run.status()["urls"]] == ["store.com"]
    assert "Skipping invalid URL" not in run.stderr


def test_cli_scrape_skips_invalid_urls(cli):
    """Malformed URLs are skipped with a warning; valid ones are still scraped."""
    run = cli(shop("store.com", 1), "https://[bad", "https://", "store.com")

    assert "Skipping invalid URL: https://[bad" in run.stderr
    assert "Skipping invalid URL: https://" in run.stderr
    assert "Skipping" not in run.stdout
    assert run.exit_code == 0
    assert all(u.startswith("https://store.com/") for u in run.requests)
    assert run.status()["urls"][0]["url"] == "store.com"


@pytest.mark.parametrize(
    "url",
    [
        "store.com",
        "https://store.com",
        "http://store.com/collections/all",
        "store.com:8080",
        "localhost:8080",
        "//store.com",
        "https://[::1]:8443",
        "store.com/collections/all?ref=https://x.com",
        "http://localhost:8000",
        "https://127.0.0.1",
        "https://bücher.de",
        "https://my_store.example.com",
        "store.com:",
    ],
)
def test_is_valid_url_accepts(url):
    assert is_valid_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",
        "https://",
        "http://:80",
        "://store.com",
        "https://store.com:99999",
        "https://store.com:abc",
        "ftp://store.com",
        "http://exa mple.com",
        "http:/store.com",
        "https://store.com:0",
        "https://sto\x00re.com",
        "https://.",
        "https://@store.com",
        "https://user:pass@store.com",
        "https://store.com\\",
        "https://[fe80::1%25eth0]",
    ],
)
def test_is_valid_url_rejects(url):
    assert not is_valid_url(url)


def test_cli_scrape_warning_escapes_markup(cli):
    """A URL that looks like Rich markup is printed literally instead of crashing."""
    run = cli(shop("store.com", 1), "https://[/bad]", "store.com")

    assert run.exit_code == 0
    assert "Skipping invalid URL: https://[/bad]" in run.stderr
    assert run.status()["urls"][1]["url"] == "https://[/bad]"


def test_status_file_lists_skipped_urls(cli):
    """Skipped invalid URLs are recorded in the status file (with a "code", §4.3)."""
    run = cli(shop("store.com", 2), "https://store.com", "https://[bad")

    assert run.status()["urls"][1] == {
        "url": "https://[bad",
        "items": 0,
        "status": "invalid URL",
        "code": "invalid_url",
    }


def test_peek_zero_items_summary_goes_to_stderr(cli):
    """With --peek, stdout is reserved for JSONL, so the zero-item summary uses stderr."""
    run = cli(unreachable("down.invalid"), "--peek", "https://down.invalid")

    assert run.exit_code == 1
    assert run.stdout == ""
    assert "Scraped 0 items" in run.stderr
    assert "No responses received" in run.stderr


def test_cli_scrape_all_urls_invalid(cli):
    """The run fails if every URL is malformed, before any request."""
    run = cli({}, "https://[bad")

    assert run.exit_code == 1
    assert "No valid URLs provided" in run.stderr
    assert run.fetchers == []


# --- --limit and platform ---


def test_limit_passed_to_woocommerce(cli):
    routes = {wc_url("store.com"): [{"id": i, "name": f"P{i}"} for i in range(10)]}
    run = cli(routes, "--platform", "woocommerce", "--limit", "3", "https://store.com")

    assert run.exit_code == 0
    assert len(run.items()) == 3
    assert run.status()["urls"][0]["items"] == 3
    assert run.data_files()[0].name.startswith("woocommerce_spider_")


def test_limit_passed_to_shopify(cli):
    run = cli(shop("example.com", 10), "--limit", "5", "https://example.com")

    assert run.exit_code == 0
    assert len(run.items()) == 5
    assert run.status()["finish_reason"] == "item_limit"


def test_limit_is_per_store(cli):
    """--limit stops each store after N items, not the whole run (§4.11)."""
    routes = merge(shop("store1.com", 10), shop("store2.com", 10))
    run = cli(routes, "--limit", "2", "https://store1.com", "https://store2.com")

    status = run.status()
    assert status["items_scraped"] == 4
    assert [u["items"] for u in status["urls"]] == [2, 2]


def test_config_platform_used_without_flag(cli, tmp_path):
    """§3.10: scrape.platform in the config file wins when --platform is absent."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scrape:\n  platform: woocommerce\n")
    routes = {wc_url("store.com"): [{"id": 1, "name": "A"}]}
    run = cli(routes, "--config", str(config_file), "https://store.com")

    assert run.exit_code == 0
    assert "Platform: woocommerce" in run.stdout
    assert any("/wp-json/wc/store/v1/products" in u for u in run.requests)
    assert run.status()["items_scraped"] == 1


def test_cli_platform_overrides_config(cli, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scrape:\n  platform: woocommerce\n  limit: 4\n")
    run = cli(
        shop("store.com", 10),
        "--config",
        str(config_file),
        "--platform",
        "shopify",
        "--limit",
        "1",
        "https://store.com",
    )

    assert "Platform: shopify" in run.stdout
    assert run.status()["items_scraped"] == 1


def test_config_values_apply_without_flags(cli, tmp_path):
    """Config file values (limit, throttle off) apply when no flag overrides them."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scrape:\n  limit: 2\nthrottle:\n  enabled: false\n")
    run = cli(shop("store.com", 10), "--config", str(config_file), "store.com", throttle=True)

    assert run.status()["items_scraped"] == 2


# --- Per-store breakdown tests (multi-URL) ---


def test_multi_url_success_shows_per_url_counts(cli):
    routes = merge(shop("store1.com", 10), shop("store2.com", 5))
    run = cli(routes, "https://store1.com", "https://store2.com")

    assert run.exit_code == 0
    assert "Done!" in run.stdout
    assert "store1.com: 10 items" in run.stdout
    assert "store2.com: 5 items" in run.stdout


def test_multi_url_scrapes_each_url_separately(cli):
    """Each URL is scraped as its own store and reported under its own entry."""
    urls = ["https://store1.com", "https://store2.com", "https://store3.com"]
    routes = merge(*(shop(f"store{i}.com", i) for i in (1, 2, 3)))
    run = cli(routes, *urls)

    status = run.status()
    assert [u["url"] for u in status["urls"]] == urls
    assert [u["items"] for u in status["urls"]] == [1, 2, 3]
    for i in (1, 2, 3):
        assert any(u.startswith(f"https://store{i}.com/products.json") for u in run.requests)


def test_multi_url_store_that_failed(cli):
    """A store that raised is reported as failed, not a crash.

    (The old test used an invalid URL that broke Scrapy; invalid URLs are now
    skipped before the run, so the failure comes from the store itself.)
    """
    routes = merge(shop("store1.com", 4), broken("store2.com"))
    run = cli(routes, "https://store1.com", "https://store2.com")

    assert run.exit_code == 0
    assert "store1.com: 4 items" in run.stdout
    assert "store2.com: failed" in run.stdout
    assert "1 store(s) failed with an error" in run.stdout
    data = run.status()
    assert data["urls"][0]["items"] == 4
    assert data["urls"][1] == {
        "url": "https://store2.com",
        "items": 0,
        "status": "failed",
        "code": "error",
    }


def test_store_that_failed_before_items(cli):
    """A single failed store gets the error hint, not a robots.txt diagnosis."""
    run = cli(broken("store.com"), "https://store.com")

    assert run.exit_code == 1
    assert "1 store(s) failed with an error" in run.stdout
    assert "robots.txt" not in run.stdout
    assert run.status()["urls"][0]["status"] == "failed"


def test_store_that_failed_after_scraping_items(cli):
    """A store that raised after scraping items is reported as failed, not ok."""

    def page2(request):
        raise RuntimeError("boom")

    routes = merge(
        shop("store1.com", 4),
        {products_url("store2.com"): bulk(250), products_url("store2.com", 2): page2},
    )
    run = cli(routes, "https://store1.com", "https://store2.com")

    assert "store2.com: failed" in run.stdout
    assert "1 store(s) failed with an error" in run.stdout
    data = run.status()
    assert data["urls"][1] == {
        "url": "https://store2.com",
        "items": 250,
        "status": "failed",
        "code": "error",
    }
    assert data["finish_reason"] == "crawler_failed"


def test_per_store_line_prints_host_literally(cli):
    """Per-store lines print the host as is (an IPv6 literal has brackets).

    The old test used "https://[/bad]", which is now skipped before the run.
    """
    routes = merge(shop("store1.com", 1), broken("[::1]:8443"))
    run = cli(routes, "https://store1.com", "https://[::1]:8443")

    assert "[::1]:8443: failed" in run.stdout


def test_peek_reports_failed_store_on_stderr(cli):
    """With --peek, a store that already failed is reported on stderr."""

    async def slow(request):
        await asyncio.sleep(0.3)
        return bulk(1)

    routes = merge({products_url("store1.com"): slow}, broken("store2.com"))
    run = cli(routes, "--peek", "https://store1.com", "https://store2.com")

    assert "failed with an error" not in run.stdout
    assert "1 store(s) failed with an error" in run.stderr


def test_single_url_success_no_per_url_breakdown(cli):
    run = cli(shop("only-one.com", 7), "https://only-one.com")

    assert "Done!" in run.stdout
    assert "only-one.com" not in run.stdout


def test_multi_url_failure_shows_per_url_status(cli):
    routes = forbidden("blocked.com")  # missing.com: everything 404s
    run = cli(routes, "https://blocked.com", "https://missing.com")

    assert run.exit_code == 1
    assert "blocked.com: 403 Forbidden" in run.stdout
    assert "missing.com: 404 Not Found" in run.stdout


def test_single_url_failure_no_per_url_breakdown(cli):
    run = cli(forbidden("blocked.com"), "https://blocked.com")

    assert run.exit_code == 1
    assert "blocked.com: 403 Forbidden" not in run.stdout
    assert "HTTP 403" in run.stdout


def test_no_responses_not_reported_as_robots(cli):
    """Zero responses means a network problem, not a robots.txt block."""
    routes = merge(unreachable("down1.invalid"), unreachable("down2.invalid"))
    run = cli(routes, "https://down1.invalid", "https://down2.invalid")

    assert "robots.txt" not in run.stdout
    assert "No responses received" in run.stdout


# --- Diagnoses (ported from the _diagnose_crawler unit tests) ---


def _single_status(run: Run) -> str:
    (entry,) = run.status()["urls"]
    return entry["status"]


def test_diagnose_items(cli):
    run = cli(shop("store.com", 5), "store.com")
    assert run.status()["urls"][0] == {
        "url": "store.com",
        "items": 5,
        "status": "ok",
        "code": "ok",
    }


def test_diagnose_403(cli):
    run = cli(forbidden("store.com"), "store.com")
    assert _single_status(run) == "403 Forbidden"
    # products.json and the sitemap, each tried 1 + 2 retries
    assert "Received 6 HTTP 403 (Forbidden) response(s)" in run.stdout


def test_diagnose_404(cli):
    run = cli({}, "store.com")
    assert _single_status(run) == "404 Not Found"
    assert "HTTP 404 (Not Found)" in run.stdout
    assert "Check the URL and --platform flag" in run.stdout


def test_robots_txt_404_does_not_hide_timeout(cli):
    """A store without a robots.txt (404) that then times out is reported as timed out."""
    routes = {
        "https://slow.com/robots.txt": (404, ""),
        products_url("slow.com"): {"products": []},
        sitemap_url("slow.com"): hang,
    }
    run = cli(routes, "--bail", "1", "https://slow.com")

    assert "Timed out after 1s" in run.stdout
    assert "HTTP 404" not in run.stdout
    assert _single_status(run) == "timed out"


def test_summary_has_a_hint_per_distinct_problem(cli):
    """One store's problem does not hide another's hint."""
    routes = merge(forbidden("blocked.com"), unreachable("down.invalid"))
    run = cli(routes, "https://blocked.com", "https://down.invalid")

    assert "blocked.com: 403 Forbidden" in run.stdout
    assert "down.invalid: no response" in run.stdout
    assert "Received 6 HTTP 403" in run.stdout
    assert "No responses received" in run.stdout


def test_summary_hint_once_per_problem_in_diagnosis_order(cli):
    """Each distinct problem gets one hint (counts summed), in diagnosis order."""
    urls = [
        "https://empty.com",
        "https://down.invalid",
        "https://robots.com",
        "https://missing.com",
        "https://blocked1.com",
        "https://blocked2.com",
        "https://broken.com",
    ]
    routes = merge(
        empty("empty.com"),
        unreachable("down.invalid"),
        {"https://robots.com/robots.txt": (200, DISALLOW_ALL)},
        forbidden("blocked1.com"),
        forbidden("blocked2.com"),
        broken("broken.com"),
    )
    run = cli(routes, *urls)

    assert run.exit_code == 1
    hints = [
        "1 store(s) failed with an error",
        "Received 12 HTTP 403",
        "HTTP 404 (Not Found)",
        "Likely blocked by robots.txt",
        "No responses received",
        "The store may be empty",
    ]
    positions = [run.stdout.find(h) for h in hints]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)
    assert run.stdout.count("HTTP 403") == 1


def test_timeout_shows_bail_hint(cli):
    """A no-item timeout prints the --bail hint, not another diagnosis."""
    routes = {products_url("slow.com"): {"products": []}, sitemap_url("slow.com"): hang}
    run = cli(routes, "--bail", "1", "https://slow.com")

    assert run.exit_code == 1
    assert "Timed out after 1s with no items" in run.stdout
    assert "--bail 0" in run.stdout
    data = run.status()
    assert data["urls"][0]["status"] == "timed out"
    assert data["finish_reason"] == "bail"


def test_timeout_does_not_hide_403(cli):
    """A store blocked with 403s until --bail fires is reported as blocked."""
    routes = merge(
        empty("empty.com"),
        {products_url("blocked.com"): (403, ""), sitemap_url("blocked.com"): hang},
    )
    run = cli(routes, "--bail", "1", "https://empty.com", "https://blocked.com")

    assert "blocked.com: 403 Forbidden" in run.stdout
    assert "Received 3 HTTP 403" in run.stdout
    assert "Timed out after" not in run.stdout


def test_diagnose_robots(cli):
    """products.json and the sitemap fallback both disallowed; the store never answered."""
    run = cli({"https://store.com/robots.txt": (200, DISALLOW_ALL)}, "store.com")

    assert _single_status(run) == "blocked by robots.txt"
    assert "--ignore-robots" in run.stdout


def test_ignore_robots_bypasses_block(cli):
    routes = merge({"https://store.com/robots.txt": (200, DISALLOW_ALL)}, shop("store.com", 2))
    run = cli(routes, "-i", "store.com")

    assert run.exit_code == 0
    assert run.status()["items_scraped"] == 2


def test_diagnose_robots_after_store_redirect(cli):
    """store.com 301s to www.store.com, whose robots.txt forbids the crawl."""

    def to_www(request):
        target = request.url.replace("https://store.com", "https://www.store.com")
        return (301, "", {"Location": target})

    routes = {
        products_url("store.com"): to_www,
        sitemap_url("store.com"): to_www,
        "https://www.store.com/robots.txt": (200, DISALLOW_ALL),
    }
    run = cli(routes, "store.com")

    assert _single_status(run) == "blocked by robots.txt"


def test_diagnose_robots_before_fallback_404(cli):
    """products.json disallowed, and the allowed sitemap fallback 404s."""
    routes = {"https://store.com/robots.txt": (200, "User-agent: *\nDisallow: /products.json\n")}
    run = cli(routes, "store.com")

    assert _single_status(run) == "blocked by robots.txt"


def test_diagnose_robots_incidental_disallow(cli):
    """A disallowed URL in a crawl where the store answered is not a block."""
    routes = {
        "https://store.com/robots.txt": (200, "User-agent: *\nDisallow: /products.json\n"),
        sitemap_url("store.com"): urlset(),
    }
    run = cli(routes, "store.com")

    assert _single_status(run) == "0 items"
    assert "robots.txt" not in run.stdout


def test_diagnose_robots_allowed_then_no_response(cli):
    """robots.txt answered and allowed the crawl, but the store never responded."""
    routes = unreachable("store.com")
    routes["https://store.com/robots.txt"] = (200, "User-agent: *\nAllow: /\n")
    run = cli(routes, "store.com")

    assert _single_status(run) == "no response"


def test_robots_block_not_blamed_for_other_store(cli):
    """One robots-blocked store does not hide an unreachable one."""
    routes = merge(
        {"https://blocked.com/robots.txt": (200, DISALLOW_ALL)},
        unreachable("down.invalid"),
    )
    run = cli(routes, "https://blocked.com", "https://down.invalid")

    assert "blocked.com: blocked by robots.txt" in run.stdout
    assert "down.invalid: no response" in run.stdout


def test_diagnose_no_response(cli):
    run = cli(unreachable("store.com"), "store.com")

    assert _single_status(run) == "no response"
    assert run.status()["urls"][0]["code"] == "no_response"


def test_diagnose_zero_items(cli):
    run = cli(empty("store.com"), "store.com")

    assert run.exit_code == 1
    assert _single_status(run) == "0 items"
    assert "The store may be empty" in run.stdout


def test_diagnose_recovered_403_is_not_blocking(cli):
    """A 403 the UA swap recovered from (a later 2xx) doesn't hide the timeout."""
    routes = {
        products_url("store.com"): Seq([(403, ""), {"products": []}]),
        sitemap_url("store.com"): hang,
    }
    run = cli(routes, "--bail", "1", "store.com")

    assert _single_status(run) == "timed out"
    assert "HTTP 403" not in run.stdout


def test_diagnose_fallback_404_is_not_blocking(cli):
    """products.json 404 followed by a working sitemap is the normal fallback."""
    routes = {
        sitemap_url("store.com"): urlset("https://store.com/products/hat"),
        "https://store.com/products/hat.json": hang,
    }
    run = cli(routes, "--bail", "1", "store.com")

    assert _single_status(run) == "timed out"
    assert "HTTP 404" not in run.stdout


def test_diagnose_fallback_404_then_sitemap_items(cli):
    routes = {
        sitemap_url("store.com"): urlset("https://store.com/products/hat"),
        "https://store.com/products/hat.json": {"product": {"id": 1, "handle": "hat"}},
    }
    run = cli(routes, "store.com")

    assert run.exit_code == 0
    assert _single_status(run) == "ok"


def test_diagnose_tls_error_hints_insecure(cli):
    """D14: a certificate failure gets its own status and an --insecure hint."""
    run = cli(unreachable("store.com", FetchTLSError("certificate verify failed")), "store.com")

    assert run.exit_code == 1
    assert run.status()["urls"][0]["code"] == "tls_error"
    assert "TLS certificate verification failed" in run.stdout
    assert "--insecure" in run.stdout


def test_wrong_platform_hint_printed(cli):
    """A Shopify run against a WooCommerce store prints the probe's hint (§4.3)."""
    routes = {"https://store.com/wp-json/": {"namespaces": ["wc/store/v1"]}}
    run = cli(routes, "store.com")

    assert run.exit_code == 1
    assert "store.com looks like WooCommerce; use --platform woocommerce." in run.stdout


def test_wrong_platform_hint_woocommerce_run(cli):
    routes = {"https://store.com/products.json": bulk(1)}
    run = cli(routes, "--platform", "woocommerce", "store.com")

    assert "store.com looks like Shopify; use --platform shopify." in run.stdout


# --- Status file tests ---


def test_status_file_written_on_success(cli):
    run = cli(shop("store.com", 12), "https://store.com")

    data = run.status()
    assert data["items_scraped"] == 12
    assert data["finish_reason"] == "finished"
    assert data["duration_seconds"] >= 0
    assert data["log_file"] is not None
    assert data["urls"] == [{"url": "https://store.com", "items": 12, "status": "ok", "code": "ok"}]


def test_status_file_written_on_failure(cli):
    run = cli(forbidden("blocked.com"), "https://blocked.com")

    data = run.status()
    assert data["items_scraped"] == 0
    assert data["urls"][0]["status"] == "403 Forbidden"
    assert data["urls"][0]["code"] == "forbidden"
    assert run.data_files() == []  # no data file without items


def test_status_file_multi_url(cli):
    routes = merge(shop("ok.com", 10), forbidden("blocked.com"))
    run = cli(routes, "https://ok.com", "https://blocked.com")

    data = run.status()
    assert data["items_scraped"] == 10
    assert data["urls"][0]["status"] == "ok"
    assert data["urls"][1]["status"] == "403 Forbidden"


def test_no_status_file_in_peek_mode(cli):
    run = cli(shop("store.com", 3), "--peek", "https://store.com")

    assert run.exit_code == 0
    assert not run.out.exists()


# --- Peek tests ---


def test_peek_prints_one_item_total(cli):
    """§3.10: --peek prints one item overall, even with several stores."""
    routes = merge(shop("store1.com", 5), shop("store2.com", 5))
    run = cli(routes, "--peek", "https://store1.com", "https://store2.com")

    assert run.exit_code == 0
    lines = run.result.stdout.splitlines()
    assert len(lines) == 1
    assert "product" in json.loads(lines[0])


def test_peek_creates_no_files_or_dirs(cli):
    """§3.10: --peek creates no output dirs (not even images/) and no log file."""
    run = cli(shop("store.com", 2), "--peek", "--images", "https://store.com")

    assert run.exit_code == 0
    assert not run.out.exists()
    assert list(run.log_dir.iterdir()) == []


def test_peek_downloads_no_images(cli):
    """§3.10: --peek disables image downloads."""
    product = {"id": 1, "handle": "hat", "images": [{"src": "https://cdn.store.com/hat.jpg"}]}
    run = cli(
        {products_url("store.com"): {"products": [product]}}, "--peek", "--images", "store.com"
    )

    item = json.loads(run.result.stdout)
    assert item["image_urls"] == []
    assert not any("cdn.store.com" in u for u in run.requests)


# --- Log file tests ---


def test_log_file_created(cli):
    run = cli(shop("store.com", 1), "https://store.com")

    (log_file,) = run.log_dir.iterdir()
    assert log_file.suffix == ".log"
    assert "shopify_spider" in log_file.name
    assert "finished" in log_file.read_text()
    assert f"Log: {log_file}" in run.stdout


def test_no_log_file_in_peek_mode(cli):
    run = cli(shop("store.com", 1), "--peek", "https://store.com")

    assert run.exit_code == 0
    assert list(run.log_dir.iterdir()) == []


def test_status_file_log_path_matches(cli):
    run = cli(shop("store.com", 1), "https://store.com")

    (log_file,) = run.log_dir.iterdir()
    assert run.status()["log_file"] == str(log_file)


def test_zero_item_summary_names_log_file(cli):
    run = cli({}, "store.com")

    (log_file,) = run.log_dir.iterdir()
    assert f"Log: {log_file}" in run.stdout


# --- Output format tests ---


def test_csv_uses_fixed_shopify_columns(cli):
    """D2: Shopify CSV has fixed columns, so collection rows keep their column."""
    routes = {
        products_url("store.com"): bulk(1),
        sitemap_url("store.com"): urlset("https://store.com/collections/all"),
        "https://store.com/collections/all.json": {"collection": {"id": 9, "handle": "all"}},
    }
    run = cli(routes, "-F", "csv", "--collections", "store.com")

    (path,) = run.data_files()
    assert path.suffix == ".csv"
    rows = list(csv.reader(path.read_text().splitlines()))
    assert rows[0] == ["product", "collection", "url", "store", "image_urls"]
    assert len(rows) == 3
    collection_row = next(r for r in rows[1:] if r[1])
    assert json.loads(collection_row[1])["handle"] == "all"
    assert collection_row[0] == ""


def test_csv_products_only_columns(cli):
    run = cli(shop("store.com", 2), "-F", "csv", "store.com")

    (path,) = run.data_files()
    header = path.read_text().splitlines()[0]
    assert header == "product,url,store,image_urls"


# --- finish_reason tests (status file) ---


def test_finish_reason_normal(cli):
    run = cli(shop("store.com", 1), "store.com")
    assert run.status()["finish_reason"] == "finished"


def test_finish_reason_item_limit(cli):
    run = cli(shop("store.com", 5), "--limit", "5", "store.com")
    assert run.status()["finish_reason"] == "item_limit"


def test_finish_reason_crawler_failed(cli):
    routes = merge(shop("ok.com", 5), broken("broken.com"))
    run = cli(routes, "ok.com", "broken.com")
    assert run.status()["finish_reason"] == "crawler_failed"


def test_finish_reason_bail_takes_priority(cli):
    """bail takes priority even when another store finished normally."""
    routes = merge(
        shop("ok.com", 3),
        {products_url("slow.com"): {"products": []}, sitemap_url("slow.com"): hang},
    )
    run = cli(routes, "--bail", "1", "ok.com", "slow.com")

    data = run.status()
    assert data["finish_reason"] == "bail"
    assert run.exit_code == 0


def _store(items: int, finish_reason: str) -> StoreResult:
    return StoreResult(
        input_url="s.com",
        url="https://s.com",
        store="s.com",
        platform="shopify",
        items=items,
        status=StoreStatus.ok if items else StoreStatus.empty,
        complete=finish_reason == "finished",
        finish_reason=finish_reason,
    )


def test_finish_reason_item_limit_derivation():
    """The run-level reason the CLI writes, derived from store results."""
    assert run_finish_reason([_store(5, "item_limit")]) == "item_limit"
    assert run_finish_reason([_store(3, "finished")]) == "finished"
    assert run_finish_reason([_store(3, "finished"), _store(0, "bail")]) == "bail"
    assert run_finish_reason([_store(3, "error"), _store(0, "bail")]) == "crawler_failed"


def test_finish_reason_item_limit_needs_a_store_at_its_limit():
    # Plan §3.10: the Scrapy CLI compared the run total with the per-store limit, so
    # two stores with 3 items each and --limit 5 reported "item_limit"
    stores = [_store(3, "finished"), _store(3, "finished")]
    assert run_finish_reason(stores) == "finished"
