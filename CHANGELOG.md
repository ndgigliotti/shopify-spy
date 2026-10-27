# Changelog

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

### Changed

- CSV output JSON-encodes nested values (Scrapy wrote Python reprs), and Shopify CSVs
  always include the `collection` column when collections are scraped.
- Images keep their original format and extension instead of being re-encoded as
  JPEG; `checksum` is the MD5 of the original file. A cached image is reused only
  when its extension matches the URL's, so PNG/WebP images cached as re-encoded
  JPEGs are downloaded once more in their original format.
- Status files gain a `code` field per URL (the library's `StoreStatus`); the
  existing `status` strings are unchanged. Stores stopped by Ctrl-C before they
  produced anything now show `stopped` instead of `no response`.
- `--concurrent` limits requests per host across the whole run (it was per store).
- Retries back off exponentially and honor `Retry-After`. A 429 (Too Many Requests)
  pauses every request to that host for the wait and slows its throttle, instead of
  letting the host's other requests use up their retries.
- Log lines differ (no more Scrapy "Crawled (200)" lines); `--verbose` shows one line
  per request.
- `--limit` help text now says it applies per store (the behavior is unchanged).
- Duplicate store URLs in one run (`a.com`, `https://a.com/`) are scraped once.
