# Changelog

## Unreleased

### Added

- `spy` command, a short alias for `shopify-spy`.
- `--schema native|unified` (config `output.schema`, `write_items(schema=...)`) separates the item
  shape from the file format. `native` writes items as scraped and stays the default for
  JSONL, JSON and XML. `unified` writes one record per variant with the same columns on
  both platforms, the ones CSV uses; in XML each row is an `<item>`. CSV is always unified,
  so `--schema native` with CSV is an error. `--peek --schema unified` prints the rows.
- **Excel output:** `--format xlsx` and `write_items(..., "products.xlsx")` write the
  unified schema's rows (always unified, like CSV) as typed cells (numbers, booleans, UTC dates; text is never a
  formula), with a frozen, filterable header and a new sheet past Excel's row limit.
  Needs the `excel` extra (`pip install 'shopify-spy[excel]'`). See "Excel output" in
  the README.
- The status JSON has an `output_error` field (`null`, or why the data file couldn't
  be written) and an `items_written` field (how many items the data file holds). A failed write now stops the run, keeps the items already written and
  still writes the status file; it used to end the command with a traceback and no
  status file.

### Changed

- **CSV output has one row per variant, with one fixed set of columns for both
  platforms and a plain value in every cell.** It used to put the whole product in one
  JSON-encoded cell, which nothing could sort or filter on. Product columns repeat
  on each variant row, and descriptions are plain text. A `platform` column tells
  Shopify rows from WooCommerce ones, so one CSV can hold both. Booleans are `true`/`false`
  in every CSV. See "Unified schema" in the README for the columns. JSONL,
  JSON and XML keep each item's native shape unless `--schema unified` is given. `write_items()` does the same for scraped items, and its
  `fields` now names these columns; other dicts still get a column per key.
  `append=True` refuses a CSV written by 0.3.0, whose columns were item fields.
- Native items have a top-level `platform` field (`"shopify"` or `"woocommerce"`), so a
  file that mixes runs says where each item came from. JSONL, JSON and XML output gains
  this field.
- `summarize()` WooCommerce prices keep the currency's decimals (`19.90`, not `19.9`).
- **Shopify stores with more than 25,000 products** need far fewer requests. Once
  `/products.json` stops at Shopify's 25,000-product limit, the scraper reads the
  sitemap's product list, then pages `/collections/all` and the store's collections
  (each has its own limit), largest first, while that saves requests: not when fewer
  products are missing than a collection has pages, and not once every listed
  product is found or they find less than one new product per request. Only the
  products still missing are fetched one at a time from the sitemap. A
  65,718-product store now takes about 1,700 requests instead of 41,000
  ([#30](https://github.com/ndgigliotti/shopify-spy/issues/30)).
- WooCommerce variations have their own prices, SKU and stock. After each page of products,
  one request (per 100 variations) lists that page's variations, and each entry in a
  product's `variations` becomes the variation's full Store API record, keeping the
  listing's `attributes`. Unified rows take `sku`, `price`, `compare_at_price` and
  `available` from it instead of repeating the product's. If the store refuses the
  listing, the variations keep their old shape and values, and the store still succeeds.

## 0.3.0

Shopify Spy no longer uses Scrapy. It now runs on httpx and asyncio, and the same
engine is available as a Python library. The CLI keeps its commands, flags and config
file; you don't need to change how you run it.

### Action needed only if you

- **Use Python 3.10:** Python 3.11 or later is now required.
- **Run `scrapy crawl shopify_spider` / `scrapy check`, or used the README's
  `CrawlerProcess` example:** these are gone. Use the CLI, or the new library:
  `for item in shopify_spy.scrape(url): ...` (see "Use as a library" in the README).

### New

- **Python library:** `scrape()` for scripts, `scrape_async()` for async code and
  notebooks, per-store `StoreResult`s with a `status` and `hint`, `ScrapeFailed` when
  every store fails, `write_items()`, `read_url_file()` and `summarize()`. Import
  them from `shopify_spy`; its submodules are private.
- `--insecure` turns off TLS certificate verification, which is now on by default.
- `--max-stores` limits how many stores are scraped at once (default 16).
- Zero-item diagnostics suggest the right `--platform` when a store looks like the
  other platform, and explain TLS failures.
- URL files may contain `#` comments.

### Fixed

- `--no-throttle` (and `throttle.enabled: false`) now turns throttling off.
- `scrape.platform` in a config file now takes effect when `--platform` isn't given.
- `--peek` prints one item in total (it printed one per store) and creates no files
  or directories.
- If `products.json` fails partway through a catalog, the sitemap fallback now
  fetches the remaining products instead of skipping them all.
- The run's `finish_reason` is `item_limit` only when a store actually reached
  `--limit`.
- Redirected images are downloaded instead of failing.
- URL files and config files are read as UTF-8 on every system, and a byte-order
  mark is accepted. On Windows they were read in the system's default encoding,
  which garbled non-ASCII text such as internationalized domain names.
- WooCommerce products are requested in product ID order, so a store whose default
  order shifts between pages no longer repeats some products and skips others.
  WooCommerce items now come out oldest first by ID (they were newest first).

### Changed

- CSV output JSON-encodes nested objects such as `product` (Scrapy wrote Python reprs
  like `{'id': 1}`); lists of strings such as `image_urls` are still comma-joined.
  Shopify CSVs always include the `collection` column when collections are scraped.
- Images keep their original format and extension instead of being re-encoded as
  JPEG; `checksum` is the MD5 of the original file. The extension comes from the
  file's own leading bytes; a download that isn't a JPEG, PNG, GIF, WebP, AVIF,
  HEIC/HEIF, JPEG XL, JPEG 2000, BMP, TIFF or ICO image (an error page, an SVG, a
  PDF) is counted as a failed image and not saved. A cached `.jpg` is reused only for
  JPEG URLs, so PNG/WebP images cached as re-encoded JPEGs are downloaded once more
  in their original format.
- Status files gain a `code` field per URL (the library's `StoreStatus`); the
  existing `status` strings are unchanged. Stores stopped by Ctrl-C before they
  produced anything now show `stopped` instead of `no response`.
- `--concurrent` limits requests per host across the whole run (it was per store).
- Retries back off exponentially and honor `Retry-After`. A 429 (Too Many Requests)
  or 503 pauses every request to that host for the wait and doubles its throttle
  delay, which then eases back down as requests succeed, instead of letting the
  host's other requests use up their retries; without `Retry-After`, those retries
  wait from 5 seconds up.
- Browser user agents are updated to current versions (Chrome 155–157, Firefox
  153/158, Safari 27, Edge 156).
- Log lines differ (no more Scrapy "Crawled (200)" lines); `--verbose` shows one line
  per request.
- `--limit` help text now says it applies per store (the behavior is unchanged).
- Duplicate store URLs in one run (`a.com`, `https://a.com/`) are scraped once.
