# Rewrite plan: Scrapy → httpx, library + CLI

Branch: `feat/httpx-library` · Worktree: `../shopify-spy-httpx-library`
Drafted 2026-10-26 · Revised after two rounds of adversarial review
(round 1: parity accuracy, architecture/API, execution/testing;
round 2: consistency, prototypes, library/CLI user experience)

This is an experiment on a clean branch. Nothing lands on `master` until the
branch reaches parity and the owner decides to adopt it; if it doesn't pan out,
the branch is deleted.

**Status (2026-10-27): milestones 0–8 implemented** on `feat/httpx-library`;
milestone 9 (merge and release 0.3.0) is the owner's decision. Parity was 9/9
on the replay suite before Scrapy was removed; two post-implementation reviews
(correctness, plan conformance) were addressed. Where the build differs from
the text below:

- **Import boundary:** httpx imports `rich` itself when it's installed (its own
  CLI), so the boundary test covers `typer`, `yaml` and `scrapy` only.
- **CLI imports:** besides the public API, the CLI shares internal helpers
  (URL validation, `FetcherFactory`, writer extensions).
- **Adapters** are `run(ctx, emit)` coroutines, not async generators, so they can
  use TaskGroups freely; each emitted item gets its own delivery task, counted
  only once a delivery slot is held (no `aclosing` needed).
- **Async runs** have no `weakref.finalize` backstop; use `async with` or
  `aclose()`. Sync runs hold their generator weakly, so a bare `break` cleans up.
- **Replay corpus:** five recorded corpora (multi-store with a dead domain,
  collections, images, WooCommerce, robots-blocked) are pinned as snapshots in
  `tests/test_replay.py`. Sitemap fallback, 403 and the §3.10 bug cases are
  covered by FakeFetcher flow tests and the e2e local servers instead, since
  no real store reliably produces them.
- **Parity tooling** (`_parity/`, `scripts/parity.py`) was deleted at
  switchover, as planned.
- **Per-host state** is keyed by origin (see §4.6).

## 1. Goals and non-goals

**Goals**

1. Replace Scrapy with a small asyncio core built on httpx.
2. Ship an importable Python library and the CLI as equal, first-class
   interfaces. The CLI uses only public or documented-provisional library API
   (§4.3).
3. Preserve intended behavior (§3), fix known bugs (§3.10), and make every
   other change an explicit, signed-off deviation (§9).
4. Leave a real seam for future transports: `curl_cffi` impersonation,
   Playwright, Lightpanda over CDP, and a future HTML-rendering adapter.
5. Make the library good for agents writing scripts (primary) and usable from
   async code and notebooks: typed and documented, no side effects, and **no
   silent failure**: every outcome an agent cares about (a failed store, a
   partial catalog, a likely wrong platform, a failed image) reaches the caller
   without the caller having to know where to look.

**Non-goals for this branch**

- New platforms, platform auto-detection (a wrong-platform *hint* is in scope),
  `curl_cffi`, Playwright (§12).
- Changing item shapes. Items stay raw platform payloads plus today's metadata
  keys; the optional `summarize()` helper (§4.3) reads them without changing them.
- Resumable crawls (Scrapy `JOBDIR`).
- A public event/callback system (deferred; see §4.3).

## 2. Why

The workload changed under the framework. Since 2026-04-02 a typical Shopify
run is a few paginated `/products.json` requests per store; WooCommerce is the
same shape. Scrapy now costs more than it gives:

- **No usable library.** The Twisted reactor can't restart: a second
  `CrawlerProcess.start()` in one process raises `ReactorNotRestartable`
  (verified on Scrapy 2.14.2). A Scrapy-based `scrape()` would work once per
  Python session.
- **Results rebuilt from stats.** Per-store status and diagnostics are
  reconstructed from Scrapy stats after the run, which is the source of
  #17 and #19–#21.
- **Every feature is indirection.** `--bail` needed an extension, a reactor
  timer and a suppressed deprecation warning; the live counter needed a shared
  mutable list in settings; a Rich console broke settings pickling.
- **Hidden configuration.** `get_project_settings()` only loads
  `shopify_spy.settings` when a `scrapy.cfg` is found above the working
  directory, so installed CLIs silently run with Scrapy defaults (§3.10).
- Minor: 17 direct dependencies (Twisted, lxml, cryptography, pyOpenSSL,
  parsel, …). Import time is not a real cost (≈0.2 s warm vs ≈0.1 s for
  httpx + protego).

## 3. Behavior contract

The contract is the **intended** behavior: what the code does when
`shopify_spy.settings` is loaded (running from the repo), after the in-flight
fixes for #17 and #19–#21 land on `master`. Verified against `dd80ad4` and the
Scrapy 2.14.2 source by review.

### 3.1 Run model

- One Scrapy crawler per store URL, all concurrent. So these are **per store**:
  `limit`, `bail`, UA state, throttle slots, concurrency caps.
- Concurrency per store: `CONCURRENT_REQUESTS_PER_DOMAIN` (from
  `--concurrent`, default 16) **and** `CONCURRENT_REQUESTS` (16, shared with
  image hosts). Effective cap is the lower of the two.
- No cap on concurrently running stores.

### 3.2 Shopify

1. If `products`: `GET /products.json?limit=250&page=1`.
   - Request failure (after retries, including non-2xx), non-JSON body, or
     missing/empty/non-list `products` → fall back to `/sitemap.xml`.
   - Otherwise one item per product:
     `{"product": {...}, "url": "<scheme>://<host>/products/<handle>.json", "store": "<host>", "image_urls": [...]}`.
     `<host>` is the post-redirect host.
   - Exactly 250 products → next page. Fewer → stop; then if `collections`,
     fetch the sitemap.
   - The store is marked "bulk-covered" after the first successful page.
2. If not `products`: straight to the sitemap.
3. Sitemap: follow all sitemap-index children; gzip bodies handled; entry
   filter: `/products/` skipped if bulk-covered else `loc + ".json"`;
   `/collections/` → `loc + ".json"`. Rules: `/products/` → product
   (if `products`), `/collections/` → collection (if `collections`).
4. Sitemap-path items: the `.json` payload (`{"product": ...}` /
   `{"collection": ...}`) plus `url` (request URL), `store` (host), `image_urls`.
   Non-2xx product/collection responses are dropped silently.
5. `image_urls` = recursive `src` search when images are enabled, else `[]`.

### 3.3 WooCommerce

- `GET /wp-json/wc/store/v1/products?per_page=100&page=N` from 1 until `[]`.
- Item = flat product + `store` + `image_urls` (`images[].src`). No `url` key.
- Any error or non-JSON ends that store; there is no fallback.
- `products`/`collections` flags have no effect; the CLI warns.

### 3.4 Network

