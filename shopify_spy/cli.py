"""Command-line interface for Shopify Spy."""

import sys
from pathlib import Path
from typing import Annotated

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
    )

    # Get URLs
    all_urls = get_urls(urls, url_file)
    if not all_urls:
        console.print("[red]Error: No URLs provided.[/red]")
        console.print("Provide a URL argument, --url-file, or run interactively.")
        raise typer.Exit(1)

    # Determine log level
    if verbose and quiet:
        console.print("[red]Error: Cannot use both --verbose and --quiet[/red]")
        raise typer.Exit(1)
    log_level = "DEBUG" if verbose else "WARNING" if quiet else None

    # Run the spider
    run_spider(all_urls, config, log_level=log_level)


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
    if output is not None:
        output_dict["dir"] = output
    if format is not None:
        output_dict["format"] = format
    if concurrent is not None:
        network_dict["concurrent_requests"] = concurrent
    if user_agent is not None:
        network_dict["user_agent"] = user_agent
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


def run_spider(
    urls: list[str],
    config: Config,
    log_level: str | None = None,
) -> None:
    """Run the appropriate spider with the given configuration."""
    # Deferred imports to avoid loading Scrapy until needed
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

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

    settings = get_project_settings()

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
    settings.set("IMAGES_STORE", str(images_dir))
    scrapy_format, file_ext = OUTPUT_FORMATS[config.output.format]
    settings.set(
        "FEEDS",
        {
            f"{output_dir.as_uri()}/%(name)s_%(time)s{file_ext}": {
                "format": scrapy_format,
                "encoding": "utf8",
                "store_empty": False,
                "item_export_kwargs": {"export_empty_fields": True},
            }
        },
    )

    if config.network.user_agent:
        settings.set("USER_AGENT", config.network.user_agent)

    if log_level:
        settings.set("LOG_LEVEL", log_level)

    # Configure auto-throttle
    if config.throttle.enabled:
        settings.set("AUTOTHROTTLE_ENABLED", True)
        settings.set("AUTOTHROTTLE_START_DELAY", config.throttle.start_delay)
        settings.set("AUTOTHROTTLE_MAX_DELAY", config.throttle.max_delay)
        settings.set("AUTOTHROTTLE_TARGET_CONCURRENCY", config.throttle.target_concurrency)

    # Disable image pipeline if images are disabled
    if not config.scrape.images:
        settings.set("ITEM_PIPELINES", {})

    console.print(f"[bold]Scraping {len(urls)} store(s)...[/bold]")
    console.print(f"  Platform: {config.scrape.platform.value}")
    if config.scrape.platform == Platform.shopify:
        console.print(f"  Products: {config.scrape.products}")
        console.print(f"  Collections: {config.scrape.collections}")
    console.print(f"  Images: {config.scrape.images}")
    console.print(f"  Format: {config.output.format}")
    console.print(f"  Output: {output_dir}")

    process = CrawlerProcess(settings)

    # Crawl each URL
    for store_url in urls:
        process.crawl(spider_cls, url=store_url, **spider_kwargs)

    process.start()
    console.print("[green]Done![/green]")


if __name__ == "__main__":
    app()
