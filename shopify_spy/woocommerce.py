"""WooCommerce: the public Store API, paginated 100 products at a time."""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from shopify_spy.session import Emit, RequestFailed, StoreContext
from shopify_spy.utils import normalize_url

logger = logging.getLogger("shopify_spy")

API_PATH = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
SHOPIFY_HINT = "looks like Shopify; pass platform='shopify'"


def get_api_url(store_url: str, page: int = 1) -> str:
    """Build the Store API URL for the given store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"per_page": PER_PAGE, "page": page})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


def next_page_url(url: str) -> str:
    """Increment the page parameter in a Store API URL."""
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params["page"] = str(int(params.get("page", 1)) + 1)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(params), "")
    )


async def run(ctx: StoreContext, emit: Emit) -> None:
    """Emit every product from the store's Store API.

    Any failure ends the store; WooCommerce has no fallback.
    """
    url = get_api_url(ctx.url)
    first = True
    while True:
        try:
            response = await ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Store API request failed: %s", exc)
            return
        if response is None:
            return
        if not 200 <= response.status < 300:
            logger.info("Store API returned %d for %s", response.status, url)
            if first and response.status == 404:
                await _probe_shopify(ctx)
            return
        try:
            products = json.loads(response.body)
        except ValueError:
            logger.warning("Store API returned non-JSON for %s", url)
            return
        if not isinstance(products, list) or not products:
            return
        store = urllib.parse.urlsplit(response.url).netloc
        logger.info("WooCommerce: %d products from %s", len(products), response.url)
        for product in products:
            if not isinstance(product, dict):
                continue
            await emit(_item(product, store, ctx.images))
        url = next_page_url(response.url)
        first = False


def _item(product: dict[str, Any], store: str, images: bool) -> dict[str, Any]:
    product["store"] = store
    product["image_urls"] = (
        [img["src"] for img in product.get("images", []) if isinstance(img, dict) and "src" in img]
        if images
        else []
    )
    return product


async def _probe_shopify(ctx: StoreContext) -> None:
    """Set a hint if the store answers like Shopify."""
    try:
        response = await ctx.session.get(f"{ctx.url}/products.json", kind="probe", dedupe=False)
    except RequestFailed:
        return
    if response is None or response.status != 200:
        return
    try:
        data = json.loads(response.body)
    except ValueError:
        return
    if isinstance(data, dict) and isinstance(data.get("products"), list):
        ctx.hint = SHOPIFY_HINT
