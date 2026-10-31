"""WooCommerce: the public Store API, paginated 100 products at a time.

A product lists each variation only by ID and attribute values. The Store API also
lists variations as products of their own (with their prices, SKU and stock), so each
page's variations are fetched in one batch filtered by parent and merged into it.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._session import (
    Emit,
    RequestFailed,
    RequestKind,
    ShutdownRequested,
    StoreContext,
)
from shopify_spy._utils import dicts, next_page_url, normalize_url

__all__ = ["get_api_url", "get_variations_url", "next_page_url", "run"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
# The default order (newest first) can shift between pages when a plugin overrides the
# ID tiebreak, repeating some products and skipping others; IDs are unique and fixed
BY_ID = {"orderby": "id", "order": "asc"}
# Optional: not counted toward the store's diagnosis (products came back, so the store
# works), and a 403 mustn't swap the user agent the product pages use. Not deduped: a
# page in the default order can repeat the last page's variable products
VARIATIONS = RequestKind("variations", counted=False, dedupe=False, rotate=False)


def get_api_url(store_url: str, page: int = 1, by_id: bool = True) -> str:
    """Build the Store API URL for the given store and page, sorted by product ID."""
    return _api_url(store_url, {"per_page": PER_PAGE, "page": page}, by_id)


def get_variations_url(
    store_url: str, parents: list[Any], page: int = 1, by_id: bool = True
) -> str:
    """Build the Store API URL for a page of the given products' variations."""
    parent = ",".join(map(str, parents))
    params = {"type": "variation", "parent": parent, "per_page": PER_PAGE, "page": page}
    return _api_url(store_url, params, by_id)


def _api_url(store_url: str, params: dict[str, Any], by_id: bool) -> str:
    parsed = normalize_url(store_url)
    if by_id:
        params = params | BY_ID
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store's Store API; return whether it got to the end.

    Any failure ends the store; WooCommerce has no fallback. A failed variation listing
    doesn't: the variations keep only their ID and attributes, and if the store refused
    it (4xx, robots.txt, not JSON, filter ignored) later pages skip it.
    """
    by_id = True
    variations = True  # until a variation listing fails
    url = get_api_url(ctx.url)
    first = True
    seen_ids: set[Any] = set()
    while True:
        try:
            response = await ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Store API request failed: %s", exc)
            return False
        if response is None:
            return False
        if first and by_id and _rejects_param(response):
            # Store API versions without orderby=id reject it; page in the default order
            logger.info("Store API rejected orderby=id for %s; using its default order", url)
            by_id = False
            url = get_api_url(ctx.url, by_id=False)
            continue
        if not response.ok:
            logger.info("Store API returned %d for %s", response.status, url)
            if first and response.status == 404:
                await ctx.probe_platform("/products.json", _looks_like_shopify, "shopify")
            return False
        products = response.json_or_none()
        if products is None:
            logger.warning("Store API returned non-JSON for %s", url)
            return False
        if not isinstance(products, list) or not products:
            return True
        store = urllib.parse.urlsplit(response.url).netloc
        logger.info("WooCommerce: %d products from %s", len(products), response.url)
        products = [p for p in products if isinstance(p, dict)]
        ids = {p["id"] for p in products if p.get("id") is not None}
        if ids and ids <= seen_ids:
            # A server that ignores `page` would otherwise repeat this page forever
            logger.warning("Page repeated earlier products; stopping at %s", response.url)
            return True
        seen_ids |= ids
        if variations:
            # Products past the item limit won't be emitted, so skip their variations
            variations = await _add_variations(ctx, response.url, products[: ctx.items_left], by_id)
        for product in products:
            await emit(_item(product, store, ctx.images))
        url = next_page_url(response.url)
        first = False


async def _add_variations(
    ctx: StoreContext, page_url: str, products: list[dict[str, Any]], by_id: bool
) -> bool:
    """Replace each product's variation entries with the variations' own records,
    keeping the entry's attributes; return False if later pages shouldn't try again.

    The listing goes to the products page's host, past any redirect.
    """
    wanted: set[Any] = set()
    parents = []
    for product in products:
        ids = {_id(v) for v in dicts(product.get("variations"))} - {None}
        if ids and _id(product) is not None:
            wanted |= ids
            parents.append(product["id"])
    if not wanted:
        return True
    url = get_variations_url(page_url, parents, by_id=by_id)
    records: dict[Any, dict[str, Any]] = {}
    # Before the first item, a slow listing must not use up --bail: give it half the
    # time left, then emit the products without it (later pages try again)
    left = ctx.session.bail_time_left()
    try:
        async with asyncio.timeout(None if left is None else left / 2):
            reason, refused = await _fetch_variations(ctx, url, wanted, records)
    except TimeoutError:
        reason, refused = "too slow before the first item", False
    logger.info("WooCommerce: %d of %d variations from %s", len(records), len(wanted), url)
    if reason is not None:
        logger.warning(
            "Variation listing failed for %s (%s); variations keep the product's prices%s",
            url,
            reason,
            ", and later pages skip the listing" if refused else "",
        )
    for product in products:
        if isinstance(product.get("variations"), list):
            product["variations"] = [
                records.get(_id(v), {}) | v if isinstance(v, dict) else v
                for v in product["variations"]
            ]
    return not refused


async def _fetch_variations(
    ctx: StoreContext, url: str, wanted: set[Any], records: dict[Any, dict[str, Any]]
) -> tuple[str | None, bool]:
    """Page the variation listing into ``records``; return why it failed, if it did,
    and whether the store refused it (so later pages shouldn't ask)."""
    while True:
        try:
            response = await ctx.session.get(url, kind=VARIATIONS)
        except RequestFailed as exc:
            return str(exc), False
        except ShutdownRequested:  # still emit the page already fetched
            return "shutting down", False
        if response is None:
            return "blocked by robots.txt", True
        batch = _variation_batch(response)
        if isinstance(batch, str):
            return batch, response.status < 500
        missing = wanted - records.keys()
        new = {v["id"]: v for v in dicts(batch) if _id(v) in missing}
        if batch and not new and not records:
            # Not one of them on the first page: the store ignores type or parent
            return "the store ignored the variation filter", True
        records |= new
        # Page until nothing new, like the product pages: a store may cap per_page
        if not new or records.keys() >= wanted:
            return None, False
        url = next_page_url(response.url)


def _variation_batch(response: Response) -> list[Any] | str:
    """A variation listing page's records, or why the response is unusable."""
    if not response.ok:
        return f"status {response.status}"
    data = response.json_or_none()
    return data if isinstance(data, list) else "not a JSON list"


def _id(record: dict[str, Any]) -> int | str | None:
    """A record's ID if it's usable as a key (a plugin can return anything)."""
    value = record.get("id")
    return value if isinstance(value, (int, str)) else None


def _item(product: dict[str, Any], store: str, images: bool) -> dict[str, Any]:
    product["store"] = store
    product["platform"] = "woocommerce"
    images_field = product.get("images") or []  # may be null
    product["image_urls"] = (
        [img["src"] for img in images_field if isinstance(img, dict) and "src" in img]
        if images
        else []
    )
    return product


def _rejects_param(response: Response) -> bool:
    if response.status != 400:
        return False
    data = response.json_or_none()
    return isinstance(data, dict) and data.get("code") == "rest_invalid_param"


def _looks_like_shopify(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("products"), list)
