# Use as a library

The CLI is built on a Python API that scripts and coding agents can use directly. `scrape()` yields items as plain dicts while the stores are scraped, so a script can scrape, filter and save in one pass. `help(spy.scrape)` documents every option. To install the package where your script can import it, see [Installation](install.md#as-a-library).

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

Items are plain dicts with typed definitions (`ShopifyProductItem`, `ShopifyCollectionItem`, `WooCommerceProductItem`), in the native shapes described under [Output](output.md#shopify-items). `summarize()` gives the same fields on both platforms.

## A workflow for coding agents

With nothing to install, an agent can learn a store's item shape and then write a script that keeps only what the task needs:

1. `uvx shopify-spy scrape --peek URL` prints one item.
2. A script run with `uv run --with 'shopify-spy>=0.4' python script.py` filters and saves the items.
3. The [CLI](cli.md) is simpler for whole-catalog dumps and image downloads.

## How a run works

`scrape()` returns a run without fetching anything; stores are scraped while something iterates it, one item at a time. Items aren't kept, so memory stays flat however big the catalog, and a run can be iterated only once (call `scrape()` again for another pass).

- Iterating the run yourself, or passing it to `write_items()`, streams. `run.collect()` and `list(run)` hold every item in memory.
- `run.result` (the per-store results) is available once iteration ends, and `run.stores` gives a snapshot mid-run.
- Leaving early (`break`, `run.close()`, or the end of a `with spy.scrape(...) as run:` block) stops the run. Stores that hadn't finished get the `finish_reason` `closed`.

## Failures

Failures are never silent. By default `scrape()` raises `ScrapeFailed`, listing each store's status, when every store failed. `on_failure="warn"` or `"ignore"` relax that, and `strict=True` raises when any store failed, counting `partial` stores as failures.

Each `StoreResult` has a `status`, an item count and sometimes a `hint`, such as a likely wrong `platform`. A store that only answered 429s, or that 429s held up until `bail` fired, is `rate_limited`, and one that only answered 5xx errors is `unavailable`; both are failures worth retrying later. [Troubleshooting](troubleshooting.md#store-statuses) lists every status and what to do about it.

## Options

The options match the CLI: `platform`, `products`, `collections`, `images` (with `images_dir`), `max_items_per_store`, `bail`, `concurrent_requests`, `request_timeout`, `retries`, `user_agent`, `respect_robots_txt`, `verify_tls`, `proxy`, `max_concurrent_stores`, `throttle` and the `throttle_*` settings. `help(spy.scrape)` gives each one's meaning and default.

- `max_items_per_store` is per store; use `itertools.islice` for a total.
- `proxy=` sends every request through one proxy, overriding the environment variables. See [Proxies](cli.md#proxies) for the URL forms, credentials and when a proxy helps.

## Images

`images=True, images_dir="images"` saves files under `images/full/`. Each item's `scraped_images` records have a `path` relative to `images_dir`, so the file is `Path(images_dir) / record["path"]`. [Images](output.md#images) describes the records and formats.

## Schemas

`write_items(run, "products.jsonl")` writes items as scraped. With `schema="unified"`, it writes the same one-row-per-variant records as the CLI's `--schema unified`, shared by both platforms (see [Unified schema](output.md#unified-schema)), so items from Shopify and WooCommerce runs can go in one file.

- CSV and Excel are always unified; `schema="native"` raises `ValueError`.
- `fields=[...]` picks unified columns. A name that isn't one of them is logged and left empty.
- Other dicts, such as `summarize(item).asdict()`, get a column per key.
- `"products.xlsx"` writes the unified rows as typed cells (see [Excel output](output.md#excel-output)), keeping rows in a temporary folder next to the file until it closes.
- A write that fails partway leaves the file ending on the last whole item.

## No side effects

Nothing is written unless `images=True`, no config file is read, and nothing is printed. Logs go to the `shopify_spy` logger, and `on_failure="warn"` issues a `ScrapeWarning`, which Python shows on stderr.

## Async code and notebooks

Async code uses `scrape_async()`, which takes the same options:

```python
async for item in spy.scrape_async("https://www.example.com"):
    ...

result = await spy.scrape_async(urls, platform="woocommerce")  # RunResult with .items
```

`scrape()` runs its own event loop, so it raises a clear error inside an already running loop (Jupyter, async frameworks); use `scrape_async()` there.
