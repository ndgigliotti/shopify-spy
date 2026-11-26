# Troubleshooting

Every store ends with a status, which the CLI lists when a store fails and the [status file](#the-status-file) records for every store. When the whole run produces no items, the CLI also explains the likely cause of each status and suggests flags to try, including the other `--platform` when a store looks like it runs on it. In the library, each `StoreResult` has the status and sometimes a `hint`. Start with the store's status below.

## Store statuses

`status` is the label the CLI and the status file show; `code` is the matching `StoreStatus` value from the Python API.

| `status` | `code` | Meaning |
|---|---|---|
| `ok` | `ok` | Items were scraped |
| `ok` | `partial` | Items were scraped, but part of the catalog couldn't be reached |
| `0 items` | `empty` | The store answered but had no products |
| `timed out` | `timed_out` | No items within `--bail` seconds, not counting `Retry-After` waits |
| `403 Forbidden` | `forbidden` | The store returned 403s and no successful response |
| `404 Not Found` | `not_found` | The store returned 404s and no successful response; check the URL and `--platform` |
| `blocked by robots.txt` | `robots_blocked` | robots.txt forbade the store's requests; try `--ignore-robots` |
| `rate limited (429)` | `rate_limited` | The store answered 429 (Too Many Requests) after retries and no successful response, or a 429 its retries didn't get past held it until `--bail` fired with no items; retry later with a lower `--concurrent` (see [Rate limiting](#rate-limiting)) |
| `unavailable (5xx)` | `unavailable` | The store answered 5xx server errors (often an outage or maintenance) and no successful response; retry later |
| `TLS error` | `tls_error` | The certificate failed verification; try `--insecure` |
| `no response` | `no_response` | The store never answered (DNS, connection or network failure), a proxy refused the request (407), or the store's own address was refused (see the `Refused request to` warning and [Refused requests to non-public addresses](#refused-requests-to-non-public-addresses)); with a proxy in use, the library's `StoreResult.hint` says to check it (see [Proxies](cli.md#proxies)) |
| `failed` | `error` | An unexpected error (details in the [log file](#log-files)) |
| `stopped` | `stopped` | Interrupted (Ctrl-C) before producing anything |
| `invalid URL` | `invalid_url` | The URL was malformed and skipped before scraping |

`partial` isn't a failure: the store's items were kept. In the library, `strict=True` counts it as one.

## Exit status

- **0:** at least one store produced items. Stores that failed are listed with their statuses.
- **1:** no store produced items, the output couldn't be written, or the input was invalid (no URLs or no valid ones, `--verbose` with `--quiet`, an option value the CLI rejects, such as a bad `--proxy`, or `--format xlsx` without the `excel` extra, which `--peek` doesn't check). The CLI prints the reason.
- **2:** a usage error, such as an unknown option.

## The status file

Each run except `--peek` writes a status file to `<output>/status/`, with each URL's item count and status and the run's timing:

```json
{
  "items_scraped": 47,
  "items_written": 47,
  "urls": [
    {"url": "https://store.com", "items": 47, "status": "ok", "code": "ok"}
  ],
  "finish_reason": "finished",
  "duration_seconds": 12.3,
  "log_file": "/home/user/.local/state/shopify-spy/logs/shopify_spider_2024-01-15T10-30-00+00-00.log",
  "output_error": null
}
```

`finish_reason` summarizes the run: `finished`, `item_limit` (a store reached `--limit`), `bail` (a store hit the `--bail` timeout), `crawler_failed` (a store raised an unexpected error), or `shutdown` (interrupted). `items_written` is how many items the data file holds. A crash or a killed process writes no status file.

`output_error` is `null` unless writing the data file failed, in any format (such as a full disk or an unwritable directory). Then it holds the error, and the command exits with status 1.

- **An item failed to write:** the run stops early (`finish_reason` is `shutdown`), and the file keeps the whole items written before the error. A partly written item is cut off; in XLSX, rows of the failed item already handed to the workbook stay. `items_written` then falls short of `items_scraped`, which also counts the item that failed and any already fetched.
- **Only finishing the file failed** (the closing bytes of JSON or XML, or building an XLSX workbook): the run itself finished, and `finish_reason` says how. An XLSX file is then removed and `items_written` is 0.

## Log files

Detailed logs go to a log file rather than the terminal, in the platform's state directory (such as `~/.local/state/shopify-spy/logs/` on Linux). The status file's `log_file` names it, and the CLI prints its path at the end of a run. `--verbose` also prints the logs to the terminal.

## Rate limiting

Stores usually rate-limit by IP address: past some request rate they answer 429 (Too Many Requests). A store that stays rate-limited ends as `rate_limited`; retry it later, with a lower `--concurrent` and throttling on.

- **Throttling:** auto-throttling is on by default. Each host is slowed to match its response times, and a 429 or 503 pauses that host for its `Retry-After` time (at most 60 seconds), even with `--no-throttle`. `--no-throttle` is faster but riskier, and the [config file](cli.md#configuration)'s `throttle` settings adjust it.
- **User agents:** requests use a random browser user agent, and a 403 or 429 swaps it before the retry. Some stores rate-limit by user agent: Shopify answers 429 to every request from a plain Linux browser user agent (`X11; Linux x86_64`, as Chrome on Linux and headless Chrome send), so the built-in pool leaves those out. Avoid that token in a custom `--user-agent`.
- **Bail and waits:** waiting out `Retry-After` doesn't count against `--bail`, and waits are excused for each request. So a store limited by IP can wait (retries + 1) × up to 60 seconds for every request it makes before its first item. That has no fixed bound: a Shopify store that answers 429 with `Retry-After` to each of the thousands of products its sitemap lists can run for hours before it fails.
- **Proxies** don't get around rate limits; see [Proxies](cli.md#proxies).

## Refused requests to non-public addresses

Redirects, image URLs, sitemap entries and product links all come from the store, so a store could point the scraper at addresses on your own machine or network. Requests to loopback, private-network, link-local (which includes cloud metadata services), shared, multicast and reserved addresses are therefore refused before they are sent, along with their robots.txt. This covers literal IP addresses, `localhost` names and hostnames that resolve to any such address. The log file gets a warning for each one, such as `Refused request to http://169.254.169.254/...: 169.254.169.254 is not a public address`, and a refused image counts as a failed image.

A store whose own URL is on a non-public address, such as `http://127.0.0.1:8000` or a LAN host whose name resolves only to private addresses, is exempt: its requests aren't checked. There is no option to turn the check off. A refused redirect or link usually leaves the store as `0 items`, since the store did answer. A refused store URL, such as a name with both public and private addresses, shows as `no response`.

- **Proxies:** with `--proxy` or a proxy environment variable, the proxy resolves hostnames, so only literal IP addresses and `localhost` names are checked. See [Proxies](cli.md#proxies).
- **Custom transports:** with the library's `fetcher_factory`, hostnames aren't resolved for the check; literal IPs and `localhost` names still are.
- **Local DNS that answers with fake or blocking addresses:** some tools answer public names with addresses that aren't public, such as the 198.18.0.0/15 fake-IP range of TUN-mode proxy tools, or `0.0.0.0` from ad-blocking DNS. Those answers show up as refusals, or exempt a store whose every name gets one. Point the scraper at that tool's proxy with `--proxy` instead.
- **What it doesn't cover:** the check runs before each request, so a name whose DNS answer changes between the check and the connection isn't caught.

## Large Shopify catalogs

Shopify's `products.json` stops paging at 25,000 items (100 pages of 250), so a larger catalog takes more requests. Past that, the scraper reads the sitemap's product list to see what's missing, then pages `/collections/all` and the store's collections, largest first, each with its own 25,000 limit. Whatever is still missing, including products in no collection, is fetched through the sitemap one product per request.

- With `--limit`, the sitemap's product list isn't read for this check, since a limit may need only a sliver of a huge catalog.
- Collections are paged only while they can save requests. They're skipped when fewer products are missing than they have pages, and they stop once every listed product is found or they find less than one new product per request.
- On a 65,718-product store, bulk paging and collections together found 99.7% of the catalog, and the whole scrape took about 1,700 requests, against about 41,000 with bulk paging and the sitemap alone ([#30](https://github.com/ndgigliotti/shopify-spy/issues/30)).
- Catalogs whose collections are far larger than 25,000 products (some print-on-demand stores list over a million) stay mostly out of reach; use `--limit` to sample them.

## WooCommerce stores

WooCommerce scraping uses the public Store API (`/wp-json/wc/store/v1/products`), built into WooCommerce since version 6.4 (2022). It needs no authentication. When it's unavailable, products come from the product pages' structured data instead (see [Items from product pages](output.md#items-from-product-pages)). Those items have no categories or tags, and variations only for products with up to 30 of them (WooCommerce's default). Stores whose pages have no schema.org `Product` data, or that broadly block crawlers, may not be scrapeable.
