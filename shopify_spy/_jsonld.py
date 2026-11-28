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

from shopify_spy import _microdata as microdata

logger = logging.getLogger("shopify_spy")

MAX_NODES = 10_000  # JSON-LD objects examined per page
MAX_DEPTH = 64  # nesting below which a product's references are left unresolved
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_LD_TYPE = re.compile(r"""\btype\s*=\s*["']?\s*application/ld\+json""", re.I)
_BODY_CLASS = re.compile(r"""<body\b[^>]*?\bclass\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_POST_ID = re.compile(r"(?:^|\s)postid-(\d{1,18})(?:\s|$)")
MAX_PRICE_DIGITS = 15  # digits before the decimal point
LIST_PRICES = frozenset({"ListPrice", "StrikethroughPrice"})
OUT_OF_STOCK = frozenset({"OutOfStock", "SoldOut", "Discontinued"})
# Strings are matched first so a ",}" or "//" inside one is kept. A string or
# comment always matches, running to the end if unterminated, and the string never
# backtracks, so each pass is linear on hostile input.
_STRING = r'"(?:[^"\\]++|\\.?)*+(?:"|\Z)'
_COMMENTS = re.compile(rf"({_STRING})|//[^\n]*|/\*.*?(?:\*/|\Z)", re.S)
_TRAILING_COMMAS = re.compile(rf"({_STRING})|,(?=\s*[}}\]])", re.S)
_EMPTY_VALUES = re.compile(rf"({_STRING})|:(?=\s*[,}}\]])", re.S)
# Some pages hide the script's JSON in an HTML comment or a CDATA section
_WRAPPER = re.compile(r"\A\s*(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)\s*\Z")


_SCHEMA = re.compile(r"^(?:https?://schema\.org/|schema:)", re.I)


def term(value: Any) -> str | None:
    """A schema.org term without its namespace: ``https://schema.org/InStock``,
    ``schema:InStock`` and ``InStock`` are all ``InStock``."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _SCHEMA.sub("", value.strip()).rstrip("/").rsplit("/", 1)[-1] or None


def _types(node: dict[str, Any]) -> list[Any]:
    kind = node.get("@type")
    return [term(k) for k in (kind if isinstance(kind, list) else [kind])]


def _is_group(node: dict[str, Any]) -> bool:
    return "ProductGroup" in _types(node)


def _is_product(node: dict[str, Any], *, groups: bool = False) -> bool:
    kinds = _types(node)
    return "Product" in kinds or (groups and "ProductGroup" in kinds)


def _products(data: Any, *, groups: bool = False) -> list[dict[str, Any]]:
    """Product nodes at the top level, in lists, in an ``@graph``, or a page's
    ``mainEntity``.

    A matched node isn't descended into, so a group's ``hasVariant`` Products
    stay inside it."""
    found: list[dict[str, Any]] = []
    stack = [data]
    seen = 0
    while stack and seen < MAX_NODES:
        node = stack.pop()
        seen += 1
        if isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            if _is_product(node, groups=groups):
                found.append(node)
            else:
                for key in ("mainEntity", "@graph"):
                    if isinstance(node.get(key), (dict, list)):
                        stack.append(node[key])
    return found


def _lenient(script: str) -> str:
    """The script without an HTML comment around it, the comments and trailing
    commas of hand-written JSON-LD, and with null for a template's empty values
    (``"description": ,``)."""
    script = _COMMENTS.sub(lambda m: m.group(1) or " ", _WRAPPER.sub("", script))
    script = _TRAILING_COMMAS.sub(lambda m: m.group(1) or "", script)
    return _EMPTY_VALUES.sub(lambda m: m.group(1) or ":null", script)


def _load(script: str) -> Any:
    # strict=False: hand-written JSON-LD often has raw newlines in strings
    try:
        return json.loads(script, strict=False)
    except ValueError:
        return json.loads(_lenient(script), strict=False)


def _strip_fragment(url: Any) -> str:
    return url.split("#", 1)[0].rstrip("/") if isinstance(url, str) else ""


_HTTP_SCHEME = re.compile(r"^https?://", re.I)


def _identity(url: Any) -> str:
    """A URL without its fragment, trailing slash or ``http``/``https`` scheme: SEO
    plugins can write a product's ``@id`` as ``http://`` on an ``https://`` page."""
    return _HTTP_SCHEME.sub("//", _strip_fragment(url))


def _unmatched(products: list[dict[str, Any]], *, fallback: bool) -> dict[str, Any] | None:
    """The product for a page none of whose nodes names it.

    Without ``fallback``, several unmatched nodes mean a listing (a category or
    home page), unless exactly one is a group: a group beside its variants."""
    if fallback or len(products) == 1:
        return products[0] if products else None
    found = [p for p in products if _is_group(p)]
    return found[0] if len(found) == 1 else None


def parse_product_page(
    html: str, url: str, *, groups: bool = False, fallback: bool = True
) -> tuple[dict[str, Any] | None, int | None]:
    """Return (the page's schema.org Product, the WordPress post ID).

    The Product comes from the page's JSON-LD, or from its microdata when the
    JSON-LD has none for the page.

    The Product is a copy whose ``{"@id": ...}`` references are replaced by the
    nodes they name, from any of the page's scripts, and whose schema.org terms
    lose their namespace (``schema:name`` is ``name``). A group's variants written
    as separate Products that name it in ``isVariantOf`` join its ``hasVariant``,
    unless an entry there has the same ``@id`` or ``sku``.

    With several products on the page (related products, say), the one whose
    ``url`` or ``@id`` is the page wins (or, failing that, the only one that is the
    page but for the query string), else the first. ``groups`` also matches ``ProductGroup`` nodes.
    With ``fallback=False``, "else the first" applies only to a page with one
    candidate, or with exactly one ProductGroup among them.
    """
    products: list[dict[str, Any]] = []
    scripts: list[Any] = []
    for attrs, script in _SCRIPT.findall(html):
        if not _LD_TYPE.search(attrs):
            continue
        try:
            scripts.append(_load(script))
        except (ValueError, RecursionError):
            logger.debug("Unparseable JSON-LD on %s", url)
            continue
        products += _products(scripts[-1], groups=groups)
    body = _BODY_CLASS.search(html)
    match = body and _POST_ID.search(body.group(2))
    post_id = int(match.group(1)) if match else None
    found = _pick(scripts, products, url, fallback=fallback)
    if found is None:
        # A page without a JSON-LD Product may have one in microdata
        nodes = microdata.items(html, url)
        found = _pick(nodes, _products(nodes, groups=groups), url, fallback=fallback)
    return found, post_id


def _pick(
    data: list[Any], products: list[dict[str, Any]], url: str, *, fallback: bool
) -> dict[str, Any] | None:
    """The page's product among ``products``, found in ``data``, resolved."""
    graph = _Graph(data, url)
    products, variants = graph.split_variants(products)
    found = graph.page_product(products) or _unmatched(products, fallback=fallback)
    if found is None:
        return None
    product = graph.resolve(found)
    if variants.get(id(found)):
        listed = product.get("hasVariant")
        merged = listify(listed) if listed is not None else []
        # Only hasVariant's own entries count as duplicates: variants without
        # their own SKU inherit the group's, so two of them can share one
        known = {key for v in merged for key in graph.variant_keys(v)}
        merged += [
            graph.resolve(v) for v in variants[id(found)] if not graph.variant_keys(v) & known
        ]
        product["hasVariant"] = merged
    return product


# Properties that point back at what holds the product (its page, its group), which
# would inline the page's whole graph
_BACK_REFERENCES = frozenset({"mainEntityOfPage", "isPartOf", "isVariantOf", "subjectOf"})


def _is_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("@id"), str)
        and value.keys() <= {"@id", "@type"}
    )


