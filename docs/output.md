# Output

Each run writes its items to a new timestamped file in the output directory (`./output` by default), as JSONL unless `--format` says otherwise. The items' shape depends on the schema: `native` keeps each item as the store's API returned it, and `unified` gives one row per variant with the same columns on every platform.

## Files

```
output/
  data/
    shopify_spider_2024-01-15T10-30-00+00-00.jsonl
  status/
    shopify_spider_2024-01-15T10-30-00+00-00_status.json
  images/
    full/
      <image files>
```

File names start with the platform and a UTC timestamp. `data/` holds the items, `status/` holds a [status file](troubleshooting.md#the-status-file) for each run, and `images/` holds the downloads from `--images`. `--peek` writes none of these.

## Schemas

- **`native`** (the default for JSONL, JSON and XML): each item as the store's API returned it, described under [Shopify items](#shopify-items), [WooCommerce items](#woocommerce-items), [Magento items](#magento-items), [BigCommerce items](#bigcommerce-items) and [JSON-LD items](#json-ld-items). It keeps every field.
- **`unified`** (`--schema unified`, or `output.schema` in the config): one record per variant with the same columns on every platform, described under [Unified schema](#unified-schema). CSV and XLSX are always unified, and `--schema native` with them is an error. In XML each row is an `<item>`.

## Shopify items

Each line holds the full product or collection JSON from Shopify's API, plus four added fields. `platform` tells it apart from a WooCommerce, Magento, BigCommerce or JSON-LD item, and `image_urls` is empty unless `--images` is given.

```json
{
  "product": { "title": "...", "variants": [...], "images": [...], ... },
  "url": "https://store.com/products/item.json",
  "store": "store.com",
  "platform": "shopify",
  "image_urls": ["https://cdn.shopify.com/.../product.jpg"]
}
```

With `--collections`, collection items have a `collection` object in place of `product`, with the same four added fields.

## WooCommerce items

Each line holds the full product JSON from the WooCommerce Store API, plus three added fields (`image_urls` is empty unless `--images` is given):

```json
{
  "id": 123,
  "name": "Product Name",
  "slug": "product-name",
  "permalink": "https://store.com/product/product-name/",
  "sku": "SKU-001",
  "prices": { "price": "5200", "currency_code": "USD", "currency_minor_unit": 2 },
  "images": [{ "id": 1, "src": "https://..." }],
  "store": "store.com",
  "platform": "woocommerce",
  "image_urls": ["https://..."]
}
```

Prices are strings in minor currency units: divide by `10^currency_minor_unit` to get the decimal value. Each entry in `variations` is the variation's own Store API record (its `prices`, `sku`, `is_in_stock` and so on), with the product listing's `attributes` for it. If the store refuses the variation listing, the entries keep only `id` and `attributes`, as the product listing gives them.

### Items from product pages

When a store's Store API is unavailable on its first page, Shopify Spy reads its product pages instead and turns each page's schema.org `Product` (or `ProductGroup`) data, JSON-LD or microdata, into a partial item in the same shape. These items have `"source": "product_page"`.

- **When it happens:** the first Store API page returns an error status other than a 429 rate limit, the request fails, the answer isn't a product list, or robots.txt disallows it.
- **How pages are found:** through the WordPress REST API's product list (`/wp-json/wp/v2/product`), or the product sitemaps (WordPress core's `/wp-sitemap.xml`, Yoast's or Rank Math's `/sitemap_index.xml`).
- **What they carry:** only `id`, `name`, `slug`, `permalink`, `sku`, `description`, `images`, `prices`, `on_sale` and `is_in_stock` (plus the added `store`, `platform` and `image_urls`), and the page's structured data in `json_ld` (with its `{"@id": ...}` references resolved). There are no categories or tags.
- **Product groups:** a `ProductGroup` (Yoast's markup for a variable product) without offers of its own takes its price range from its variants' offers, and is in stock when any variant is.
- **Cleanup:** relative image URLs are resolved against the page's URL, names and descriptions are unescaped until they stop changing (pages escape them, sometimes twice), names lose the site name an SEO plugin may append (`Hat - My Shop`), and a SKU that's really the product ID (WooCommerce's placeholder for a missing SKU) is set to `null`.
- **Variations:** a variable product also gets `attributes` and `variations` from the page's variation form, each variation with its own `prices`, `sku` and `is_in_stock`, so the unified schema has a row per variation. WooCommerce embeds that form's data only for products with up to 30 variations (by default), so larger ones keep just the price range.
- **Pages that don't work:** pages without schema.org `Product` data for the page are skipped (the page's product is chosen as for [JSON-LD items](#json-ld-items), so a page of several products none of which names it isn't read), as are deleted (404 or 410) pages. Other pages that fail to load mark the store `partial`.
- **XML:** keys that aren't valid element names (such as JSON-LD's `@type`) have their invalid characters replaced with `_`.

## Magento items

Each line holds a product as the storefront GraphQL `products` query at `/graphql` returns it, plus four added fields (`image_urls` is empty unless `--images` is given):

```json
{
  "__typename": "ConfigurableProduct",
  "uid": "MTIz",
  "sku": "TEE",
  "name": "Tee",
  "url_key": "tee",
  "url_suffix": ".html",
  "stock_status": "IN_STOCK",
  "price_range": {
    "minimum_price": { "regular_price": { "value": 30, "currency": "EUR" }, "final_price": { "value": 20, "currency": "EUR" } },
    "maximum_price": { "regular_price": { "value": 30, "currency": "EUR" }, "final_price": { "value": 30, "currency": "EUR" } }
  },
  "media_gallery": [{ "url": "https://...", "label": null, "position": 1, "disabled": false }],
  "configurable_options": [{ "attribute_code": "size", "label": "Size", "values": [...] }],
  "variants": [{ "attributes": [{ "code": "size", "label": "S", "value_index": 1 }], "product": { "uid": "...", "sku": "TEE-S", ... } }],
  "store": "store.com",
  "platform": "magento",
  "html_url": "https://store.com/tee.html",
  "image_urls": ["https://..."]
}
```

- **Fields:** `uid`, `sku`, `name`, `url_key`, `url_suffix`, `stock_status`, `description` and `short_description` (as `{"html": ...}`), `categories` (names), `price_range` and `media_gallery`. Prices are decimal numbers in the store view's currency.
- **`html_url`** is the product page, `<scheme>://<host>/<url_key><url_suffix>`, or `null` without a `url_key`.
- **`image_urls`** lists the `media_gallery` URLs that aren't disabled, in `position` order.
- **Variants:** a `ConfigurableProduct` also has `configurable_options` and `variants`; each variant has its `attributes` (option values) and its own `product`, with `uid`, `sku`, `stock_status`, `image` and `price_range`. Other product types (simple, virtual, bundle, ...) have neither.
- **Store view:** only the default store view is scraped, in its currency. A multi-store site's other views (picked by a `Store` header or a URL path such as `/ch-de/`) aren't.

## BigCommerce items

Each line holds a product node as the Storefront GraphQL `site.products` query at `/graphql` returns it, with its `edges`/`node` connections, plus four added fields (`image_urls` is empty unless `--images` is given):

```json
{
  "id": "UHJvZHVjdDoxMTI=",
  "entityId": 112,
  "sku": "TEE",
  "name": "Tee",
  "path": "/tee/",
  "prices": {
    "price": { "value": 20, "currencyCode": "USD" },
    "basePrice": { "value": 30, "currencyCode": "USD" },
    "priceRange": { "min": { "value": 20, "currencyCode": "USD" }, "max": { "value": 30, "currencyCode": "USD" } }
  },
  "inventory": { "isInStock": true },
  "images": { "edges": [{ "node": { "urlOriginal": "https://cdn11.bigcommerce.com/...", "altText": "", "isDefault": true } }] },
  "productOptions": { "edges": [{ "node": { "entityId": 1, "displayName": "Size", "isVariantOption": true } }] },
  "variants": { "pageInfo": { "hasNextPage": false, "endCursor": "YXJyYXljb25uZWN0aW9uOjE=" }, "edges": [{ "node": { "entityId": 78, "sku": "TEE-S", "prices": {...}, "options": {...} } }] },
  "store": "store.com",
  "platform": "bigcommerce",
  "html_url": "https://store.com/tee/",
  "image_urls": ["https://cdn11.bigcommerce.com/..."]
}
```

- **Fields:** `id`, `entityId`, `sku`, `gtin`, `mpn`, `upc`, `name`, `path`, `description` (HTML), `createdAt`, `brand`, `categories`, `prices` (`price`, `basePrice`, `salePrice`, `retailPrice` and `priceRange`), `inventory`, `availabilityV2`, `reviewSummary`, `images`, `productOptions` and `variants`. Prices are `{"value", "currencyCode"}` objects in the store's default currency; other currencies and channels aren't scraped.
- **`html_url`** is the product page, `<scheme>://<host><path>`, or `null` without a `path`.
- **`image_urls`** lists the `images` `urlOriginal` values, the default image first.
- **Variants:** each variant node has its `entityId`, `sku`, `gtin`, `mpn`, `upc`, `isPurchasable`, `prices`, `inventory`, `defaultImage` and `options` (each with its `values`). Every variant is read, 250 per query, up to 2,500 a product; `variants.pageInfo.hasNextPage` is `true` only on a product whose variants weren't all read (past that bound, or after a failed variants query), and the log has a warning for it.

## JSON-LD items

With `--platform jsonld`, each line holds the schema.org `Product` or `ProductGroup` node from one product page's JSON-LD (or its microdata), as the page has it except that references to other nodes on the page (`{"@id": ...}`, as Yoast gives a product's images) are replaced by those nodes, plus four added fields (`image_urls` is empty unless `--images` is given):

```json
{
  "@context": "https://schema.org",
  "@type": "ProductGroup",
  "name": "Tee",
  "productGroupID": "TEE",
  "brand": { "@type": "Brand", "name": "Acme" },
  "image": ["https://store.com/tee.jpg"],
  "variesBy": ["https://schema.org/size"],
  "hasVariant": [
    { "@type": "Product", "sku": "TEE-S", "size": "S", "offers": { "@type": "Offer", "price": "20.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock" } },
    { "@type": "Product", "sku": "TEE-M", "size": "M", "offers": { "@type": "Offer", "price": "22.00", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock" } }
  ],
  "store": "store.com",
  "platform": "jsonld",
  "html_url": "https://store.com/products/tee",
  "image_urls": ["https://store.com/tee.jpg"]
}
```

- **How pages are found:** from the store's sitemaps: the `Sitemap:` lines in robots.txt, else the first of `/sitemap.xml`, `/sitemap_index.xml` and `/xmlsitemap.php` that is a sitemap. When a sitemap index has product sitemaps (a file name or query string containing `product`, such as BigCommerce's `/xmlsitemap.php?type=products&page=1`), only those are read. Pages on other hosts are skipped.
- **Microdata:** a page whose JSON-LD has no Product for it is read for schema.org microdata (`itemscope`, `itemtype` and `itemprop` attributes), which gives a node of the same shape: `{"@type": "Product", "name": ..., "offers": {"@type": "Offer", ...}}`, with a property given more than once as a list. Values come from `content`, `href`, `src`, `datetime` or `value` attributes, else the element's text; `itemref` isn't followed. A rating's `ratingValue` is kept only with its `bestRating` or `worstRating`, since some themes give a percentage (Magento's Luma) or `0` for no reviews.
- **Which pages count:** a page whose JSON-LD (or microdata) has a `Product` or `ProductGroup` for that page: one whose `url` or `@id` is the page, else the only one that is the page but for its query string (a `?variant=1` URL), else the page's only product or only group. The node can be at the top level, in an `@graph`, or a page's `mainEntity`, and a type can be written `Product`, `https://schema.org/Product` or `schema:Product`. A listing page with several products, none of them the page's own, is skipped, as are pages with no Product data and deleted (404 or 410) pages.
- **Fields:** whatever the page's node has. Common ones are `name`, `sku`, `brand`, `category`, `description`, `image` and `offers` (an `Offer`, an `AggregateOffer` with `lowPrice`/`highPrice`, or a list of offers). Prices are strings or numbers as the page wrote them.
- **Normalized:** the node keeps the page's values, but property names and `@type` lose a `schema:` or `https://schema.org/` prefix, and variants written as separate Products that name the group in `isVariantOf` are put in its `hasVariant`. JSON-LD wrapped in an HTML comment (`<!-- ... -->`), with comments, trailing commas or empty values (`"description": ,`, read as `null`) is read too.
- **`html_url`** is the page the node came from, without its fragment.
- **Images:** `image_urls` and the unified `image_url` and `variant_image_url` columns resolve a relative `image` (`/media/tee.jpg`) against the page's URL; the node's own `image` stays as written.
- **Variants:** a `ProductGroup` keeps its `hasVariant` Products inside it, each with its own `sku` and `offers` (the unified schema skips entries with neither). Many stores publish a single Product with one offer or a price range instead, so variants are often missing.
- **Currencies:** pages come from the store's sitemaps, so a store with regional sections (such as vitamix.com's `/ca/en_us/` pages) can give items in several currencies. Each item's price carries its own currency (the offers' `priceCurrency`, and the unified `currency` column).
- **Fidelity:** a store's JSON-LD is written for search engines, not as a catalog, so it can leave out fields the native platforms have (tags, stock counts, variant options). See [JSON-LD stores](troubleshooting.md#json-ld-stores).
- **XML:** keys that aren't valid element names (such as `@type`) have their invalid characters replaced with `_`.

