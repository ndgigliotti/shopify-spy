"""Product page data: schema.org ``Product`` JSON-LD, and WooCommerce's variation form.

Pages come from arbitrary servers: unparseable scripts are skipped and nesting is
bounded. Scripts are found with regular expressions rather than an HTML parser,
which blocks the event loop for tens of milliseconds on a large page.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
from collections.abc import Iterator
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


_META = re.compile(r"<meta\b([^>]*)>", re.I)
# Separators SEO plugins put between a title and the site name
TITLE_SEPARATORS = (" - ", " – ", " — ", " | ", " • ", " · ", " :: ")


def site_name(page: str) -> str | None:
    """The site's name from ``og:site_name`` in the page's head (Yoast, Rank Math)."""
    end = page.find("</head")
    for attrs in _META.findall(page[: end if end >= 0 else len(page)]):
        if _attr(attrs, "property") == "og:site_name":
            return _attr(attrs, "content") or None
    return None


def strip_site_name(title: str, site: str | None) -> str | None:
    """The title without a trailing site name (``Hat - Shop`` -> ``Hat``) that an
    SEO plugin added, or None if it has none."""
    if not site:
        return None
    plain = html.unescape(title).strip()
    for separator in TITLE_SEPARATORS:
        suffix = separator + site.strip()
        if plain.endswith(suffix) and len(plain) > len(suffix):
            return plain[: -len(suffix)].rstrip()
    return None


# --- WooCommerce's variation form -------------------------------------------------
# A variable product's page embeds its variations as JSON on the form, unless it has
# more than woocommerce_ajax_variation_threshold (30 by default): then the attribute
# is "false" and the page asks for them as options are picked.
VARIATIONS_ATTR = "data-product_variations"
_ATTR_VALUE = re.compile(r"""\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TAG = re.compile(r"<[^>]*>")
_ATTRS: dict[str, re.Pattern[str]] = {}
# esc_attr()'s entities; html.unescape is ~10x slower on a large attribute
_ENTITIES = (("&quot;", '"'), ("&#034;", '"'), ("&#039;", "'"), ("&#39;", "'"),
             ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))  # fmt: skip


def _element(tag: str) -> re.Pattern[str]:
    """An element and its body. The body can't run past the next tag of its kind,
    so an unclosed one costs a short scan, not one to the end of the page each."""
    return re.compile(rf"<{tag}\b([^>]*)>([^<]*(?:<(?!/?{tag}\b)[^<]*)*)</{tag}\s*>", re.I)


_LABEL = _element("label")
_SELECT = _element("select")
_OPTION = _element("option")


def _attr(attrs: str, name: str) -> str | None:
    """An attribute's value: exactly ``name`` (``id``, not ``data-id``)."""
    pattern = _ATTRS.get(name)
    if pattern is None:
        pattern = _ATTRS[name] = re.compile(
            rf"""(?<![\w-]){re.escape(name)}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I
        )
    match = pattern.search(attrs)
    if match is None:
        return None
    return html.unescape(next(g for g in match.groups() if g is not None))


def _plain(markup: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", markup)).split())


def field_name(field: str) -> str:
    """A name for a form field without a label: ``attribute_pa_color`` -> ``color``."""
    return urllib.parse.unquote(field.removeprefix("attribute_").removeprefix("pa_"))


@dataclass
class FormField:
    name: str  # display name, from the field's label
    terms: dict[str, str]  # option value (a term's slug, or a local value) -> its name


@dataclass
class VariationForm:
    records: list[Any]  # the embedded variations, as WooCommerce wrote them
    fields: dict[str, FormField]  # by field name, e.g. "attribute_pa_color"

    def name(self, field: str) -> str:
        known = self.fields.get(field)
        return known.name if known else field_name(field)


def _forms(page: str) -> Iterator[tuple[str, str, str]]:
    """Each variation form's (opening tag's other attributes, JSON value, body)."""
    start = 0
    while (i := page.find(VARIATIONS_ATTR, start)) >= 0:
        start = i + len(VARIATIONS_ATTR)
        value = _ATTR_VALUE.match(page, start)
        opening = page.rfind("<", 0, i)
        if value is None or not page.startswith("<form", opening):
            continue
        tag_end = page.find(">", value.end())
        if tag_end < 0:
            return
        close = page.find("</form", tag_end)
        body = page[tag_end + 1 : close if close >= 0 else len(page)]
        attrs = page[opening:i] + page[value.end() : tag_end]
        yield attrs, value.group(1) if value.group(1) is not None else value.group(2), body
        start = tag_end


def _decode(raw: str) -> Any:
    """The attribute's JSON: esc_attr()'s entities first, then any entity."""
    fast = raw
    for entity, char in _ENTITIES:
        fast = fast.replace(entity, char)
    for unescape in (lambda: fast, lambda: html.unescape(raw)):
        try:
            return json.loads(unescape(), strict=False)
        except (ValueError, RecursionError):
            continue
    return None


def variation_form(page: str, product_id: int | None) -> VariationForm | None:
    """The product's variation form: the one whose ``data-product_id`` is the
    product (a related product's can be on the page too), else the only one.

    None when the page has none, or its variations aren't embedded."""
    forms = list(_forms(page))
    if product_id is not None:
        mine = [f for f in forms if _attr(f[0], "data-product_id") == str(product_id)]
        forms = mine or [f for f in forms if _attr(f[0], "data-product_id") is None]
    if len(forms) != 1:
        return None
    _, raw, body = forms[0]
    records = _decode(raw)
    if not isinstance(records, list):  # false: too many to embed
        return None
    labels = {
        target: _plain(text)
        for attrs, text in _LABEL.findall(body)
        if (target := _attr(attrs, "for"))
    }
    fields: dict[str, FormField] = {}
    for attrs, options in _SELECT.findall(body):
        field = _attr(attrs, "data-attribute_name") or _attr(attrs, "name") or ""
        if not field.startswith("attribute_"):
            continue
        terms = {
            value: _plain(text)
            for option_attrs, text in _OPTION.findall(options)
            if (value := _attr(option_attrs, "value"))
        }
        fields[field] = FormField(labels.get(_attr(attrs, "id") or "") or "", terms)
    # Unlabeled fields are named from the field; where two would share a name
    # (pa_size and a local size), keep the global one's pa_ prefix
    for field, found in fields.items():
        if not found.name:
            found.name = field_name(field)
    names = [f.name for f in fields.values()]
    for field, found in fields.items():
        if names.count(found.name) > 1:
            found.name = urllib.parse.unquote(field.removeprefix("attribute_"))
    return VariationForm(records, fields)
