"""CSV rows for scraped items: one row per variant, every cell a scalar.

A CSV has one header, so each platform gets a fixed column set. Shopify products
get a row per variant with the product's columns repeated (a product without
variants still gets one row); collections get one row each, sharing the product
columns they have. WooCommerce products get a row per variation; the Store API
lists only a variation's ID and attributes, so the price columns are the
product's. JSONL stays the full-fidelity format.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from typing import Any

from shopify_spy._summary import html_url, summarize, woo_money

SHOPIFY_COLUMNS = [
    "kind",
    "store",
    "id",
    "handle",
    "title",
    "vendor",
    "product_type",
    "tags",
    "options",
    "created_at",
    "updated_at",
    "published_at",
    "description",
    "html_url",
    "image_url",
    "image_count",
    "products_count",
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
WOOCOMMERCE_COLUMNS = [
    "store",
    "id",
    "sku",
    "name",
    "slug",
    "type",
    "categories",
    "tags",
    "brands",
    "attributes",
    "short_description",
    "description",
    "permalink",
    "image_url",
    "image_count",
    "currency",
    "price",
    "regular_price",
    "sale_price",
    "min_price",
    "max_price",
    "on_sale",
    "is_in_stock",
    "is_purchasable",
    "average_rating",
    "review_count",
    "variation_count",
    "variation_id",
    "variation_attributes",
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


def platform_columns(item: dict[str, Any]) -> list[str] | None:
    """Every column of the item's platform; None when it isn't a scraped item."""
    builder = _builder(item)
    if builder is None:
        return None
    # Shopify columns cover both kinds, either of which may come first in a file
    return WOOCOMMERCE_COLUMNS if builder is _woocommerce else SHOPIFY_COLUMNS


def default_columns(item: dict[str, Any]) -> list[str] | None:
    """Columns for a CSV whose first item is ``item``; None when it isn't a scraped item."""
    columns = platform_columns(item)
    if columns is None:
        return None
    return [c for c in columns if c != "image_paths" or "scraped_images" in item]


def flatten(item: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The CSV rows for a scraped item, or None when it isn't one."""
    builder = _builder(item)
    return builder(item) if builder else None


def _shopify_common(
    item: dict[str, Any], kind: str, data: dict[str, Any], images: list[dict[str, Any]]
) -> dict[str, Any]:
    image = data.get("image") if isinstance(data.get("image"), dict) else None
    return {
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
    attributes = _dicts(item.get("attributes"))
    # A variation's value is a term's slug for global attributes; show the term's name
    term_names = {
        t.get("slug"): t.get("name")
        for a in attributes
        for t in _dicts(a.get("terms"))
        if t.get("slug")
    }
    product = {
        "store": item.get("store"),
        "id": item.get("id"),
        "sku": item.get("sku"),
        "name": summary.title,
        "slug": item.get("slug"),
        "type": item.get("type"),
        "categories": _names(item.get("categories")),
        "tags": _names(item.get("tags")),
        "brands": _names(item.get("brands")),
        "attributes": _pairs((a.get("name"), _names(a.get("terms"))) for a in attributes),
        "short_description": _text(item.get("short_description")),
        "description": _text(item.get("description")),
        "permalink": item.get("permalink"),
        "image_url": images[0].get("src") if images else None,
        "image_count": len(images),
        "currency": summary.currency,
        "price": money(prices.get("price")),
        "regular_price": money(prices.get("regular_price")),
        "sale_price": money(prices.get("sale_price")),
        "min_price": summary.min_price,
        "max_price": summary.max_price,
        "on_sale": item.get("on_sale"),
        "is_in_stock": item.get("is_in_stock"),
        "is_purchasable": item.get("is_purchasable"),
        "average_rating": item.get("average_rating"),
        "review_count": item.get("review_count"),
        "variation_count": len(variations),
    } | _image_paths(item)
    rows = [
        product
        | {
            "variation_id": v.get("id"),
            "variation_attributes": _pairs(
                (a.get("name"), _unescape(term_names.get(a.get("value"), a.get("value"))))
                for a in _dicts(v.get("attributes"))
            ),
        }
        for v in variations
    ]
    return rows or [product]


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _join(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in values if v not in (None, ""))


def _names(value: Any) -> str:
    return _join(html.unescape(str(d.get("name"))) for d in _dicts(value) if d.get("name"))


def _pairs(pairs: Iterable[tuple[Any, Any]]) -> str:
    """``Size: S, L; Color: Red`` from (HTML name, plain value) pairs, skipping empty ones."""
    return "; ".join(
        f"{html.unescape(str(name))}: {value}"
        for name, value in pairs
        if name not in (None, "") and value not in (None, "")
    )


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
