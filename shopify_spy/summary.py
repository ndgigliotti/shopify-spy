"""``summarize()``: the same handful of fields from any item, without changing it.

Provisional API. Shopify and WooCommerce items have different shapes (Shopify wraps
the payload in "product"/"collection"; WooCommerce is flat, with prices in minor
units); this reads either.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ItemSummary:
    kind: str  # "product" or "collection"
    platform: str  # "shopify" or "woocommerce"
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


def _images(item: dict[str, Any]) -> list[str]:
    return [u for u in item.get("image_urls") or [] if isinstance(u, str)]


def summarize(item: dict[str, Any]) -> ItemSummary:
    """Summarize a Shopify or WooCommerce item from ``scrape()``."""
    if isinstance(item.get("product"), dict) or isinstance(item.get("collection"), dict):
        return _shopify(item)
    return _woocommerce(item)


def _shopify(item: dict[str, Any]) -> ItemSummary:
    kind = "product" if isinstance(item.get("product"), dict) else "collection"
    data: dict[str, Any] = item[kind]
    prices = [
        p
        for v in data.get("variants") or []
        if isinstance(v, dict) and (p := _decimal(v.get("price"))) is not None
    ]
    store = item.get("store")
    scheme = urllib.parse.urlsplit(item.get("url") or "").scheme or "https"
    handle = data.get("handle")
    html_url = f"{scheme}://{store}/{kind}s/{handle}" if store and handle else None
    return ItemSummary(
        kind=kind,
        platform="shopify",
        id=data.get("id"),
        title=data.get("title"),
        min_price=min(prices) if prices else None,
        max_price=max(prices) if prices else None,
        currency=None,
        html_url=html_url,
        store=store,
        image_urls=_images(item),
    )


def _woocommerce(item: dict[str, Any]) -> ItemSummary:
    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    unit = Decimal(10) ** int(prices.get("currency_minor_unit") or 0)

    def money(value: Any) -> Decimal | None:
        amount = _decimal(value)
        return amount / unit if amount is not None else None

    price_range = prices.get("price_range") if isinstance(prices.get("price_range"), dict) else {}
    price = money(prices.get("price"))
    low = money(price_range.get("min_amount"))
    high = money(price_range.get("max_amount"))
    return ItemSummary(
        kind="product",
        platform="woocommerce",
        id=item.get("id"),
        title=item.get("name"),
        min_price=low if low is not None else price,
        max_price=high if high is not None else price,
        currency=prices.get("currency_code"),
        html_url=item.get("permalink"),
        store=item.get("store"),
        image_urls=_images(item),
    )
