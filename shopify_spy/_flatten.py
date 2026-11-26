"""Flat rows for scraped items: one row per variant, every cell a scalar.

Every platform shares one fixed column list, ``COLUMNS``, with a ``platform``
column, so one CSV (or any flat table) can hold a mixed run. Columns a platform
lacks are blank on its rows. Shopify products get a row per variant with the product's
columns, including the price range across its variants, repeated (a product without
variants still gets one row); collections get
one row each, sharing the product columns they have. WooCommerce products get a row
per variation, with the variation's own price, SKU and stock when the scrape fetched
its record (otherwise the product's). Magento products get a row per configurable
variant, with the variant's own price, SKU and stock (a simple product gets one row
with its own). BigCommerce products get a row per variant in the same way, with the
GraphQL ``edges``/``node`` connections read through. JSONL, JSON and XML keep each
platform's native item shape.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from shopify_spy._bigcommerce import image_list, nodes
from shopify_spy._magento import gallery_urls
from shopify_spy._summary import (
    bigcommerce_price,
    html_url,
    is_bigcommerce,
    is_magento,
    magento_price,
    shopify_price_range,
    summarize,
    woo_money,
)
from shopify_spy._utils import dicts

COLUMNS = [
    "platform",
    "kind",
    "store",
    "id",
    "handle",
    "title",
    "vendor",
    "product_type",
    "categories",
    "tags",
    "options",
    "created_at",
    "updated_at",
    "published_at",
    "description",
    "short_description",
    "html_url",
    "image_url",
    "image_count",
    "products_count",
    "currency",
    "min_price",
    "max_price",
    "on_sale",
    "is_purchasable",
    "average_rating",
    "review_count",
    "variant_count",
    "variant_id",
    "variant_title",
    "sku",
    "option1",
    "option2",
    "option3",
    "price",
    "compare_at_price",
    "available",
    "grams",
    "requires_shipping",
    "taxable",
    "variant_position",
    "variant_image_url",
    "url",
    "source",
    "image_paths",
]
# Column types, for formats with typed cells (XLSX). IDs stay text: as numbers, Excel
# shows them in scientific notation. Numbers may arrive as strings (Shopify prices,
# WooCommerce ratings, summarize().asdict() prices), which also share these names.
ID_COLUMNS = {"id", "variant_id"}
NUMBER_COLUMNS = {
    "price",
    "compare_at_price",
    "min_price",
    "max_price",
    "average_rating",
    "review_count",
    "image_count",
    "variant_count",
    "products_count",
    "grams",
    "variant_position",
}
DATE_COLUMNS = {"created_at", "updated_at", "published_at"}


def _builder(item: dict[str, Any]) -> Callable[[dict[str, Any]], list[dict[str, Any]]] | None:
    """The item's row builder: None when it isn't a scraped item (such as
    ``summarize().asdict()``)."""
    if isinstance(item.get("product"), dict):
        return _shopify_product
    if isinstance(item.get("collection"), dict):
        return _shopify_collection
    if is_magento(item):
        return _magento
    # Before WooCommerce: both have a prices dict
    if is_bigcommerce(item):
        return _bigcommerce
    if isinstance(item.get("prices"), dict) and "permalink" in item:
        return _woocommerce
    return None


def item_platform(item: dict[str, Any]) -> str | None:
    """``"shopify"``, ``"woocommerce"``, ``"magento"`` or ``"bigcommerce"``; None when
    it isn't a scraped item."""
    builder = _builder(item)
    return _PLATFORMS.get(builder) if builder else None


def default_columns(item: dict[str, Any]) -> list[str] | None:
    """Columns for a file whose first item is ``item``: ``COLUMNS``, without
    ``image_paths`` unless images were downloaded; None when it isn't a scraped item."""
    if _builder(item) is None:
        return None
    return [c for c in COLUMNS if c != "image_paths" or "scraped_images" in item]


