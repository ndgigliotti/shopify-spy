"""Flat rows for scraped items: one row per variant, every cell a scalar.

Both platforms share one fixed column list, ``COLUMNS``, with a ``platform`` column,
so one CSV (or any flat table) can hold a mixed run. Columns only one platform has
are blank for the other. Shopify products get a row per variant with the product's
columns repeated (a product without variants still gets one row); collections get
one row each, sharing the product columns they have. WooCommerce products get a row
per variation; the Store API lists only a variation's ID and attributes, so the
price, SKU and stock columns are the product's. JSONL, JSON and XML keep each
platform's native item shape.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from shopify_spy._summary import html_url, summarize, woo_money

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
    "image_paths",
]


def _builder(item: dict[str, Any]) -> Callable[[dict[str, Any]], list[dict[str, Any]]] | None:
    """The item's row builder: None when it isn't a scraped item (such as
    ``summarize().asdict()``)."""
    if isinstance(item.get("product"), dict):
        return _shopify_product
    if isinstance(item.get("collection"), dict):
        return _shopify_collection
    if isinstance(item.get("prices"), dict) and "permalink" in item:
        return _woocommerce
    return None


def item_platform(item: dict[str, Any]) -> str | None:
    """``"shopify"`` or ``"woocommerce"``; None when it isn't a scraped item."""
    builder = _builder(item)
    if builder is None:
        return None
    return "woocommerce" if builder is _woocommerce else "shopify"


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
    images = _dicts(data.get("images"))
    image_srcs = {image.get("id"): image.get("src") for image in images}
    variants = _dicts(data.get("variants"))
    tags = data.get("tags")
    product = _shopify_common(item, "product", data, images) | {
        "vendor": data.get("vendor"),
        "product_type": data.get("product_type"),
        # products.json has a list of tags, a product's own .json a comma-joined string
        "tags": _join(tags) if isinstance(tags, list) else tags,
        "options": _join(o.get("name") for o in _dicts(data.get("options"))),
        "created_at": data.get("created_at"),
        "description": _text(data.get("body_html")),
        "image_count": len(images),
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
    prices: dict[str, Any] = item["prices"]
    money = woo_money(prices)
    summary = summarize(item)
    images = _dicts(item.get("images"))
    variations = _dicts(item.get("variations"))
    # Attributes that define variations play Shopify's options; others only describe
    options = [a for a in _dicts(item.get("attributes")) if a.get("has_variations", True)]
    option_names = [_unescape(a.get("name")) for a in options if a.get("name")]
    # A variation's value is a term's slug for global attributes; show the term's name
    term_names = {
        (_unescape(a.get("name")), t.get("slug")): t.get("name")
        for a in options
        for t in _dicts(a.get("terms"))
        if t.get("slug")
    }
    price = money(prices.get("price"))
    regular_price = money(prices.get("regular_price"))
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
        "sku": item.get("sku"),
        "price": price,
        # Like Shopify's, set only when the item sells below it
        "compare_at_price": regular_price if _below(price, regular_price) else None,
        "available": item.get("is_in_stock"),
    } | _image_paths(item)
    rows = []
    for variation in variations:
        values: dict[Any, Any] = {}
        for a in _dicts(variation.get("attributes")):
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
        )
    return rows or [product]


def _below(price: Decimal | None, regular_price: Decimal | None) -> bool:
    if price is None or regular_price is None or price.is_nan() or regular_price.is_nan():
        return False
    return price < regular_price


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _join(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in values if v not in (None, ""))


def _names(value: Any) -> str:
    return _join(html.unescape(str(d.get("name"))) for d in _dicts(value) if d.get("name"))


def _unescape(value: Any) -> Any:
    return html.unescape(value) if isinstance(value, str) else value


def _image_paths(item: dict[str, Any]) -> dict[str, str]:
    """The ``image_paths`` column, only for items that went through image downloads."""
    if "scraped_images" not in item:
        return {}
    return {"image_paths": _join(i.get("path") for i in _dicts(item["scraped_images"]))}


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
