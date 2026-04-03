"""Command-line interface for Shopify Spy."""

import json
import logging
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import platformdirs
import typer
from rich.console import Console

from shopify_spy.config import (
    OUTPUT_FORMATS,
    Config,
    OutputFormat,
    Platform,
    create_default_config,
    load_config,
)

app = typer.Typer(
    name="shopify-spy",
    help="Scrape product and collection data from Shopify stores.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        from importlib.metadata import version

        console.print(f"shopify-spy {version('shopify-spy')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Shopify Spy - Scrape product data from any Shopify store."""


@app.command()
def scrape(
    urls: Annotated[
        list[str] | None,
        typer.Argument(help="Store URL(s) to scrape."),
    ] = None,
    url_file: Annotated[
        Path | None,
        typer.Option(
            "--url-file",
            "-f",
            help="File containing URLs to scrape (one per line).",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    products: Annotated[
        bool | None,
        typer.Option("--products/--no-products", help="Scrape products."),
    ] = None,
    collections: Annotated[
        bool | None,
        typer.Option("--collections/--no-collections", help="Scrape collections."),
    ] = None,
    images: Annotated[
        bool | None,
        typer.Option("--images/--no-images", help="Download product images."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output directory for results."),
    ] = None,
    format: Annotated[
        OutputFormat | None,
        typer.Option("--format", "-F", help="Output format: json, jsonl, csv, xml."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to YAML configuration file.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    concurrent: Annotated[
        int | None,
        typer.Option("--concurrent", help="Concurrent requests per domain."),
    ] = None,
    throttle: Annotated[
        bool | None,
        typer.Option("--throttle/--no-throttle", help="Auto-throttle requests."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", "-n", help="Stop after scraping N items. Useful for sampling or testing."
        ),
    ] = None,
    user_agent: Annotated[
        str | None,
        typer.Option("--user-agent", "-A", help="Custom User-Agent header."),
    ] = None,
    platform: Annotated[
        Platform,
        typer.Option("--platform", "-p", help="Ecommerce platform: shopify, woocommerce."),
    ] = Platform.shopify,
    bail: Annotated[
        int | None,
        typer.Option(
            "--bail",
            help="Abort if no items scraped within N seconds (0 = off).",
        ),
    ] = None,
    ignore_robots: Annotated[
        bool,
        typer.Option(
            "--ignore-robots",
            "-i",
            help="Ignore robots.txt restrictions.",
        ),
    ] = False,
    peek: Annotated[
        bool,
        typer.Option("--peek", help="Print 1 item to stdout as JSONL and exit. No file output."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show debug output."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Show only warnings and errors."),
    ] = False,
) -> None:
    """Scrape products and collections from Shopify and WooCommerce stores."""
    # Load config file (or defaults)
    config = load_config(config_path)

    # Apply CLI overrides
    config = apply_cli_overrides(
        config,
        platform=platform,
        products=products,
        collections=collections,
        images=images,
        output=output,
        format=format,
        concurrent=concurrent,
        throttle=throttle,
        limit=limit,
        user_agent=user_agent,
        ignore_robots=ignore_robots,
        bail=bail,
    )

    # Warn about Shopify-only flags used with WooCommerce
    if platform == Platform.woocommerce:
        if collections is True:
            console.print(
                "[yellow]Warning: --collections has no effect with WooCommerce "
                "(collections are not supported).[/yellow]"
            )
        if products is False:
            console.print(
                "[yellow]Warning: --no-products has no effect with WooCommerce "
                "(WooCommerce always scrapes products).[/yellow]"
            )

    # Get URLs
    all_urls = get_urls(urls, url_file)
    if not all_urls:
        console.print("[red]Error: No URLs provided.[/red]")
        console.print("Provide a URL argument, --url-file, or run interactively.")
        raise typer.Exit(1)

    # --peek implies limit=1, quiet logging, no images
    if peek:
        if config.scrape.limit is None:
            config = config.model_copy(
                update={"scrape": config.scrape.model_copy(update={"limit": 1})}
            )
        quiet = True

    # Validate flag combinations
    if verbose and quiet:
        console.print("[red]Error: Cannot use both --verbose and --quiet[/red]")
        raise typer.Exit(1)

    # Run the spider
    run_spider(all_urls, config, peek=peek, verbose=verbose, quiet=quiet)


@app.command()
def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Path for config file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing file."),
    ] = False,
) -> None:
    """Create a default configuration file."""
    target = path or Path("./shopify-spy.yaml")
    target = target.expanduser().resolve()

    if target.exists() and not force:
        console.print(f"[yellow]File already exists: {target}[/yellow]")
        console.print("Use --force to overwrite.")
        raise typer.Exit(1)

    created = create_default_config(target)
    console.print(f"[green]Created config file: {created}[/green]")


def apply_cli_overrides(
    config: Config,
    *,
    platform: Platform | None,
    products: bool | None,
    collections: bool | None,
    images: bool | None,
    output: Path | None,
    format: OutputFormat | None,
    concurrent: int | None,
    throttle: bool | None,
    limit: int | None,
    user_agent: str | None,
    ignore_robots: bool = False,
    bail: int | None = None,
) -> Config:
    """Apply CLI argument overrides to config."""
    # Create copies to avoid mutating original
    scrape_dict = config.scrape.model_dump()
    output_dict = config.output.model_dump()
    network_dict = config.network.model_dump()
    throttle_dict = config.throttle.model_dump()

    if platform is not None:
        scrape_dict["platform"] = platform
    if products is not None:
        scrape_dict["products"] = products
    if collections is not None:
        scrape_dict["collections"] = collections
    if images is not None:
        scrape_dict["images"] = images
    if limit is not None:
        scrape_dict["limit"] = limit
    if bail is not None:
        scrape_dict["bail"] = bail
    if output is not None:
        output_dict["dir"] = output
    if format is not None:
        output_dict["format"] = format
    if concurrent is not None:
        network_dict["concurrent_requests"] = concurrent
    if user_agent is not None:
        network_dict["user_agent"] = user_agent
    if ignore_robots:
        network_dict["respect_robots_txt"] = False
    if throttle is not None:
        throttle_dict["enabled"] = throttle

    return Config(
        scrape=scrape_dict,
        output=output_dict,
        network=network_dict,
        throttle=throttle_dict,
    )


def get_urls(urls: list[str] | None, url_file: Path | None) -> list[str]:
    """Get URLs from arguments or file, with interactive fallback."""
    if urls:
        return list(urls)

    if url_file:
        with open(url_file) as f:
            return [line.strip() for line in f if line.strip()]

    # Interactive prompt if stdin is a TTY
    if sys.stdin.isatty():
        url = typer.prompt("Enter Shopify store URL")
        if url:
            return [url]

    return []


def _log_dir() -> Path:
    """Return the platform-appropriate log directory, creating it if needed."""
    path = platformdirs.user_state_path("shopify-spy") / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _finish_reason(crawlers: list, config: Config) -> str:
    """Derive an overall finish reason from a list of crawlers."""
    reasons = [c.stats.get_value("finish_reason", "unknown") for c in crawlers]
    if any(r == "no_item_timeout" for r in reasons):
        return "bail"
    limit = config.scrape.limit
    if limit is not None:
        total = sum(c.stats.get_value("item_scraped_count", 0) for c in crawlers)
        if total >= limit:
            return "item_limit"
    if all(r == "finished" for r in reasons):
        return "finished"
    # Return the first non-"finished" reason
    return next((r for r in reasons if r != "finished"), reasons[0])


def _write_status_file(
    status_path: Path,
    crawlers: list,
    urls: list[str],
    config: Config,
    duration: float,
    log_file: Path | None,
    total: int,
) -> None:
    """Write a _status.json file alongside the output."""
    url_entries = []
    for url, crawler in zip(urls, crawlers):
        items = crawler.stats.get_value("item_scraped_count", 0)
        status = "ok" if items > 0 else _diagnose_crawler(crawler, config)
        url_entries.append({"url": url, "items": items, "status": status})

    data = {
        "items_scraped": total,
        "urls": url_entries,
        "finish_reason": _finish_reason(crawlers, config),
        "duration_seconds": round(duration, 1),
        "log_file": str(log_file) if log_file else None,
    }
    status_path.write_text(json.dumps(data, indent=2) + "\n")


def run_spider(
    urls: list[str],
    config: Config,
    peek: bool = False,
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    """Run the appropriate spider with the given configuration."""
    # Deferred imports to avoid loading Scrapy until needed
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    settings = get_project_settings()

    # Generate a timestamp used for log, output, and status file names
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S+00-00")
    name = f"{config.scrape.platform.value}_spider"

    # Configure output directory
    output_dir = config.output.dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = (output_dir / config.output.images_subdir).resolve()
    images_dir.mkdir(parents=True, exist_ok=True)

    # Apply config to Scrapy settings
    settings.set("CONCURRENT_REQUESTS_PER_DOMAIN", config.network.concurrent_requests)
    settings.set("DOWNLOAD_TIMEOUT", config.network.timeout)
    settings.set("RETRY_TIMES", config.network.retries)
    settings.set("ROBOTSTXT_OBEY", config.network.respect_robots_txt)
    settings.set("NO_ITEM_TIMEOUT", config.scrape.bail)
    settings.set("IMAGES_STORE", str(images_dir))

    # --- Logging setup ---
    log_file: Path | None = None
    if not peek:
        log_file = _log_dir() / f"{name}_{timestamp}.log"
        settings.set("LOG_FILE", str(log_file))

    log_level = "DEBUG" if verbose else "WARNING" if quiet else "INFO"
    settings.set("LOG_LEVEL", log_level)

    # --- Feed output ---
    if peek:
        settings.set(
            "FEEDS",
            {
                "stdout://": {
                    "format": "jsonlines",
                    "encoding": "utf8",
                }
            },
        )
    else:
        scrapy_format, file_ext = OUTPUT_FORMATS[config.output.format]
        settings.set(
            "FEEDS",
            {
                f"{output_dir.as_uri()}/{name}_{timestamp}{file_ext}": {
                    "format": scrapy_format,
                    "encoding": "utf8",
                    "store_empty": False,
                    "item_export_kwargs": {"export_empty_fields": True},
                }
            },
        )

    if config.network.user_agent:
        settings.set("USER_AGENT", config.network.user_agent)
        # Disable our rotation middleware when user sets an explicit UA
        middlewares = settings.getdict("DOWNLOADER_MIDDLEWARES", {}).copy()
        middlewares["shopify_spy.middlewares.UserAgentMiddleware"] = None
        middlewares["scrapy.downloadermiddlewares.useragent.UserAgentMiddleware"] = 400
        settings.set("DOWNLOADER_MIDDLEWARES", middlewares)

    # Configure auto-throttle
    if config.throttle.enabled:
        settings.set("AUTOTHROTTLE_ENABLED", True)
        settings.set("AUTOTHROTTLE_START_DELAY", config.throttle.start_delay)
        settings.set("AUTOTHROTTLE_MAX_DELAY", config.throttle.max_delay)
        settings.set("AUTOTHROTTLE_TARGET_CONCURRENCY", config.throttle.target_concurrency)

    # Disable image pipeline if images are disabled
    if not config.scrape.images:
        settings.set("ITEM_PIPELINES", {})

    # --- Live item counter (unless quiet or peek) ---
    show_counter = not peek and not quiet
    counter: list[int] = [0]
    if show_counter:
        settings.set("_ITEM_COUNTER", counter)
        extensions = settings.getdict("EXTENSIONS", {}).copy()
        extensions["shopify_spy.extensions.LiveItemCounter"] = 501
        settings.set("EXTENSIONS", extensions)

    # Select spider and build kwargs based on platform
    if config.scrape.platform == Platform.woocommerce:
        from shopify_spy.spiders.woocommerce import WooCommerceSpider

        spider_cls = WooCommerceSpider
        spider_kwargs: dict = {"images": config.scrape.images, "limit": config.scrape.limit}
    else:
        from shopify_spy.spiders.shopify import ShopifySpider

        spider_cls = ShopifySpider
        spider_kwargs = {
            "products": config.scrape.products,
            "collections": config.scrape.collections,
            "images": config.scrape.images,
            "limit": config.scrape.limit,
        }

    if not peek:
        console.print(f"[bold]Scraping {len(urls)} store(s)...[/bold]")
        console.print(f"  Platform: {config.scrape.platform.value}")
        if config.scrape.platform == Platform.shopify:
            console.print(f"  Products: {config.scrape.products}")
            console.print(f"  Collections: {config.scrape.collections}")
        console.print(f"  Images: {config.scrape.images}")
        console.print(f"  Format: {config.output.format}")
        console.print(f"  Output: {output_dir}")

    process = CrawlerProcess(settings)

    # In verbose mode, also log to stderr (Scrapy only logs to file when LOG_FILE is set)
    stderr_handler: logging.Handler | None = None
    if verbose and log_file:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        stderr_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        logging.getLogger().addHandler(stderr_handler)

    # Crawl each URL; capture crawler refs before start() clears the set
    for store_url in urls:
        process.crawl(spider_cls, url=store_url, **spider_kwargs)
    crawlers = list(process.crawlers)

    start = time.monotonic()
    process.start()
    duration = time.monotonic() - start

    # Clear the counter line
    if show_counter and counter[0] > 0:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    # Clean up the stderr handler added for verbose mode
    if stderr_handler is not None:
        logging.getLogger().removeHandler(stderr_handler)

    total = sum(c.stats.get_value("item_scraped_count", 0) for c in crawlers)
    multi = len(urls) > 1

    # Write status file (unless peek mode)
    if not peek:
        status_path = output_dir / f"{name}_{timestamp}_status.json"
        _write_status_file(status_path, crawlers, urls, config, duration, log_file, total)

    if total > 0:
        if not peek:
            if show_counter:
                console.print("[green]Done![/green]")
            else:
                console.print(f"[green]Done! Scraped {total} item(s).[/green]")
            if multi:
                for url, c in zip(urls, crawlers):
                    host = urllib.parse.urlparse(url).netloc or url
                    count = c.stats.get_value("item_scraped_count", 0)
                    console.print(f"  {host}: {count} items")
        return

    # Diagnose why nothing was scraped
    timed_out = any(c.stats.get_value("finish_reason", "") == "bail" for c in crawlers)
    http_403 = sum(c.stats.get_value("downloader/response_status_count/403", 0) for c in crawlers)
    http_404 = sum(c.stats.get_value("downloader/response_status_count/404", 0) for c in crawlers)
    response_count = sum(c.stats.get_value("downloader/response_count", 0) for c in crawlers)
    robotstxt_blocked = any(
        c.stats.get_value("downloader/response_count", 0)
        == c.stats.get_value("robotstxt/response_count", 0)
        and config.network.respect_robots_txt
        for c in crawlers
    )

    console.print("[yellow]Warning: Scraped 0 items.[/yellow]")
    if multi:
        for url, c in zip(urls, crawlers):
            host = urllib.parse.urlparse(url).netloc or url
            status = _diagnose_crawler(c, config)
            console.print(f"  {host}: {status}")

    if timed_out:
        console.print(
            f"  Timed out after {config.scrape.bail}s with no items. "
            "Use [bold]--bail 0[/bold] to disable."
        )
    elif robotstxt_blocked and response_count <= len(crawlers):
        console.print(
            "  Likely blocked by robots.txt. "
            "Retry with [bold]--ignore-robots[/bold] (-i) to bypass."
        )
    elif http_403 > 0:
        console.print(
            f"  Received {http_403} HTTP 403 (Forbidden) response(s). "
            "The server is actively blocking scrapers."
        )
        if config.network.respect_robots_txt:
            console.print("  Try [bold]--ignore-robots[/bold] (-i) to bypass robots.txt.")
    elif http_404 > 0:
        console.print(
            f"  Received {http_404} HTTP 404 (Not Found) response(s). "
            "Check the URL and --platform flag."
        )
    elif response_count == 0:
        console.print("  No responses received. Check the URL and your network connection.")
    else:
        console.print("  The store may be empty or the endpoint returned no products.")

    raise typer.Exit(1)


def _diagnose_crawler(crawler: object, config: Config) -> str:
    """Return a short status string for a single crawler."""
    items = crawler.stats.get_value("item_scraped_count", 0)
    if items > 0:
        return f"{items} items"
    if crawler.stats.get_value("finish_reason", "") == "bail":
        return "timed out"
    h403 = crawler.stats.get_value("downloader/response_status_count/403", 0)
    if h403 > 0:
        return "403 Forbidden"
    h404 = crawler.stats.get_value("downloader/response_status_count/404", 0)
    if h404 > 0:
        return "404 Not Found"
    resp = crawler.stats.get_value("downloader/response_count", 0)
    robots = crawler.stats.get_value("robotstxt/response_count", 0)
    if resp > 0 and resp == robots and config.network.respect_robots_txt:
        return "blocked by robots.txt"
    if resp == 0:
        return "no response"
    return "0 items"


if __name__ == "__main__":
    app()