def flatten(item: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The rows for a scraped item, keyed by ``COLUMNS``, or None when it isn't one."""
    builder = _builder(item)
    return builder(item) if builder else None


def _shopify_common(
    item: dict[str, Any], kind: str, data: dict[str, Any], images: list[dict[str, Any]]
) -> dict[str, Any]:
    image = data.get("image") if isinstance(data.get("image"), dict) else None
    return {
        "platform": "shopify",
        "kind": kind,
        "store": item.get("store"),
        "id": data.get("id"),
        "handle": data.get("handle"),
        "title": data.get("title"),
        "updated_at": data.get("updated_at"),
        "published_at": data.get("published_at"),
        "html_url": html_url(item, kind, data.get("handle")),
        "image_url": (images[0] if images else image or {}).get("src"),
        "url": item.get("url"),
    } | _image_paths(item)


def _shopify_product(item: dict[str, Any]) -> list[dict[str, Any]]:
    data: dict[str, Any] = item["product"]
    images = dicts(data.get("images"))
    image_srcs = {image.get("id"): image.get("src") for image in images}
    variants = dicts(data.get("variants"))
    tags = data.get("tags")
    min_price, max_price = shopify_price_range(data)
    product = _shopify_common(item, "product", data, images) | {
        "vendor": data.get("vendor"),
        "product_type": data.get("product_type"),
        # products.json has a list of tags, a product's own .json a comma-joined string
        "tags": _join(tags) if isinstance(tags, list) else tags,
        "options": _join(o.get("name") for o in dicts(data.get("options"))),
        "created_at": data.get("created_at"),
        "description": _text(data.get("body_html")),
        "image_count": len(images),
        "min_price": min_price,
        "max_price": max_price,
        "variant_count": len(variants),
    }
    rows = []
    for variant in variants:
        featured = variant.get("featured_image")
        rows.append(
            product
            | {
                "variant_id": variant.get("id"),
                "variant_title": variant.get("title"),
                "sku": variant.get("sku"),
                "option1": variant.get("option1"),
                "option2": variant.get("option2"),
                "option3": variant.get("option3"),
                "price": variant.get("price"),
                "compare_at_price": variant.get("compare_at_price"),
                "available": variant.get("available"),
                "grams": variant.get("grams"),
                "requires_shipping": variant.get("requires_shipping"),
                "taxable": variant.get("taxable"),
                "variant_position": variant.get("position"),
                "variant_image_url": featured.get("src")
                if isinstance(featured, dict)
                else image_srcs.get(variant.get("image_id")),
            }
        )
    return rows or [product]


def _shopify_collection(item: dict[str, Any]) -> list[dict[str, Any]]:
    data: dict[str, Any] = item["collection"]
    return [
        _shopify_common(item, "collection", data, [])
        | {
            "description": _text(data.get("description")),
            "products_count": data.get("products_count"),
        }
    ]


def _woocommerce(item: dict[str, Any]) -> list[dict[str, Any]]:
    summary = summarize(item)
    images = dicts(item.get("images"))
    variations = dicts(item.get("variations"))
    # Attributes that define variations play Shopify's options; others only describe
    options = [a for a in dicts(item.get("attributes")) if a.get("has_variations", True)]
    option_names = [_unescape(a.get("name")) for a in options if a.get("name")]
    # A variation's value is a term's slug for global attributes; show the term's name
    term_names = {
        (_unescape(a.get("name")), t.get("slug")): t.get("name")
        for a in options
        for t in dicts(a.get("terms"))
        if t.get("slug")
    }
    product = {
        "platform": "woocommerce",
        "kind": "product",
        "store": item.get("store"),
        "id": item.get("id"),
        "handle": item.get("slug"),
        "title": summary.title,
        "vendor": _names(item.get("brands")),
        "categories": _names(item.get("categories")),
        "tags": _names(item.get("tags")),
        "options": _join(option_names),
        "description": _text(item.get("description")),
        "short_description": _text(item.get("short_description")),
        "html_url": item.get("permalink"),
        "image_url": images[0].get("src") if images else None,
        "image_count": len(images),
        "currency": summary.currency,
        "min_price": summary.min_price,
        "max_price": summary.max_price,
        "on_sale": item.get("on_sale"),
        "is_purchasable": item.get("is_purchasable"),
        "average_rating": item.get("average_rating"),
        "review_count": item.get("review_count"),
        "variant_count": len(variations),
        **_woo_offer(item),
        # "product_page" for the partial items read from product pages' JSON-LD
        "source": item.get("source", "store_api"),
    } | _image_paths(item)
    rows = []
    for variation in variations:
        values: dict[Any, Any] = {}
        for a in dicts(variation.get("attributes")):
            name, value = _unescape(a.get("name")), a.get("value")
            if value not in (None, ""):
                values[name] = _unescape(term_names.get((name, value), value))
        # option1-3 line up with the options column, so an "any" value stays empty;
        # without attribute names the variation's own order stands in
        names = option_names or list(values)
        title = [values[n] for n in names if n in values]
        title += [v for n, v in values.items() if n not in names]
        rows.append(
            product
            | {
                "variant_id": variation.get("id"),
                "variant_title": " / ".join(str(v) for v in title) or None,
            }
            | {f"option{i + 1}": values.get(n) for i, n in enumerate(names[:3])}
            # Entries the scrape merged the variation's record into have its prices
            | (_woo_offer(variation) if isinstance(variation.get("prices"), dict) else {})
        )
    return rows or [product]


def _magento(item: dict[str, Any]) -> list[dict[str, Any]]:
    summary = summarize(item)
    images = gallery_urls(item)
    variants = dicts(item.get("variants"))
    options = dicts(item.get("configurable_options"))
    # One entry per coded option, so option1-3 line up with the options column; the
    # code names an option without a label
    options = [o for o in options if o.get("attribute_code")]
    codes = [o["attribute_code"] for o in options]
    product = {
        "platform": "magento",
        "kind": "product",
        "store": item.get("store"),
        "id": item.get("uid"),
        "handle": item.get("url_key"),
        "title": summary.title,
        "categories": _names(item.get("categories")),
        "options": _join(o.get("label") or o["attribute_code"] for o in options),
        "description": _text(_html(item.get("description"))),
        "short_description": _text(_html(item.get("short_description"))),
        "html_url": item.get("html_url"),
        "image_url": images[0] if images else None,
        "image_count": len(images),
        "currency": summary.currency,
        "min_price": summary.min_price,
        "max_price": summary.max_price,
        "variant_count": len(variants),
        **_magento_offer(item),
    } | _image_paths(item)
    rows = []
    for variant in variants:
        data = variant.get("product") if isinstance(variant.get("product"), dict) else {}
        labels = {a.get("code"): a.get("label") for a in dicts(variant.get("attributes"))}
        image = data.get("image") if isinstance(data.get("image"), dict) else {}
        rows.append(
            product
            | {"variant_id": data.get("uid")}
            | _variant_options(labels, codes)
            | _magento_offer(data, fallback=product)
            | {"variant_image_url": image.get("url")}
        )
    return rows or [product]


def _magento_offer(item: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    """The price, SKU and stock columns of a Magento product or variant.

    A variant without a price keeps the product's ``fallback`` price and compare-at price,
    so its row still shows what the product costs.
    """
    prices = item.get("price_range")
    price, _ = magento_price(prices)
    regular_price, _ = magento_price(prices, kind="regular_price")
    stock = item.get("stock_status")
    offer = {
        "sku": item.get("sku"),
        "price": price,
        "compare_at_price": regular_price if _below(price, regular_price) else None,
        "available": stock == "IN_STOCK" if isinstance(stock, str) else None,
    }
    if price is None and fallback is not None:
        offer["price"] = fallback.get("price")
        offer["compare_at_price"] = fallback.get("compare_at_price")
    return offer


def _html(value: Any) -> Any:
    """The ``html`` of a Magento ``ComplexTextValue``."""
    return value.get("html") if isinstance(value, dict) else None


def _bigcommerce(item: dict[str, Any]) -> list[dict[str, Any]]:
    summary = summarize(item)
    images = image_list(item)
    variants = nodes(item.get("variants"))
    options = [o for o in nodes(item.get("productOptions")) if o.get("isVariantOption") is True]
    path = item.get("path")
    rating, reviews = _bigcommerce_reviews(item.get("reviewSummary"))
    product = {
        "platform": "bigcommerce",
        "kind": "product",
        "store": item.get("store"),
        "id": item.get("entityId"),
        "handle": path.strip("/") or None if isinstance(path, str) else None,
        "title": summary.title,
        "vendor": _field(item.get("brand"), "name"),
        "categories": _names(nodes(item.get("categories"))),
        "options": _join(o.get("displayName") for o in options),
        "created_at": _field(item.get("createdAt"), "utc"),
        "description": _text(item.get("description")),
        "html_url": item.get("html_url"),
        "image_url": images[0] if images else None,
        "image_count": len(images),
        "currency": summary.currency,
        "min_price": summary.min_price,
        "max_price": summary.max_price,
        "average_rating": rating,
        "review_count": reviews,
        "variant_count": len(variants),
        **_bigcommerce_offer(item),
    } | _image_paths(item)
    ids = [o.get("entityId") for o in options]
    return [product | _bigcommerce_variant(v, ids, item) for v in variants] or [product]


def _bigcommerce_variant(
    variant: dict[str, Any], option_ids: list[Any], item: dict[str, Any]
) -> dict[str, Any]:
    """A variant's own columns; ``option_ids`` order option1-3 like the options column."""
    labels = {}
    for option in nodes(variant.get("options")):
        values = nodes(option.get("values"))
        labels[option.get("entityId")] = values[0].get("label") if values else None
    return (
        {"variant_id": variant.get("entityId"), "is_purchasable": variant.get("isPurchasable")}
        | _variant_options(labels, option_ids)
        | _bigcommerce_offer(variant, fallback=item)
        | {"variant_image_url": _field(variant.get("defaultImage"), "urlOriginal")}
    )


def _bigcommerce_offer(
    data: dict[str, Any], fallback: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The price, SKU and stock columns of a BigCommerce product or variant.

    A variant without its own price takes the ``fallback`` product's prices, its
    "was" price included, so a variant's price is never compared with another's.
    The "was" price is ``basePrice``, else ``retailPrice`` (MSRP): the first one the
    item sells below.
    """
    prices = data.get("prices")
    if bigcommerce_price(prices, "price")[0] is None and fallback is not None:
        prices = fallback.get("prices")

    def money(key: str) -> Decimal | None:
        return bigcommerce_price(prices, key)[0]

    price = money("price")
    was = [m for m in (money("basePrice"), money("retailPrice")) if _below(price, m)]
    return {
        "sku": data.get("sku"),
        "price": price,
        "compare_at_price": was[0] if was else None,
        "available": _field(data.get("inventory"), "isInStock"),
    }


def _bigcommerce_reviews(summary: Any) -> tuple[Decimal | None, Any]:
    """The average rating (None without reviews) and the review count."""
    count = _field(summary, "numberOfReviews")
    total = _field(summary, "summationOfRatings")
    numeric = all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in (count, total))
    if not numeric or count <= 0:
        return None, count
    return (Decimal(str(total)) / Decimal(str(count))).quantize(Decimal("0.01")), count


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


_PLATFORMS: dict[Callable[[dict[str, Any]], list[dict[str, Any]]], str] = {
    _shopify_product: "shopify",
    _shopify_collection: "shopify",
    _woocommerce: "woocommerce",
    _magento: "magento",
    _bigcommerce: "bigcommerce",
}


def _woo_offer(item: dict[str, Any]) -> dict[str, Any]:
    """The price, SKU and stock columns of a WooCommerce product or variation."""
    prices: dict[str, Any] = item["prices"]
    money = woo_money(prices)
    price = money(prices.get("price"))
    regular_price = money(prices.get("regular_price"))
    return {
        "sku": item.get("sku"),
        "price": price,
        # Like Shopify's, set only when the item sells below it
        "compare_at_price": regular_price if _below(price, regular_price) else None,
        "available": item.get("is_in_stock"),
    }


def _below(price: Decimal | None, regular_price: Decimal | None) -> bool:
    if price is None or regular_price is None or price.is_nan() or regular_price.is_nan():
        return False
    return price < regular_price


def _variant_options(labels: dict[Any, Any], order: list[Any]) -> dict[str, Any]:
    """A variant's ``variant_title`` and option1-3 from its option labels by key.

    ``order`` lines option1-3 up with the options column; keys it lacks come after.
    """
    values = [labels.get(k) for k in order] + [v for k, v in labels.items() if k not in order]
    return {"variant_title": " / ".join(str(v) for v in values if v not in (None, "")) or None} | {
        f"option{i + 1}": v for i, v in enumerate(values[:3])
    }


def _join(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in values if v not in (None, ""))


def _names(value: Any) -> str:
    return _join(html.unescape(str(d.get("name"))) for d in dicts(value) if d.get("name"))


def _unescape(value: Any) -> Any:
    return html.unescape(value) if isinstance(value, str) else value


def _image_paths(item: dict[str, Any]) -> dict[str, str]:
    """The ``image_paths`` column, only for items that went through image downloads."""
    if "scraped_images" not in item:
        return {}
    return {"image_paths": _join(i.get("path") for i in dicts(item["scraped_images"]))}


# Tags that break text; inline ones (a, b, span, ...) join their neighbors
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _text(value: Any) -> str | None:
    """HTML as plain text on one line."""
    if not isinstance(value, str):
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()
