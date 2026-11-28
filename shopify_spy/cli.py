"""Command-line interface for Shopify Spy, built on the public library API."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import signal
import sys
import time
from collections.abc import Callable, Container
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import platformdirs
import typer
from rich.console import Console
from rich.markup import escape
from typer.core import MarkupMode, TyperCommand, TyperGroup

from shopify_spy._api import DOCS_URL, AsyncScrapeRun, scrape_async
from shopify_spy._config import (
    Config,
    OutputFormat,
    OutputSchema,
    Platform,
    create_default_config,
    load_config,
)
from shopify_spy._export import (
    EXTENSIONS,
    UNIFIED_ONLY_FORMATS,
    ItemWriter,
    schema_for,
    unified_records,
    write_status,
)
from shopify_spy._fetch import (
    DEFAULT_IMPERSONATE,
    IMPERSONATE_PROFILES,
    FetcherFactory,
    ca_locations,
    check_proxy_support,
    validate_impersonate,
    validate_proxy,
)
from shopify_spy._flatten import default_columns
from shopify_spy._results import _PLATFORM_NAMES, RunResult, StoreResult, StoreStatus
from shopify_spy._utils import blank_to_none, normalize_url, read_url_file, split_valid_urls
from shopify_spy._xlsx import require_xlsxwriter

if TYPE_CHECKING:
    # Typer 0.27 vendors Click as typer._click and no longer installs it
    import click

# Shown under --help so agents find the Python API, not just the CLI. Help text is
# plain: Rich mode escapes it, so it reads the same in both renderers.
LIBRARY_EPILOG = (
    "Also a Python library: import shopify_spy as spy; "
    "spy.scrape(urls) yields items as dicts (options: help(spy.scrape)). "
    "Run a script without installing anything: "
    "uv run --with 'shopify-spy>=0.5' python script.py"
)
# Whole pages, not headings: installed help keeps these links for good
DOCS_EPILOG = (
    f"Docs: {DOCS_URL}/cli.md (options, config, proxies), "
    f"{DOCS_URL}/output.md (files and item shapes), "
    f"{DOCS_URL}/troubleshooting.md (store statuses and exit codes), "
    f"{DOCS_URL}/library.md (the Python API)."
)

# Wide enough that piped help never wraps; an agent reads whole lines better than a
# 78-column reflow
_PLAIN_HELP_WIDTH = 10_000
CONTEXT_SETTINGS: dict[str, Any] = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": _PLAIN_HELP_WIDTH,
    "terminal_width": _PLAIN_HELP_WIDTH,
}

SCRAPE_PANEL = "What to scrape"
OUTPUT_PANEL = "Output"
NETWORK_PANEL = "Network"
RUN_PANEL = "Run control"


def use_rich_help() -> bool:
    """Whether help and usage errors render with Rich: only when stdout is a terminal.

    Decided at render time, not import. Typer forces Rich when FORCE_COLOR, PY_COLORS
    or GITHUB_ACTIONS is set, which gives CI and agent harnesses boxed help truncated
    at 80 columns; this ignores them. Tests patch it to force either mode.
    """
    return sys.stdout.isatty()


def _escaped(obj: Any, *attrs: str) -> Any:
    shown = copy.copy(obj)
    for attr in attrs:
        if getattr(shown, attr, None):
            setattr(shown, attr, escape(getattr(shown, attr)))
    return shown


class _AdaptiveHelp:
    """Rich help in a terminal, plain Click help otherwise (see ``use_rich_help``)."""

    @property
    def rich_markup_mode(self) -> MarkupMode:
        # TYPER_USE_RICH=0 clears HAS_RICH and Typer then renders plain Click help,
        # which must not get the Rich-escaped text
        return "rich" if typer.core.HAS_RICH and use_rich_help() else None

    @rich_markup_mode.setter
    def rich_markup_mode(self, value: MarkupMode) -> None:
        """Ignored: Typer sets the mode at construction; it's decided per render."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)  # type: ignore[misc]
        # Rich would read shopify-spy[excel] as a style tag and drop it
        shown = _escaped(self, "help", "epilog")
        shown.params = [_escaped(param, "help") for param in shown.params]
        return super(_AdaptiveHelp, shown).format_help(ctx, formatter)


class _HelpGroup(_AdaptiveHelp, TyperGroup):
    pass


