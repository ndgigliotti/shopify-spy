<div align="center"><img src="assets/icon.png", width=150></div>

# Shopify Spy

[![CI](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml/badge.svg)](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/shopify-spy.svg)](https://badge.fury.io/py/shopify-spy)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Shopify Spy is a command-line tool for scraping product and collection data from any Shopify store. Built on [Scrapy](https://docs.scrapy.org/en/latest/index.html), it extracts detailed data including high-value information like vendor names and inventory levels.

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

Requires Python 3.10+.

## Quick Start

```bash
# Scrape a single store
shopify-spy scrape https://www.example.com

# Scrape multiple stores
shopify-spy scrape https://store1.com https://store2.com https://store3.com

# Download product images
shopify-spy scrape https://www.example.com --images

# Include collections
shopify-spy scrape https://www.example.com --collections

# Scrape multiple stores from a file
shopify-spy scrape --url-file stores.txt

# Specify output directory
shopify-spy scrape https://www.example.com --output ./my-data
```

Results are saved as JSON lines in the output directory (default: `./output`).

## Commands

### `scrape`

Scrape products and collections from Shopify stores.

```bash
shopify-spy scrape [URL] [OPTIONS]
```

**Arguments:**
- `URL...` - One or more Shopify store URLs (optional if using `--url-file`)

**Options:**
- `--url-file, -f FILE` - File containing URLs (one per line)
- `--products / --no-products` - Scrape products (default: yes)
- `--collections / --no-collections` - Scrape collections (default: no)
- `--images / --no-images` - Download images (default: no)
- `--output, -o PATH` - Output directory (default: `./output`)
- `--config, -c FILE` - Path to YAML config file
- `--concurrent INT` - Concurrent requests per domain (default: 16)
- `--throttle / --no-throttle` - Auto-throttle requests (default: yes)
- `--user-agent, -A TEXT` - Custom User-Agent header
- `--verbose, -v` - Show debug output
- `--quiet, -q` - Show only warnings and errors

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
  products: true      # Scrape product data
  collections: false  # Scrape collection data
  images: false       # Download product images

output:
  dir: ./output       # Output directory for results
  images_subdir: images  # Subdirectory for downloaded images

network:
  concurrent_requests: 16  # Concurrent requests per domain
  timeout: 180             # Download timeout (seconds)
  retries: 2               # Retry failed requests
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent
  respect_robots_txt: true

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

Results are saved as JSON lines files in the output directory:

```
output/
  shopify_spider_2024-01-15T10-30-00.jsonl
  images/
    full/
      <image files>
```

Each line in the JSON file contains a product or collection with full metadata from Shopify's JSON API.

### Parsing Output

**With jq:**
```bash
# Extract product titles
cat output/*.jsonl | jq '.product.title'

# Get prices
cat output/*.jsonl | jq '{title: .product.title, price: .product.variants[0].price}'
```

**With Python:**
```python
import json

with open("output/shopify_spider_2024-01-15.jsonl") as f:
    for line in f:
        item = json.loads(line)
        print(item["product"]["title"])
```

**With pandas:**
```python
import pandas as pd

df = pd.read_json("output/shopify_spider_2024-01-15.jsonl", lines=True)
products = pd.json_normalize(df["product"])
```

**With polars:**
```python
import polars as pl

df = pl.read_ndjson("output/shopify_spider_2024-01-15.jsonl")
```

## Limitations

**Standard Shopify stores only.** This tool works with standard Shopify stores using Liquid themes, which represent nearly all Shopify sites. The small number of headless stores built on [Hydrogen](https://hydrogen.shopify.dev/) or other custom storefronts are not supported, as they use the Storefront GraphQL API instead of the JSON endpoints this tool relies on.

**Rate limiting.** Scraping very large stores may still result in temporary bans. Auto-throttling is enabled by default, but you can adjust the settings or disable it for faster scraping:

```bash
# Disable throttling (faster but riskier)
shopify-spy scrape https://example.com --no-throttle
```

## Advanced Usage

For advanced Scrapy configuration or custom pipelines, you can use Shopify Spy as a library:

```python
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from shopify_spy.spiders.shopify import ShopifySpider

process = CrawlerProcess(get_project_settings())
process.crawl(ShopifySpider, url="https://example.com", products=True)
process.start()
```

## Feedback

Found a bug or have a suggestion? [Open an issue](https://github.com/ndgigliotti/shopify-spy/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)

## Credits

Icon by [Bartama Graphic](https://www.flaticon.com/authors/bartama-graphic).