| Behavior | Contract |
|---|---|
| Timeout | `network.timeout` (180 s) |
| Retries | `network.retries` (2) on 500, 502, 503, 504, 522, 524, 408, 429, 403 and on transport errors (timeouts, connection refused/lost/reset, DNS, `OSError`, truncated bodies). Retries re-enter the queue behind the slot delay (no explicit backoff) |
| User agent | One random browser UA per store; changed only when retrying a 403. `-A/--user-agent` fixes the UA and disables rotation |
| Default headers | `Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8`, `Accept-Language: en`, `Accept-Encoding: gzip, deflate` |
| robots.txt | Obeyed by default, fetched once per host per store. Rules evaluated against the `Scrapy/2.14.2` token (UA middleware runs after robots), or the `-A` UA. Any response body, even 4xx/5xx, is parsed as robots.txt (an HTML error page has no rules, so allow). Transport failure → allow. `-i/--ignore-robots` disables |
| TLS | Certificates **not** verified (Scrapy's context factory uses `verify=False`) |
| Cookies | Disabled |
| Redirects | 301/302/303/307/308 followed, max 20 (not for image requests) |
| Duplicate filter | Per store, by method + canonical URL (+ body); not applied to robots or image requests |
| Size limits | Abort > 1 GiB, warn > 32 MiB |
| Proxies | `http_proxy`, `https_proxy`, `no_proxy` honored (httpx also honors `ALL_PROXY`, D16) |
| HTTP version | HTTP/1.1 |

### 3.5 AutoThrottle

Always on (see §3.10 for the `--no-throttle` bug). Per slot (host): delay
starts at `start_delay`. On each downloaded response (per redirect hop),
`target = latency / target_concurrency`;
`new = max(target, (delay + target) / 2)`, clamped to
`[DOWNLOAD_DELAY (0), max_delay]`; non-200 responses may only increase the
delay. Transport exceptions don't adjust the delay. When the delay is
non-zero, one request leaves the slot per pass; the random factor
`uniform(0.5, 1.5) × delay` is redrawn every time the slot queue is processed
(on enqueue and on each download finish), not once per interval. Latency is
time to response headers. (Verified against Scrapy 2.14.2 source.)

### 3.6 Images (when `scrape.images`)

- Each URL in `image_urls` is downloaded through robots/throttle/retry, with its
  own host slot. Redirects are **not** followed (3xx fails the image).
- Saved to `<images_dir>/full/<sha1(url)>.jpg`, re-encoded as RGB JPEG
  (RGBA/P-mode PNG and WebP flattened onto white).
- Skipped if the file exists and is < 90 days old (`status: "uptodate"`).
- `scraped_images: [{"url", "path", "checksum", "status"}]` added to the item;
  `checksum` is the md5 of the re-encoded JPEG. Failures are omitted. The item
  is emitted only after its images finish.

### 3.7 Output files (CLI)

- `<output>/data/<platform>_spider_<UTC ts>.<ext>`; no file if zero items.
- **JSONL / JSON:** UTF-8, `ensure_ascii=False`. JSON is `[\n{…},\n{…}\n]`.
- **CSV:** header from the first item's keys (later keys are dropped); lists of
  strings joined with `,`; everything else via `str()`, so nested dicts appear
  as Python repr.
- **XML:** `<?xml version="1.0" encoding="utf8"?>`, `<items><item>…</item></items>`,
  dicts as nested elements, lists as `<value>` children, `None`/`True` as text,
  newlines but no indentation.
- `<output>/status/<platform>_spider_<ts>_status.json`: `items_scraped`,
  per-URL `{url, items, status}`, `finish_reason`, `duration_seconds`, `log_file`.
- Log file under `platformdirs.user_state_path("shopify-spy")/logs/`.
  `--verbose` adds DEBUG on stderr; `--quiet` → WARNING.

### 3.8 CLI

`scrape [URLS...]`, `--url-file`, TTY prompt, `--products/--no-products`,
`--collections/--no-collections`, `--images/--no-images`, `-o`, `-F`,
`--concurrent`, `--throttle/--no-throttle`, `--limit`, `-A`, `-p`, `-i`,
`--bail`, `--peek`, `-v`, `-q`, `--config`; `init [PATH] [--force]`;
`-V/--version`. Banner, live counter, per-store summary, zero-item diagnostics
(wording per post-fix `master`), exit 1 on zero items. First Ctrl-C: in-flight
requests finish and their items are still emitted, `finish_reason: shutdown`;
second Ctrl-C: immediate stop.

### 3.9 Config

YAML schema (`scrape`, `output`, `network`, `throttle`), search order
(`--config`, `./shopify-spy.yaml`, `~/.config/shopify-spy/config.yaml`),
precedence defaults → file → CLI.

### 3.10 Known bugs: fix, don't preserve

| Bug | Fix in rewrite |
|---|---|
| Installed CLI (no `scrapy.cfg` above cwd) runs with Scrapy defaults: `Scrapy/x` UA, no 403 retry or rotation, no throttle, cookies on, no image pipeline, `--bail` inert, telnet on. Verified. **Also worth a quick fix on `master` now.** | Configuration is explicit; no ambient settings discovery |
| `--no-throttle` / `throttle.enabled: false` do nothing (`AUTOTHROTTLE_ENABLED=True` hard-coded in `settings.py`) | Honor the flag |
| `--platform` default is always applied, so `scrape.platform` in the config file is ignored | Option default `None`; config value wins when the flag is absent |
| `--peek` with N URLs prints N items (limit is per store); images not disabled; output dirs created | Peek stops after the first item overall; no images; no dirs |
| Bulk pagination failing at page > 1 falls back to the sitemap but skips all products (store already "bulk-covered") | Sitemap fallback after a partial bulk run fetches products whose handles weren't seen |
| Valid non-object JSON from `/products.json` (e.g. `[]`) crashes the callback; no fallback | Treat as a fallback condition |
| Second Ctrl-C leaves `finish_reason: unknown` and possibly truncated JSON | Writers close in `finally`; status says `shutdown` |
| #17, #19–#21 (status/diagnostic attribution) | Being fixed on `master`; the rewrite matches the post-fix behavior |

## 4. Architecture

### 4.1 Layers

```
 CLI          cli.py, config.py (typer, rich, yaml) — config, progress, files, exit codes
                │ public + provisional API only (scrape_async, ScrapeRun, ItemWriter, write_status)
 Public API   api.py      scrape(), scrape_async(), ScrapeRun, RunResult, write_items()
 Runner       runner.py   per-store tasks, isolation, limits, bail, shutdown
 Adapters     shopify.py  woocommerce.py  sitemap.py  images.py
 Policy       session.py  StoreSession (per store) + HostRegistry (per run)
              policy.py   throttle, retry, robots, identities
 Transport    fetch.py    Fetcher protocol, HttpxFetcher, FakeFetcher, Record/Replay
 Writers      export.py   jsonl/json/csv/xml, status file
```

**Import boundary:** only the CLI modules import `typer`, `rich`, `yaml`.
`import shopify_spy` imports none of them (enforced by a test). `config.py` is
never imported by `__init__` and imports `yaml` inside its loader function.

**Layering rule:** lower layers never import higher ones. `fetch.py` imports
nothing from `policy.py` or `session.py`; transports describe themselves with a
`FetcherCapabilities` value defined in `fetch.py`, and `policy.py` picks the
retry behavior from it.

### 4.2 Package layout (flat)

```
shopify_spy/
  __init__.py      # public re-exports, __all__
  py.typed
  api.py           # scrape, scrape_async, ScrapeRun, RunResult, StoreResult, StoreStatus,
                   # ScrapeFailed, read_url_file
  summary.py       # summarize(), ItemSummary
  runner.py
  session.py       # HostRegistry, StoreSession, StoreStats
  policy.py        # AutoThrottle, RetryPolicy, RobotsCache, Identity, identities/UA list
  fetch.py         # Request, Response, Fetcher, HttpxFetcher, FakeFetcher, Recording/ReplayFetcher
  sitemap.py
  shopify.py
  woocommerce.py
  images.py
  export.py        # write_items(), ItemWriter, status file
  utils.py         # normalize_url, find_all_values, canonicalize_url
  config.py        # YAML models (CLI); Config.to_kwargs()
  cli.py           # typer app (replaces today's cli.py at switchover)
```

During development the new CLI is exposed as a temporary `shopify-spy-next`
script (`shopify_spy/_cli_next.py`). At switchover it replaces `cli.py` and the
Scrapy modules are deleted. The legacy modules are not edited, with one
exception: milestone 0 adds parity-only code in a new `shopify_spy/_parity/`
package (record/replay downloader middlewares), enabled by the parity script
through Scrapy settings overrides, never by `settings.py` (§7.2).

### 4.3 Public API

#### Committed examples (README and docstrings)

```python
import shopify_spy as spy

# 1. Script: data plus failures (README hero example)
run = spy.scrape(["a.com", "b.com"], on_failure="warn")
spy.write_items(run, "products.jsonl")  # streams any iterable
for s in run.result.failed:
    print(s.input_url, s.status, s.hint)

# 2. Learn the schema
item = spy.scrape("a.com").first()  # raises ScrapeFailed with diagnostics if 0 items
print(spy.summarize(item))  # same fields on both platforms

# 3. Filter across platforms, write CSV
rows = (spy.summarize(i) for i in spy.scrape(urls))
spy.write_items((r.asdict() for r in rows if r.min_price and r.min_price < 50), "cheap.csv")

# 4. Retry blocked stores
r = spy.scrape(spy.read_url_file("stores.txt"), on_failure="ignore").collect()
retry = [s.input_url for s in r.failed if s.status == "robots_blocked"]
spy.write_items(spy.scrape(retry, respect_robots_txt=False), "products.jsonl", append=True)

# 5. Async code / notebooks
result = await spy.scrape_async(url, platform="woocommerce", max_items_per_store=50)
async for item in spy.scrape_async(urls):
    ...
```

#### Entry points

- `scrape(urls, **options) -> ScrapeRun` and `scrape_async(urls, **options) ->
  AsyncScrapeRun`. Both are plain functions (no `await` needed to call them).
  `ScrapeRun` supports `for`, `with`, `.collect()`, `.first()`.
  `AsyncScrapeRun` supports `async for`, `async with`, and `await` (returns a
  `RunResult` with items).
- `urls`: a single `str`, or an iterable of `str`. A string containing commas
  or whitespace is rejected with a message suggesting a list. Malformed URLs
  raise `ValueError` at call time; DNS failures become `no_response`.
- One platform per call; mixed lists must be split (per-URL platforms later).
- `platform="auto"` raises: "auto-detection isn't supported yet; use 'shopify'
  or 'woocommerce'."
- `read_url_file(path) -> list[str]`: the CLI's URL-file rules (blank lines and
  `#` comments skipped).

