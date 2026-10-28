"""Documented item shapes. Items are plain dicts; these TypedDicts describe them."""

from typing import Any, NotRequired, TypedDict


class ScrapedImage(TypedDict):
    url: str
    path: str  # relative to images_dir, e.g. "full/<sha1>.jpg"
    checksum: str  # md5 of the file
    status: str  # "downloaded" or "uptodate"


class ShopifyProductItem(TypedDict):
    """A Shopify product. ``url`` points at the product's ``.json`` document."""

    product: dict[str, Any]  # the full Shopify product payload
    url: str
    store: str  # host
    image_urls: list[str]  # empty unless images are enabled
    scraped_images: NotRequired[list[ScrapedImage]]  # only when images are enabled


class ShopifyCollectionItem(TypedDict):
    collection: dict[str, Any]
    url: str
    store: str
    image_urls: list[str]
    scraped_images: NotRequired[list[ScrapedImage]]


class WooCommerceProductItem(TypedDict, total=False):
    """A WooCommerce Store API product: the API's fields plus ``store`` and
    ``image_urls`` (and ``scraped_images`` with images enabled). Prices are strings
    in minor units (see ``prices.currency_minor_unit``). There is no ``url`` key."""

    id: int
    name: str
    permalink: str
    prices: dict[str, Any]
    images: list[dict[str, Any]]
    store: str
    image_urls: list[str]
    scraped_images: list[ScrapedImage]
