"""Command-line interface for Shopify Spy, built on the public library API."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import platformdirs
import typer
from rich.console import Console
from rich.markup import escape

from shopify_spy._api import AsyncScrapeRun, scrape_async
from shopify_spy._config import (
    Config,
    OutputFormat,
    Platform,
    create_default_config,
    load_config,
)
from shopify_spy._export import EXTENSIONS, ItemWriter, write_status
from shopify_spy._fetch import FetcherFactory
from shopify_spy._results import _PLATFORM_NAMES, RunResult, StoreResult, StoreStatus
from shopify_spy._utils import normalize_url, read_url_file, split_valid_urls

# Shown under --help so agents find the Python API, not just the CLI
LIBRARY_EPILOG = (
    "Also a Python library: [bold]import shopify_spy as spy[/bold]; "
    "spy.scrape(urls) yields items as dicts (options: help(spy.scrape)). "
    "Run a script without installing anything: "
    "[bold]uv run --with 'shopify-spy>=0.3' python script.py[/bold]"
)

app = typer.Typer(
    name="shopify-spy",
    help="Scrape product and collection data from Shopify and WooCommerce stores.",
    epilog=LIBRARY_EPILOG,
    no_args_is_help=True,
)
console = Console()
# Warnings go to stderr so they don't mix with --peek's JSONL on stdout
err_console = Console(stderr=True)
logger = logging.getLogger("shopify_spy")

# Test hook: a transport factory to use instead of HTTP
FETCHER_FACTORY: FetcherFactory | None = None


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
    """Shopify Spy - Scrape product data from Shopify and WooCommerce stores."""


@app.command(epilog=LIBRARY_EPILOG)
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
            help="File containing URLs to scrape (one per line; # starts a comment).",
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
        typer.Option("--concurrent", help="Concurrent requests per host."),
    ] = None,
    max_stores: Annotated[
        int | None,
        typer.Option("--max-stores", help="Stores scraped at once (default 16)."),
    ] = None,
    throttle: Annotated[
        bool | None,
        typer.Option("--throttle/--no-throttle", help="Auto-throttle requests."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            help="Stop each store after N items (per store). Useful for sampling or testing.",
        ),
    ] = None,
    user_agent: Annotated[
        str | None,
        typer.Option("--user-agent", "-A", help="Custom User-Agent header."),
    ] = None,
    platform: Annotated[
        Platform | None,
        typer.Option(
            "--platform",
            "-p",
            help="Ecommerce platform: shopify, woocommerce (default: shopify, or the config's).",
        ),
    ] = None,
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
    insecure: Annotated[
        bool,
        typer.Option("--insecure", help="Don't verify TLS certificates."),
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
        insecure=insecure,
        max_stores=max_stores,
    )

    # --peek prints one item in total, logs quietly and downloads no images
    if peek:
        scrape_update = {"limit": 1, "images": False}
        config = config.model_copy(
            update={"scrape": config.scrape.model_copy(update=scrape_update)}
        )
        quiet = True

    # Validate flag combinations
    if verbose and quiet:
        err_console.print("[red]Error: Cannot use both --verbose and --quiet[/red]")
        raise typer.Exit(1)

    # Warn about Shopify-only flags used with WooCommerce
    if config.scrape.platform == Platform.woocommerce:
        if collections is True:
            err_console.print(
                "[yellow]Warning: --collections has no effect with WooCommerce "
                "(collections are not supported).[/yellow]"
            )
        if products is False:
            err_console.print(
                "[yellow]Warning: --no-products has no effect with WooCommerce "
                "(WooCommerce always scrapes products).[/yellow]"
            )

    # Get URLs
    all_urls = get_urls(urls, url_file)
    if not all_urls:
        err_console.print("[red]Error: No URLs provided.[/red]")
        err_console.print("Provide a URL argument, --url-file, or run interactively.")
        raise typer.Exit(1)

    # Skip malformed URLs and record them in the status file
    valid_urls, invalid_urls = split_valid_urls(all_urls)
    for u in invalid_urls:
        err_console.print(f"[yellow]Warning: Skipping invalid URL: {escape(u)}[/yellow]")
    if not valid_urls:
        err_console.print("[red]Error: No valid URLs provided.[/red]")
        raise typer.Exit(1)

    run_scrape(
        valid_urls, config, peek=peek, verbose=verbose, quiet=quiet, skipped_urls=invalid_urls
    )


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
    insecure: bool = False,
    max_stores: int | None = None,
) -> Config:
    """Apply CLI argument overrides to config. ``None`` keeps the config value."""
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
    if max_stores is not None:
        network_dict["max_concurrent_stores"] = max_stores
    if user_agent is not None:
        network_dict["user_agent"] = user_agent
    if ignore_robots:
        network_dict["respect_robots_txt"] = False
    if insecure:
        network_dict["verify_tls"] = False
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
    # Blank arguments (e.g. an empty shell variable) fall through to --url-file
    args = [u.strip() for u in urls or [] if u.strip()]
    if args:
        return args

    if url_file:
        return read_url_file(url_file)

    # Interactive prompt if stdin is a TTY
    if sys.stdin.isatty():
        url = typer.prompt("Enter Shopify store URL").strip()
        if url:
            return [url]

    return []


def _log_dir() -> Path:
    """Return the platform-appropriate log directory, creating it if needed."""
    path = platformdirs.user_state_path("shopify-spy") / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _setup_logging(log_file: Path | None, verbose: bool, quiet: bool) -> list[logging.Handler]:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    handlers: list[logging.Handler] = []
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if verbose or log_file is None:
        # --verbose also logs to stderr; without a log file (--peek) warnings go there
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setLevel(level)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handlers


def _restore_logging(handlers: list[logging.Handler], level: int, propagate: bool) -> None:
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = propagate


def run_scrape(
    urls: list[str],
    config: Config,
    peek: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    skipped_urls: list[str] | None = None,
) -> None:
    """Scrape the stores, write output files, and print a summary.

    skipped_urls are invalid inputs the caller dropped; they are recorded in the status file.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S+00-00")
    name = f"{config.scrape.platform.value}_spider"

    output_dir = config.output.dir.expanduser().resolve()
    log_file: Path | None = None
    writer: ItemWriter | None = None
    if not peek:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / config.output.images_subdir).mkdir(parents=True, exist_ok=True)
        log_file = _log_dir() / f"{name}_{timestamp}.log"
        ext = EXTENSIONS[config.output.format]
        writer = ItemWriter(
            output_dir / "data" / f"{name}_{timestamp}{ext}",
            config.output.format,
        )

    kwargs: dict[str, Any] = config.to_kwargs()
    kwargs["on_failure"] = "ignore"
    if FETCHER_FACTORY is not None:
        kwargs["fetcher_factory"] = FETCHER_FACTORY

    if not peek:
        console.print(f"[bold]Scraping {len(urls)} store(s)...[/bold]")
        console.print(f"  Platform: {config.scrape.platform.value}")
        if config.scrape.platform == Platform.shopify:
            console.print(f"  Products: {config.scrape.products}")
            console.print(f"  Collections: {config.scrape.collections}")
        console.print(f"  Images: {config.scrape.images}")
        console.print(f"  Format: {config.output.format}")
        console.print(f"  Output: {output_dir}")

    saved = (logger.level, logger.propagate)
    handlers = _setup_logging(log_file, verbose, quiet)
    show_counter = not peek and not quiet
    try:
        result, forced = asyncio.run(
            _scrape(urls, kwargs, writer=writer, peek=peek, show_counter=show_counter)
        )
    finally:
        _restore_logging(handlers, *saved)

    if show_counter and result.total_items > 0:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    stores = result.stores
    total = result.total_items
    multi = len(urls) > 1
    failures = sum(s.status == StoreStatus.error for s in stores)

    if not peek:
        finish_reason = "shutdown" if forced else result.finish_reason
        write_status(
            output_dir / "status" / f"{name}_{timestamp}_status.json",
            stores,
            finish_reason=finish_reason,
            duration=result.duration,
            log_file=str(log_file) if log_file else None,
            skipped_urls=skipped_urls,
        )

    if total > 0:
        if not peek:
            if show_counter:
                console.print("[green]Done![/green]")
            else:
                console.print(f"[green]Done! Scraped {total} item(s).[/green]")
            if log_file:
                console.print(f"  Log: {log_file}")
            if multi:
                for s in stores:
                    console.print(f"  {escape(_host(s.input_url))}: {_display_status(s)}")
        if failures:
            (err_console if peek else console).print(
                f"[yellow]Warning: {failures} store(s) failed with an error.[/yellow]"
            )
        return

    # Diagnose why nothing was scraped; --peek reserves stdout for JSONL
    out = err_console if peek else console
    out.print("[yellow]Warning: Scraped 0 items.[/yellow]")
    if multi:
        for s in stores:
            out.print(f"  {escape(_host(s.input_url))}: {_display_status(s)}")
    for hint in summary_hints(stores, config):
        out.print(f"  {hint}")
    if log_file:
        out.print(f"  Log: {log_file}")
    raise typer.Exit(1)


