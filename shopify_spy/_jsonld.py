"""schema.org ``Product`` data from a product page's JSON-LD.

Pages come from arbitrary servers: unparseable scripts are skipped and nesting is
bounded. Scripts are found with regular expressions rather than an HTML parser,
which blocks the event loop for tens of milliseconds on a large page.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("shopify_spy")

MAX_NODES = 10_000  # JSON-LD objects examined per page
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_LD_TYPE = re.compile(r"""\btype\s*=\s*["']?\s*application/ld\+json""", re.I)
_BODY_CLASS = re.compile(r"""<body\b[^>]*?\bclass\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_POST_ID = re.compile(r"(?:^|\s)postid-(\d{1,18})(?:\s|$)")
MAX_PRICE_DIGITS = 15  # digits before the decimal point
LIST_PRICES = frozenset({"ListPrice", "StrikethroughPrice"})


def _is_product(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    return kind == "Product" or (isinstance(kind, list) and "Product" in kind)


def _products(data: Any) -> list[dict[str, Any]]:
    """Product nodes at the top level, in lists, or in an ``@graph``."""
    found: list[dict[str, Any]] = []
    stack = [data]
    seen = 0
    while stack and seen < MAX_NODES:
        node = stack.pop()
        seen += 1
        if isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            if _is_product(node):
                found.append(node)
            elif isinstance(node.get("@graph"), list):
                stack.extend(reversed(node["@graph"]))
    return found


def _strip_fragment(url: Any) -> str:
    return url.split("#", 1)[0].rstrip("/") if isinstance(url, str) else ""


def parse_product_page(html: str, url: str) -> tuple[dict[str, Any] | None, int | None]:
    """Return (the page's schema.org Product, the WordPress post ID).

    With several products on the page (related products, say), the one whose
    ``url`` or ``@id`` is the page wins, else the first.
    """
    products: list[dict[str, Any]] = []
    for attrs, script in _SCRIPT.findall(html):
        if not _LD_TYPE.search(attrs):
            continue
        try:
            # strict=False: hand-written JSON-LD often has raw newlines in strings
            products += _products(json.loads(script, strict=False))
        except (ValueError, RecursionError):
            logger.debug("Unparseable JSON-LD on %s", url)
    body = _BODY_CLASS.search(html)
    match = body and _POST_ID.search(body.group(2))
    post_id = int(match.group(1)) if match else None
    page = _strip_fragment(url)
    for product in products:
        if page and page in (
            _strip_fragment(product.get("url")),
            _strip_fragment(product.get("@id")),
        ):
            return product, post_id
    return (products[0] if products else None), post_id


def text(value: Any) -> str | None:
    """A string field; numbers (an all-digit SKU, say) as strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) else None


def _listify(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in _listify(value) if isinstance(v, dict)]


def image_urls(value: Any) -> list[str]:
    """URLs from ``image``: a URL, an ImageObject, or a list of either."""
    urls = []
    for image in _listify(value):
        if isinstance(image, dict):
            image = image.get("url") or image.get("contentUrl")
        if isinstance(image, str) and image:
            urls.append(image)
    return urls


def amount(value: Any) -> Decimal | None:
    """A non-negative, finite, bounded price, or None."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    # Bounded, so minor-unit arithmetic can't overflow on a hostile "1e5000"
    if not parsed.is_finite() or parsed < 0 or parsed.adjusted() > MAX_PRICE_DIGITS:
        return None
    return parsed


@dataclass
class Offers:
    """Amounts from a Product's ``offers``, as written."""

    current: list[Decimal] = field(default_factory=list)  # what the product sells for
    listed: list[Decimal] = field(default_factory=list)  # before a sale (ListPrice)
    bounds: list[Decimal] = field(default_factory=list)  # AggregateOffer low/high
    currency: str | None = None
    availability: str | None = None  # the first offer's, e.g. "InStock"


def offers(value: Any) -> Offers:
    """Read Offer and AggregateOffer prices, directly or in ``priceSpecification``."""
    result = Offers()
    for offer in _dicts(value):
        result.currency = result.currency or text(offer.get("priceCurrency"))
        availability = offer.get("availability")
        if result.availability is None and isinstance(availability, str) and availability:
            result.availability = availability.rstrip("/").rsplit("/", 1)[-1]
        for key in ("lowPrice", "highPrice"):
            if (bound := amount(offer.get(key))) is not None:
                result.bounds.append(bound)
        own = amount(offer.get("price"))
        if own is not None:
            result.current.append(own)
        raw = offer.get("priceSpecification")
        specs = _dicts(raw)
        if isinstance(raw, dict):  # Yoast merges specifications into {"0": {...}, ...}
            specs += _dicts(list(raw.values()))
        for spec in specs:
            spec_price = amount(spec.get("price"))
            if spec_price is None:
                continue
            result.currency = result.currency or text(spec.get("priceCurrency"))
            if str(spec.get("priceType") or "").rsplit("/", 1)[-1] in LIST_PRICES:
                result.listed.append(spec_price)
            elif own is None:
                result.current.append(spec_price)
    return result
