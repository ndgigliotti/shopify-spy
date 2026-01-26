<div align="center"><img src="resources/icon.png", width=150></div>

# Shopify Spy

[![CI](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml/badge.svg)](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Shopify Spy is a simple but powerful [Scrapy](https://docs.scrapy.org/en/latest/index.html) application for scraping Shopify websites. Its main feature is `shopify_spider`, a universal spider for Shopify stores. The spider extracts detailed data including high-value information like vendor names and inventory levels.

To find Shopify stores to scrape, try searching Google with the argument `site:myshopify.com`.

## Installation

Shopify Spy is a Scrapy project meant to be forked or cloned. Forking is recommended if you plan to customize settings in `shopify_spy/settings.py` while still being able to pull upstream updates.

```shell
git clone https://github.com/your-username/shopify-spy.git
cd shopify-spy

# Optional: add upstream to pull future updates
git remote add upstream https://github.com/ORIGINAL_OWNER/shopify-spy.git
```

Install dependencies with pip:

```shell
pip install .
```

Or using [uv](https://docs.astral.sh/uv/) (faster):

```shell
uv sync
```

Requires Python 3.10+.

## Usage

The spider can be used like any Scrapy spider, but you must provide it with a URL. Run from the project directory:

```shell
# Scrape a single store
scrapy crawl shopify_spider -a url=https://www.example.com/

# Scrape multiple stores from a text file (one URL per line)
scrapy crawl shopify_spider -a url_file=resources/urls.txt

# Specify what to scrape (products/collections/images can be True/False)
scrapy crawl shopify_spider -a url=https://www.example.com/ -a products=False -a collections=True
```

Arguments must always be preceded with the `-a` flag, as is standard for Scrapy. Results are stored as JSON lines in `resources/shopify_spider/`.

Refer to the [Scrapy documentation](https://docs.scrapy.org/en/latest/index.html) for adjusting settings or advanced usage.

## Limitations

**Standard Shopify stores.** This spider works with standard Shopify stores using Liquid themes, which represent nearly all Shopify sites. The small number of headless stores built on [Hydrogen](https://hydrogen.shopify.dev/) or other custom storefronts are not supported, as they use the Storefront GraphQL API instead of the JSON endpoints this spider relies on.

Attempting to scrape a large store may result in a temporary ban. This can be mitigated by enabling AutoThrottle in `shopify_spy/settings.py`.

## Feedback

Found a bug or have a suggestion? [Open an issue](https://github.com/ndgigliotti/shopify-spy/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)

## Credits

Icon by [Bartama Graphic](https://www.flaticon.com/authors/bartama-graphic).
