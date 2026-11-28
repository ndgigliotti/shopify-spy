"""``summarize()``: the same handful of fields from any item, without changing it.

Provisional API. Each platform's items have their own shape (Shopify wraps the
payload in "product"/"collection"; WooCommerce is flat, with prices in minor units;
Magento is flat, with prices in ``price_range``; BigCommerce is a GraphQL node, with
prices in ``prices``; JSON-LD is the page's schema.org node, with prices in
``offers``; Squarespace is a ``?format=json`` record, with prices in
``structuredContent.variants``, or a JSON-LD node); this reads any of them.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from shopify_spy import _jsonld as jsonld
from shopify_spy._bigcommerce import image_list
from shopify_spy._magento import _dig, gallery_urls
from shopify_spy._squarespace import image_urls as squarespace_images
from shopify_spy._utils import dicts

logger = logging.getLogger("shopify_spy")


@dataclass(frozen=True)
class ItemSummary:
    kind: str  # "product" or "collection"
    platform: str  # "shopify", "woocommerce", "magento", "bigcommerce", "jsonld" or "squarespace"
    id: Any
    title: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    currency: str | None  # None for Shopify, whose products.json has no currency
    html_url: str | None
    store: str | None
    image_urls: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        """Fields as a plain dict (prices as strings), ready for ``write_items``."""
        data = dataclasses.asdict(self)
        for key in ("min_price", "max_price"):
            if data[key] is not None:
                data[key] = str(data[key])
        return data


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def html_url(item: dict[str, Any], kind: str, handle: Any) -> str | None:
    """A Shopify product or collection page, on the scheme the item was fetched over."""
    store = item.get("store")
    scheme = urllib.parse.urlsplit(item.get("url") or "").scheme or "https"
    return f"{scheme}://{store}/{kind}s/{handle}" if store and handle else None


def woo_money(prices: dict[str, Any]) -> Callable[[Any], Decimal | None]:
    """Converts a Store API amount, in minor units, to a Decimal."""
    minor = _decimal(prices.get("currency_minor_unit") or 0)
    if minor is None or not minor.is_finite() or minor != minor.to_integral_value() or minor < 0:
        logger.warning(
            "Unreadable currency_minor_unit %r; prices are left in minor units",
            prices.get("currency_minor_unit"),
        )
        minor = Decimal(0)
    unit = Decimal(10) ** int(minor)

    def money(value: Any) -> Decimal | None:
        amount = _decimal(value)
        if amount is None:
            return None
        try:
            # Keep the currency's decimals: 1990 cents is 19.90, not 19.9
            return (amount / unit).quantize(1 / unit)
        except InvalidOperation:  # infinite, or too many digits to round
            return amount / unit

    return money


def _images(item: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    # image_urls is only filled when images are downloaded; fall back to the payload
    urls = item.get("image_urls") or [
        image.get("src")
        for image in [*(payload.get("images") or []), payload.get("image")]
        if isinstance(image, dict)
    ]
    return [u for u in urls if isinstance(u, str)]


def is_magento(item: dict[str, Any]) -> bool:
    """Whether ``item`` is a scraped Magento product (``summarize().asdict()`` has a
    ``platform`` but no ``price_range``)."""
    return item.get("platform") == "magento" and isinstance(item.get("price_range"), dict)


def is_bigcommerce(item: dict[str, Any]) -> bool:
    """Whether ``item`` is a scraped BigCommerce product (``summarize().asdict()`` has
    a ``platform`` but no ``entityId``)."""
    return item.get("platform") == "bigcommerce" and "entityId" in item


def is_jsonld(item: dict[str, Any]) -> bool:
    """Whether ``item`` is a scraped JSON-LD product, from the ``jsonld`` platform or a
    Squarespace product page (``summarize().asdict()`` has a ``platform`` but no
    ``@type``)."""
    return item.get("platform") in ("jsonld", "squarespace") and "@type" in item


def is_squarespace(item: dict[str, Any]) -> bool:
    """Whether ``item`` is a scraped Squarespace ``?format=json`` product
    (``summarize().asdict()`` has a ``platform`` but no ``structuredContent``)."""
    return item.get("platform") == "squarespace" and isinstance(item.get("structuredContent"), dict)


def summarize(item: dict[str, Any]) -> ItemSummary:
    """Summarize a Shopify, WooCommerce, Magento, BigCommerce, JSON-LD or Squarespace
    item from ``scrape()``."""
    if isinstance(item.get("product"), dict) or isinstance(item.get("collection"), dict):
        return _shopify(item)
    if is_magento(item):
        return _magento(item)
    if is_bigcommerce(item):
        return _bigcommerce(item)
    if is_squarespace(item):
        return _squarespace(item)
    if is_jsonld(item):
        return _jsonld(item)
    return _woocommerce(item)


def _shopify(item: dict[str, Any]) -> ItemSummary:
    kind = "product" if isinstance(item.get("product"), dict) else "collection"
    data: dict[str, Any] = item[kind]
    low, high = shopify_price_range(data)
    return ItemSummary(
        kind=kind,
        platform="shopify",
        id=data.get("id"),
        title=data.get("title"),
        min_price=low,
        max_price=high,
        currency=None,
        html_url=html_url(item, kind, data.get("handle")),
        store=item.get("store"),
        image_urls=_images(item, data),
    )


def shopify_price_range(product: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """The lowest and highest variant price of a Shopify product, or (None, None)."""
    # NaN can't be compared, so min() and max() would raise on it
    prices = [
        p
        for v in dicts(product.get("variants"))
        if (p := _decimal(v.get("price"))) is not None and p.is_finite()
    ]
    return (min(prices), max(prices)) if prices else (None, None)


def _woocommerce(item: dict[str, Any]) -> ItemSummary:
    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    money = woo_money(prices)
    price_range = prices.get("price_range") if isinstance(prices.get("price_range"), dict) else {}
    price = money(prices.get("price"))
    low = money(price_range.get("min_amount"))
    high = money(price_range.get("max_amount"))
    return ItemSummary(
        kind="product",
        platform="woocommerce",
        id=item.get("id"),
        # The Store API returns names as HTML (17&#8243; for 17″); the item keeps it raw
        title=html.unescape(name) if isinstance(name := item.get("name"), str) else name,
        min_price=low if low is not None else price,
        max_price=high if high is not None else price,
        currency=prices.get("currency_code"),
        html_url=item.get("permalink"),
        store=item.get("store"),
        image_urls=_images(item, item),
    )


def magento_price(
    price_range: Any, bound: str = "minimum_price", kind: str = "final_price"
) -> tuple[Decimal | None, str | None]:
    """The amount and currency at ``price_range[bound][kind]``, or Nones if absent."""
    level = price_range.get(bound) if isinstance(price_range, dict) else None
    money = level.get(kind) if isinstance(level, dict) else None
    if not isinstance(money, dict):
        return None, None
    currency = money.get("currency")
    return _decimal(money.get("value")), currency if isinstance(currency, str) else None


def _magento(item: dict[str, Any]) -> ItemSummary:
    low, currency = magento_price(item.get("price_range"))
    high, high_currency = magento_price(item.get("price_range"), "maximum_price")
    urls = item.get("image_urls") or gallery_urls(item)
    return ItemSummary(
        kind="product",
        platform="magento",
        id=item.get("uid"),
        title=item.get("name"),
        min_price=low,
        max_price=high,
        currency=currency or high_currency,
        html_url=item.get("html_url"),
        store=item.get("store"),
        image_urls=[u for u in urls if isinstance(u, str)],
    )


def jsonld_prices(item: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str | None]:
    """The lowest and highest price and the currency across a JSON-LD product's
    ``offers`` and its variants' ``offers``, or Nones."""
    found = [jsonld.offers(item.get("offers"))]
    found += [jsonld.offers(v.get("offers")) for v in jsonld.variants(item)]
    # An offer set without a price of its own (an AggregateOffer) gives its bounds
    amounts = [a for o in found for a in (o.current or o.bounds)]
    currency = next((o.currency for o in found if o.currency), None)
    if not amounts:
        return None, None, currency
    return min(amounts), max(amounts), currency