## Images

With `--images`, each item gets a `scraped_images` field recording each download:

```json
{
  "image_urls": ["https://cdn.shopify.com/.../product.jpg"],
  "scraped_images": [
    {
      "url": "https://cdn.shopify.com/.../product.jpg",
      "path": "full/abc123def.jpg",
      "checksum": "d41d8cd98f00b204e9800998ecf8427e",
      "status": "downloaded"
    }
  ]
}
```

Images are saved in their original format, named by the SHA-1 of their URL, with the extension of the format their bytes are in. The `path` is relative to the images directory (`output/images/` by default). Files younger than 90 days are reused, with `"status": "uptodate"`.

Supported formats are JPEG, PNG, GIF, WebP, AVIF, HEIC/HEIF, JPEG XL, JPEG 2000, BMP, TIFF and ICO. Anything else, such as an SVG or an error page served in place of an image, is logged and counted as a failed image instead of being saved (its first bytes are logged at debug level).

## Unified schema

The unified schema has one row per variant and a plain value in every cell (no embedded JSON), so you can sort and filter it in a spreadsheet or load it as a table. CSV and Excel (XLSX) always use it; JSONL, JSON and XML use it with `--schema unified`. The product's columns repeat on each of its variant rows, and a product without variants still gets one row. Every platform shares the same columns, so one file can hold Shopify, WooCommerce, Magento, BigCommerce and JSON-LD items. A column a platform lacks is blank on its rows (empty in CSV and XML, `null` in JSON).

