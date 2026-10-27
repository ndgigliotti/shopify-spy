<div align="center"><img src="https://raw.githubusercontent.com/ndgigliotti/shopify-spy/master/assets/icon.png" width=150></div>

# Shopify Spy

[![CI](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml/badge.svg)](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/shopify-spy.svg)](https://badge.fury.io/py/shopify-spy)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Shopify Spy is a command-line tool and Python library for scraping product and collection data from ecommerce stores. It supports Shopify and WooCommerce stores out of the box, using their public JSON endpoints.

To find Shopify stores to scrape, try searching Google with `site:myshopify.com`.

## Installation

[pipx](https://pipx.pypa.io/) and [uv tool](https://docs.astral.sh/uv/) install CLI tools in isolated environments, so they won't conflict with other Python projects:

```bash
# pipx
pipx install shopify-spy

# uv
uv tool install shopify-spy
```

Or install with pip if you want it in a specific virtual environment:

```bash
pip install shopify-spy
```

Requires Python 3.11+.

## Quick Start

```bash
# Scrape a Shopify store (default)
shopify-spy scrape https://www.example.com

# Scrape a WooCommerce store
shopify-spy scrape --platform woocommerce https://www.example.com

# Scrape multiple stores
shopify-spy scrape https://store1.com https://store2.com https://store3.com

# Download product images
shopify-spy scrape https://www.example.com --images

# Include collections (Shopify only)
shopify-spy scrape https://www.example.com --collections

# Scrape multiple stores from a file
shopify-spy scrape --url-file stores.txt

# Specify output directory
shopify-spy scrape https://www.example.com --output ./my-data

# Sample 10 items (useful for testing)
shopify-spy scrape https://www.example.com --limit 10

# Quick peek at a store (prints 1 item to stdout, no file output)
shopify-spy scrape --peek https://www.example.com

# Peek and pipe to jq
shopify-spy scrape --peek https://www.example.com 2>/dev/null | jq '.product.title'

# Bypass robots.txt restrictions
shopify-spy scrape --ignore-robots https://www.example.com
```

Results are saved as JSONL in the output directory (default: `./output`). Use `--format` to choose JSON, CSV, or XML instead. A live item counter is displayed during scraping.

## Features

- **Automatic user-agent rotation.** Requests use a random browser user-agent. If a server responds with 403, the tool swaps to a different UA and retries.

<div align="center"><img src="https://raw.githubusercontent.com/ndgigliotti/shopify-spy/master/assets/ua-rotation-neon.png" width="700"></div>

- **Failure diagnostics.** When a scrape returns 0 items, the tool explains why (403, 404, robots.txt, timeout) and suggests flags to try.
- **Python library.** `import shopify_spy` gives the same scraping engine as the CLI; see [Use as a library](#use-as-a-library).
- **Log files.** Detailed logs go to a log file, keeping the terminal clean. Use `--verbose` to also print to the terminal.
- **Status JSON.** Each run writes a machine-readable status file with per-URL results, errors, and timing.
- **Bail timeout.** Automatically aborts if no items are scraped within `--bail` seconds (default: 30).
- **Coding-agent friendly.** Designed for use by AI coding agents (Claude Code, Cursor, etc.):
  - `--peek` pipes a single item to stdout for quick schema inspection
  - Non-zero exit codes on failure with diagnostic messages explaining why
  - Status JSON with machine-readable per-URL results and error details
  - `--bail` prevents dead runs from hanging indefinitely
  - `--quiet` suppresses visual noise; log files capture the details separately
  - A Python API whose failures are explicit: `ScrapeFailed` when every store fails, per-store statuses and hints

## Supported Platforms

| Platform | Mechanism | Notes |
|---|---|---|
| Shopify | `/products.json` bulk endpoint, sitemap fallback | Products and collections |
| WooCommerce | `/wp-json/wc/store/v1/products` | No authentication required |

## Commands

### `scrape`

Scrape products and collections from Shopify and WooCommerce stores.

```bash
shopify-spy scrape [URL] [OPTIONS]
```

**Arguments:**
- `URL...` - One or more store URLs (optional if using `--url-file`)

**Options:**
- `--platform, -p PLATFORM` - Ecommerce platform: `shopify`, `woocommerce` (default: `shopify`)
- `--limit, -n INT` - Stop each store after N items (per store; useful for sampling or testing)
- `--url-file, -f FILE` - File containing URLs (one per line; `#` starts a comment)
- `--products / --no-products` - Scrape products (default: yes; Shopify only)
- `--collections / --no-collections` - Scrape collections (default: no; Shopify only)
- `--images / --no-images` - Download images (default: no)
- `--output, -o PATH` - Output directory (default: `./output`)
- `--format, -F FORMAT` - Output format: `json`, `jsonl`, `csv`, `xml` (default: `jsonl`)
- `--config, -c FILE` - Path to YAML config file
- `--concurrent INT` - Concurrent requests per host (default: 16)
- `--max-stores INT` - Stores scraped at once (default: 16)
- `--throttle / --no-throttle` - Auto-throttle requests (default: yes)
- `--user-agent, -A TEXT` - Custom User-Agent header
- `--ignore-robots, -i` - Ignore robots.txt restrictions
- `--insecure` - Don't verify TLS certificates
- `--bail INT` - Abort if no items scraped within N seconds (default: 30, 0 to disable)
- `--peek` - Print 1 item to stdout as JSONL and exit (no files or directories created)
- `--verbose, -v` - Show debug output (logs to both file and terminal)
- `--quiet, -q` - Suppress the live item counter

### `init`

Create a default configuration file.

```bash
shopify-spy init [PATH]
```

**Arguments:**
- `PATH` - Where to create the config file (default: `./shopify-spy.yaml`)

**Options:**
- `--force, -f` - Overwrite existing file

## Configuration

Shopify Spy can be configured via YAML file. Create one with `shopify-spy init`:

```yaml
# shopify-spy.yaml
scrape:
  platform: shopify   # Platform: shopify, woocommerce
  products: true      # Scrape product data (Shopify only)
  collections: false  # Scrape collection data (Shopify only)
  images: false       # Download product images
  bail: 30            # Abort if no items scraped within N seconds (0 = off)

output:
  dir: ./output       # Output directory for results
  format: jsonl       # Output format: json, jsonl, csv, xml
  images_subdir: images  # Subdirectory for downloaded images

network:
  concurrent_requests: 16  # Concurrent requests per host
  timeout: 180             # Request timeout (seconds)
  retries: 2               # Retry failed requests
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent
  respect_robots_txt: true
  verify_tls: true         # Verify TLS certificates (false = --insecure)
  max_concurrent_stores: 16  # Stores scraped at once

throttle:
  enabled: true            # Auto-throttle based on server response
  start_delay: 1           # Initial download delay (seconds)
  max_delay: 60            # Maximum download delay (seconds)
  target_concurrency: 1.0  # Target concurrent requests (higher = faster)
```

**Config file search order:**
1. Path specified with `--config`
2. `./shopify-spy.yaml`
3. `~/.config/shopify-spy/config.yaml`

CLI options override config file settings.

## Output

Results are saved in the output directory (JSONL by default, configurable via `--format`):

```
output/
  data/
    shopify_spider_2024-01-15T10-30-00.jsonl
  status/
    shopify_spider_2024-01-15T10-30-00_status.json
  images/
    full/
      <image files>
```

A status JSON file is written for each run with metadata:

```json
{
  "items_scraped": 47,
  "urls": [
    {"url": "https://store.com", "items": 47, "status": "ok", "code": "ok"}
  ],
  "finish_reason": "finished",
  "duration_seconds": 12.3,
  "log_file": "/home/user/.local/state/shopify-spy/logs/shopify_spider_2024-01-15T10-30-00.log"
}
```

`status` is a human-readable label (`ok`, `403 Forbidden`, `blocked by robots.txt`, `timed out`, ...); `code` is the matching `StoreStatus` value from the Python API.

Detailed logs are written to a log file (not the terminal). Log files are stored in the platform-appropriate state directory (e.g., `~/.local/state/shopify-spy/logs/` on Linux). Use `--verbose` to also print logs to the terminal.

### Shopify output

Each line contains the full product or collection JSON from Shopify's API, plus two added fields:

```json
{
  "product": { "title": "...", "variants": [...], "images": [...], ... },
  "url": "https://store.com/products/item.json",
  "store": "store.com",
  "image_urls": ["https://cdn.shopify.com/.../product.jpg"]
}
```

### WooCommerce output

Each line contains the full product JSON from the WooCommerce Store API, plus two added fields:

```json
{
  "id": 123,
  "name": "Product Name",
  "slug": "product-name",
  "permalink": "https://store.com/product/product-name/",
  "sku": "SKU-001",
  "prices": { "price": "5200", "currency_code": "USD", "currency_minor_unit": 2 },
  "images": [{ "id": 1, "src": "https://..." }],
  "store": "store.com",
  "image_urls": ["https://..."]
}
```

Note: WooCommerce prices are strings in minor currency units (divide by `10^currency_minor_unit` to get the decimal value).

### Image Metadata

When using `--images`, each item includes a `scraped_images` field with download info:

```json
{
  "image_urls": ["https://cdn.shopify.com/.../product.jpg"],
  "scraped_images": [
    {
      "url": "https://cdn.shopify.com/.../product.jpg",
      "path": "full/abc123def.jpg",
      "checksum": "d41d8cd98f00b204e9800998ecf8427e",
      "status": "downloaded"
    }
  ]
}
```

Images are saved in their original format, named by the SHA-1 of their URL. The `path` is relative to the images directory (`output/images/` by default), and files younger than 90 days are reused (`"status": "uptodate"`).

In CSV output, nested values (such as `product`) are JSON-encoded.

### Parsing Output

**With jq:**
```bash
# Shopify: extract product titles
cat output/*.jsonl | jq '.product.title'

# WooCommerce: extract product names and prices
cat output/*.jsonl | jq '{name: .name, price: .prices.price, currency: .prices.currency_code}'
```

**With Python:**
```python
import json

with open("output/shopify_spider_2024-01-15.jsonl") as f:
    for line in f:
        item = json.loads(line)
        print(item["product"]["title"])  # Shopify
        # print(item["name"])            # WooCommerce
```

**With pandas:**
```python
import pandas as pd

df = pd.read_json("output/shopify_spider_2024-01-15.jsonl", lines=True)
products = pd.json_normalize(df["product"])  # Shopify
```

**With polars:**
```python
import polars as pl

df = pl.read_ndjson("output/shopify_spider_2024-01-15.jsonl")
```

## Limitations

**WooCommerce Store API required.** The WooCommerce spider uses the public Store API (`/wp-json/wc/store/v1/products`), available in WooCommerce 3.x and later. Stores that have disabled the REST API via security plugins or broadly block crawlers may not be scrapeable. When a scrape returns 0 items, the tool prints a diagnostic message explaining the likely cause (403 Forbidden, 404 Not Found, robots.txt blocking, etc.) and exits with code 1.

**Rate limiting.** Scraping very large stores may result in temporary bans. Auto-throttling is enabled by default, but you can adjust the settings or disable it for faster scraping:

```bash
# Disable throttling (faster but riskier)
shopify-spy scrape https://example.com --no-throttle
```

## Use as a library

The CLI is built on a Python API that scripts (and coding agents) can use directly:

```python
import shopify_spy as spy

# Items arrive as plain dicts while stores are scraped
for item in spy.scrape("https://www.example.com", max_items_per_store=100):
    print(item["product"]["title"])

# Several stores; stream to a file and inspect failures afterwards
run = spy.scrape(["a.com", "b.com"], on_failure="warn")
spy.write_items(run, "products.jsonl")
for store in run.result.failed:
    print(store.input_url, store.status, store.hint)

# Learn the schema from one item
item = spy.scrape("a.com").first()
print(spy.summarize(item))  # title, prices as Decimal, URL: same fields on both platforms

# Filter across platforms and write a CSV
rows = (spy.summarize(i) for i in spy.scrape(urls))
spy.write_items((r.asdict() for r in rows if r.min_price and r.min_price < 50), "cheap.csv")

# Retry stores that robots.txt blocked
result = spy.scrape(spy.read_url_file("stores.txt"), on_failure="ignore").collect()
blocked = [s.input_url for s in result.failed if s.status == "robots_blocked"]
spy.write_items(spy.scrape(blocked, respect_robots_txt=False), "products.jsonl", append=True)
```

- **Failures are never silent.** By default `scrape()` raises `ScrapeFailed` (listing each store's status) when every store failed; `on_failure="warn"` or `"ignore"` relax that, and `strict=True` raises when any store failed. Each `StoreResult` has a `status`, an item count and sometimes a `hint`, such as a likely wrong `platform`.
- **Options** match the CLI: `platform`, `products`, `collections`, `images` (with `images_dir`), `max_items_per_store` (per store; use `itertools.islice` for a total), `bail`, `concurrent_requests`, `request_timeout`, `retries`, `user_agent`, `respect_robots_txt`, `verify_tls`, `max_concurrent_stores` and the `throttle_*` settings.
- **Images:** `images=True, images_dir="images"` saves files under `images/full/`; each item's `scraped_images` records have a `path` relative to `images_dir`, so the file is `Path(images_dir) / record["path"]`.
- **No side effects:** nothing is written unless `images=True`, no config file is read, and nothing is printed (logs go to the `shopify_spy` logger).
- **Async code and notebooks** use `scrape_async()`, which takes the same options:

```python
async for item in spy.scrape_async("https://www.example.com"):
    ...

result = await spy.scrape_async(urls, platform="woocommerce")  # RunResult with .items
```

`scrape()` runs its own event loop, so it raises a clear error inside an already running loop (Jupyter, async frameworks); use `scrape_async()` there.

## Feedback

Found a bug or have a suggestion? [Open an issue](https://github.com/ndgigliotti/shopify-spy/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)

## Credits

Icon by [Bartama Graphic](https://www.flaticon.com/authors/bartama-graphic).