def _jsonld(item: dict[str, Any]) -> ItemSummary:
    low, high, currency = jsonld_prices(item)
    return ItemSummary(
        kind="product",
        platform=item["platform"],
        id=jsonld.first_text(item, ("productID", "productGroupID", "sku", "@id")),
        title=jsonld.name(item.get("name")),
        min_price=low,
        max_price=high,
        currency=currency,
        html_url=item.get("html_url"),
        store=item.get("store"),
        # Not ``item["image_urls"]``, which is filled only with --images
        image_urls=jsonld.product_image_urls(item, item.get("html_url")),
    )


def bigcommerce_price(prices: Any, *keys: str) -> tuple[Decimal | None, str | None]:
    """The amount and currency of the BigCommerce ``Money`` at ``prices[keys...]``
    (say ``"price"``, or ``"priceRange", "min"``), or Nones if absent."""
    money = _dig(prices, *keys)
    if not isinstance(money, dict):
        return None, None
    currency = money.get("currencyCode")
    return _decimal(money.get("value")), currency if isinstance(currency, str) else None


def _bigcommerce(item: dict[str, Any]) -> ItemSummary:
    prices = item.get("prices")
    price, price_currency = bigcommerce_price(prices, "price")
    low, low_currency = bigcommerce_price(prices, "priceRange", "min")
    high, high_currency = bigcommerce_price(prices, "priceRange", "max")
    urls = item.get("image_urls") or image_list(item)
    return ItemSummary(
        kind="product",
        platform="bigcommerce",
        id=item.get("entityId"),
        title=item.get("name"),
        min_price=low if low is not None else price,
        max_price=high if high is not None else price,
        currency=low_currency or high_currency or price_currency,
        html_url=item.get("html_url"),
        store=item.get("store"),
        image_urls=[u for u in urls if isinstance(u, str)],
    )