class _Graph:
    """The page's JSON-LD nodes by ``@id``, from every script, to resolve the
    ``{"@id": ...}`` references a product makes to them (Yoast lists a product's
    images that way)."""

    def __init__(self, scripts: list[Any], url: str) -> None:
        self.url = url
        self.nodes: dict[str, dict[str, Any]] = {}
        stack = list(scripts)
        seen = 0
        while stack and seen < MAX_NODES:
            value = stack.pop()
            seen += 1
            if isinstance(value, list):
                stack.extend(v for v in reversed(value) if isinstance(v, (dict, list)))
            elif isinstance(value, dict):
                if isinstance(value.get("@id"), str) and not _is_reference(value):
                    self.nodes.setdefault(self.key(value["@id"]), value)
                stack.extend(v for v in reversed(value.values()) if isinstance(v, (dict, list)))
        self._budget = MAX_NODES

    def key(self, ref: str) -> str:
        """An ``@id`` or ``url``, resolved against the page."""
        try:
            return urllib.parse.urljoin(self.url, ref.strip())
        except ValueError:
            return ref

    def split_variants(
        self, products: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
        """The candidates without the Products whose ``isVariantOf`` names a group
        among them, and those variants by their group's ``id()``."""
        groups = {
            self.key(p["@id"]): p
            for p in products
            if _is_group(p) and isinstance(p.get("@id"), str)
        }
        kept: list[dict[str, Any]] = []
        variants: dict[int, list[dict[str, Any]]] = {}
        for product in products:
            parent = product.get("isVariantOf")
            ref = parent.get("@id") if isinstance(parent, dict) else None
            group = groups.get(self.key(ref)) if isinstance(ref, str) else None
            if group is None or group is product:
                kept.append(product)
            else:
                variants.setdefault(id(group), []).append(product)
        return kept, variants

    def variant_keys(self, variant: Any) -> set[str]:
        """What identifies a variant across ``hasVariant`` and ``isVariantOf``: its
        ``@id`` (either scheme) and ``sku``. Not its ``url``, which variants can share
        with the page."""
        if not isinstance(variant, dict):
            return set()
        keys = set()
        if isinstance(ref := variant.get("@id"), str) and ref.strip():
            keys.add(f"@id:{_HTTP_SCHEME.sub('//', self.key(ref))}")
        if (sku := text(variant.get("sku"))) and sku.strip():
            keys.add(f"sku:{sku.strip()}")
        return keys

    def page_product(self, products: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The product whose ``url`` or ``@id`` is the page, else the only one that
        is the page but for the query string (a ``?variant=`` URL, say). Several
        such are a group's variants, written beside it. ``http`` and ``https``
        name the same page."""
        page = _identity(self.url)
        if not page:
            return None
        bare = page.split("?", 1)[0].rstrip("/")
        loose = []
        for product in products:
            names = {
                _identity(self.key(v))
                for v in (product.get("url"), product.get("@id"))
                if isinstance(v, str) and v
            }
            if page in names:
                return product
            if bare in {n.split("?", 1)[0].rstrip("/") for n in names}:
                loose.append(product)
        return loose[0] if len(loose) == 1 else None

    def resolve(self, product: dict[str, Any]) -> dict[str, Any]:
        """A copy of the product with its references replaced by the nodes they
        name and its terms without their namespace. A node isn't inlined inside
        itself, and the copying is bounded."""
        return self._copy(product, (), 0)

    def _copy(self, value: Any, path: tuple[str, ...], depth: int) -> Any:
        if not isinstance(value, (dict, list)):
            return value
        self._budget -= 1
        if self._budget < 0 or depth > MAX_DEPTH:
            return value
        if _is_reference(value):
            key = self.key(value["@id"])
            target = self.nodes.get(key)
            if target is None or key in path:
                return value
            return self._copy(target, path, depth + 1)
        if isinstance(value, list):
            return [self._copy(v, path, depth + 1) for v in value]
        if isinstance(value.get("@id"), str):
            path = (*path, self.key(value["@id"]))
        copy: dict[str, Any] = {}
        for k, v in value.items():
            name = _SCHEMA.sub("", k)
            if k == "@type":
                v = [term(t) or t for t in v] if isinstance(v, list) else term(v) or v
            elif name not in _BACK_REFERENCES:
                v = self._copy(v, path, depth + 1)
            if name not in copy or name == k:
                copy[name] = v
        return copy


def text(value: Any) -> str | None:
    """A string field; numbers (an all-digit SKU, say) as strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) else None


def first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first of ``keys`` that has a string (or number) value."""
    return next((v for key in keys if (v := text(node.get(key)))), None)


def unescaped(value: Any) -> str | None:
    """A text field unescaped until it stops changing: JSON-LD should be plain text,
    but pages escape it, sometimes twice (``&amp;amp;``, ``&amp;nbsp;``)."""
    found = text(value)
    for _ in range(3):
        if found is None or (plain := html.unescape(found)) == found:
            break
        found = plain
    return found


def name(value: Any) -> str | None:
    """A name field, unescaped, on one line (some pages break names with newlines)."""
    found = unescaped(value)
    return " ".join(found.split()) if found is not None else None


def listify(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in listify(value) if isinstance(v, dict)]


def variants(node: dict[str, Any]) -> list[dict[str, Any]]:
    """A ProductGroup's ``hasVariant`` Products: a list, or a lone one.

    Entries with neither ``offers`` nor ``sku`` are left out: stores list the
    product's other colourways that way, as a bare ``url``, and they aren't variants
    of this product."""
    return [v for v in _dicts(node.get("hasVariant")) if v.get("offers") or text(v.get("sku"))]


def image_urls(value: Any, base: Any = None) -> list[str]:
    """URLs from ``image``: a URL, an ImageObject, or a list of either; resolved
    against ``base`` (the page's URL), if it's a string."""
    urls = []
    for image in listify(value):
        if isinstance(image, dict):
            image = image.get("url") or image.get("contentUrl")
        if not isinstance(image, str) or not image:
            continue
        if isinstance(base, str):
            try:
                image = urllib.parse.urljoin(base, image)
            except ValueError:  # a malformed host, such as an unclosed "[" in an IPv6 one
                continue
        urls.append(image)
    return urls


def product_image_urls(node: dict[str, Any], page: Any) -> list[str]:
    """A product's images, then its variants' (see ``variants``) not already listed."""
    urls = image_urls(node.get("image"), page)
    for variant in variants(node):
        urls += [u for u in image_urls(variant.get("image"), page) if u not in urls]
    return urls


# A price as a page may format it: "$1,299.00", "1.234,56 €", "19,99", "1'234.50"
_FORMATTED_PRICE = re.compile(r"[^\d.,-]*?(\d[\d.,'\s]*)[^\d.,]*")
MAX_PRICE_TEXT = 64  # characters of a formatted price read


def _formatted_amount(raw: str) -> Decimal | None:
    """A formatted price's amount: of a dot and a comma, the last is the decimal
    separator; a lone comma before exactly three digits groups thousands
    (``1,299``), else it's decimal (``19,99``). None unless the grouping is
    regular."""
    match = _FORMATTED_PRICE.fullmatch(raw) if len(raw) <= MAX_PRICE_TEXT else None
    if match is None:
        return None
    number = re.sub(r"['\s]", "", match.group(1)).rstrip(".,")
    separators = [c for c in ".," if c in number]
    if len(separators) == 2:
        point = "." if number.rfind(".") > number.rfind(",") else ","
    elif separators:
        point = separators[0]
        tail = number.partition(point)[2]
        if point in tail or (point == "," and len(tail) == 3):
            point = ""  # the separator groups thousands
    else:
        point = ""
    whole, _, fraction = number.rpartition(point) if point else (number, "", "")
    groups = re.split(r"[.,]", whole)
    if len(groups) > 1 and not (
        1 <= len(groups[0]) <= 3
        and all(len(g) in (2, 3) for g in groups[1:])  # 2 for India's 1,23,456
        and len(groups[-1]) == 3
    ):
        return None
    if point and point in whole:
        return None
    return Decimal(f"{''.join(groups)}.{fraction}" if fraction else "".join(groups))


def amount(value: Any) -> Decimal | None:
    """A non-negative, finite, bounded price, or None.

    schema.org prices are plain numbers (``1299.00``), but a string with a currency
    symbol or separators is read too, as ``_formatted_amount`` describes."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    raw = str(value).strip()
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        found = _formatted_amount(raw)
        if found is None:
            return None
        parsed = found
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


def in_stock(availability: str | None) -> bool | None:
    """Whether an ``Offers.availability`` is in stock; None when it isn't given."""
    return None if availability is None else availability not in OUT_OF_STOCK


def offers(value: Any, *, nested: bool = True) -> Offers:
    """Read Offer and AggregateOffer prices, directly or in ``priceSpecification``.

    An AggregateOffer's own ``offers`` give what it lacks: its prices, and its
    availability (an in-stock offer's, if any)."""
    result = Offers()
    for offer in _dicts(value):
        result.currency = result.currency or text(offer.get("priceCurrency"))
        availability = term(offer.get("availability"))
        amounts = len(result.current) + len(result.bounds)
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
            if term(spec.get("priceType")) in LIST_PRICES:
                result.listed.append(spec_price)
            elif own is None:
                result.current.append(spec_price)
        if nested and offer.get("offers"):
            each = [offers(o, nested=False) for o in _dicts(offer["offers"])]
            if amounts == len(result.current) + len(result.bounds):
                for inner in each:
                    result.current += inner.current
                    result.listed += inner.listed
                    result.bounds += inner.bounds
            result.currency = result.currency or next(
                (i.currency for i in each if i.currency), None
            )
            states = [i.availability for i in each if i.availability]
            in_stock = [s for s in states if s not in OUT_OF_STOCK]
            availability = availability or next(iter(in_stock or states), None)
        result.availability = result.availability or availability
    return result


GTINS = ("gtin", "gtin13", "gtin12", "gtin14", "gtin8")
MAX_RATING_SCALE = 5  # the scale average_rating is given on, as WooCommerce's is


def codes(*nodes: Any) -> dict[str, str | None]:
    """The first ``gtin`` (any of ``GTINS``) and ``mpn`` among ``nodes`` (a variant,
    then its product, say), each a Product or Offer."""
    found: dict[str, str | None] = {"gtin": None, "mpn": None}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        found["gtin"] = found["gtin"] or next(
            (v.strip() for key in GTINS if (v := text(node.get(key))) and v.strip()), None
        )
        found["mpn"] = found["mpn"] or ((text(node.get("mpn")) or "").strip() or None)
    return found


def rating(value: Any) -> tuple[Decimal | None, int | None]:
    """An ``aggregateRating``'s average, out of 5, and its review count.

    ``bestRating`` defaults to 5, as schema.org says; a rating on another scale
    (Magento's percentages) is converted. A value outside the scale, or 0 with no
    reviews, isn't a rating."""
    found = next(iter(_dicts(value)), None)
    if found is None:
        return None, None
    count = amount(found.get("reviewCount"))
    if count is None:
        count = amount(found.get("ratingCount"))
    reviews = int(count) if count is not None and count == count.to_integral_value() else None
    average = amount(found.get("ratingValue"))
    best = amount(found.get("bestRating")) or Decimal(MAX_RATING_SCALE)
    if average is None or average > best or (average == 0 and not reviews):
        return None, reviews
    if best != MAX_RATING_SCALE:
        average = (average * MAX_RATING_SCALE / best).quantize(Decimal("0.01"))
    return average, reviews


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