#### Options and their config/CLI names

Keyword-only, validated eagerly (pydantic). One table, tested for consistency:

| Library kwarg | YAML key | CLI flag | Default |
|---|---|---|---|
| `platform` | `scrape.platform` | `-p/--platform` | `"shopify"` |
| `products` | `scrape.products` | `--products/--no-products` | `True` |
| `collections` | `scrape.collections` | `--collections/--no-collections` | `False` |
| `images` | `scrape.images` | `--images/--no-images` | `False` |
| `images_dir` | `output.dir` + `output.images_subdir` | `-o` (+ config) | `None`; required when `images=True` |
| `max_items_per_store` | `scrape.limit` | `--limit` | `None` |
| `bail` (seconds, `float \| None`) | `scrape.bail` | `--bail` | `30` (CLI/config), `30` (library) |
| `concurrent_requests` (per host) | `network.concurrent_requests` | `--concurrent` | `16` |
| `request_timeout` | `network.timeout` | — | `180` |
| `retries` | `network.retries` | — | `2` |
| `user_agent` | `network.user_agent` | `-A/--user-agent` | `None` (rotate) |
| `respect_robots_txt` | `network.respect_robots_txt` | `-i/--ignore-robots` | `True` |
| `verify_tls` | `network.verify_tls` | `--insecure` | `True` (D14) |
| `max_concurrent_stores` | `network.max_concurrent_stores` | `--max-stores` | `16` (D3) |
| `throttle` | `throttle.enabled` | `--throttle/--no-throttle` | `True` |
| `throttle_start_delay`, `throttle_max_delay`, `throttle_target_concurrency` | `throttle.*` | — | `1`, `60`, `1.0` |
| `on_failure` | — | — (CLI uses `"ignore"`) | `"raise"` |
| `strict` | — | — | `False` |
| `fetcher_factory` (provisional) | — | — | `None` (httpx) |

`request_timeout` (not `timeout`) so nobody mistakes it for a whole-run limit.
`max_items_per_store` (not `limit`) so nobody mistakes it for a total; docs show
`itertools.islice` for a total cap. Parity-only switches (backoff off,
`max_concurrent_stores=None`) are private `_`-prefixed kwargs, not in this table.

#### Failure handling: nothing silent

- `on_failure="raise"` (default): when iteration ends, raise `ScrapeFailed`
  if every store failed (with `strict=True`: if any store failed). The message
  lists each failed store's input URL, status and hint; `exc.result` holds the
  `RunResult`. Items already yielded stay yielded.
- `on_failure="warn"`: `warnings.warn` with the same message. `"ignore"`: rely
  on `run.result`. The CLI passes `"ignore"` and renders its own diagnostics.

#### Results

