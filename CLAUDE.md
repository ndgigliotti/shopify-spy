## Project Overview

Shopify Spy is a CLI tool and Scrapy-based web scraper for extracting product and collection data from any Shopify store.

## Common Commands

```bash
uv sync --all-extras        # Install dependencies (dev)
uv run pytest               # Run tests (integration tests deselected by default)
uv run scrapy check shopify_spider  # Spider contract tests (hit real URLs)
uv run ruff check . && uv run ruff format .  # Lint and format
```

## Architecture

### CLI (`shopify_spy/cli.py`)
Typer-based CLI wrapping Scrapy's `CrawlerProcess`. `scrape` accepts one or more URL arguments, a `--url-file`, or prompts interactively when stdin is a TTY. `init` creates a default YAML config. Scrapy imports are deferred until `run_spider()` to keep startup fast.

### Config (`shopify_spy/config.py`)
Pydantic models for YAML config with four sections: `scrape`, `output`, `network`, `throttle`. Precedence: defaults -> config file -> CLI args. Config file is auto-discovered at `./shopify-spy.yaml` then `~/.config/shopify-spy/config.yaml`. Notable non-default: `scrape.collections` is `False` and `scrape.images` is `False` by default; `throttle.enabled` is `True` by default.

`scrape.limit` (int, optional) stops the spider after N items total across all parse methods.

### Spider (`shopify_spy/spiders/shopify.py`)
Extends `SitemapSpider`. Input URLs are normalized to `https://<host>/sitemap.xml`. `sitemap_filter` appends `.json` to any sitemap entry whose path contains `/products/` or `/collections/`, then yields all entries; `sitemap_rules` routes the `.json` URLs to `parse_product` or `parse_collection` based on the same path patterns. Each yielded item contains the full Shopify JSON payload plus two added fields: `url` (request URL) and `store` (hostname). `image_urls` is always present; when images are enabled it contains every `src` value found anywhere in the JSON via `find_all_values`.

### Exporters (`shopify_spy/exporters.py`)
Custom Scrapy `ItemExporter` subclasses for non-built-in output formats. Registered in `settings.py` via `FEED_EXPORTERS`. Two exporters:
- `SqliteItemExporter` -- writes items to a SQLite database table (`items`). Columns are derived from the first item's keys. Dict/list values are JSON-serialized to text. Uses stdlib `sqlite3` (no extra dependencies).
- `ParquetItemExporter` -- buffers items in memory and writes a single Parquet file on close. Dict/list values are JSON-serialized to string columns. Requires the optional `pyarrow` package (`pip install shopify-spy[parquet]`).

### Settings (`shopify_spy/settings.py`)
Scrapy defaults: autothrottle on, 16 concurrent requests per domain, robots.txt respected, image pipeline enabled, JSONL feed output. `FEED_EXPORTERS` registers the custom SQLite and Parquet exporters. The CLI overrides feed settings at runtime via `get_project_settings()`.

### Utilities (`shopify_spy/utils.py`)
`as_bool()` converts strings or bools to `bool`, handling values like `"yes"`, `"1"`, `"null"`. Used to coerce spider arguments that arrive as strings when called from the Scrapy CLI. `find_all_values(key, obj)` recursively searches nested dicts/lists and yields all matching values.

## Testing

`pytest` by default deselects tests marked `integration` (see `pyproject.toml` `addopts`). Integration tests call real Shopify endpoints via `scrapy check`. Run them explicitly with:

```bash
uv run pytest -m integration
```

## Git Conventions

Feature branches: `feat/<description>`
