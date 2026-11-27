"""Documented item shapes. Items are plain dicts; these TypedDicts describe them."""

from typing import Any, Literal, NotRequired, TypedDict


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
    platform: Literal["shopify"]
    image_urls: list[str]  # empty unless images are enabled
    scraped_images: NotRequired[list[ScrapedImage]]  # only when images are enabled


class ShopifyCollectionItem(TypedDict):
    collection: dict[str, Any]
    url: str
    store: str
    platform: Literal["shopify"]
    image_urls: list[str]
    scraped_images: NotRequired[list[ScrapedImage]]


class WooCommerceProductItem(TypedDict, total=False):
    """A WooCommerce Store API product: the API's fields plus ``store``, ``platform``
    and ``image_urls`` (and ``scraped_images`` with images enabled). Prices are strings
    in minor units (see ``prices.currency_minor_unit``). There is no ``url`` key.

    Each ``variations`` entry is the variation's own Store API record (``prices``,
    ``sku``, ``is_in_stock``, ...) with the listing's ``attributes``; only ``id`` and
    ``attributes`` if the store refused the variation listing.

    When the Store API is unavailable, items come from product pages instead:
    ``source`` is ``"product_page"``, only the fields schema.org JSON-LD carries are
    set (plus ``attributes`` and ``variations`` from a variable product's variation
    form), and ``json_ld`` holds the page's Product object."""

    id: int
    name: str
    permalink: str
    prices: dict[str, Any]
    images: list[dict[str, Any]]
    variations: list[dict[str, Any]]
    store: str
    platform: Literal["woocommerce"]
    image_urls: list[str]
    scraped_images: list[ScrapedImage]
    source: str  # only on fallback items: "product_page"
    json_ld: dict[str, Any]  # only on fallback items


class MagentoProductItem(TypedDict, total=False):
    """A Magento product from the storefront GraphQL ``products`` query: its fields
    as the query returns them, plus ``store``, ``platform``, ``html_url`` and
    ``image_urls`` (and ``scraped_images`` with images enabled). Prices are numbers
    in the default store view's currency. There is no ``url`` key.

    ``html_url`` is the product page, ``<scheme>://<host>/<url_key><url_suffix>``, or
    None without a ``url_key``. ``image_urls`` lists the ``media_gallery`` URLs that
    aren't disabled, ordered by ``position``. A ``ConfigurableProduct`` also has
    ``configurable_options`` and ``variants``, each with the variant's
    ``attributes`` and its own ``product`` (``uid``, ``sku``, ``stock_status``,
    ``image``, ``price_range``)."""

    uid: str
    sku: str
    name: str
    url_key: str
    url_suffix: str
    price_range: dict[str, Any]
    media_gallery: list[dict[str, Any]]
    variants: list[dict[str, Any]]
    store: str
    platform: Literal["magento"]
    html_url: str | None
    image_urls: list[str]
    scraped_images: list[ScrapedImage]


JsonLdProductItem = TypedDict(
    "JsonLdProductItem",
    {
        "@context": Any,
        "@type": str | list[str],
        "name": str,
        "sku": str,
        "offers": Any,
        "image": Any,
        "brand": Any,
        "hasVariant": list[dict[str, Any]],
        "store": str,
        "platform": Literal["jsonld"],
        "html_url": str,
        "image_urls": list[str],
        "scraped_images": list[ScrapedImage],
    },
    total=False,
)
# The functional form, since keys like "@type" aren't identifiers, can't take a docstring
JsonLdProductItem.__doc__ = """A schema.org ``Product`` or ``ProductGroup`` node from a product
page's JSON-LD, as found but with its ``{"@id": ...}`` references to the page's
other nodes resolved (only the common properties are listed), plus ``store``,
``platform``, ``html_url`` and ``image_urls`` (and ``scraped_images`` with images
enabled). schema.org has none of those four properties, so they can't collide.

``html_url`` is the page the node came from, without its fragment. ``image_urls``
lists ``image`` and then each ``hasVariant`` entry's images not already listed.
A ``ProductGroup`` keeps its ``hasVariant`` Products inside it. Prices are in
``offers``: an Offer, an AggregateOffer or a list of Offers."""
