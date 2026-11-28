# Changelog

## Unreleased

### Added

- `--rotate/--no-rotate` (`network.rotate`, `rotate=`) turns the identity switch
  after a refusal on or off (default on), and the status file's
  `identity_changes` and `StoreResult.identity_changes` count the switches per
  store.
- A `magento` platform (`--platform magento`, `platform="magento"`) for Magento
  Open Source and Adobe Commerce 2.4 stores, read through the storefront GraphQL
  API at `/graphql` without a token. Items are the GraphQL products plus `store`,
  `platform`, `html_url` and `image_urls` (`MagentoProductItem`), and the unified
  schema has a row per configurable variant. A store whose `/graphql` answers
  without products reports `failed` with Magento's message, and one whose robots.txt
  disallows URLs with query strings (`Disallow: /*?`) gets a hint saying so
  ([#89](https://github.com/ndgigliotti/shopify-spy/issues/89)).
- A `bigcommerce` platform (`--platform bigcommerce`, `platform="bigcommerce"`) for
  BigCommerce Stencil storefronts, read through the Storefront GraphQL API at
  `/graphql` with the public API token the storefront's homepage carries. Items are
  the GraphQL product nodes plus `store`, `platform`, `html_url` and `image_urls`
  (`BigCommerceProductItem`), and the unified schema has a row per variant. A store
  whose homepage has no unexpired token (such as the default Cornerstone theme)
  reports `failed` with that reason. The theme's own Stencil token is preferred
  over app tokens, a headless storefront's page that names its
  `store-<hash>.mybigcommerce.com/graphql` endpoint is read through it, and
  products with more than 250 variants are paged in full
  ([#90](https://github.com/ndgigliotti/shopify-spy/issues/90)).
- A `generic` platform (`--platform generic`, `platform="generic"`) for stores on
  platforms without their own adapter. It reads the product pages listed in the
  store's sitemaps (robots.txt `Sitemap:` lines, `/sitemap.xml`,
  `/sitemap_index.xml` or `/xmlsitemap.php`, product sitemaps first) and keeps each
  page's schema.org `Product` or `ProductGroup` JSON-LD, or its microdata on a page
  without a JSON-LD Product. Items are the schema.org node
  plus `store`, `platform`, `html_url` and `image_urls` (`GenericProductItem`), and
  the unified schema has a row per `hasVariant` Product or per offer. It is a
  best-effort fallback at one request per product. A store whose pages have no
  Product data reports `failed` with the number of pages checked and what stopped
  it: the end of the listing, 50 pages, or `--bail` (about 20 to 25 pages at the
  default on a throttled store). Its hint names Shopify, WooCommerce or Magento when
  the store answers like one
  ([#93](https://github.com/ndgigliotti/shopify-spy/issues/93)).
- A `squarespace` platform (`--platform squarespace`, `platform="squarespace"`)
  for Squarespace Commerce stores. Squarespace's robots.txt disallows its
  `?format=json` URLs, so by default it reads the product pages the store's sitemap
  lists, as `generic` does, and never requests a `format=json` URL; those items are
  `generic`-shaped with `platform: "squarespace"` and `source: "product_page"`. With
  `--ignore-robots` (`respect_robots_txt=False`), it reads each collection's
  `?format=json` pages instead, giving the Squarespace product records with every
  variant, price and stock (`SquarespaceProductItem`, `source: "format_json"`), and
  falls back to the product pages when no collection gives a product. The log says
  which source each store used, and the unified schema has a row per variant (a
  digital product, which has none, gets one row priced from its `structuredContent`). A
  store read with `--ignore-robots` whose sitemap lists product pages its
  collections don't give ends `partial`
  ([#91](https://github.com/ndgigliotti/shopify-spy/issues/91)).

### Changed

- Requests go through curl_cffi with a current browser's TLS and HTTP/2 settings
  and headers by default, starting as `chrome`
  ([#95](https://github.com/ndgigliotti/shopify-spy/issues/95)). curl_cffi
  (`>=0.16.3,<0.18`) is a new required dependency. `--impersonate BROWSER` (`-I`,
  `network.impersonate`, `impersonate=`) picks the first profile: `chrome`,
  `safari`, `firefox` or any other curl_cffi target. Setting your own
  `--user-agent` (`network.user_agent`, `user_agent=`) is the only way to send
  plain httpx requests, with exactly that string and never switched; it can't be
  combined with `--impersonate`, and a blank one counts as unset.
- Both transports trust the same CAs, for the store and for an `https://` proxy,
  read from the environment during the run: the file in `SSL_CERT_FILE`, else
  `REQUESTS_CA_BUNDLE`, else `CURL_CA_BUNDLE`, and the directory in
  `SSL_CERT_DIR`, either one replacing certifi's bundle as `SSL_CERT_FILE`
  already did. httpx now reads `REQUESTS_CA_BUNDLE` and
  `CURL_CA_BUNDLE` too, and trusts `SSL_CERT_DIR` alongside `SSL_CERT_FILE`
  rather than ignoring it when both are set. A CA file that can't be read,
  or a directory with no hashed certificate, stops the run before it starts,
  naming the variable, on both transports.
- After a 403 or 429, a store switches to another browser profile to test
  whether the refusal is about identity: only after the retry's usual backoff,
  within the existing retries, and never again for that store if the new profile
  is refused too. A host's refusals are remembered for the run, so no store
  switches to a profile it refused ([#67](https://github.com/ndgigliotti/shopify-spy/issues/67)). Challenge pages
  (Cloudflare's `cf-mitigated: challenge`), 401s, image downloads and
  `*.myshopify.com` refusals never switch, and a challenge page isn't retried. Each switch is logged at INFO.
- The unified schema (CSV, XLSX and `--schema unified`) has two new columns,
  `gtin` and `mpn`, after `sku`: a generic item's `gtin` (or `gtin13`, `gtin12`,
  `gtin14`, `gtin8`) and `mpn`, and a BigCommerce item's `gtin` (else `upc`) and
  `mpn`, the variant's own when it has them. They're blank for Shopify, Magento and
  WooCommerce Store API items, whose APIs have no such fields (Magento's only as
  store-defined custom attributes). Appending to a CSV written by an earlier version keeps that file's columns,
  without these. Generic items (and WooCommerce items read from product pages) also
  fill `average_rating` and `review_count` from `aggregateRating`, with a rating on
  another `bestRating` scale converted to one out of 5.
- The WooCommerce product-page fallback reads more pages: a Yoast `ProductGroup`
  (a variable product, priced from its variants' offers), schema.org microdata on a
  page without a JSON-LD Product, JSON-LD in an HTML comment or with empty values
  (`"description": ,`), `@type` written as an IRI or `schema:Product`, and a Product
  under a page's `mainEntity`. Images given as `{"@id": ...}` references or relative
  URLs are resolved, and names and descriptions escaped twice (`&amp;amp;`) are
  unescaped. Prices with a currency symbol or separators (`$19.99`, `1.234,56`) are
  read. A page's product is chosen as `generic` chooses it, so a page of several
  products, none of which names the page, is now skipped rather than read as the
  first.
- Throttling is more conservative by default. A robots.txt `Crawl-delay` (for the
  `shopify-spy` group, else `*`) spaces that host's requests, capped at the
  throttle's `max_delay` and applied even with `--no-throttle`. Throttled page and
  API requests to one host are at least 1 second apart (typically 1 to 1.5 seconds), set by the new
  `--min-delay` option, config `throttle.min_delay` and library option
  `throttle_min_delay`. Throttled image downloads from one host, the store's own
  included, are at least 0.25 seconds apart, set by the new `--image-delay`
  option, config `throttle.image_delay` and library option `throttle_image_delay`. The gaps vary randomly by up to half again (config `throttle.jitter`, library option `throttle_jitter`, 0 for exact spacing). A `Retry-After` longer than 60 seconds is no longer
  retried after 60 seconds: the host is paused for the whole `Retry-After` and the
  request ends as if its retries had run out, so a store with no successful
  response ends `rate_limited` for a 429 (`unavailable` for a 503).

### Removed

- The unreleased `impersonate` extra: curl_cffi is always installed.
- The built-in list of browser user agents (`user_agents.json`) and
  `scripts/update_user_agents.py`, which refreshed it. Without `--user-agent`,
  requests carry the browser profile's own user agent; with it, exactly the one
  you set, so httpx never sends one Shopify Spy picks.

## 0.5.1

### Security

- Requests to loopback, private-network, link-local (including cloud metadata),
  shared, multicast and reserved addresses are refused, along with their
  robots.txt, unless the store's own URL is on such an address. This covers
  redirects, image URLs and every other URL a store supplies, given as a literal IP,
  a `localhost` name or a hostname that resolves to one. Through a proxy, only
  literal IPs and `localhost` names are checked. A warning names each refused URL
  ([#96](https://github.com/ndgigliotti/shopify-spy/issues/96)).
- The "unsupported format" image warning no longer includes the response's first
  bytes; they are logged at debug level.

### Added

- A Shopify store whose own `/products.json` is a 404, as on headless (Hydrogen)
  storefronts, is tried on the `myshopify.com` domain its homepage names before the
  sitemap. Items keep the store's own domain. `StoreResult.products_from`, the
  status file's new `products_from` key and the CLI's end-of-run summary name that
  domain. Requests to `*.myshopify.com` go one at a time across the run and never
  swap the user agent ([#92](https://github.com/ndgigliotti/shopify-spy/issues/92)).
  `scripts/contract_test.py` reports a DIFF in the status file against 0.5.0 because
  of the new key.

### Changed

- `--help` is plain text when stdout isn't a terminal, as when an agent or script
  reads it, even with `FORCE_COLOR`, `PY_COLORS` or `GITHUB_ACTIONS` set: no boxes,
  no truncation and no wrapping. A terminal still gets Rich panels. Usage errors
  follow the same rule. `-h` is an alias for `--help`
  ([#75](https://github.com/ndgigliotti/shopify-spy/issues/75)).
- `spy scrape --help` groups its options by task (what to scrape, output, network,
  run control), gives each option's default, marks `--products` and `--collections`
  as Shopify only, and ends with examples, where results go, the exit status and the
  config files read without `--config`. `scripts/contract_test.py` reports a DIFF for
  both `--help` outputs against 0.5.0 because of this.
- The reference documentation moved from the README into `docs/`: installation, the
  command line, output, the library and troubleshooting each have a page. The README
  keeps the quick start and links to them.
- The README leads with use from scripts and AI agents, installs with uv first
  (`uv tool install` for the CLI, `uv add` for the library), and has a Responsible
  use section. User-agent rotation is described as a retry detail.

### Fixed

- A product page whose JSON-LD has a trailing comma or a `//` or `/* */` comment
  no longer loses its product in the WooCommerce sitemap fallback
  ([#88](https://github.com/ndgigliotti/shopify-spy/issues/88)).
- The robots.txt hint now says `--ignore-robots` ignores robots.txt, not that it
  "bypasses" it.
- `help(spy.scrape)` and `help(spy.scrape_async)` no longer indent everything from
  "Args" on a level deeper than the summary on Python 3.13+. They now give each
  option's default and document `images_dir`.

## 0.5.0

### Added

- `--proxy URL`, `network.proxy` in the config file and `proxy=` on
  `scrape`/`scrape_async` send every request through one fixed proxy, overriding the
  environment. Credentials in the URL are masked in logs and errors.
- `--proxy` and `proxy=` take credentials as typed: a `/`, `?` or `#` in them no
  longer sends requests to the wrong host. In `HTTPS_PROXY` and the other variables
  they're an error that says to percent-encode them.
- With a proxy in use, a store with no response gets a hint to check the proxy, and a
  proxy's 407 (refused credentials) counts as `no_response` rather than an empty store.
- `socks5://` proxies work out of the box: socksio (httpx's `socks` extra) is now a
  default dependency.
- The README documents the `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY`
  environment variables, which were already honored.

### Changed

- A 429 (Too Many Requests) now swaps the store's user agent, as a 403 already did
  ([#62](https://github.com/ndgigliotti/shopify-spy/issues/62)), but only when the
  refused request carried the store's current one. A fixed `--user-agent` stays fixed.
- Time spent waiting out a server's `Retry-After` on a 429 or 503 no longer counts
  against `--bail`, so a rate-limited store isn't cut off mid-wait. A rate-limited
  store can take minutes or longer to fail (see the README's rate limiting notes). A
  429 without `Retry-After` is still capped by bail.
- Nix flake: the library package (`python3Packages.shopify-spy` from the overlay) no
  longer includes xlsxwriter, matching nixpkgs; add
  `ps.shopify-spy.optional-dependencies.excel` for Excel output. The CLI, the `python`
  output and the dev shell still include it.

### Fixed

- `--collections` gets collection items from Shopify stores again
  ([#64](https://github.com/ndgigliotti/shopify-spy/issues/64)). Shopify answers
  `/collections/<handle>.json` with 429 for every request, so the items now come from
  the paged `/collections.json` list, and the sitemap is no longer read for them.
  Their fields, values and `url` are unchanged. Only the order changes: collections
  now come as a block after the products, instead of mixed in with the sitemap's
  product fetches.
- A SOCKS proxy in the environment (`HTTPS_PROXY=socks5://...`) no longer crashes the
  run with an `ImportError` for the missing socksio package.

## 0.4.1

### Changed

- Browser user agents are updated to current versions (Chrome 156/157, Firefox
  153/160, Safari 27.2, Edge 157).

### Fixed

- Shopify stores no longer fail as `rate_limited` when a run picks a Linux user agent.
  Shopify answers every request from a plain `X11; Linux x86_64` browser user agent
  with 429, so the pool drops Chrome on Linux and keeps Ubuntu's Firefox for Linux.

### Known issues

- `--collections` gets no collection items on Shopify stores, which end as
  `rate_limited`: Shopify now refuses `/collections/<handle>.json` for every request.
  0.4.0 is affected too. Products are unaffected. See
  [#64](https://github.com/ndgigliotti/shopify-spy/issues/64).

## 0.4.0

### Added

- `spy` command, a short alias for `shopify-spy`.
- A Nix flake: `nix run github:ndgigliotti/shopify-spy`, `nix profile add`, an overlay
  adding `python3Packages.shopify-spy`, and a dev shell. Its build runs the test suite,
  and the package includes Excel support.
- `--schema native|unified` (config `output.schema`, `write_items(schema=...)`) separates the item
  shape from the file format. `native` writes items as scraped and stays the default for
  JSONL, JSON and XML. `unified` writes one record per variant with the same columns on
  both platforms, the ones CSV uses; in XML each row is an `<item>`. CSV and XLSX are always
  unified, so `--schema native` with either is an error. `--peek --schema unified` prints the rows.
- **Excel output:** `--format xlsx` and `write_items(..., "products.xlsx")` write the
  unified schema's rows (always unified, like CSV) as typed cells (numbers, booleans, UTC dates; text is never a
  formula), with a frozen, filterable header and a new sheet past Excel's row limit.
  Needs the `excel` extra (`pip install 'shopify-spy[excel]'`). See "Excel output" in
  the README.
- The status JSON has an `output_error` field (`null`, or why the data file couldn't
  be written) and an `items_written` field (how many items the data file holds).
- Text with a lone surrogate (a broken `\uXXXX` escape in a store's JSON) is written
  with U+FFFD in its place; JSONL, JSON and CSV output used to fail on it. A failed write now stops the run, keeps the items already written and
  still writes the status file; it used to end the command with a traceback and no
  status file.
- WooCommerce stores whose Store API is unavailable (an error status other than a
  429 rate limit, no product list, a failed request or robots.txt) are scraped
  from their product pages instead. Pages are found through the WordPress REST API's
  product list or the product sitemaps, and each page's schema.org Product data
  becomes a partial item in the Store API's shape, marked `"source": "product_page"`;
  a variable product's variations, with their own prices, SKUs and stock, come from
  the page's variation form (WooCommerce embeds it for up to 30 variations).
  Unified rows (CSV, XLSX) gain a `source` column: `store_api` or `product_page` for
  WooCommerce, blank for Shopify. XML output replaces characters that aren't valid in
  element names (`@type` becomes `_type`); valid keys are unchanged.

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
- Unified rows (CSV, XLSX, `--schema unified`) fill `min_price` and `max_price` for
  Shopify products too: the lowest and highest variant price, on each of the product's
  rows, as `summarize()` gives them. They used to be WooCommerce-only. Collections leave
  them blank ([#52](https://github.com/ndgigliotti/shopify-spy/issues/52)).
  `summarize()` skips Shopify variant prices that aren't finite numbers (`NaN`,
  `Infinity`); a `NaN` price used to make it raise.
- **A store that only answered 429 (Too Many Requests) is `rate_limited`, and one that
  only answered 5xx errors is `unavailable`,** instead of `empty`. A store that hit
  `--bail` with no items after a 429 its retries didn't get past (robots.txt's
  included) is `rate_limited` instead of `timed_out`, even if other requests
  succeeded; `StoreResult.rate_limited_requests` counts those requests. Both are failures:
  the status file's `status` is `rate limited (429)` or `unavailable (5xx)`, the CLI
  prints a hint to retry later, and `scrape()` raises `ScrapeFailed` when every store
  fails. A 403 or robots.txt block still takes precedence
  ([#50](https://github.com/ndgigliotti/shopify-spy/issues/50)).

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