async def _scrape(
    urls: list[str],
    kwargs: dict[str, Any],
    *,
    writer: ItemWriter | None,
    peek: bool,
    show_counter: bool,
) -> tuple[RunResult, bool]:
    """Run the scrape; first Ctrl-C stops gracefully, a second one forces it."""
    run: AsyncScrapeRun = scrape_async(urls, **kwargs)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    interrupts = 0

    def on_sigint(signum: int, frame: object) -> None:
        nonlocal interrupts
        interrupts += 1
        if interrupts == 1:
            err_console.print(
                "\n[yellow]Stopping: finishing in-flight requests "
                "(Ctrl-C again to force)...[/yellow]"
            )
            loop.call_soon_threadsafe(run.request_shutdown)
        elif task is not None:
            loop.call_soon_threadsafe(task.cancel)

    previous = signal.signal(signal.SIGINT, on_sigint)
    forced = False
    count = 0
    last_draw = 0.0
    try:
        async with run:
            async for item in run:
                if peek:
                    print(json.dumps(item, ensure_ascii=False), flush=True)
                    break
                assert writer is not None
                writer.write(item)
                count += 1
                # Redraw at most ten times a second; printing every item is slow
                if show_counter and (now := time.monotonic()) - last_draw >= 0.1:
                    last_draw = now
                    print(f"\r  Scraped {count} items...", end="", flush=True)
    except asyncio.CancelledError:
        forced = True
        if task is not None:
            task.uncancel()
    finally:
        signal.signal(signal.SIGINT, previous)
        if writer is not None:
            writer.close()
    if show_counter and count:  # the throttled redraw can skip the final count
        print(f"\r  Scraped {count} items...", end="", flush=True)
    return run.result, forced


