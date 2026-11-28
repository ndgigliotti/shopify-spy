# Platforms

Each store is read by the adapter for its platform, chosen with `--platform` (`platform=` in the library). Shopify is the default; every other platform has to be named. This page says how each adapter reads a store, what it returns, and where it stops. [Output](output.md) lists the fields, and [Troubleshooting](troubleshooting.md) covers what to do when a store fails.

| Platform | Reads | Products per request | Variants | Stock | GTIN/MPN | Ratings | Collections |
|---|---|---|---|---|---|---|---|
| `shopify` | `/products.json`, then the sitemap | 250 | yes | yes | no | no | yes |
| `woocommerce` | the Store API, then product pages | 100 | yes | yes | product pages only | yes | no |
| `magento` | storefront GraphQL | 100 | yes | yes | no | no | no |
| `bigcommerce` | Storefront GraphQL, with the homepage's token | 50 | yes | yes | yes | yes | no |
| `squarespace` | product pages; `?format=json` with `--ignore-robots` | 1; about 200 with `--ignore-robots` | with `--ignore-robots` | yes | product pages only | product pages only | no |
| `generic` | product pages from the sitemaps | 1 | when the page lists them | when the page says | when the page says | when the page says | no |

"Products per request" is the most a page of results holds. The adapters that read product pages make one request per product, so a large catalog takes many requests and, at the default one-second minimum gap per host, a long time; use `--limit` for a sample.

## Choosing a platform

Pass the platform the store runs on: its own adapter reads far more than `generic`, in far fewer requests. A store read with the wrong platform usually fails with a 404. When a store yields nothing and its site answers like Shopify, WooCommerce or Magento, its hint names that platform, in the CLI's end-of-run summary, the [status file](troubleshooting.md#the-status-file) and `StoreResult.hint`. `generic` is for everything else: Wix, PrestaShop, Shopware, custom sites, and BigCommerce stores whose theme doesn't expose a token.

Every adapter obeys robots.txt by default, matched against the `shopify-spy` token's group (see [For site owners](site-owners.md)), and keeps to the throttle: at least one second between requests to a host, longer when the host slows down or asks for a `Crawl-delay`.

## Completeness

A store that produced items ends `ok`, or `partial` when part of its catalog was missed: a page of results or a listed product that failed, a list cut short, or a product a platform's API reported it couldn't return. The items read are kept either way, and `StoreResult.complete` is `False` for a partial store. A page that answers 404 or 410, or redirects to another path, is a product that no longer exists and doesn't count against the store. A store that produced nothing ends with the status that explains why (see [Store statuses](troubleshooting.md#store-statuses)).

## Shopify

Shopify stores are read from `/products.json`, 250 products a page, the endpoint Shopify serves on every storefront.