class _HelpCommand(_AdaptiveHelp, TyperCommand):
    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """List options under their Rich panel names, in the order Rich shows them."""
        sections: dict[str, list[tuple[str, str]]] = {"Arguments": [], "Options": []}
        for param in self.get_params(ctx):
            record = param.get_help_record(ctx)
            if record is None:
                continue
            # not isinstance(click.Argument): Typer 0.27 has its own vendored Click
            default = "Arguments" if param.param_type_name == "argument" else "Options"
            panel = getattr(param, "rich_help_panel", None) or default
            sections.setdefault(panel, []).append(record)
        for name, records in sections.items():
            if records:
                with formatter.section(name):
                    formatter.write_dl(records)


app = typer.Typer(
    name="shopify-spy",
    cls=_HelpGroup,
    help=(
        "Scrape product and collection data from Shopify, WooCommerce, Magento, "
        "BigCommerce and other stores."
    ),
    epilog=f"{LIBRARY_EPILOG}\n\n{DOCS_EPILOG}",
    no_args_is_help=True,
    context_settings=CONTEXT_SETTINGS,
)
console = Console()
# Warnings go to stderr so they don't mix with --peek's JSONL on stdout
err_console = Console(stderr=True)
logger = logging.getLogger("shopify_spy")

# Test hook: a transport factory to use instead of HTTP
FETCHER_FACTORY: FetcherFactory | None = None

# Help shows the built-in defaults; CLI options default to None so a config can
# override them
_DEFAULTS = Config()


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _dir_text(path: Path) -> str:
    text = path.as_posix()
    return text if path.is_absolute() or text.startswith((".", "~")) else f"./{text}"


_OUTPUT_DIR = _dir_text(_DEFAULTS.output.dir)
_IMAGES_DIR = f"<output>/{_DEFAULTS.output.images_subdir}/full/"

SCRAPE_EPILOG = "\n\n".join(
    [
        "Examples:",
        "spy scrape --peek https://example.com  (print one item as JSONL to learn its "
        "shape; writes nothing)",
        f"spy scrape --limit 10 https://example.com  (10 items into {_OUTPUT_DIR}/data/)",
        "spy scrape --output ./shops --format csv https://a.com https://b.com  "
        "(every item of two stores, as CSV)",
        "Results: items go to <output>/data/; each run also writes a status file (every "
        f"store's status and item count) to <output>/status/, and images to {_IMAGES_DIR}. "
        "--peek writes nothing.",
        "Exit status: 0 when any store produced items (failed stores are listed); 1 when "
        "none did, the output couldn't be written, or the input was invalid (no or "
        "invalid URLs, a bad --proxy or --impersonate, --verbose with --quiet, a missing "
        "excel extra, --impersonate with --user-agent); 2 "
        "for a usage error such as an unknown option.",
        "Config: without --config, ./shopify-spy.yaml or else "
        "~/.config/shopify-spy/config.yaml is read if it exists, and its values replace "
        "the defaults shown above.",
        LIBRARY_EPILOG,
        DOCS_EPILOG,
    ]
)


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
    """Shopify Spy - Scrape product data from Shopify, WooCommerce, Magento, BigCommerce
    and other stores."""