def _host(url: str) -> str:
    """Return the URL's host for display, or the URL itself if it is malformed."""
    try:
        return normalize_url(url).netloc or url
    except ValueError:
        return url


def _display_status(store: StoreResult) -> str:
    """Per-store summary text: the item count, or why there were none."""
    if store.status == StoreStatus.error:
        return "failed"
    if store.items > 0:
        return f"{store.items} items"
    return store.status.label


def summary_hints(stores: list[StoreResult], config: Config) -> list[str]:
    """Return one hint per distinct zero-item status, in diagnosis order."""

    def total(status: StoreStatus, code: int) -> int:
        return sum(s.responses_by_status.get(code, 0) for s in stores if s.status == status)

    present = {s.status for s in stores}
    hints = []
    if StoreStatus.error in present:
        n = sum(s.status == StoreStatus.error for s in stores)
        hints.append(f"{n} store(s) failed with an error.")
    if StoreStatus.forbidden in present:
        hints.append(
            f"Received {total(StoreStatus.forbidden, 403)} HTTP 403 (Forbidden) response(s). "
            "The server is actively blocking scrapers."
        )
    if StoreStatus.not_found in present:
        hints.append(
            f"Received {total(StoreStatus.not_found, 404)} HTTP 404 (Not Found) response(s). "
            "Check the URL and --platform flag."
        )
    if StoreStatus.robots_blocked in present:
        hints.append(
            "Likely blocked by robots.txt. Retry with [bold]--ignore-robots[/bold] (-i) to bypass."
        )
    if StoreStatus.timed_out in present:
        hints.append(
            f"Timed out after {config.scrape.bail}s with no items. "
            "Use [bold]--bail 0[/bold] to disable."
        )
    if StoreStatus.tls_error in present:
        hints.append(
            "TLS certificate verification failed. "
            "Retry with [bold]--insecure[/bold] to skip verification."
        )
    if StoreStatus.no_response in present:
        hints.append("No responses received. Check the URL and your network connection.")
    if StoreStatus.empty in present:
        hints.append("The store may be empty or the endpoint returned no products.")
    if StoreStatus.stopped in present:
        hints.append("Stopped before every store finished.")
    for s in stores:
        if s.suggested_platform:
            name = _PLATFORM_NAMES.get(s.suggested_platform, s.suggested_platform)
            hints.append(
                f"{escape(_host(s.input_url))} looks like {name}; "
                f"use --platform {s.suggested_platform}."
            )
    return hints


if __name__ == "__main__":
    app()
