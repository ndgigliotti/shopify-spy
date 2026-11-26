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

- **`native`** (the default for JSONL, JSON and XML): each item as the store's API returned it, described under [Shopify items](#shopify-items), [WooCommerce items](#woocommerce-items) and [Magento items](#magento-items). It keeps every field.
- **`unified`** (`--schema unified`, or `output.schema` in the config): one record per variant with the same columns on every platform, described under [Unified schema](#unified-schema). CSV and XLSX are always unified, and `--schema native` with them is an error. In XML each row is an `<item>`.

## Shopify items

Each line holds the full product or collection JSON from Shopify's API, plus four added fields. `platform` tells it apart from a WooCommerce or Magento item, and `image_urls` is empty unless `--images` is given.

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

When a store's Store API is unavailable on its first page, Shopify Spy reads its product pages instead and turns each page's schema.org `Product` data into a partial item in the same shape. These items have `"source": "product_page"`.

- **When it happens:** the first Store API page returns an error status other than a 429 rate limit, the request fails, the answer isn't a product list, or robots.txt disallows it.
- **How pages are found:** through the WordPress REST API's product list (`/wp-json/wp/v2/product`), or the product sitemaps (WordPress core's `/wp-sitemap.xml`, Yoast's or Rank Math's `/sitemap_index.xml`).
- **What they carry:** only `id`, `name`, `slug`, `permalink`, `sku`, `description`, `images`, `prices`, `on_sale` and `is_in_stock` (plus the added `store`, `platform` and `image_urls`), and the page's original structured data in `json_ld`. There are no categories or tags.
- **Cleanup:** names lose the site name an SEO plugin may append (`Hat - My Shop`), and a SKU that's really the product ID (WooCommerce's placeholder for a missing SKU) is set to `null`.
- **Variations:** a variable product also gets `attributes` and `variations` from the page's variation form, each variation with its own `prices`, `sku` and `is_in_stock`, so the unified schema has a row per variation. WooCommerce embeds that form's data only for products with up to 30 variations (by default), so larger ones keep just the price range.
- **Pages that don't work:** pages without schema.org `Product` data are skipped, as are deleted (404 or 410) pages. Other pages that fail to load mark the store `partial`.
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

Supported formats are JPEG, PNG, GIF, WebP, AVIF, HEIC/HEIF, JPEG XL, JPEG 2000, BMP, TIFF and ICO. Anything else, such as an SVG or an error page served in place of an image, is logged and counted as a failed image instead of being saved.

## Unified schema

The unified schema has one row per variant and a plain value in every cell (no embedded JSON), so you can sort and filter it in a spreadsheet or load it as a table. CSV and Excel (XLSX) always use it; JSONL, JSON and XML use it with `--schema unified`. The product's columns repeat on each of its variant rows, and a product without variants still gets one row. Every platform shares the same columns, so one file can hold Shopify, WooCommerce and Magento items. A column a platform lacks is blank on its rows (empty in CSV and XML, `null` in JSON).

| Column | Shopify | WooCommerce | Magento |
|---|---|---|---|
| `platform` | `shopify` | `woocommerce` | `magento` |
| `kind` | `product` or `collection` (`--collections`) | `product` | `product` |
| `store`, `sku`, `image_count` | yes | yes | yes |
| `id` | `id` | `id` | `uid` |
| `tags` | yes | yes | |
| `handle` | `handle` | `slug` | `url_key` |
| `title` | `title` | `name` | `name` |
| `vendor` | `vendor` | brand names | |
| `product_type` | `product_type` | | |
| `categories` | | category names | category names |
| `options` | option names, such as `Color, Size` | names of the attributes that define variations | `configurable_options` labels |
| `created_at`, `updated_at`, `published_at` | yes | | |
| `description` | `body_html` as plain text | `description` as plain text | `description.html` as plain text |
| `short_description` | | as plain text | `short_description.html` as plain text |
| `html_url` | the product or collection page | `permalink` | `html_url` |
| `image_url` | the first image | the first image | the first `media_gallery` image by position, not disabled |
| `products_count` | collections only | | |
| `currency` | | `currency_code` | the minimum final price's `currency` |
| `min_price`, `max_price` | the lowest and highest variant price (products only) | the price range | the minimum and maximum final price |
| `on_sale`, `is_purchasable`, `average_rating`, `review_count` | | yes | |
| `variant_count` | variants | variations | `variants` |
| `variant_id` | the variant's ID | the variation's ID | the variant product's `uid` |
| `variant_title` | such as `Red / S` | the variation's attribute values, such as `Red / S` | the variant's option labels, such as `Red / S` |
| `option1`-`option3` | the variant's option values | the variation's attribute values, in the product's attribute order | the variant's option labels, in `configurable_options` order |
| `price` | the variant's price | `price` | the final price (the variant's, else the product's minimum) |
| `compare_at_price` | the variant's `compare_at_price` | `regular_price`, only when `price` is below it | the regular price, only when `price` is below it |
| `available` | the variant's `available` | `is_in_stock` | `stock_status` is `IN_STOCK` |
| `grams`, `requires_shipping`, `taxable`, `variant_position` | the variant's | | |
| `variant_image_url` | the variant's | | the variant product's `image.url` |
| `url` | the scraped JSON document | | |
| `source` | | `store_api`, or `product_page` for the partial items read from product pages | |
| `image_paths` | with `--images`: the downloaded files | with `--images`: the downloaded files | with `--images`: the downloaded files |

- **Prices** are decimal amounts, with WooCommerce's minor units converted. `min_price` and `max_price` are the product's price range on each of its rows. In JSON, prices are strings.
- **WooCommerce variation rows** have the variation's own `sku`, `price`, `compare_at_price` and `available`. If the store refuses the variation listing, they're the product's.
- **Magento variant rows** have the variant's own `sku`, `price`, `compare_at_price` and `available`. A product without `variants` gets one row with its own.
- **Left out:** WooCommerce's `type` (`simple`, `variable`, ...) and descriptive attributes are only in the native schema. So are Magento's `__typename` and option `value_index` codes. Without `--images`, the `image_paths` column is left out; with it, the column lists the downloaded files, relative to the images directory.
- **Values:** booleans are `true`/`false` in JSON, CSV and unified XML. In CSV and XML, an empty cell means no value.
- **Old CSV files:** appending to a CSV written by 0.3.0 or earlier, which had a column per item field, is refused; start a new file.

## Excel output

`--format xlsx` (with the [`excel` extra](install.md#excel-support) installed) writes the unified schema's rows to an Excel workbook, every platform in one sheet. It avoids the CSV's problems in Excel:

- Text is UTF-8-safe and never evaluated as a formula.
- Prices, counts and ratings are numbers, and booleans are `TRUE`/`FALSE`.
- `created_at`, `updated_at` and `published_at` are dates in UTC (Excel has no time zones).
- IDs stay text, since Excel would show them in scientific notation.
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

# Unified schema (--schema unified): the same fields on every platform, a line per variant
cat output/data/*.jsonl | jq -r '[.platform, .title, .variant_title, .price] | @tsv'
```

**With DuckDB**, which reads JSONL and CSV directly. Each run writes a new timestamped file, so a glob over `output/data/` queries every run at once, such as each variant's price over time. Point a query at files of one shape: a JSONL query that reads `product` fails if the glob also matches WooCommerce, Magento or unified files.

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
        # print(item["name"])            # WooCommerce or Magento
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
