"""Shopify: bulk /products.json first, the sitemap as fallback and for collections."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any

from shopify_spy import sitemap
from shopify_spy.session import Emit, RequestFailed, StoreContext
from shopify_spy.utils import find_all_values, normalize_url

logger = logging.getLogger("shopify_spy")

BULK_PAGE_SIZE = 250
# Sitemap documents fetched at once per store (the session's slots still apply)
FETCH_CONCURRENCY = 32
WOOCOMMERCE_HINT = "looks like WooCommerce; pass platform='woocommerce'"


def get_sitemap_url(url: str) -> str:
    """Infer the sitemap URL from a store URL, normalizing if needed."""
    parsed = normalize_url(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "sitemap.xml", "", "", ""))


def get_bulk_products_url(store_url: str, page: int = 1) -> str:
    """Build the bulk products JSON URL for the given store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"limit": BULK_PAGE_SIZE, "page": page})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/products.json", "", query, ""))


def next_bulk_page_url(url: str) -> str:
    """Increment the page parameter in a bulk products URL."""
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params["page"] = str(int(params.get("page", 1)) + 1)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(params), "")
    )


def product_handle(url: str) -> str | None:
    """Return the product handle from a product URL, ignoring host and locale prefix.

    ``https://www.a.com/en-ca/products/Blue%20Hat?x=1`` -> ``"blue hat"``.
    """
    path = urllib.parse.urlsplit(url).path
    if "/products/" not in path:
        return None
    rest = path.rsplit("/products/", 1)[1].split("/", 1)[0]
    if rest.endswith(".json"):
        rest = rest[: -len(".json")]
    return urllib.parse.unquote(rest).lower() or None


def image_urls(data: Any, enabled: bool) -> list[Any]:
    return list(find_all_values("src", data)) if enabled else []


async def run(ctx: StoreContext, emit: Emit) -> None:
    """Emit a store's products (and collections, if enabled)."""
    seen_handles: set[str] = set()
    bulk_complete = False
    bulk_404 = False
    if ctx.products:
        bulk_complete, bulk_404 = await _bulk(ctx, emit, seen_handles)
        if bulk_complete and not ctx.collections:
            return
    await _sitemap(ctx, emit, seen_handles, skip_products=bulk_complete)
    if ctx.items == 0 and bulk_404:
        await _probe_woocommerce(ctx)


async def _bulk(ctx: StoreContext, emit: Emit, seen: set[str]) -> tuple[bool, bool]:
    """Page through /products.json. Return (complete, first page was a 404).

    Not complete means fall back to the sitemap: a failed request, non-2xx, non-JSON,
    or a page without products, on any page.
    """
    url = get_bulk_products_url(ctx.url)
    first = True
    while True:
        try:
            response = await ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Bulk products.json failed for %s, falling back to sitemap: %s", url, exc)
            return False, False
        if response is None:
            logger.info("Bulk products.json skipped for %s, falling back to sitemap", url)
            return False, False
        if not 200 <= response.status < 300:
            logger.info(
                "Bulk products.json returned %d for %s, falling back to sitemap",
                response.status,
                url,
            )
            return False, first and response.status == 404
        try:
            data = json.loads(response.body)
        except ValueError:
            data = None
        products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(products, list) or not products:
            logger.info("Bulk products.json empty for %s, falling back to sitemap", url)
            return False, False

        parsed = urllib.parse.urlsplit(response.url)
        store = parsed.netloc
        base_url = f"{parsed.scheme}://{store}/products/"
        logger.info("Bulk: %d products from %s", len(products), response.url)
        for product in products:
            if not isinstance(product, dict):
                continue
            handle = product.get("handle", "")
            if handle:
                seen.add(str(handle).lower())
            await emit(
                {
                    "product": product,
                    "url": f"{base_url}{handle}.json",
                    "store": store,
                    "image_urls": image_urls(product, ctx.images),
                }
            )
        if len(products) < BULK_PAGE_SIZE:
            return True, False
        url = next_bulk_page_url(response.url)
        first = False


async def _sitemap(ctx: StoreContext, emit: Emit, seen: set[str], skip_products: bool) -> None:
    """Fetch product/collection JSON documents listed in the sitemap.

    After a complete bulk run every product is already emitted, so product entries
    are skipped; after a partial one, only products whose handles weren't seen are
    fetched.
    """
    sitemap_url = get_sitemap_url(ctx.url)
    limiter = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(url: str, key: str) -> None:
        async with limiter:
            try:
                response = await ctx.session.get(url)
            except RequestFailed as exc:
                logger.info("Request failed: %s", exc)
                return
        if response is None or not 200 <= response.status < 300:
            return
        try:
            data = json.loads(response.body)
        except ValueError:
            logger.warning("Non-JSON response from %s", url)
            return
        if not isinstance(data, dict):
            return
        parsed = urllib.parse.urlsplit(response.url)
        data["url"] = response.url
        data["store"] = parsed.netloc
        data["image_urls"] = image_urls(data, ctx.images)
        await emit(data)

    async with asyncio.TaskGroup() as tg:
        async for loc in sitemap.iter_locs(ctx.session, sitemap_url):
            if "/products/" in loc:
                if not ctx.products or skip_products:
                    continue
                handle = product_handle(loc)
                if handle is not None and handle in seen:
                    continue
                tg.create_task(fetch(loc + ".json", "product"))
            elif "/collections/" in loc and ctx.collections:
                tg.create_task(fetch(loc + ".json", "collection"))


async def _probe_woocommerce(ctx: StoreContext) -> None:
    """Set a hint if the store answers like WooCommerce."""
    try:
        response = await ctx.session.get(f"{ctx.url}/wp-json/", kind="probe", dedupe=False)
    except RequestFailed:
        return
    if response is None or response.status != 200:
        return
    try:
        data = json.loads(response.body)
    except ValueError:
        return
    if isinstance(data, dict) and ("namespaces" in data or "routes" in data):
        ctx.hint = WOOCOMMERCE_HINT
