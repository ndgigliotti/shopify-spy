# Troubleshooting

Every store ends with a status, which the CLI lists when a store fails and the [status file](#the-status-file) records for every store. When the whole run produces no items, the CLI also explains the likely cause of each status and suggests flags to try. Some stores also get a hint of their own, such as the platform a store looks like it runs on, or the proxy when one was in use; the CLI prints it for each store that failed or produced nothing, and the status file and `StoreResult.hint` carry it. Start with the store's status below.

## Store statuses

`status` is the label the CLI and the status file show; `code` is the matching `StoreStatus` value from the Python API.

| `status` | `code` | Meaning |
|---|---|---|
| `ok` | `ok` | Items were scraped |
| `ok` | `partial` | Items were scraped, but part of the catalog couldn't be reached, such as a listed product page that answered without Product data, or, for a Squarespace store read with `--ignore-robots`, a collection page couldn't be read or a product page its sitemap lists, missing from its collections' data, couldn't be read either (see [Squarespace stores](#squarespace-stores)) |
| `0 items` | `empty` | The store answered but had no products |
| `timed out` | `timed_out` | No items within `--bail` seconds, not counting `Retry-After` waits |
| `403 Forbidden` | `forbidden` | The store returned 403s and no successful response, also after switching browser profile (see [Retries and identities](#retries-and-identities)), or answered with a challenge page that only a real browser can pass |
| `404 Not Found` | `not_found` | The store returned 404s and no successful response; check the URL and `--platform` |
| `blocked by robots.txt` | `robots_blocked` | robots.txt forbade the store's requests, through its `shopify-spy` group (else `*`), or a custom `--user-agent`'s group (see [For site owners](site-owners.md)); `--ignore-robots` ignores it, at your own risk. For a Magento store whose robots.txt disallows URLs with query strings, the store's hint says so (see [Magento stores](#magento-stores)) |
| `rate limited (429)` | `rate_limited` | The store answered 429 (Too Many Requests) after retries, or with a `Retry-After` longer than 60 s, and no successful response, or a 429 its retries didn't get past held it until `--bail` fired with no items; retry later with a lower `--concurrent` (see [Rate limiting](#rate-limiting)) |
| `unavailable (5xx)` | `unavailable` | The store answered 5xx server errors (often an outage or maintenance) and no successful response; retry later |
| `TLS error` | `tls_error` | The certificate failed verification; try `--insecure`, or behind a company proxy point `SSL_CERT_FILE` or `SSL_CERT_DIR` at its CA (`REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` work too; see [Network](cli.md#network)) |
| `no response` | `no_response` | The store never answered (DNS, connection or network failure), a proxy refused the request (407), or the store's own address was refused (see the `Refused request to` warning and [Refused requests to non-public addresses](#refused-requests-to-non-public-addresses)); with a proxy in use, the store's hint says to check it (see [Proxies](cli.md#proxies)) |
| `failed` | `error` | An unexpected error, a Magento store whose `/graphql` answered without products, a BigCommerce store without a Storefront API token on its homepage or whose `/graphql` answered without products, a generic store whose pages had no Product data or whose sitemaps weren't found or couldn't be read, or a Squarespace store whose sitemap listed no product page or, with `--ignore-robots`, whose collections and product pages both gave no product (the reason is in the [log file](#log-files) and `StoreResult.errors`; see [Magento stores](#magento-stores), [BigCommerce stores](#bigcommerce-stores), [generic stores](#generic-stores) and [Squarespace stores](#squarespace-stores)) |
| `stopped` | `stopped` | Interrupted (Ctrl-C) before producing anything |
| `invalid URL` | `invalid_url` | The URL was malformed and skipped before scraping |

`partial` isn't a failure: the store's items were kept. In the library, `strict=True` counts it as one.

## Exit status

- **0:** at least one store produced items. Stores that failed are listed with their statuses.
- **1:** no store produced items, the output couldn't be written, or the input was invalid (no URLs or no valid ones, `--verbose` with `--quiet`, an option value the CLI rejects, such as a bad `--proxy` or an unknown `--impersonate` browser, a config file with an unknown key or a bad value, `--impersonate` with `--user-agent`, or `--format xlsx` without the `excel` extra, which `--peek` doesn't check). The CLI prints the reason.
- **2:** a usage error, such as an unknown option.

## The status file

Each run except `--peek` writes a status file to `<output>/status/`, with each URL's item count and status and the run's timing:

```json
{
  "items_scraped": 47,
  "items_written": 47,
  "urls": [
    {"url": "https://store.com", "items": 47, "status": "ok", "code": "ok", "products_from": null,
     "identity_changes": 0, "hint": null}
  ],
  "finish_reason": "finished",
  "duration_seconds": 12.3,
  "log_file": "/home/user/.local/state/shopify-spy/logs/shopify_spider_2024-01-15T10-30-00+00-00.log",
  "output_error": null
}
```

`products_from` is `null` unless a store's products came from another host, such as the `myshopify.com` domain of a [headless store](#headless-shopify-stores). `identity_changes` counts the store's switches to another browser profile after a refusal; see [Retries and identities](#retries-and-identities). `hint` is the store's hint, the same text as `StoreResult.hint`, such as `looks like WooCommerce; pass platform='woocommerce'`, and `null` when there is none.

`finish_reason` summarizes the run: `finished`, `item_limit` (a store reached `--limit`), `bail` (a store hit the `--bail` timeout; a generic store stopped there after checking pages reports `failed` with the pages checked), `crawler_failed` (a store failed with an error: unexpected, a Magento or BigCommerce answer without products, a BigCommerce homepage without a token, or a generic store without Product data), or `shutdown` (interrupted). `items_written` is how many items the data file holds. A crash or a killed process writes no status file.

`output_error` is `null` unless writing the data file failed, in any format (such as a full disk or an unwritable directory). Then it holds the error, and the command exits with status 1.

- **An item failed to write:** the run stops early (`finish_reason` is `shutdown`), and the file keeps the whole items written before the error. A partly written item is cut off; in XLSX, rows of the failed item already handed to the workbook stay. `items_written` then falls short of `items_scraped`, which also counts the item that failed and any already fetched.
- **Only finishing the file failed** (the closing bytes of JSON or XML, or building an XLSX workbook): the run itself finished, and `finish_reason` says how. An XLSX file is then removed and `items_written` is 0.

## Log files

Detailed logs go to a log file rather than the terminal, in the platform's state directory (such as `~/.local/state/shopify-spy/logs/` on Linux). The status file's `log_file` names it, and the CLI prints its path at the end of a run. `--verbose` also prints the logs to the terminal.

## Rate limiting

Stores usually rate-limit by IP address: past some request rate they answer 429 (Too Many Requests). A store that stays rate-limited ends as `rate_limited`; retry it later, with a lower `--concurrent` and throttling on.

- **Throttling:** auto-throttling is on by default. Each host is slowed to match its response times, never to less than 1 second between page and API requests (`--min-delay`, at most 1 per second) or 0.25 seconds between image downloads (`--image-delay`, at most 4 per second). A robots.txt `Crawl-delay` spaces that host's requests too, capped at the throttle's `max_delay` (60 seconds). A 429 or 503 pauses that host for its full `Retry-After` time, even when it is longer than 60 seconds, and such a request isn't retried early. `--no-throttle` drops the adaptive delay and both minimums, but `Crawl-delay` and `Retry-After` pauses still apply. The [config file](cli.md#configuration)'s `throttle` settings adjust the rest.
- **Identities:** some stores rate-limit by user agent, so a 429 may be retried as another browser; see [Retries and identities](#retries-and-identities). Shopify answers 429 to every request from a plain Linux browser user agent (`X11; Linux x86_64`, as Chrome on Linux and headless Chrome send), so none of the browser profiles uses one. Avoid that token in a custom `--user-agent`.
- **Bail and waits:** waiting out a `Retry-After` of up to 60 seconds doesn't count against `--bail`, and waits are excused for each request. So a store limited by IP can wait (retries + 1) × up to 60 seconds for every request it makes before its first item. That has no fixed bound: a Shopify store that answers 429 with `Retry-After` to each of the thousands of products its sitemap lists can run for hours before it fails. A longer `Retry-After` isn't excused from `--bail`. Once a store has produced its first item, though, `--bail` no longer applies, so a long `Retry-After` on its host (for example during Shopify's collections or sitemap fallback) holds that store for the whole pause. The log file then has a `Pausing requests to ...` warning giving the length of the pause, and Ctrl-C stops the run with what it has.
- **Images:** the same holds for an image host, which every store in the run shares. A long `Retry-After` from the image CDN pauses image downloads for every store, and since images are fetched as each item is delivered, each `--images` store stalls until the pause ends (`--no-images` or Ctrl-C ends it sooner).
- **Proxies** don't get around rate limits; see [Proxies](cli.md#proxies).

## Retries and identities

Requests go out as a browser: a curl_cffi browser profile, `chrome` first (see [Browser profiles](cli.md#browser-profiles)). A custom `--user-agent` sends plain requests with exactly that string and is never switched. A 403 or 429 may come from the store refusing that one identity, as Shopify once did with a 429 for a single user agent ([#62](https://github.com/ndgigliotti/shopify-spy/issues/62)), so the retry switches the store to another identity to find out. The switch never changes the pace:

- **Backoff first.** The retry with the new identity waits like any retry: a 429 for its `Retry-After` or the doubled host delay, a 403 for the usual short retry delay. It counts against the retries (2 by default) and never adds one.
- **One test.** If the new identity is refused too before it gets a normal answer, the block isn't about identity: the store stops switching for the rest of the run, and the request ends as it would have (`forbidden` or `rate_limited`). A store that refuses one profile and serves the next ends `ok` with one identity change.
- **Remembered refusals.** A host's refusals are remembered for the run, so no store switches to a profile that host already refused ([#67](https://github.com/ndgigliotti/shopify-spy/issues/67)).
- **No switch on explicit refusals.** A robots.txt disallow, a 401, a challenge page (a Cloudflare `cf-mitigated: challenge` response or its "Just a moment..." page, which ends the request without a retry), and refusals from `*.myshopify.com` hosts keep the identity. Image downloads keep it too, and an image's 403 isn't retried.
- **Reported.** Each switch is logged at INFO as `Switching browser profile for HOST: chrome -> safari`, and the [status file](#the-status-file)'s `identity_changes` and `StoreResult.identity_changes` count them per store.
- **Off switch.** `--no-rotate` (`network.rotate: false`, `rotate=False`) keeps one identity for the whole run; refusals are still retried. A custom `--user-agent` is never switched either.

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

## Headless Shopify stores

A headless Shopify store runs its storefront on a custom frontend (Hydrogen or another framework), so its own domain answers 404 to `/products.json`. When that happens, the scraper reads the store's homepage for the shop's `<handle>.myshopify.com` domain, which the HTML usually names in checkout links or analytics config, and pages `/products.json` there before trying the sitemap ([#92](https://github.com/ndgigliotti/shopify-spy/issues/92)). Items keep the store's own domain in `url` and `store`, and with `--collections` the collections come from the same domain. The status file's `products_from`, `StoreResult.products_from` and the CLI's end-of-run summary name the `myshopify.com` domain the products came from.

- The `myshopify.com` domain is used only when the homepage names exactly one; none or several (an app or marketplace domain, say) go straight to the sitemap.
- A `myshopify.com` domain behind a password page (a redirect to `/password`), or one that fails in any other way, falls back to the sitemap as before.
- Requests to `*.myshopify.com`, robots.txt included, go one at a time across the whole run, since those hosts are Shopify's rather than the store's. A 403 or 429 there doesn't switch the store's browser profile.
- With `--bail`, the homepage and the wait for the first item take at most half of the store's remaining bail time before the sitemap is tried with the rest.

## WooCommerce stores

WooCommerce scraping uses the public Store API (`/wp-json/wc/store/v1/products`), built into WooCommerce since version 6.4 (2022). It needs no authentication. When it's unavailable, products come from the product pages' structured data instead (see [Items from product pages](output.md#items-from-product-pages)); a listed product page without Product data for it, or one that fails to load, makes the store `partial` (a 404 or 410, a redirect to another path, or the shop page Yoast lists in its product sitemap, doesn't). Those items have no categories or tags, and variations only for products with up to 30 of them (WooCommerce's default). Stores whose pages have no schema.org `Product` data, or that broadly block crawlers, may not be scrapeable.

## Magento stores

Magento scraping uses the storefront GraphQL API at `/graphql`, which Magento Open Source and Adobe Commerce 2.4 serve without a token (the REST API needs one). The `products` query is sent as a GET, with the query in the URL, 100 products a page. Only the default store view is read, in its currency; a multi-store site's other views and currencies aren't.

- **Page size:** some stores cap the page size. A GraphQL error about the page size on the first page makes the scraper retry it with the size the message names (or half), and a first page whose `page_info.page_size` is smaller than asked for sets the size for the pages after it, so these stores still finish `ok`. Paging stops at the answer's `page_info.total_pages` when it gives one.
- **`failed` with a GraphQL message:** the store's `/graphql` answered the first page without products. The message from Magento is in the log file and `StoreResult.errors`. Common causes are a query complexity or depth limit the store set below Magento's defaults, a disabled GraphQL module, a store whose catalog search errors, or a site that isn't Magento (WordPress's WPGraphQL also answers at `/graphql`). When `/products.json` or `/wp-json/` answers like Shopify or WooCommerce, the hint names that platform.
- **`blocked by robots.txt`:** robots.txt rules that match query strings, such as `Disallow: /*?`, also match the GraphQL URLs, since the query is in the URL. Many Magento stores have such a rule, and their catalog can't be read with robots.txt obeyed; the scraper keeps to it rather than sending the query another way. When robots.txt allows `/graphql` itself but not the query URL, the store's hint says its robots.txt disallows URLs with query strings, and `StoreResult.query_disallowed` is `True`. `--ignore-robots` reads the catalog anyway; see the `robots_blocked` status above.
- **`partial` with a warning that products failed to resolve:** the answer had a null in place of a product and a GraphQL error naming it (its `path` is `products.items.<n>`). The other products on the page are kept. A null without such an error is a product the storefront hides and doesn't make the store partial.
- **`ok` with a warning about fewer products than `total_count`:** `total_count` can include products the storefront hides, such as out-of-stock ones. If some products also came back twice, the result order shifted between pages and products were likely missed; the store is then `partial`. Scraping it again may give a complete result.
- **`403 Forbidden`:** sites behind a bot challenge (such as Cloudflare's) answer 403 to scripted requests, on `/graphql` as on every other page.

## BigCommerce stores

BigCommerce scraping uses the Storefront GraphQL API at `/graphql`, with the storefront's public API token. Stencil themes put this token in the homepage so the theme's own scripts can query the API; BigCommerce issues it for use in the browser, scoped to read-only storefront data. The scraper reads it from the homepage and sends the `site.products` query as a POST, 50 products a page (BigCommerce's maximum). Only the store's default currency is read. When the page carries several tokens, the one whose `cors` claim names the store wins, then the theme's own Stencil token (`sub` "BC") over an app's or a merchant script's; tokens past their expiry (`eat`) are skipped.

- **`failed` with "no Storefront API token on the homepage":** the theme doesn't expose a token. BigCommerce's default Cornerstone theme doesn't, and some custom themes don't either; the message then says it's a Stencil theme. Headless storefronts (Catalyst, Next.js) don't use Stencil and have their own setups, and the message says the page isn't a Stencil storefront. These stores can't be scraped with this platform yet. When `/products.json` or `/wp-json/` answers like Shopify or WooCommerce, the hint names that platform.
- **Headless storefronts:** a page that isn't a Stencil storefront but carries a token can name its real endpoint, a `https://store-<hash>.mybigcommerce.com/graphql` URL (Nuxt storefronts do, one per region). The scraper queries that endpoint, the one whose setting name ends like the token's (`BC_GRAPHQL_AU` for `BIGCOMMERCE_ACCESS_TOKEN_AU`) or else the first, and logs which; robots.txt for that host applies, and `html_url` stays on the store's domain. Without one, the store's own `/graphql` usually redirects, and the store fails with a message saying it looks like a headless storefront.
- **Token expiry:** tokens expire (Stencil's after about two days). A homepage whose tokens have all expired fails with "no Storefront API token on the homepage (only expired ones)". A 401 partway through a catalog makes the scraper read the homepage again for a fresh token and retry the same page. A second 401 in a row, a homepage without an unexpired token, or one that gives the same token again stops the store: `failed` on the first page, and `partial` after it.
- **May stop working:** BigCommerce describes storefront tokens as meant for the browser and deprecates using them from servers. Tokens created without CORS origins stop working there on 2027-03-31. The theme's token carries the store's origin and works today, but BigCommerce could start refusing it outside a browser at any time. A refused token ends the store `failed` rather than being worked around.
- **Query complexity:** BigCommerce limits each query's complexity. A complexity error halves the page size and retries the same page, so these stores still finish `ok`.
- **Variants:** a product with more than 250 variants has the rest read 250 at a time, with the same token refresh and complexity handling, up to 2,500. Past that, or if a later variants query fails, the product keeps the variants read so far, with a warning in the log, and the store is `partial`.
- **`failed` with a GraphQL message:** the store's `/graphql` answered the first page without products, or redirected it. The message is in the log file and `StoreResult.errors`.
- **`403 Forbidden`:** sites behind a bot challenge (such as Cloudflare's) answer 403 to scripted requests, on the homepage as on every other page.

## Generic stores

`--platform generic` (`platform="generic"`) is a best-effort fallback for stores on platforms without their own adapter, such as Wix, PrestaShop or a custom site, and BigCommerce stores whose theme doesn't expose a Storefront API token. It reads the product pages the store's sitemaps list and keeps the schema.org `Product` data each page publishes for search engines, as JSON-LD or, on pages without a JSON-LD Product, microdata (see [generic items](output.md#generic-items)). It doesn't match the native adapters: prefer `shopify`, `woocommerce`, `magento`, `bigcommerce` or `squarespace` when the store runs on one of them. When a store yields no item, the hint names one of them if `/products.json`, `/wp-json/` or a `storeConfig` query to `/graphql` answers like it (one request each, robots.txt permitting).

- **Cost:** one request per product, where Shopify's bulk endpoint returns 250 products a request. A 10,000-product catalog takes over 10,000 requests. Use `--limit` (`max_items_per_store`) for a sample, and keep throttling on so the store isn't overloaded or provoked into rate limiting.
- **`failed` with "no schema.org Product JSON-LD or microdata on N of N product pages checked":** the pages had no Product data the scraper could use. Before the first item, the store stops at whichever comes first: the end of the listed pages (the message ends "no more pages listed"), 50 pages without a Product ("stopped at the 50-page limit for pages without a Product"), or `--bail` ("stopped at --bail", below). The message counts the pages that answered with an error status, failed to respond or were disallowed by robots.txt. Common causes:
  - **Client-rendered stores** (Ecwid's embeddable widget, some headless sites) build product pages in the browser, so the HTML the server sends has no Product.
  - **RDFa only:** pages that mark products up with RDFa (`typeof`/`property` attributes) and neither JSON-LD nor microdata aren't read.
  - **A flat sitemap that lists other pages first:** a store with one sitemap of categories, blog posts and products can use up its checks before reaching a product. At the default `--bail` that is usually the bail time rather than the 50 pages; raise `--bail` (or pass `--bail 0`) to check up to 50. There is no option to check more than 50 pages yet.
  - **Bot challenges** that answer pages with a challenge instead of the product.
- **`failed` with "stopped at --bail":** the store had checked some pages without finding a Product when `--bail` (30 seconds by default) fired. Throttling keeps a host's requests at least 1 second apart (`--min-delay`), and further apart on a slow host, so at the default `--bail` the store usually checks about 20 to 25 pages, not the 50 the page limit allows. A store whose sitemaps list many non-product pages before its products needs a larger `--bail`, such as `--bail 120`, or `--bail 0` to stop only at the 50-page limit. The message gives the number of pages checked, and how many answered 429 (Too Many Requests): the store ends `failed` then too, as it would without `--bail`, rather than `rate_limited`.
- **`failed` with "no sitemap found":** the sitemap URLs answered, but not with a sitemap (an HTML catch-all page, say). The scraper reads the robots.txt `Sitemap:` lines, then `/sitemap.xml`, `/sitemap_index.xml` and `/xmlsitemap.php`. A store whose sitemap is elsewhere, and not named in robots.txt, can't be read. When every sitemap URL answers 404, 403 or is disallowed, the store ends `404 Not Found`, `403 Forbidden` or `blocked by robots.txt`.
- **`failed` with "no page listed; unreadable sitemaps":** a sitemap was found, but the sitemaps it pointed to for products (an index's product sitemaps, say) answered an error status or failed to respond, so no page was checked. The message lists them. A retry later may get through; a 403 suggests a bot challenge.
- **Several currencies:** the scraper follows the store's sitemaps, so a store with regional sections in them (such as `/ca/en_us/` pages beside the US ones) can return items in several currencies. Each item's offers carry their own `priceCurrency`, and the unified `currency` column follows it; filter on it to keep one region.
- **Microdata:** a page without a JSON-LD Product is read for `itemscope`/`itemprop` microdata (Shopware 6, some BigCommerce themes, Magento's Luma), with the same properties. `itemref` isn't followed. For Magento, `--platform magento` reads much more; the hint suggests it when `/graphql` answers.
- **Lower fidelity:** variants are often missing (many stores publish one Product with a single offer or a price range), prices may be a range (`AggregateOffer`), and fields the native platforms have, such as tags or stock counts, usually aren't there.

## Squarespace stores

`--platform squarespace` (`platform="squarespace"`) reads Squarespace Commerce stores. Every Squarespace site serves the same robots.txt, which disallows URLs with `format=json` (and `format=page-context` and `format=json-pretty`) for every crawler but allows the sitemap and the product pages. So what a run returns depends on whether robots.txt is obeyed. Each store logs which source it read at INFO level, and each item's `source` field records it (see [Squarespace items](output.md#squarespace-items)).

- **By default (robots.txt obeyed):** the scraper reads the `/<collection>/p/<slug>` product pages the store's sitemap lists and keeps each page's schema.org Product data, as `--platform generic` does (`"source": "product_page"`). It never requests a URL with a `format` query parameter, even when robots.txt can't be read, and leaves out sitemap pages that aren't products (blog posts, info pages). This costs one request per product, so use `--limit` for a sample, and the data is what the page publishes for search engines: often one offer per product, without every variant or stock counts. The [generic stores](#generic-stores) section's `failed` cases apply. Once the store has an item, a listed product page that fails to load (other than a 404 or 410) or answers without Product data makes it `partial`, since every page read is a product page; one that redirects to another path (a deleted product sent to its collection) doesn't.
- **With `--ignore-robots` (`respect_robots_txt=False`):** the scraper reads each collection's `?format=json` pages instead (`"source": "format_json"`), the collections being the parents of the listed product pages. A collection page holds many products, so a store takes a few requests, and each product has every variant with its price, sale price and stock. When no collection gives a product, the listed product pages are read instead, as by default; the log says so, and those items have `"source": "product_page"`. When the collections give products but not every listed one, the missing product pages are read the same way after the collections, one request each.
- **Your choice:** `--ignore-robots` ignores robots.txt for every request of the run, not only the `?format=json` ones, and it is the user's choice and responsibility; the scraper never reads `?format=json` unless you pass it. Squarespace's Acceptable Use Policy is an agreement between Squarespace and its customers, the site owners; a store's own terms of use may say more.
- **`failed` with "no product page (/<collection>/p/<slug>) among the N pages listed":** the sitemap answered, but listed no Squarespace product page, so the site likely has no store, or isn't on Squarespace (the hint names Shopify, WooCommerce or Magento when the site answers like one).
- **`failed` with "no Squarespace product from N collection pages":** with `--ignore-robots`, no collection gave a product (each answered without collection data, with an error status, failed to respond, or held no record with variants), and the product pages read instead had no Product data either. The message gives both outcomes, joined by `;`. A store that `--bail` stops before its first item ends the same way, with "stopped at --bail" and the outcome so far.
- **`ok` (`partial`) with `--ignore-robots`:** a collection page couldn't be read, or a listed product page read after the collections (because it was missing from them, or because no collection gave a product) failed when read (an error status other than 404 or 410, no response, or no Product data). The INFO log logs each collection page that couldn't be read with its status, its failed request, or "answered without Squarespace collection data", and gives the count of listed product pages "not in the ?format=json collections, reading them from product pages". The items read are kept. A missing page that answers 404 or 410, or redirects to another path, no longer exists and doesn't make the store partial.
- **`404 Not Found`:** every sitemap URL answered 404, as for a generic store (see "no sitemap found" under [generic stores](#generic-stores)), in either mode.