@app.command(cls=_HelpCommand, epilog=SCRAPE_EPILOG)
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
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    platform: Annotated[
        Platform | None,
        typer.Option(
            "--platform",
            "-p",
            help="The store's platform "
            f"(default: {_DEFAULTS.scrape.platform.value}). shopify, woocommerce, magento and "
            "bigcommerce read the platform's own API. squarespace reads a Squarespace store's "
            "product pages, or with --ignore-robots its collections' ?format=json data. "
            "generic is for any other store (Wix, custom sites): it reads schema.org product "
            "data (JSON-LD or microdata) from each product page the sitemaps list, one request "
            "per product, so it's slow; use --limit for a sample. A store that fails because it "
            "looks like Shopify, WooCommerce or Magento gets a hint naming that platform (in the "
            "end-of-run summary when nothing was scraped, and in the library's StoreResult.hint).",
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    products: Annotated[
        bool | None,
        typer.Option(
            "--products/--no-products",
            help="Scrape products. Shopify only; WooCommerce, Magento, BigCommerce, generic and "
            "Squarespace always do "
            f"(default: {_on_off(_DEFAULTS.scrape.products)}).",
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    collections: Annotated[
        bool | None,
        typer.Option(
            "--collections/--no-collections",
            help="Scrape collections. Shopify only "
            f"(default: {_on_off(_DEFAULTS.scrape.collections)}).",
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    images: Annotated[
        bool | None,
        typer.Option(
            "--images/--no-images",
            help=f"Download product images into {_IMAGES_DIR} "
            f"(default: {_on_off(_DEFAULTS.scrape.images)}). Downloads from one host are "
            "spaced by --image-delay.",
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            help="Stop each store after N items (per store). Useful for sampling or testing "
            "(default: no limit).",
            rich_help_panel=SCRAPE_PANEL,
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help=f"Output directory for results (default: {_OUTPUT_DIR}).",
            rich_help_panel=OUTPUT_PANEL,
        ),
    ] = None,
    format: Annotated[
        OutputFormat | None,
        typer.Option(
            "--format",
            "-F",
            help="Output format: json, jsonl, csv, xlsx or xml "
            f"(default: {_DEFAULTS.output.format}). xlsx needs the shopify-spy[excel] extra.",
            rich_help_panel=OUTPUT_PANEL,
        ),
    ] = None,
    schema: Annotated[
        OutputSchema | None,
        typer.Option(
            "--schema",
            help="Item shape (default: "
            f"{schema_for(_DEFAULTS.output.format, _DEFAULTS.output.schema_)}): native (as "
            "scraped) or unified (a row per variant, same columns on every platform). "
            "CSV and XLSX are always unified.",
            rich_help_panel=OUTPUT_PANEL,
        ),
    ] = None,
    peek: Annotated[
        bool,
        typer.Option(
            "--peek",
            help="Print 1 item to stdout as JSONL (its rows with --schema unified) and exit. "
            "No file output.",
            rich_help_panel=OUTPUT_PANEL,
        ),
    ] = False,
    concurrent: Annotated[
        int | None,
        typer.Option(
            "--concurrent",
            help="Most requests in flight to one host at once "
            f"(default: {_DEFAULTS.network.concurrent_requests}). While throttling, "
            "--min-delay spaces requests to one host, so they go out about one at a time; "
            "this caps how many can be in flight when responses are slow or with "
            "--no-throttle.",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    max_stores: Annotated[
        int | None,
        typer.Option(
            "--max-stores",
            help=f"Stores scraped at once (default: {_DEFAULTS.network.max_concurrent_stores}).",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    throttle: Annotated[
        bool | None,
        typer.Option(
            "--throttle/--no-throttle",
            help="Auto-throttle requests; --no-throttle drops the adaptive delay, "
            "--min-delay and --image-delay, but robots.txt Crawl-delay and Retry-After "
            "pauses still apply "
            f"(default: {_on_off(_DEFAULTS.throttle.enabled)}).",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    min_delay: Annotated[
        float | None,
        typer.Option(
            "--min-delay",
            min=0.0,
            metavar="SECONDS",
            help="Minimum seconds between page and API requests to one host while "
            "throttling; 1 means at most 1 request per second "
            f"(default: {_DEFAULTS.throttle.min_delay:g}).",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    image_delay: Annotated[
        float | None,
        typer.Option(
            "--image-delay",
            min=0.0,
            metavar="SECONDS",
            help="Minimum seconds between image downloads from one host while "
            "throttling; 0.25 means at most 4 per second "
            f"(default: {_DEFAULTS.throttle.image_delay:g}).",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    user_agent: Annotated[
        str | None,
        typer.Option(
            "--user-agent",
            "-A",
            help="Fixed User-Agent header, never swapped; turns impersonation off "
            "(requests go through httpx). Can't be combined with --impersonate.",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    impersonate: Annotated[
        str | None,
        typer.Option(
            "--impersonate",
            "-I",
            metavar="BROWSER",
            help="Browser profile to start with: requests use that browser's TLS and "
            "HTTP/2 settings and headers, and a 403 or 429 switches the store to the next "
            f"of {', '.join(IMPERSONATE_PROFILES)} (default: {DEFAULT_IMPERSONATE}). Any "
            "curl_cffi target, e.g. chrome150.",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    rotate: Annotated[
        bool | None,
        typer.Option(
            "--rotate/--no-rotate",
            help="After a 403 or 429, retry with another browser profile, once per "
            "refusal and only after the usual backoff; "
            "--no-rotate keeps one identity for the whole run "
            f"(default: {_on_off(_DEFAULTS.network.rotate)}).",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    proxy: Annotated[
        str | None,
        typer.Option(
            "--proxy",
            help="Proxy URL for every request (http://, https:// or socks5://); "
            "overrides HTTPS_PROXY/HTTP_PROXY.",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure", help="Don't verify TLS certificates.", rich_help_panel=NETWORK_PANEL
        ),
    ] = False,
    ignore_robots: Annotated[
        bool,
        typer.Option(
            "--ignore-robots",
            "-i",
            help="Ignore robots.txt restrictions.",
            rich_help_panel=NETWORK_PANEL,
        ),
    ] = False,
    bail: Annotated[
        int | None,
        typer.Option(
            "--bail",
            help=(
                "Abort a store with no items within N seconds, not counting Retry-After "
                "waits, so a rate-limited store can take minutes "
                f"(default: {_DEFAULTS.scrape.bail}; 0 = off)."
            ),
            rich_help_panel=RUN_PANEL,
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to YAML configuration file.",
            exists=True,
            dir_okay=False,
            rich_help_panel=RUN_PANEL,
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show debug output.", rich_help_panel=RUN_PANEL),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet", "-q", help="Show only warnings and errors.", rich_help_panel=RUN_PANEL
        ),
    ] = False,
) -> None:
    """Scrape products and collections from Shopify, WooCommerce, Magento, BigCommerce
    and other stores."""
    # Load config file (or defaults)
    config = load_config(config_path)

    # Checked before the overrides so a bad --proxy is a clean error, not a
    # ValidationError traceback from Config()
    if proxy is not None:
        _exit_on_invalid(lambda: validate_proxy(proxy))

    # Apply CLI overrides
    config = apply_cli_overrides(
        config,
        platform=platform,
        products=products,
        collections=collections,
        images=images,
        output=output,
        format=format,
        schema=schema,
        concurrent=concurrent,
        throttle=throttle,
        min_delay=min_delay,
        image_delay=image_delay,
        limit=limit,
        user_agent=user_agent,
        impersonate=impersonate,
        rotate=rotate,
        ignore_robots=ignore_robots,
        bail=bail,
        insecure=insecure,
        max_stores=max_stores,
        proxy=proxy,
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
    _exit_on_invalid(lambda: _check_options(config, peek))

    # Warn about Shopify-only flags used with another platform
    if config.scrape.platform != Platform.shopify:
        name = _PLATFORM_NAMES[config.scrape.platform.value]
        if collections is True:
            err_console.print(
                f"[yellow]Warning: --collections has no effect with {name} "
                "(collections are not supported).[/yellow]"
            )
        if products is False:
            err_console.print(
                f"[yellow]Warning: --no-products has no effect with {name} "
                f"({name} always scrapes products).[/yellow]"
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


def _exit_on_invalid(check: Callable[[], object]) -> None:
    """Run ``check``; print its ValueError or missing module as an error and exit 1."""
    try:
        check()
    except (ValueError, ModuleNotFoundError) as exc:
        err_console.print(f"[red]Error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None


def _check_options(config: Config, peek: bool) -> None:
    schema_for(config.output.format, config.output.schema_)
    if config.output.format == "xlsx" and not peek:
        require_xlsxwriter()
    if config.network.impersonate is not None:
        _check_impersonate(config.network.impersonate, config.network.user_agent)
    if FETCHER_FACTORY is None:
        check_proxy_support(config.network.proxy)
        if config.network.verify_tls:
            ca_locations()  # a bad CA variable fails here, not once the run is under way


def _check_impersonate(target: str, user_agent: str | None) -> None:
    if user_agent is not None:
        raise ValueError(
            "--impersonate sets the user agent from the browser profile, so it can't be "
            "combined with --user-agent, which turns impersonation off; drop one (or, in "
            "the config file, network.impersonate or network.user_agent)"
        )
    # A test transport ignores the profile, as the library does with fetcher_factory
    if FETCHER_FACTORY is None:
        validate_impersonate(target)


@app.command(cls=_HelpCommand)
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


def _given(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


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
    schema: OutputSchema | None = None,
    throttle: bool | None,
    limit: int | None,
    user_agent: str | None,
    impersonate: str | None = None,
    rotate: bool | None = None,
    ignore_robots: bool = False,
    bail: int | None = None,
    insecure: bool = False,
    max_stores: int | None = None,
    proxy: str | None = None,
    min_delay: float | None = None,
    image_delay: float | None = None,
) -> Config:
    """Apply CLI argument overrides to config. ``None`` keeps the config value."""
    user_agent = blank_to_none(user_agent)
    scrape_dict = config.scrape.model_dump() | _given(
        platform=platform,
        products=products,
        collections=collections,
        images=images,
        limit=limit,
        bail=bail,
    )
    output_dict = config.output.model_dump() | _given(dir=output, format=format, schema_=schema)
    if schema is None and format in UNIFIED_ONLY_FORMATS and output_dict["schema_"] == "native":
        # A config's native schema (meant for JSONL, say) doesn't block -F csv
        output_dict["schema_"] = None
    network_dict = config.network.model_dump() | _given(
        concurrent_requests=concurrent,
        max_concurrent_stores=max_stores,
        user_agent=user_agent,
        proxy=proxy,
        impersonate=impersonate,
        rotate=rotate,
    )
    # The two pick the transport, so one given here replaces the config file's other
    if user_agent is not None and impersonate is None:
        network_dict["impersonate"] = None
    if impersonate is not None and user_agent is None:
        network_dict["user_agent"] = None
    if ignore_robots:
        network_dict["respect_robots_txt"] = False
    if insecure:
        network_dict["verify_tls"] = False
    throttle_dict = config.throttle.model_dump() | _given(
        enabled=throttle, min_delay=min_delay, image_delay=image_delay
    )

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
            schema=config.output.schema_,
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
        console.print(f"  Schema: {schema_for(config.output.format, config.output.schema_)}")
        console.print(f"  Output: {output_dir}")

    saved = (logger.level, logger.propagate)
    handlers = _setup_logging(log_file, verbose, quiet)
    show_counter = not peek and not quiet
    close_error: Exception | None = None
    try:
        result, forced, write_error = asyncio.run(
            _scrape(
                urls,
                kwargs,
                writer=writer,
                peek=peek,
                peek_rows=config.output.schema_ == "unified",
                show_counter=show_counter,
            )
        )
    finally:
        if writer is not None:
            close_error = _close_writer(writer)
        _restore_logging(handlers, *saved)
    output_error = _output_error(write_error, close_error)

    if show_counter and result.total_items > 0:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    stores = result.stores
    total = result.total_items
    multi = len(urls) > 1
    failures = sum(s.status == StoreStatus.error for s in stores)

    if not peek:
        # A failed write stops the run early, like Ctrl-C
        finish_reason = "shutdown" if forced or write_error else result.finish_reason
        write_status(
            output_dir / "status" / f"{name}_{timestamp}_status.json",
            stores,
            finish_reason=finish_reason,
            duration=result.duration,
            log_file=str(log_file) if log_file else None,
            skipped_urls=skipped_urls,
            output_error=output_error,
            items_written=writer.count,
        )

    if output_error is not None:
        assert writer is not None
        err_console.print(
            f"[red]Error: Could not write {escape(str(writer.path))}: {escape(output_error)}[/red]"
        )
        if close_error is None and writer.count:
            err_console.print(f"  The {writer.count} item(s) written before it are kept.")
        if log_file:
            err_console.print(f"  Log: {log_file}")
        raise typer.Exit(1)

    if total > 0:
        if not peek:
            _print_done(total, stores, show_counter=show_counter, multi=multi, log_file=log_file)
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
    peek_rows: bool = False,
) -> tuple[RunResult, bool, Exception | None]:
    """Run the scrape; first Ctrl-C stops gracefully, a second one forces it.

    Returns the result, whether the stop was forced, and the error that stopped the
    run if an item couldn't be written (there's no point scraping what can't be saved).
    """
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
    write_error: Exception | None = None
    count = 0
    last_draw = 0.0
    try:
        async with run:
            async for item in run:
                if peek:
                    columns = default_columns(item) if peek_rows else None
                    for record in unified_records(item, columns) if columns else [item]:
                        # default=str: unified rows have Decimal prices
                        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
                    break
                assert writer is not None
                try:
                    writer.write(item)
                except Exception as e:
                    logger.exception("Could not write %s; stopping", writer.path)
                    write_error = e
                    break
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
    if show_counter and count:  # the throttled redraw can skip the final count
        print(f"\r  Scraped {count} items...", end="", flush=True)
    return run.result, forced, write_error


def _output_error(write_error: Exception | None, close_error: Exception | None) -> str | None:
    """Why the data file couldn't be written, naming both errors when there were two."""
    if write_error and close_error:
        return f"{write_error}; then closing the file failed: {close_error}"
    error = write_error or close_error
    return None if error is None else str(error)


def _close_writer(writer: ItemWriter) -> Exception | None:
    """Close the output file, returning the error instead of raising it so the status
    file is still written. XLSX is written here, which can take a while on big runs."""

    def on_sigint(signum: int, frame: object) -> None:
        err_console.print(f"\n[yellow]Still writing {escape(writer.path.name)}...[/yellow]")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        writer.close()
    except Exception as e:
        logger.exception("Could not write %s", writer.path)
        return e
    finally:
        signal.signal(signal.SIGINT, previous)
    return None


def _host(url: str) -> str:
    """Return the URL's host for display, or the URL itself if it is malformed."""
    try:
        return normalize_url(url).netloc or url
    except ValueError:
        return url


def _print_done(
    total: int, stores: list[StoreResult], *, show_counter: bool, multi: bool, log_file: Path | None
) -> None:
    """The end-of-run summary of a run that scraped items."""
    if show_counter:
        console.print("[green]Done![/green]")
    else:
        console.print(f"[green]Done! Scraped {total} item(s).[/green]")
    if not multi and stores and stores[0].products_from:
        console.print(f"  Products came from {escape(stores[0].products_from)}.")
    if log_file:
        console.print(f"  Log: {log_file}")
    if multi:
        for s in stores:
            console.print(f"  {escape(_host(s.input_url))}: {_display_status(s)}")


def _display_status(store: StoreResult) -> str:
    """Per-store summary text: the item count, or why there were none."""
    if store.status == StoreStatus.error:
        return "failed"
    if store.items > 0:
        source = f" (from {store.products_from})" if store.products_from else ""
        return f"{store.items} items{escape(source)}"
    return store.status.label


def summary_hints(stores: list[StoreResult], config: Config) -> list[str]:
    """Return one hint per distinct zero-item status, in diagnosis order."""

    def total(status: StoreStatus, codes: Container[int]) -> int:
        return sum(
            n
            for s in stores
            if s.status == status
            for code, n in s.responses_by_status.items()
            if code in codes
        )

    present = {s.status for s in stores}
    hints = []
    if StoreStatus.error in present:
        n = sum(s.status == StoreStatus.error for s in stores)
        hints.append(f"{n} store(s) failed with an error.")
    if StoreStatus.forbidden in present:
        hints.append(
            f"Received {total(StoreStatus.forbidden, {403})} HTTP 403 (Forbidden) response(s). "
            "The server is actively blocking scrapers."
        )
    if StoreStatus.not_found in present:
        hints.append(
            f"Received {total(StoreStatus.not_found, {404})} HTTP 404 (Not Found) response(s). "
            "Check the URL and --platform flag."
        )
    if StoreStatus.robots_blocked in present:
        hints.append(
            "Likely blocked by robots.txt. "
            "Retry with [bold]--ignore-robots[/bold] (-i) to ignore it."
        )
    if StoreStatus.rate_limited in present:
        # Requests, not responses: robots.txt's 429 isn't in responses_by_status
        n = sum(s.rate_limited_requests for s in stores if s.status == StoreStatus.rate_limited)
        hints.append(
            f"{n} request(s) ended on HTTP 429 (Too Many Requests). "
            "The store is rate limiting requests; retry later, with a lower "
            "[bold]--concurrent[/bold] and AutoThrottle on."
        )
    if StoreStatus.unavailable in present:
        n = total(StoreStatus.unavailable, range(500, 600))
        hints.append(
            f"Received {n} HTTP 5xx (server error) response(s). "
            "The store may be down or under maintenance; retry later."
        )
    if StoreStatus.timed_out in present:
        hints.append(
            f"Timed out after {config.scrape.bail}s with no items "
            "(not counting Retry-After waits). "
            "Use [bold]--bail 0[/bold] to disable."
        )
    if StoreStatus.tls_error in present:
        hints.append(
            "TLS certificate verification failed. "
            "Retry with [bold]--insecure[/bold] to skip verification, or behind a "
            "company proxy point SSL_CERT_FILE or SSL_CERT_DIR at its CA."
        )
    if StoreStatus.no_response in present:
        hints.append("No responses received. Check the URL and your network connection.")
    if StoreStatus.empty in present:
        hints.append("The store may be empty or the endpoint returned no products.")
    if StoreStatus.stopped in present:
        hints.append("Stopped before every store finished.")
    for s in stores:
        if s.query_disallowed and s.status == StoreStatus.robots_blocked:
            hints.append(
                f"{escape(_host(s.input_url))}'s robots.txt disallows URLs with query "
                "strings, which covers the GraphQL query."
            )
        if s.suggested_platform:
            name = _PLATFORM_NAMES.get(s.suggested_platform, s.suggested_platform)
            hints.append(
                f"{escape(_host(s.input_url))} looks like {name}; "
                f"use --platform {s.suggested_platform}."
            )
    return hints


if __name__ == "__main__":
    app()