- **Large catalogs:** `/products.json` stops at 25,000 products. Past that, the adapter pages `/collections/all` and the store's collections, largest first, then fetches what's still missing from the sitemap, one product per request (see [Large Shopify catalogs](troubleshooting.md#large-shopify-catalogs)).
- **Headless stores:** when the store's own domain answers 404, the products come from its `myshopify.com` domain if the homepage names one, else from the sitemap (see [Headless Shopify stores](troubleshooting.md#headless-shopify-stores)).
- **Collections:** `--collections` adds the store's collections from `/collections.json`, as items of their own (`kind` `collection`). Shopify is the only platform with collections.
- **What you get:** the full product JSON, with every variant's price, `compare_at_price`, `available` and weight. `products.json` has no barcodes, ratings or stock counts.
- **Partial:** a bulk page that fails or repeats partway sends the adapter to the sitemap, and the store is `partial` if the sitemap can't fill the gap: a child sitemap that can't be read, or a listed product that fails or answers without a product. A collection list cut short makes a `--collections` store partial. A store whose sitemap lists products, none of which can be read, ends `failed`.

## WooCommerce

WooCommerce stores are read from the public Store API (`/wp-json/wc/store/v1/products`), 100 products a page, then each variable product's variations. WooCommerce has served it without authentication since version 6.4.

- **Fallback:** when the Store API is unavailable, the products come from the product pages' schema.org data, one request per product (see [Items from product pages](output.md#items-from-product-pages)). Those items have no categories or tags, and variations only when the page lists them.
- **What you get:** prices in the store's currency, with WooCommerce's minor units converted; each variation's own SKU, price and stock; ratings and review counts.
- **Partial:** in the fallback, a listed product page that fails or has no Product for it makes the store partial. The shop page Yoast lists in its product sitemap doesn't count.

## Magento

Magento stores are read from the storefront GraphQL API at `/graphql`, which Magento Open Source and Adobe Commerce 2.4 serve without a token. The `products` query goes out as a GET, 100 products a page, for the default store view in its currency.

- **robots.txt:** the query is in the URL, so a robots.txt rule against query strings (`Disallow: /*?`, common on Magento) blocks the catalog. The adapter keeps to it, and the store's hint says so (see [Magento stores](troubleshooting.md#magento-stores)).
- **Page size:** a store that caps the page size gets the size it names, in an error or in the answer's `page_info`; paging stops at `page_info.total_pages`.
- **What you get:** each configurable product's variants, with their own SKU, price and stock. GTINs and MPNs live in store-defined attributes, which aren't read.
- **Partial:** a null product with a GraphQL error naming it, or a result order that shifts between pages.

## BigCommerce

BigCommerce stores are read from the Storefront GraphQL API at `/graphql` with the public token that Stencil themes put in the homepage, 50 products a page, in the store's default currency.

- **Token:** a theme that doesn't expose a token, such as BigCommerce's default Cornerstone, can't be read with this platform; try `generic`. Headless storefronts that name their `mybigcommerce.com` endpoint are read there. A token that expires partway is refreshed from the homepage (see [BigCommerce stores](troubleshooting.md#bigcommerce-stores)).
- **What you get:** variants with their own SKU, price, stock, GTIN (else UPC) and MPN; brands, categories, ratings and review counts.
- **Variants:** a product with more than 250 variants has the rest read 250 at a time, up to 2,500. A product whose variants stop early keeps those read, and the store is partial.

## Squarespace

Squarespace Commerce stores are read from the `/<collection>/p/<slug>` product pages their sitemap lists. Squarespace's robots.txt, the same on every site, disallows `?format=json`, so that's what the adapter reads by default.

- **By default:** each product page's schema.org data, one request per product, often one offer per product without every variant or stock counts (`"source": "product_page"`).
- **With `--ignore-robots`:** each collection's `?format=json` pages, about 200 products a request, with every variant's price, sale price and stock (`"source": "format_json"`). Listed products the collections don't give are read from their product pages. `--ignore-robots` applies to every request of the run, and is the user's choice (see [Squarespace stores](troubleshooting.md#squarespace-stores)).
- **Digital products** have no variants and are priced from the product itself.
- **Partial:** a listed product page that fails or has no Product data, since every page read is a product page, or, with `--ignore-robots`, a collection page that can't be read.

## Generic

`generic` reads the product pages the store's sitemaps list and keeps the schema.org Product data each page publishes for search engines, as JSON-LD or else microdata (see [Generic items](output.md#generic-items)). It finds the sitemaps through robots.txt's `Sitemap:` lines, then `/sitemap.xml`, `/sitemap_index.xml` and `/xmlsitemap.php`.

- **Cost:** one request per page, product or not, so a store whose sitemaps list other pages first takes longer to reach its products. Before the first item, a store stops after 50 pages without a Product, or at `--bail` (see [Generic stores](troubleshooting.md#generic-stores)).
- **What you get:** what the page publishes, which varies by store: often one offer or a price range rather than every variant, and rarely tags or stock counts. A `ProductGroup`'s variants come from both `hasVariant` and the variants that name it in `isVariantOf`. A store with regional sections in its sitemaps can return items in several currencies.
- **Not read:** pages built in the browser (client-rendered stores), RDFa-only markup, and sitemaps the store neither names in robots.txt nor serves at the usual paths.
- **Partial:** a sitemap or listed page that can't be read. A page that answers but has no Product doesn't make a store partial, since sitemaps also list category, blog and info pages.
