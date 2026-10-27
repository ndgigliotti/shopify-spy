## Project Overview

Shopify Spy is a Python library and CLI for extracting product and collection data from Shopify and WooCommerce stores through their public JSON endpoints (httpx + asyncio).

## Common Commands

```bash
uv sync                     # Install dependencies (dev group included)
uv run pytest               # Run tests (integration and e2e deselected by default)
uv run ruff check . && uv run ruff format .  # Lint and format
```

## Architecture

Layers, top to bottom; lower layers never import higher ones, and only the CLI imports `typer`, `rich` or `yaml`. The public API is what `__init__.py` exports; `_` modules are private.

- **CLI** (`cli.py`, `_config.py`): Typer app built on `scrape_async`. YAML config precedence: defaults -> config file -> CLI args; `Config.to_kwargs()` maps config to library options. `FETCHER_FACTORY` is the test hook for a fake transport.
- **Public API** (`_api.py`, `_export.py`, `_summary.py`, `_types.py`): `scrape()` (sync; steps a private event loop whose clock excludes paused time) and `scrape_async()`; `on_failure` raises `ScrapeFailed` by default. Writers match the old Scrapy exporters byte for byte (JSONL/JSON/XML).
- **Runner** (`_runner.py`, `_results.py`): one driver task owns all store tasks and feeds a bounded queue; per-store error isolation, `max_items_per_store`, bail, graceful shutdown; `StoreResult`/`StoreStatus`/`RunResult`. Never hold a TaskGroup or timeout across a `yield`.
- **Adapters** (`_shopify.py`, `_woocommerce.py`, `_sitemap.py`, `_images.py`): `run(ctx, emit)` coroutines. Shopify tries bulk `/products.json`, falls back to the sitemap; WooCommerce pages the Store API.
- **Session** (`_session.py`, `_policy.py`): per-store `StoreSession` (dedupe, robots.txt, retries with UA rotation on 403, hop-by-hop redirects, stats) over a run-wide `HostRegistry` (robots cache, concurrency and AutoThrottle slots keyed by origin).
- **Transport** (`_fetch.py`): `Fetcher` protocol; `HttpxFetcher`, plus `FakeFetcher`/`ReplayFetcher` for tests.

Design history and remaining roadmap: `.context/httpx-library-rewrite.md`.

## Testing

`pytest` by default deselects tests marked `integration` (see `pyproject.toml` `addopts`). Integration tests call real stores. Run them explicitly with:

```bash
uv run pytest -m integration
```

Tests marked `e2e` (`tests/test_e2e_diagnosis.py`) run the CLI against local HTTP servers that each play one store scenario (robots.txt blocks, redirects, retried 403s, hangs) and check the per-store status. They are also deselected by default. Run them after changing the diagnosis or the session layer:

```bash
uv run pytest -m e2e
```

## Git Conventions

Feature branches: `feat/<description>`