- `StoreResult`: `input_url` (as given), `url` (normalized), `store` (host),
  `platform`, `items`, `status`, `complete` (`False` for limit, bail, error,
  shutdown, closed), `finish_reason`, `hint` (e.g. "looks like WooCommerce; pass
  platform='woocommerce'"), `responses_by_status`, `robots_disallowed`,
  `images_failed`, `errors` (with tracebacks for `error`), `duration`.
- `RunResult`: `items` (only from `collect()`/`await`), `stores`, `ok`,
  `failed`, `finish_reason`, `raise_for_status()`.
- `run.result` raises `RuntimeError("iterate to completion or call collect()")`
  while the run is still going. After a `break` or `close()` it is valid, with
  unfinished stores marked `finish_reason="closed"`.
- `run.stores` returns a snapshot at any time (the CLI uses it for progress).

`StoreStatus` is a `StrEnum`; new values may be added (documented). Mapping:

| `StoreStatus` | Meaning | `is_failure` | Status-file `status` (CLI parity string) |
|---|---|---|---|
| `ok` | Finished, items > 0 | no | `ok` |
| `partial` | Items > 0, then ended by an error or exhausted retries | only with `strict` | `ok` (+ `code: partial`) |
| `empty` | Finished cleanly with 0 items | no | post-fix `master` string |
| `timed_out` | Bail fired before the first item | yes | `timed out` |
| `forbidden` | 403 after retries | yes | `403 Forbidden` |
| `not_found` | 404 on the platform endpoint and fallback | yes | `404 Not Found` |
| `robots_blocked` | All needed URLs disallowed | yes | `blocked by robots.txt` |
| `tls_error` | Certificate verification failed | yes | new string (D14) |
| `no_response` | DNS/connection failures only | yes | `no response` |
| `error` | Unexpected exception in this store | yes | new string |
| `stopped` | Closed or shut down before producing anything (incl. never started) | no | `stopped` |

Limits, shutdown and `close()` don't change the status; they set
`finish_reason` and `complete=False`. The status file keeps today's per-URL
`status` strings (as fixed on `master`) and adds a `code` field with the
`StoreStatus` value, so existing parsers keep working.

#### Wrong-platform hint (not auto-detection)

When a Shopify run gets a 404 from `/products.json` and finds no product
entries, the store does one cheap probe of `/wp-json/`; when a WooCommerce run
gets a 404 from the Store API, it probes `/products.json`. A positive probe
sets `StoreResult.hint`, and the CLI diagnostics print the same line.

#### Items and `summarize()`

- Items are plain `dict[str, Any]` with unchanged keys. `TypedDict`s
  (`ShopifyProductItem`, `ShopifyCollectionItem`, `WooCommerceProductItem`)
  document the shapes, including that Shopify `url` points at the `.json`.
- `summarize(item) -> ItemSummary` (provisional) reads any item without
  changing it: `kind` (product/collection), `platform`, `id`, `title`,
  `min_price`/`max_price` as `Decimal` (Shopify variant price strings;
  WooCommerce minor units divided by `currency_minor_unit`), `currency`
  (`None` for Shopify, which doesn't expose it), `html_url`, `store`,
  `image_urls`. `.asdict()` for writers.

#### Other API

- `ScrapeRun.first() -> dict`: returns the first item, closes the run, and
  raises `ScrapeFailed` with diagnostics if there are none. Docs note that a
  product URL is reduced to its store, so this is the store's first item.
- `write_items(items, path, *, format=None, fields=None, append=False)`:
  streams any iterable (including a run); format from the extension;
  `append=True` for JSONL and CSV; `fields=` fixes CSV columns (the CLI passes
  them, D2). `collect()`'s docstring warns about memory on large catalogs.
- `images=True` without `images_dir` raises `ValueError` naming the fix.
  A relative `images_dir` is resolved at call time. Docs show
  `Path(images_dir) / record["path"]` for absolute paths.

#### Side effects and stability

- No files unless images are on, no config discovery, logging only through
  `logging.getLogger("shopify_spy")` with a `NullHandler`.
- **Public:** `scrape`, `scrape_async`, `ScrapeRun`, `AsyncScrapeRun`
  (including `request_shutdown()`), `RunResult`, `StoreResult`, `StoreStatus`,
  `ScrapeFailed`, `write_items`, `read_url_file`.
- **Provisional** (documented, may change): `summarize`/`ItemSummary`,
  `ItemWriter`, `write_status`, `fetcher_factory`.
- No event/callback API in v1; the CLI counts items as it iterates and reads
  `run.stores` snapshots.
- The `scrape()` docstring states: what counts as a failure and the default
  `on_failure`; that `max_items_per_store` is per store; that `images` requires
  `images_dir` and where files land; the running-loop error; that it reads no
  config files.

### 4.4 `ScrapeRun` lifecycle and the sync wrapper

All scraping is async, and the project uses no threads. Scripts are the
primary target; notebooks are supported through the async API.

| Context | Call | Execution |
|---|---|---|
| Script (primary) | `for item in scrape(...)` | Private loop on the caller's thread |
| Async code | `async for item in scrape_async(...)` | Caller's loop |
| Jupyter | `async for item in scrape_async(...)` (top-level `await`) | Notebook's loop |
| Jupyter, sync style | `for item in scrape(...)` | Raises `RuntimeError`: "An event loop is already running (e.g. Jupyter). Use `async for item in scrape_async(...)` instead." |

A sync call can't run a second event loop on a thread that already runs one.
Rejected workarounds: a worker-thread fallback (threads for a declining use
case) and `nest_asyncio` (patches asyncio internals, unmaintained).

- Arguments validated eagerly; nothing runs until iteration starts.
- **Driver task, not a generator holding tasks.** One long-lived *driver* task
  owns the store `TaskGroup` and puts items into a bounded `asyncio.Queue`. No
  `TaskGroup` or `asyncio.timeout` is ever held across a `yield` (prototyped:
  doing so makes `break`/close raise `BaseExceptionGroup([GeneratorExit])`,
  wraps errors in `ExceptionGroup`, and silently disables timeouts, because
  each `runner.run()` step is a new task). Both APIs read from this queue:
  - sync: `__iter__` opens an `asyncio.Runner` (3.11+) and yields
    `runner.run(queue.get())` per item;
  - async: `__anext__` awaits `queue.get()`.
- Closing (`break`, exception, leaving `with`, `close()`/`aclose()`) cancels the
  driver and waits for it. `weakref.finalize` is a backstop for runs that are
  never closed.
- **Errors unwrap:** exception groups from the driver are unwrapped so the
  caller sees the original exception (per-store errors never reach here; they
  become `StoreStatus.error`).
- `asyncio.Runner` turns Ctrl-C into cancellation of the running task
  (prototyped: stores cancelled, handler restored).
- **Paused-loop clock.** In the sync API the loop is paused while the caller
  handles an item, so nothing runs (natural backpressure) — but the loop clock
  keeps advancing, so on resume timers are overdue. Prototyped: a 2 s consumer
  pause caused spurious httpx timeouts and bailed a store that was 0.3 s from
  its first item. Fix (prototyped, removes both): the sync runner passes
  `loop_factory=` an event loop subclass whose `time()` excludes paused
  intervals, so timeouts, bail and throttle count only active time. Server-side
  keep-alive closes during a long pause still happen; `RemoteProtocolError` on
  a reused connection is retried (it's a retryable transport error).
- The async API needs no special clock: the caller's loop keeps running, and
  the bail clock stops at the adapter's first raw item, before the queue.
- Exceptions raised by the caller's own code propagate normally (same thread).
- `run.stores` returns a snapshot copy; `run.done` reports completion; iterating
  a run twice raises.
- **Two kinds of stop:** `close()` (and `break`, `aclose()`) cancels at once and
  marks unfinished stores `finish_reason="closed"`. `request_shutdown()` is the
  graceful stop the CLI's first Ctrl-C uses: no new requests are scheduled,
  in-flight requests finish (bounded wait), their items are still yielded, and
  stores end with `finish_reason="shutdown"`; iteration then ends normally.

### 4.5 Transport: `Fetcher`

```python
@dataclass(frozen=True)
class Identity:  # what a request presents as
    user_agent: str | None
    headers: Mapping[str, str]
    impersonate: str | None = None  # for curl_cffi later


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    identity: Identity
    timeout: float  # from request_timeout
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    cookies: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    url: str  # final URL
    status: int
    headers: Mapping[str, str]
    body: bytes
    latency: float  # seconds to headers
    set_cookies: Mapping[str, str]


@dataclass(frozen=True)
class FetcherCapabilities:
    applies_identity_headers: bool  # False for browsers that bring their own
    can_impersonate: bool  # curl_cffi
    renders_js: bool  # browsers


class Fetcher(Protocol):
    capabilities: FetcherCapabilities

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc) -> None: ...
    async def fetch(self, request: Request) -> Response: ...
```

`policy.py` chooses retry and rotation behavior from `capabilities` (e.g. no
UA rotation for a browser solving a challenge), so `fetch.py` stays the
bottom layer.

- Runs take a `fetcher_factory: Callable[[], Fetcher]`, so each run builds its
  own fetcher inside its own loop (an `httpx.AsyncClient` is bound to one loop).
- `HttpxFetcher` (details verified against httpx 0.28.1 by prototype):
  - **Single hop:** `follow_redirects=False`. Redirects are handled by the
    session hop by hop (§4.6), so each hop goes through robots, throttle and
    latency measurement like Scrapy's redirect middleware.
  - **Total deadline:** each fetch is wrapped in `asyncio.timeout(request_timeout)`.
    httpx timeouts are per phase (a trickled body never times out), while
    Scrapy's `DOWNLOAD_TIMEOUT` is a total.
  - **Latency:** measured ourselves at response headers while streaming
    (`Response.elapsed` on a stream measures to close).
  - **Size limits:** counted on *decoded* bytes from `aiter_bytes()`
    (`num_bytes_downloaded` counts compressed bytes), like Scrapy.
  - **Encoding:** client-level pinned `Accept-Encoding: gzip, deflate` (a
    per-request header would replace it too). Bodies are decoded by us, not
    httpx: gzip (incl. concatenated members) and deflate, in `Content-Encoding`
    order; an unknown token (servers send e.g. `utf-8`) leaves the body as is,
    as Scrapy did (changed after code review; originally an error).
  - HTTP/1.1 (`http1=True, http2=False`), cookies never stored (a deny-all jar),
    `trust_env=True`, `verify` from `verify_tls` (client-level only, which is
    fine because the client is per run).
- Transport errors map to `FetchTimeout`, `FetchConnectionError`,
  `FetchTooLarge`, `FetchIncomplete` (truncated bodies, `RemoteProtocolError`),
  `FetchInvalidURL`, `FetchTLSError` (certificate verification only; other TLS
  failures are retried connection errors).
- **No cookies** (parity with Scrapy's settings). The per-store cookie plumbing was
  removed after code review (it was never enabled and would have leaked across
  hosts); the browser→HTTP clearance handoff (§12) adds a domain-aware jar.
- HTML rendering for a future Hydrogen adapter gets its own `PageRenderer`
  protocol later; `Fetcher` stays request/response.

### 4.6 Policy: `HostRegistry` and `StoreSession`

**`HostRegistry` (one per run).** All per-host state is keyed by origin
(scheme + host + port):

| State | Key | Value |
|---|---|---|
| robots.txt cache | origin | parsed rules |
| Concurrency semaphore | origin | `concurrent_requests` |
| AutoThrottle slot | origin | delay state |

Shared hosts like `cdn.shopify.com` get one robots fetch and one politeness
budget for the whole run (D10). Scrapy keys slots by hostname, but per store;
sharing across stores by hostname alone would make the e2e test stores (all on
`127.0.0.1`, different ports) throttle each other. For real stores, origin and
hostname keys behave the same.

**`StoreSession` (one per store):** identity and rotation, cookie jar,
dedupe set, a store-level cap of **16 concurrent requests** (fixed, like
Scrapy's `CONCURRENT_REQUESTS`; not changed by `--concurrent`), `StoreStats`.

So `--concurrent N` means at most N requests to one host *across the run*
(today: per store); the store cap of 16 still applies on top.

`session.get(url, *, follow_redirects=True, dedupe=True) -> Response | None`.
Redirects (301/302/303/307/308, max 20) are followed here, one hop at a time;
each hop re-enters steps 2–7 (robots, slot, throttle, retry) as a new request,
like Scrapy's redirect middleware. Per request:

1. Dedupe (canonical method + URL + body) → skip, counted.
2. robots.txt via the registry (unless ignored) → disallowed: skip,
   `robots_disallowed += 1`. Evaluated as `shopify-spy` → `*` (D4), or the
   fixed `-A` UA.
3. Acquire store cap + host semaphore; wait for the host throttle gate.
4. Fetch with the store's current identity.
5. Update throttle from latency/status.
6. Retry per the `RetryPolicy` chosen for the fetcher's capabilities: retry
   set or retryable transport error → retry up to `retries`; a 403 rotates
   identity first (HTTP transports only). Delay per D5: exponential backoff
   with jitter; `Retry-After` honored but capped at `throttle_max_delay`, and,
   while the store's bail clock is still running, also below the remaining
   bail time. Exhausted → `RequestFailed`.
7. Record the outcome in `StoreStats`.

Store URLs are normalized and de-duplicated before the run (D11).

### 4.7 Adapters

```python
class PlatformAdapter(Protocol):
    def items(self, ctx: StoreContext) -> AsyncIterator[dict[str, Any]]: ...
```

- **WooCommerce:** sequential page loop.
- **Shopify:** bulk page loop, tracking seen handles → fallback / collections
  via `sitemap.iter_locs()` → fetch `.json` documents concurrently (bounded by
  the session, not the helper).
  - **Handle matching** (partial-bulk fix, §3.10): the handle is the path
    segment after the last `/products/`, percent-decoded and lowercased. Host,
    locale prefix (`/en-ca/products/x`) and query are ignored, so
    `www.`/apex/`myshopify.com` variants match. Never match on full URLs.
    Fixtures cover a locale prefix and a different sitemap host.
  - After `/products.json` 404s and no product entries are found, run the
    wrong-platform probe (§4.3).
- **`sitemap.py` (untrusted input):** `defusedxml` parsing with `iterparse`;
  decompressed size capped at 50 MiB per file; index depth ≤ 3 with a visited
  set; child sitemaps restricted to the store's host (D12).

### 4.8 Runner

- One task per store, at most `max_concurrent_stores` at once (D3).
- **Isolation:** each store task catches `Exception` and records
  `StoreStatus.error` with the traceback. Only cancellation and bugs in the
  runner itself propagate. (A bare `TaskGroup` would cancel every store when
  one raised.)
- **Limit:** stop the store's adapter after `max_items_per_store`
  (`finish_reason="item_limit"`).
- **Bail:** the clock starts when the store acquires its run slot and stops
  when the adapter yields its first raw item (before image downloads and before
  the consumer queue). In the async API a slow consumer therefore can't cause a
  bail; in the sync API that also depends on the paused-loop clock (§4.4).
  Small deviation from Scrapy, where images delay the timer's cancellation.
- Adapter generators are wrapped in `contextlib.aclosing` so cancellation
  closes them cleanly.
- **Images:** per store, up to 8 items wait on their image downloads at once
  (Scrapy also processes pipeline items concurrently), so the adapter isn't
  paused per item. Item order within a store is not guaranteed (it isn't
  today either).
- **Shutdown:** `request_shutdown()` stops scheduling, lets in-flight requests
  finish within a bounded wait and still emits their items, and marks
  `shutdown`. `close()`/a second signal cancels.
- Run `finish_reason` derived as on post-fix `master`.

### 4.9 Images

`sha1(url)` names under `full/`, 90-day freshness, md5 checksum, the
`scraped_images` records. Failures are logged, omitted from the item (parity)
and counted in `StoreResult.images_failed`. Format per D1. Redirects: followed
(D13).

### 4.10 Writers

`write_items(items, path, *, format=None, fields=None, append=False)` and a
streaming `ItemWriter` context manager used by the CLI. Formats per §3.7 with D2.

- **CSV columns:** `fields=` when given; otherwise the first item's keys
  (parity). The CLI passes, for Shopify, `product`, `collection` (only when
  collections are on), `url`, `store`, `image_urls`, `scraped_images` (only
  when images are on). For WooCommerce it passes nothing, so columns come from
  the first item, as today; later keys are dropped and logged once.
- `append=True` only for JSONL and CSV (CSV skips the header when appending to
  a non-empty file).
- `write_status()` for the status file. Writers close in `finally`, so partial
  output is always valid.

### 4.11 CLI

- Builds kwargs from `Config` via the §4.3 option table, then
  `asyncio.run(main())` on the **main thread** using `scrape_async` with
  `on_failure="ignore"`.
- Two-stage Ctrl-C with `signal.signal` + `loop.call_soon_threadsafe`: first
  signal → `run.request_shutdown()`, second → `run.aclose()`. Works on Windows,
  where `loop.add_signal_handler` doesn't exist.
- `--peek` uses the equivalent of `first()` (one item total, §3.10).
- `--limit` help text fixed to "per store".
- New flags: `--insecure` (D14), `--max-stores` (D3).
- Live counter, summary and diagnostics from items iterated and `StoreResult`s.
- Log content defined explicitly: INFO = one line per store start/finish, per
  bulk page, fallbacks, retries, robots blocks; DEBUG = one line per request
  (method, URL, status, latency, attempt). Replaces Scrapy's "Crawled (200)" lines.

## 5. Dependencies

| | Package | Reason |
|---|---|---|
| Keep | `typer`, `rich`, `pyyaml` | CLI only |
| Keep | `pydantic` | Config + argument validation + result models |
| Keep | `platformdirs` | Log/state paths |
| Add | `httpx` | Default transport |
| Add | `protego` | robots.txt with `*`/`$` wildcards; the parser Scrapy uses today |
| Add | `defusedxml` | Safe parsing of untrusted sitemaps |
| Drop | `scrapy` | Replaced |
| Drop | `pillow` | Only for JPEG re-encoding (D1) |
| Dev | `respx` (for `HttpxFetcher` tests only), `pytest-asyncio`, `pytest-cov` | Consolidate dev deps into one place (today split between `[dependency-groups]` and `optional-dependencies.dev`) |

CLI deps stay required so `uv tool install shopify-spy` keeps working.
Later extras: `[stealth]` → `curl_cffi`, `[browser]` → `playwright`.

## 6. Python and platforms

- `requires-python >= 3.11` (`TaskGroup`, `asyncio.timeout`; 3.10 is EOL in
  October 2026). Ruff `target-version = "py311"`. CI matrix 3.11–3.14.
- Add one Windows and one macOS CI job (signals, paths, event loops). The package
  claims "OS Independent".

## 7. Testing

### 7.1 Layers

1. **Unit:** sitemap parsing (index, gzip, malformed, depth/size limits),
   throttle math vs Scrapy's formulas, retry/rotation, robots wrapper,
   canonicalization, writers, status file, diagnostics classifier, image naming.
2. **Flow tests against `FakeFetcher`** (at the protocol seam, so they survive
   swapping transports): bulk paging, all fallback triggers, partial-bulk
   fallback, collections, WooCommerce paging, limit, bail, 403 rotation, robots
   block, dedupe, per-store error isolation, shutdown with valid partial output.
3. **`HttpxFetcher` tests with respx and a local server:** compression header
   (with and without `brotli`/`zstandard` installed), unsupported encodings,
   decoded-size limits, total deadline on a trickled body, error mapping
   (incl. truncated bodies), latency at headers.
4. **Session redirect tests:** hop-by-hop following, max 20, each hop through
   robots and throttle.
5. **Paused-loop tests:** a consumer that sleeps between items causes no
   spurious timeouts or bails; a server that closes idle keep-alive connections
   during a pause is retried.
6. **Public API:** sync iteration, `collect()`, `first()`, `await`, async
   iteration, early `break`, an exception inside iteration, a running loop in
   the caller, no stdout output, `on_failure` modes and `strict`,
   `run.result` before/after completion and after `break`,
   `request_shutdown()`, wrong-platform hint, `read_url_file`, the option
   table (every kwarg has its YAML key and CLI flag, and defaults match).
7. **Import boundary** test.
8. **Replay tests** in CI from the recorded corpus (§7.2).
9. **Integration (`-m integration`):** live runs against the Shopify demo store
   and one WooCommerce store; replaces `scrapy check`.

### 7.2 Parity: record once, replay into both engines

Live A/B runs are too noisy to gate on (random UA, catalog drift, doubled
request volume, 429s). Instead:

1. **Record** (milestone 0): `shopify_spy/_parity/record.py`, a Scrapy
   downloader middleware enabled by the parity script through settings
   overrides, records every outcome from a store set: bulk Shopify,
   sitemap-fallback Shopify, collections, WooCommerce, a robots-blocked store,
   a 403 store, a dead domain. Gaps (403/robots cases) are filled with
   synthetic fixtures.
   - **Key:** `(method, canonical URL)` → an **ordered list** of outcomes,
     consumed in sequence on replay (so 403-then-200 replays correctly).
   - **Outcomes** are responses (status, headers, body) or recorded transport
     exceptions (DNS failure, timeout, connection refused) for dead domains.
2. **Replay** the corpus into the legacy engine (`_parity/replay.py` middleware)
   and the new engine (`ReplayFetcher`). The legacy engine always runs with
   the repo root as working directory, so `scrapy.cfg` loads `settings.py`
   (§3.10 row 1). UA choice is seeded by calling `random.seed()` in the parity
   script before each run (the UA middleware uses the global `random`); no
   legacy module is edited.
3. **Diff** `scripts/parity.py`: items as multisets keyed by `url`/`id`,
   `_status.json`, image records, exit codes, summary text.
4. **Expected differences.** Deviations are built in as the milestones go (not
   deferred), so the diff script applies this table; anything not listed must
   be fixed or added to §9 first:

   | Change | Parity handling |
   |---|---|
   | D1 image bytes/extension | Normalize image `path` extension and `checksum` |
   | D2 CSV | Compare CSV via parsed rows; JSON-decode nested cells |
   | D3 store cap, D5 backoff | Disabled for parity runs (`_max_concurrent_stores=None`, `_backoff=False`) |
   | D4 robots token, D9, D12, D13, D15 | No corpus case should differ; a difference is a corpus bug or a new deviation |
   | D10 shared host state | Corpus stores use distinct hosts; difference only in request counts to shared CDNs (ignored) |
   | D11 duplicate store URLs | Corpus has no duplicates; covered by unit tests |
   | D14 TLS | Replay has no TLS; covered by unit tests and the milestone 0 measurement |
   | §3.10 bug fixes | Each fix has a corpus case whose legacy output shows the bug; the diff expects the fixed output |
   | D6, D7, D8 | Not observable in the diff |

5. **Live runs** are only a smoke test, the block-rate measurement for §12, and
   the TLS measurement for D14.

### 7.3 Exporter fixtures

Captured from Scrapy's exporters in milestone 0 for JSONL, JSON and XML
(byte-for-byte targets). CSV gets a golden file from the new writer, because
D2 changes it on purpose.

### 7.4 Existing test inventory

| File (lines) | Fate |
|---|---|
| `test_config.py` (162) | Keep; add tests for the `--platform`/config precedence fix |
| `test_utils.py` (131) | Keep `normalize_url`, `find_all_values`; delete `as_bool` tests with the function |
| `test_shopify.py` (479) | Keep URL-helper tests; rewrite spider-method tests as FakeFetcher flow tests |
| `test_woocommerce.py` (258) | Same as Shopify |
| `test_middlewares.py` (252) | Rewrite as identity/retry policy tests |
| `test_cli.py` (660) | Rewrite against the new CLI (mocks `CrawlerProcess` and Scrapy stat keys today). Exit criterion: every diagnostics assertion has a counterpart |
| integration (`scrapy check`) | Replace with library-based live tests |
| `test_e2e_diagnosis.py` (`-m e2e`, arriving with the `master` fixes) | **Keep and reuse.** It runs the CLI against local HTTP servers per store scenario (robots blocks, redirects, retried 403s, hangs). Add a parameter for the CLI command (`shopify-spy` vs `shopify-spy-next`) so the same scenarios run against both engines until switchover; extend the scenarios as a parity check that complements the replay corpus. Expected differences per the §7.2 table |

## 8. Milestones

One commit (or short series) per milestone on `feat/httpx-library`, reviewed
commit by commit. Rebase on `master` regularly. Milestones 1–5 only add files,
so their rebases are conflict-free; milestone 0 edits `pyproject.toml` and
`uv.lock` (resolve by regenerating the lock), and milestone 6 touches the CLI.
Milestones 0–5 start now; only milestone 6 waits for the `master` fixes.

Deviations (§9) are built into the milestone that owns the code, not deferred
to the end; §7.2 says how the parity diff handles each.

| # | Milestone | Done when |
|---|---|---|
| 0 | Baseline: Python 3.11 floor (D8), deps added, `_parity/` record/replay middlewares, recorded corpus (with §3.10 bug cases), exporter fixtures, block-rate snapshot, TLS verification failure count (D14) | Corpus replays through the legacy engine deterministically |
| 1 | **WooCommerce vertical slice:** `fetch.py` (Httpx/Fake/Replay), minimal `StoreSession`, WooCommerce adapter, runner (isolation, limit), `scrape`/`scrape_async`/`ScrapeRun`/`AsyncScrapeRun`, `first()`, `on_failure`, JSONL writer | `scrape()` works sync and async on replayed WooCommerce stores; import-boundary test passes |
| 2 | Policy: `HostRegistry` (D10), robots (D4), throttle, retry + backoff (D5) + identity rotation, dedupe, stats → `StoreResult`/`StoreStatus`, bail, `request_shutdown()`, wrong-platform probe, D3, D11, D14 | Flow tests for §3.4–3.5 and §4.3 results pass |
| 3 | Shopify: bulk, sitemap (hardened, D12), collections, partial-bulk fix with handle matching | Shopify flow tests pass; Shopify corpus replays |
| 4 | Images (D1, D13, concurrency, `images_failed`) | Image tests pass |
| 5 | Writers (JSON/XML byte-for-byte, CSV per D2 with `fields=`, `append`), status file with `code`, `write_items`, `read_url_file`, `summarize()` | Writer and summary tests pass |
| 6 | `shopify-spy-next` CLI on the library, rebased on post-fix `master`; option table, Ctrl-C, logging, diagnostics + hints, new flags; e2e tests parameterized by command; Windows/macOS CI | Rewritten CLI tests pass, diagnostics assertions all covered, e2e passes on both commands (with §7.2 expected differences) |
| 7 | Parity diff on the replay corpus; fix differences | Diff clean apart from the §7.2 expected-difference table |
| 8 | Switchover: new CLI becomes `shopify-spy`; delete Scrapy code, `scrapy.cfg`, Pillow; packaging and docs (below) | CI green on all platforms |
| 9 | Owner decision: merge and release `0.3.0`, or abandon the branch | — |

**Milestone 8 checklist:** `pyproject.toml` (drop `Framework :: Scrapy`
classifier and `scrapy` keyword, update description, Python classifiers,
`py.typed`, consolidated dev deps, regenerated `uv.lock`); CI (drop the
`scrapy check` integration step, new matrix, Windows/macOS); README (remove
"Built on Scrapy" and Scrapy-log wording at lines 10, 80, 88, 211; replace the
`CrawlerProcess` library example at lines 316–330 with the new API; add a
"Use as a library" section); `CLAUDE.md` architecture section; CHANGELOG
(below).

**Release notes.** CLI users don't need to change anything: same command,
flags and config. The notes list what they'll notice:

- *Action needed only for:* Python 3.10 users (upgrade Python); anyone running
  `scrapy crawl`/`scrapy check` directly or using the README's `CrawlerProcess`
  recipe (use the CLI or the new library).
- *Fixes that change behavior:* installs via pip/pipx/uv now actually get
  throttling, 403 retries with UA rotation, image downloads and `--bail`
  (previously inactive outside a repo checkout), so runs may be slower and can
  stop after 30 s without items; `--no-throttle` works; `scrape.platform` in
  config files now takes effect; `--peek` prints one item total; partial bulk
  runs no longer drop products.
- *Output changes:* CSV nested fields are JSON (D2); images keep their original
  format and extension (D1), so the 90-day cache misses once and every image
  downloads again; status files gain a `code` field (existing `status`
  strings unchanged); log lines differ (no "Crawled (200)").
- *New:* TLS verification on by default with `--insecure` (D14),
  `--max-stores` (D3), wrong-platform hints, the Python library.
- `--limit` is per store (help text corrected; behavior unchanged).

### Milestone 0 measurements (2026-10-26)

`scripts/measure_live.py` against 17 stores (12 Shopify, 5 WooCommerce), new
engine, robots.txt respected, one item per store:

| Result | Stores |
|---|---|
| ok | 10 |
| timed out | 4 (Bombas is a headless store: `products.json` 404, product `.json` redirects to `/404`; one Snow Devil timeout was transient and passed on rerun) |
| robots.txt blocks the Store API | 2 (woocommerce.com, woothemes.com) |
| empty | 1 |
| **TLS verification failures** | **0 of 17** |

D14 (verify by default) stands. Explicit blocking (403) was not seen; the main
gap is headless stores, which supports prioritizing §12 item 5 over stealth
transports.

## 9. Deviations needing sign-off

| ID | Deviation | Recommendation |
|---|---|---|
| D1 | Images saved as original bytes with real extension; checksum of original bytes | **Adopt.** Drops Pillow; lossless; changes `path`/`checksum` |
| D2 | CSV: nested values JSON-encoded; the CLI passes fixed Shopify columns via `fields=` (§4.10) so mixed product/collection files keep all columns; WooCommerce keeps first-item columns | **Adopt.** Today's CSV is Python repr and drops columns |
| D3 | `max_concurrent_stores` (default 16) | **Adopt.** Today 1,000 URLs start 1,000 crawlers |
| D4 | robots.txt evaluated as `shopify-spy` → `*` (today effectively `Scrapy` → `*`) | **Adopt** |
| D5 | Exponential backoff with jitter; `Retry-After` honored, capped at 60 s (independent of throttle settings) and, while the bail clock runs, below the remaining bail time. A 429 or 503 pauses the whole host for that wait and doubles its throttle delay (once per episode), which becomes a floor AutoThrottle can't go below; the floor decays 2% per success, like TCP congestion control; without Retry-After, rate-limited retries back off from 5 s (added after live testing: 0.2.x runs lost requests to 429s, #17; a field run saw AutoThrottle drift back to the refused rate, and a permanent floor doubled per 429 throttled bursty hosts to one request a minute) | **Adopt** |
| D6 | Keep `<platform>_spider_<ts>` file names | **Keep** for now |
| D7 | `scrapy crawl`/`scrapy check` and the `CrawlerProcess` recipe removed | Unavoidable |
| D8 | Python ≥ 3.11 | **Adopt** |
| D9 | `<meta http-equiv="refresh">` following dropped; URL-length filter dropped | **Adopt.** Irrelevant to JSON endpoints |
| D10 | Per-host politeness (robots, throttle, semaphore) shared across stores in a run; `--concurrent` becomes a per-host limit across the run instead of per store | **Adopt.** Stops N stores × 16 connections to one CDN |
| D11 | Duplicate store URLs (`a.com`, `https://a.com/`) scraped once | **Adopt** |
| D12 | Child sitemaps limited to the store's host; size/depth caps | **Adopt.** Safety against hostile sitemaps |
| D13 | Image redirects followed | **Adopt.** A redirected image is a missing image today |
| D14 | TLS certificates verified by default, `verify_tls=False` / `--insecure` to opt out (today never verified) | **Adopt, pending measurement** (milestone 0): connect to every store in the sample with and without verification and count failures by cause (expired, self-signed, hostname mismatch, incomplete chain). Shopify-hosted stores use Shopify-managed certificates, so weight the sample toward self-hosted WooCommerce, where failures are likely. `StoreStatus.tls_error` and an `--insecure` hint ship either way; if failures are common, revisit the default |
| D15 | No `Referer` header | **Adopt.** No observable benefit |
| D16 | `ALL_PROXY` honored (httpx `trust_env`) in addition to the scheme-specific proxy variables | **Adopt.** Only matters to users who set it |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Edge cases Scrapy absorbs silently | Replay corpus from varied stores; add a fixture for every surprise |
| Sync wrapper edge cases (break-early, exceptions, Ctrl-C, running loop) | §4.4 design; API tests for each, including the clear error under a running loop and `scrape_async` under one |
| Throttle/concurrency differs in practice | Unit tests vs Scrapy formulas; compare request timing logs on replay |
| CLI conflicts with ongoing `master` fixes | Only milestone 6 touches the CLI, after the fixes land |
| Windows/macOS behavior | CI jobs from milestone 6 |
| The experiment stalls | It's a branch; delete it. Milestone 1 alone shows whether the library approach works |

## 11. Library documentation

README "Use as a library" section with §4.3 examples; agent-oriented docstrings
on every public symbol (returns, statuses, files written); `py.typed`.

## 12. After the branch lands (not in this work)

1. **Browser identities:** each UA paired with matching headers (client hints,
   `Sec-Fetch-*`), versions regenerated at release time.
2. **Platform auto-detect** (`platform="auto"`).
3. **`[stealth]`:** `curl_cffi` `Fetcher` using `Identity.impersonate`.
4. **`[browser]`:** Playwright `Fetcher` for anti-bot challenges, handing
   clearance cookies to the HTTP transport via the session cookie jar. Where
   stealth isn't needed, Lightpanda over CDP (`connect_over_cdp`) behind the
   same interface.
5. **Headless/Hydrogen stores** (removed from `master` in `db05673`): an adapter
   using a `PageRenderer` protocol to extract JSON-LD, meta tags or Shopify JS
   objects; `parsel` standalone if selectors are needed.
6. **Escalation:** per store, move up transports on block signals; record the
   level in `StoreResult`.
7. Optional run-wide item limit; public event/progress API if a real need appears.

Priority of 3, 4 and 6 depends on the block rate measured in milestone 0.