def squarespace_variants(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``structuredContent.variants`` of a Squarespace record that are dicts."""
    content = item.get("structuredContent")
    return dicts(content.get("variants")) if isinstance(content, dict) else []


def squarespace_price(variant: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str | None]:
    """A Squarespace variant's selling price (its sale price when ``onSale``), its
    regular price and its currency, or Nones."""
    money = variant.get("priceMoney") if isinstance(variant.get("priceMoney"), dict) else {}
    regular = _decimal(money.get("value"))
    price = regular
    sale = variant.get("salePriceMoney")
    if variant.get("onSale") is True and isinstance(sale, dict):
        sale_price = _decimal(sale.get("value"))
        price = sale_price if sale_price is not None else regular
    currency = money.get("currency")
    return price, regular, currency if isinstance(currency, str) else None


def squarespace_price_range(item: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """The lowest and highest variant selling price of a Squarespace record, or Nones."""
    prices = [
        p
        for v in squarespace_variants(item)
        if (p := squarespace_price(v)[0]) is not None and p.is_finite()
    ]
    return (min(prices), max(prices)) if prices else (None, None)


def _squarespace(item: dict[str, Any]) -> ItemSummary:
    low, high = squarespace_price_range(item)
    variants = squarespace_variants(item)
    title = item.get("title")
    urls = item.get("image_urls") or squarespace_images(item)
    return ItemSummary(
        kind="product",
        platform="squarespace",
        id=item.get("id"),
        title=title if isinstance(title, str) else None,
        min_price=low,
        max_price=high,
        currency=next((c for v in variants if (c := squarespace_price(v)[2])), None),
        html_url=item.get("html_url"),
        store=item.get("store"),
        image_urls=[u for u in urls if isinstance(u, str)],
    )
