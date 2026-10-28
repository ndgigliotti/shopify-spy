"""WooCommerce: the public Store API, paginated 100 products at a time."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._utils import next_page_url, normalize_url

__all__ = ["get_api_url", "next_page_url", "run"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
# The default order (newest first) can shift between pages when a plugin overrides the
# ID tiebreak, repeating some products and skipping others; IDs are unique and fixed
BY_ID = {"orderby": "id", "order": "asc"}


def get_api_url(store_url: str, page: int = 1, by_id: bool = True) -> str:
    """Build the Store API URL for the given store and page, sorted by product ID."""
    parsed = normalize_url(store_url)
    params: dict[str, Any] = {"per_page": PER_PAGE, "page": page}
    if by_id:
        params |= BY_ID
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store's Store API; return whether it got to the end.

    Any failure ends the store; WooCommerce has no fallback.
    """
    by_id = True
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
        for product in products:
            await emit(_item(product, store, ctx.images))
        url = next_page_url(response.url)
        first = False


def _item(product: dict[str, Any], store: str, images: bool) -> dict[str, Any]:
    product["store"] = store
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
