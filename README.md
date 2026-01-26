<div align="center"><img src="resources/icon.png", width=150></div>

# Shopify Spy

Shopify Spy is a simple but powerful [Scrapy](https://docs.scrapy.org/en/latest/index.html) application for scraping Shopify websites. Its main feature is `shopify_spider`, a universal spider for classic Shopify stores. The spider extracts detailed data including high-value information like vendor names and inventory levels.

To find Shopify stores to scrape, try searching Google with the argument `site:myshopify.com`.

## Installation

Shopify Spy is a Scrapy project meant to be forked or cloned. Forking is recommended if you plan to customize settings in `shopify_spy/settings.py` while still being able to pull upstream updates.

```shell
git clone https://github.com/your-username/shopify-spy.git
cd shopify-spy

# Optional: add upstream to pull future updates
git remote add upstream https://github.com/ORIGINAL_OWNER/shopify-spy.git
```

Install dependencies using [uv](https://docs.astral.sh/uv/) (recommended) or pip:

```shell
# Using uv
uv sync

# Using pip
pip install .
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

**Classic Shopify stores only.** This spider works with traditional Shopify stores using Liquid themes, which expose `/products/[handle].json` and `/collections/[handle].json` endpoints. It does not work with headless Shopify stores built on [Hydrogen](https://hydrogen.shopify.dev/), which use the Storefront GraphQL API instead.

Attempting to scrape a large store may result in a temporary ban. This can be mitigated by enabling AutoThrottle in `shopify_spy/settings.py`.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

```shell
# Install dev dependencies
uv sync --all-extras

# Run tests
pytest

# Run linter
ruff check .
```

## License

[MIT](https://choosealicense.com/licenses/mit/)

## Credits

Icon by [Bartama Graphic](https://www.flaticon.com/authors/bartama-graphic).
