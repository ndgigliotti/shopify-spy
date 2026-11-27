<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ndgigliotti/shopify-spy/master/assets/icon-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/ndgigliotti/shopify-spy/master/assets/icon.svg">
    <img src="https://raw.githubusercontent.com/ndgigliotti/shopify-spy/master/assets/icon.png" alt="Shopify Spy" width="150">
  </picture>
</div>

# Shopify Spy

**Product and collection scraper for Shopify, WooCommerce and Magento stores, built for scripts and AI coding agents.**

[![CI](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml/badge.svg)](https://github.com/ndgigliotti/shopify-spy/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/shopify-spy.svg)](https://badge.fury.io/py/shopify-spy)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Shopify Spy is a command-line tool and Python library for scraping product and collection data from ecommerce stores. It supports Shopify, WooCommerce and Magento stores out of the box, using their public JSON and GraphQL endpoints, so it needs no API keys or store access. It's designed to be driven by AI agents as much as by people: coding agents such as Claude Code, Codex and GitHub Copilot, and personal agents such as Hermes and OpenClaw, or any agent that can run a shell command or a Python script.

## Installation

```bash
# The command (spy and shopify-spy)
uv tool install shopify-spy

# The library, in a project
uv add shopify-spy        # or: pip install shopify-spy

# Or run either once, nothing to install
uvx shopify-spy scrape --peek https://www.example.com
uv run --with 'shopify-spy>=0.4' python script.py

# Excel output (--format xlsx) needs the excel extra: install 'shopify-spy[excel]'
```

A `uv tool` install provides only the command, so `import shopify_spy` needs the library install. With `uvx`, name the package (`uvx shopify-spy ...`), since `uvx spy` would fetch an unrelated package. Python 3.11 or later is required. [Installation](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/install.md) covers pipx, inline script metadata and Nix.

## Quick Start

### Command line

```bash
# Scrape a Shopify store (the default platform)
spy scrape https://www.example.com

# Scrape a WooCommerce store
spy scrape --platform woocommerce https://www.example.com

# Scrape a Magento store
spy scrape --platform magento https://www.example.com

# Several stores, with collections (Shopify only) and images
spy scrape https://store1.com https://store2.com --collections --images

# Sample 10 items
spy scrape https://www.example.com --limit 10

# Print one item to stdout, writing nothing, and pipe it to jq
spy scrape --peek https://www.example.com 2>/dev/null | jq '.product.title'
```

Results are saved as JSONL in `./output` (change it with `--output`), with a live item counter while it runs. `--format` picks JSON, CSV, Excel (XLSX) or XML instead, and `--schema unified` gives one row per variant with the same columns on every platform. [Command line](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/cli.md) lists every option and the config file, and [Output](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/output.md) describes what's written.

### Python

The same engine is a library, so a script can scrape, filter and save in one pass. Install the library first (see [Installation](#installation)).

```python
import shopify_spy as spy

# Items are plain dicts, yielded while the stores are scraped
for item in spy.scrape("https://www.example.com", max_items_per_store=100):
    print(item["product"]["title"])

# Several stores to a file, then check which ones failed and why
run = spy.scrape(["store1.com", "store2.com"], on_failure="warn")
spy.write_items(run, "products.jsonl")
for store in run.result.failed:
    print(store.input_url, store.status, store.hint)
```

`help(spy.scrape)` documents every option, and [Use as a library](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/library.md) covers failure handling, schemas, images and async use.

### With a coding agent

An agent needs nothing installed. `uvx shopify-spy scrape --peek URL` shows an item's shape, and a script run with `uv run --with 'shopify-spy>=0.4' python script.py` filters and saves only what the task needs. The CLI is simpler for whole-catalog dumps and image downloads. Everything an agent needs to work this out is in `spy scrape --help`, which prints plain, complete text when piped, and in `help(spy.scrape)`.

## Features

- **Agent-friendly.** Built for AI agents, coding or personal, as much as for people:
  - `--help` prints plain, untruncated text when piped, with every default, examples, where results go and what each exit code means.
  - Items are plain dicts with typed definitions, and `help(spy.scrape)` documents every library option, so an agent can write a script without reading the source.
  - `--peek` prints one item to stdout for a quick look at its shape; warnings go to stderr, so stdout stays valid JSONL.
  - Every run writes a machine-readable status file with each store's status, the library suggests a fix for some failures (such as a likely wrong platform), and the exit code says whether any store produced items.
- **Failure diagnostics.** A failed store gets a status saying what went wrong (403, 404, robots.txt, 429 rate limits, 5xx errors, TLS errors, timeout, no response). When a run returns 0 items, the tool explains the likely cause and suggests flags to try, including the other `--platform` when a store looks like it runs on it. See [Troubleshooting](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/troubleshooting.md).
- **Python library.** `import shopify_spy` gives the same scraping engine as the CLI, with items as typed plain dicts and failures that are never silent.
- **Spreadsheet-ready output.** JSONL, JSON, CSV, Excel and XML, with a unified schema shared by every platform.
- **Polite by default.** Robots.txt is honored, requests are auto-throttled, and a store that produces nothing within `--bail` seconds (default 30) is abandoned instead of hanging the run.
- **Retries.** Timeouts, dropped connections, transient 5xx errors and 429 rate limits are retried, honoring `Retry-After`. Requests use a common browser user agent, and a 403 or 429 retries with another.

## Supported Platforms

| Platform | Mechanism | Notes |
|---|---|---|
| Shopify | `/products.json` bulk endpoint, then collection pages past 25,000 products, then the store's `myshopify.com` domain if its own 404s, sitemap fallback; collections from `/collections.json` | Products and collections |
| WooCommerce | `/wp-json/wc/store/v1/products`, then each page's variations (`?type=variation`); product pages' structured data as a fallback | No authentication required |
| Magento | storefront GraphQL `products` query at `/graphql` | No authentication required; default store view |

## Documentation

- [Installation](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/install.md): uv, pipx, library installs, the `excel` extra, Nix
- [Command line](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/cli.md): `scrape` and `init` options, the config file, proxies
- [Output](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/output.md): files, item shapes, the unified schema, Excel, images, working with the output
- [Use as a library](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/library.md): the Python API, failures, options, schemas, async
- [Troubleshooting](https://github.com/ndgigliotti/shopify-spy/blob/v0.5.1/docs/troubleshooting.md): store statuses, exit codes, the status file, logs, rate limiting, large catalogs
- [Changelog](https://github.com/ndgigliotti/shopify-spy/blob/master/CHANGELOG.md): what changed in each release

## Responsible use

Shopify Spy reads public catalog data, follows robots.txt and throttles itself by default. You are responsible for how you use it, including following each site's terms and the laws that apply to you. Options that change how requests reach a site, such as `--proxy`, are opt-in for that reason. Product descriptions and images are often copyrighted; collecting them doesn't give you the right to republish them.

Shopify Spy is not affiliated with or endorsed by Shopify or any other platform it supports. If a site asks you to stop collecting data, please do. The software is provided as is under the MIT License.

## Feedback

Found a bug or have a suggestion? [Open an issue](https://github.com/ndgigliotti/shopify-spy/issues).

## License

[MIT](https://choosealicense.com/licenses/mit/)
