"""WooCommerce: the public Store API, paginated 100 products at a time.

A product lists each variation only by ID and attribute values. The Store API also
lists variations as products of their own (with their prices, SKU and stock), so each
page's variations are fetched in one batch filtered by parent and merged into it.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import KeysView
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._policy import now
from shopify_spy._session import (
    Emit,
    RequestFailed,
    RequestKind,
    ShutdownRequested,
    StoreContext,
)
from shopify_spy._utils import next_page_url, normalize_url

__all__ = ["get_api_url", "get_variations_url", "next_page_url", "run"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
# The default order (newest first) can shift between pages when a plugin overrides the
# ID tiebreak, repeating some products and skipping others; IDs are unique and fixed
BY_ID = {"orderby": "id", "order": "asc"}
# Not counted toward the store's diagnosis: products came back, so the store works
VARIATIONS = RequestKind("variations", counted=False)


def get_api_url(store_url: str, page: int = 1, by_id: bool = True) -> str:
    """Build the Store API URL for the given store and page, sorted by product ID."""
    return _api_url(store_url, {"per_page": PER_PAGE, "page": page}, by_id)


def get_variations_url(store_url: str, parents: list[Any], by_id: bool = True) -> str:
    """Build the Store API URL for the first page of the given products' variations."""
    params = {"type": "variation", "parent": ",".join(map(str, parents)), "per_page": PER_PAGE}
    return _api_url(store_url, params | {"page": 1}, by_id)


def _api_url(store_url: str, params: dict[str, Any], by_id: bool) -> str:
    parsed = normalize_url(store_url)
    if by_id:
        params = params | BY_ID
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store's Store API; return whether it got to the end.

    Any failure ends the store; WooCommerce has no fallback. A failed variation listing
    doesn't: the variations keep only their ID and attributes, and later pages skip it.
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
            remaining = None if ctx.item_limit is None else ctx.item_limit - ctx.items
            variations = await _add_variations(ctx, products[:remaining], by_id)
        for product in products:
            await emit(_item(product, store, ctx.images))
        url = next_page_url(response.url)
        first = False


async def _add_variations(ctx: StoreContext, products: list[dict[str, Any]], by_id: bool) -> bool:
    """Replace each product's variation entries with the variations' own records,
    keeping the entry's attributes; return False if the listing failed."""
    entries = {
        v["id"]: v for p in products for v in _dicts(p.get("variations")) if v.get("id") is not None
    }
    if not entries:
        return True
    parents = [p["id"] for p in products if _dicts(p.get("variations")) and p.get("id") is not None]
    url = get_variations_url(ctx.url, parents, by_id)
    records: dict[Any, dict[str, Any]] = {}
    # Before the first item, a slow listing must not use up --bail: give it half the
    # time left, then emit the products without it
    deadline = ctx.session.bail_deadline
    try:
        async with asyncio.timeout_at(None if deadline is None else (now() + deadline) / 2):
            reason = await _fetch_variations(ctx, url, entries.keys(), records)
    except TimeoutError:
        reason = "too slow before the first item"
    logger.info("WooCommerce: %d of %d variations from %s", len(records), len(entries), ctx.url)
    if reason is not None:
        logger.warning(
            "Variation listing failed for %s (%s); variations keep the product's prices",
            url,
            reason,
        )
    for product in products:
        if isinstance(product.get("variations"), list):
            product["variations"] = [
                records[v["id"]] | v if isinstance(v, dict) and v.get("id") in records else v
                for v in product["variations"]
            ]
    return reason is None


async def _fetch_variations(
    ctx: StoreContext, url: str, wanted: KeysView[Any], records: dict[Any, dict[str, Any]]
) -> str | None:
    """Page the variation listing into ``records``; return why it failed, if it did."""
    while True:
        try:
            response = await ctx.session.get(url, kind=VARIATIONS)
        except RequestFailed as exc:
            return str(exc)
        except ShutdownRequested:  # still emit the page already fetched
            return "shutting down"
        if (reason := _failure(response)) is not None:
            return reason
        assert response is not None
        batch = _dicts(response.json_or_none())
        new = {v["id"]: v for v in batch if v.get("id") in wanted and v["id"] not in records}
        records |= new
        # A short page is the last; a page with nothing new means the filter or the
        # paging was ignored, so stop rather than page through the whole catalog
        if len(batch) < PER_PAGE or not new or records.keys() >= wanted:
            return None
        url = next_page_url(response.url)


def _failure(response: Response | None) -> str | None:
    """Why a variation listing response is unusable, or None if it's a JSON list."""
    if response is None:
        return "skipped: robots.txt or already requested"
    if not response.ok:
        return f"status {response.status}"
    if not isinstance(response.json_or_none(), list):
        return "not a JSON list"
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


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