| Column | Shopify | WooCommerce | Magento | BigCommerce | JSON-LD |
|---|---|---|---|---|---|
| `platform` | `shopify` | `woocommerce` | `magento` | `bigcommerce` | `jsonld` |
| `kind` | `product` or `collection` (`--collections`) | `product` | `product` | `product` | `product` |
| `store`, `sku`, `image_count` | yes | yes | yes | yes | yes |
| `id` | `id` | `id` | `uid` | `entityId` | `productID`, `productGroupID`, `sku` or `@id` |
| `tags` | yes | yes | | | |
| `handle` | `handle` | `slug` | `url_key` | `path` without its slashes | the last segment of the page's path |
| `title` | `title` | `name` | `name` | `name` | `name` |
| `vendor` | `vendor` | brand names | | `brand.name` | `brand` names |
| `product_type` | `product_type` | | | | |
| `categories` | | category names | category names | category names | `category` |
| `options` | option names, such as `Color, Size` | names of the attributes that define variations | `configurable_options` labels (the attribute code for an option without one) | `displayName` of the `productOptions` that define variants | the `variesBy` properties of a `ProductGroup`, such as `size` |
| `created_at`, `updated_at`, `published_at` | yes | | | `created_at` (`createdAt.utc`) | |
| `description` | `body_html` as plain text | `description` as plain text | `description.html` as plain text | `description` as plain text | `description` as plain text |
| `short_description` | | as plain text | `short_description.html` as plain text | | |
| `html_url` | the product or collection page | `permalink` | `html_url` | `html_url` | `html_url` |
| `image_url` | the first image | the first image | the first `media_gallery` image by position, not disabled | the default image, else the first | the first `image` |
| `products_count` | collections only | | | | |
| `currency` | | `currency_code` | the minimum final price's `currency` | the price range's `currencyCode` | the first offer's `priceCurrency` |
| `min_price`, `max_price` | the lowest and highest variant price (products only) | the price range | the minimum and maximum final price | `priceRange` (else `price`) | the lowest and highest offer price, across variants (an `AggregateOffer`'s `lowPrice` and `highPrice`) |
| `on_sale`, `is_purchasable` | | yes | | `is_purchasable` (the variant's) | |
| `average_rating`, `review_count` | | yes; for items from product pages, as for JSON-LD | | `summationOfRatings / numberOfReviews`, to 2 decimals (blank without reviews), and `numberOfReviews` | the `aggregateRating`'s `ratingValue`, out of 5 (one on another `bestRating` scale is converted), and its `reviewCount` or `ratingCount` |
| `variant_count` | variants | variations | `variants` | `variants` | `hasVariant` entries with `offers` or a `sku`, or the offers in a list of offers |
| `variant_id` | the variant's ID | the variation's ID | the variant product's `uid` | the variant's `entityId` | the variant's `productID`, `sku` or `@id`; an offer's `sku`, `@id` or `url` |
| `variant_title` | such as `Red / S` | the variation's attribute values, such as `Red / S` | the variant's option labels, such as `Red / S` | the variant's option values, such as `Red / S` | the variant's `variesBy` values, such as `Red / S`, else its `name`; an offer's `name` |
| `gtin`, `mpn` | | for items from product pages only, as for JSON-LD | blank: only store-defined custom attributes hold them | the variant's `gtin` (else its `upc`) and `mpn`, else the product's | the variant's (or its offer's) `gtin`, `gtin13`, `gtin12`, `gtin14` or `gtin8` and `mpn`, else the product's (or its offer's; an offer's row doesn't take another offer's) |
| `option1`-`option3` | the variant's option values | the variation's attribute values, in the product's attribute order | the variant's option labels, in `configurable_options` order | the variant's option values, in `productOptions` order | the variant's `variesBy` values, in `variesBy` order |
| `price` | the variant's price | `price` | the final price (the variant's, else the product's minimum) | `prices.price` (the variant's, else the product's) | the lowest offer price (the variant's or offer's, else the product's) |
| `compare_at_price` | the variant's `compare_at_price` | `regular_price`, only when `price` is below it | the regular price, only when `price` is below it | the first of `basePrice` and `retailPrice` (MSRP) that `price` is below | a `ListPrice` or `StrikethroughPrice`, only when `price` is below it |
| `available` | the variant's `available` | `is_in_stock` | `stock_status` is `IN_STOCK` | `inventory.isInStock` | `availability` isn't `OutOfStock`, `SoldOut` or `Discontinued` (an `AggregateOffer` without one has an in-stock offer's among its `offers`, if any) |
| `grams`, `requires_shipping`, `taxable`, `variant_position` | the variant's | | | | |
| `variant_image_url` | the variant's | | the variant product's `image.url` | the variant's `defaultImage.urlOriginal` | the variant's first `image` |
| `url` | the scraped JSON document | | | | |
| `source` | | `store_api`, or `product_page` for the partial items read from product pages | | | |
| `image_paths` | with `--images`: the downloaded files | with `--images`: the downloaded files | with `--images`: the downloaded files | with `--images`: the downloaded files | with `--images`: the downloaded files |

- **Prices** are decimal amounts, with WooCommerce's minor units converted. JSON-LD prices written with a currency symbol or separators are read: of a dot and a comma, the last is the decimal point (`1.234,56`, `1,299.00`), and a lone comma is decimal (`19,99`) unless three digits follow it (`1,299`). A price that can't be read is left blank. `min_price` and `max_price` are the product's price range on each of its rows. In JSON, prices are strings.
- **WooCommerce variation rows** have the variation's own `sku`, `price`, `compare_at_price` and `available`. If the store refuses the variation listing, they're the product's.
- **Magento variant rows** have the variant's own `sku`, `price`, `compare_at_price` and `available`. A product without `variants` gets one row with its own.
- **BigCommerce variant rows** have the variant's own `sku`, `price`, `compare_at_price`, `available` and `is_purchasable`; a variant without its own price has the product's. A product without variant edges gets one row with its own `sku`, price and stock.
- **JSON-LD variant rows:** a `ProductGroup` gets a row per `hasVariant` Product, with the variant's own `sku`, `price` and `available`; a variant without a price keeps the product's. Entries with neither `offers` nor a `sku`, which some stores use to link the product's other colours, get no row. A Product with a list of offers gets a row per offer, with each offer's `sku`, `price` and `available`. A single `Offer` or an `AggregateOffer` gives one row.
- **`gtin` and `mpn`** come from BigCommerce's own fields and from schema.org data. Shopify's `products.json` has no barcodes, WooCommerce's Store API has no GTINs or MPNs, and Magento's storefront GraphQL has them only as custom attributes each store defines and names, which aren't read, so they're blank on those rows (WooCommerce items read from product pages have their JSON-LD's).
- **Left out:** WooCommerce's `type` (`simple`, `variable`, ...) and descriptive attributes are only in the native schema. So are Magento's `__typename` and option `value_index` codes, BigCommerce's `availabilityV2`, `salePrice` and option `entityId`s, and JSON-LD properties without a column (`color`, `weight`, ...). Without `--images`, the `image_paths` column is left out; with it, the column lists the downloaded files, relative to the images directory.
- **JSON-LD text:** pages often escape their JSON-LD text, sometimes twice (`&amp;amp;`), so `title`, `vendor`, `categories`, `description` and the variant titles are unescaped until they stop changing; names are put on one line.
- **Values:** booleans are `true`/`false` in JSON, CSV and unified XML. In CSV and XML, an empty cell means no value.
- **Old CSV files:** appending to a CSV written by 0.3.0 or earlier, which had a column per item field, is refused; start a new file.

## Excel output

`--format xlsx` (with the [`excel` extra](install.md#excel-support) installed) writes the unified schema's rows to an Excel workbook, every platform in one sheet. It avoids the CSV's problems in Excel:

- Text is UTF-8-safe and never evaluated as a formula.
- Prices, counts and ratings are numbers, and booleans are `TRUE`/`FALSE`.
- `created_at`, `updated_at` and `published_at` are dates in UTC (Excel has no time zones).
- IDs, `gtin` and `mpn` stay text, since Excel would show them in scientific notation and drop a GTIN's leading zero.
- The header row is frozen and has filters.
- Text longer than Excel's cell limit (32,767 characters) is cut, with a warning in the log.
- Past Excel's 1,048,576 rows, items continue on a new sheet (`items 2`, ...), keeping a product's variant rows together.

The file is written when the run ends, including after Ctrl-C (the CLI ignores Ctrl-C while it writes). So a killed process leaves no usable file, and there is no `append`. If building the workbook fails, the partial file is removed. During the run, rows are kept in a temporary folder next to the output file (`products.xlsx.tmp-…`), which needs disk space for roughly the uncompressed data. It is deleted at the end, but a killed process leaves it behind, and it can be deleted.

For long runs, write JSONL and convert it afterwards:

```python
import json
import shopify_spy as spy

with open("products.jsonl", encoding="utf-8") as f:
    spy.write_items((json.loads(line) for line in f), "products.xlsx")
```

## Working with the output

**With jq:**

```bash
# Shopify: extract product titles
cat output/data/*.jsonl | jq 'select(.product) | .product.title'

# WooCommerce: extract product names and prices
cat output/data/*.jsonl | jq 'select(.prices) | {name: .name, price: .prices.price, currency: .prices.currency_code}'

# Magento: extract product names, SKUs and prices
cat output/data/*.jsonl | jq 'select(.platform == "magento") | {name, sku, price: .price_range.minimum_price.final_price.value}'

# BigCommerce: extract product names, SKUs and prices
cat output/data/*.jsonl | jq 'select(.platform == "bigcommerce") | {name, sku, price: .prices.price.value}'

# JSON-LD: extract product names, pages and offers
cat output/data/*.jsonl | jq 'select(.platform == "jsonld") | {name, html_url, offers}'

# Unified schema (--schema unified): the same fields on every platform, a line per variant
cat output/data/*.jsonl | jq -r '[.platform, .title, .variant_title, .price] | @tsv'
```

**With DuckDB**, which reads JSONL and CSV directly. Each run writes a new timestamped file, so a glob over `output/data/` queries every run at once, such as each variant's price over time. Point a query at files of one shape: a JSONL query that reads `product` fails if the glob also matches WooCommerce, Magento, BigCommerce, JSON-LD or unified files.

```bash
# Native JSONL from Shopify runs
duckdb -c "select regexp_extract(filename, '\d{4}-\d\d-\d\dT[\d-]+', 0) as run, v.id, v.price
           from (select filename, unnest(product.variants) as v
                 from read_json('output/data/shopify_spider_*.jsonl', filename=true))
           order by v.id, run"

# Unified CSV from any platform
duckdb -c "select platform, store, count(*) as variants, min(price), max(price)
           from read_csv('output/data/*.csv', union_by_name=true)
           group by all"
```

**With Python:**

```python
import json

with open("output/data/shopify_spider_2024-01-15T10-30-00+00-00.jsonl", encoding="utf-8") as f:
    for line in f:
        item = json.loads(line)
        print(item["product"]["title"])  # Shopify
        # print(item["name"])            # WooCommerce, Magento, BigCommerce or JSON-LD
```

**With pandas:**

```python
import glob
import pandas as pd

# Native JSONL: one item per row
df = pd.read_json("output/data/shopify_spider_2024-01-15T10-30-00+00-00.jsonl", lines=True)
products = pd.json_normalize(df["product"])  # Shopify

# Unified CSV: a row per variant, every run in one frame
runs = sorted(glob.glob("output/data/*.csv"))
df = pd.concat((pd.read_csv(f).assign(run=f) for f in runs), ignore_index=True)
df.groupby(["platform", "store"])["price"].agg(["count", "min", "max"])
```

**With polars:**

```python
import polars as pl

df = pl.read_ndjson("output/data/shopify_spider_2024-01-15T10-30-00+00-00.jsonl")  # native Shopify
rows = pl.read_csv("output/data/shopify_spider_2024-01-15T10-30-00+00-00.csv")  # unified
```

To filter or reshape items while they're scraped, rather than after, write a script against the [library](library.md).
