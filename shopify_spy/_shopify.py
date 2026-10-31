"""Shopify: bulk /products.json first, the sitemap as fallback and for collections."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

from shopify_spy import _sitemap as sitemap
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._utils import find_all_values, next_page_url, normalize_url

logger = logging.getLogger("shopify_spy")

BULK_PAGE_SIZE = 250
# Sitemap documents fetched at once per store (the session's slots still apply)
FETCH_CONCURRENCY = 32

# Shopify child sitemaps that never list products or collections
SKIPPED_CHILD_SITEMAPS = ("sitemap_pages_", "sitemap_blogs_", "sitemap_agentic_discovery")

next_bulk_page_url = next_page_url


def get_sitemap_url(url: str) -> str:
    """Infer the sitemap URL from a store URL, normalizing if needed."""
    parsed = normalize_url(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "sitemap.xml", "", "", ""))


def get_bulk_products_url(store_url: str, page: int = 1) -> str:
    """Build the bulk products JSON URL for the given store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"limit": BULK_PAGE_SIZE, "page": page})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/products.json", "", query, ""))


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


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit a store's products (and collections, if enabled).

    Returns whether the catalog was covered: False only when the bulk endpoint
    stopped partway and the sitemap couldn't be read to fill the gap.
    """
    seen_handles: set[str] = set()
    bulk_complete = False
    bulk_404 = False
    if ctx.products:
        bulk_complete, bulk_404 = await _bulk(ctx, emit, seen_handles)
        if bulk_complete and not ctx.collections:
            return True
    sitemap_ok = await _sitemap(ctx, emit, seen_handles, skip_products=bulk_complete)
    if ctx.items == 0 and bulk_404:
        await ctx.probe_platform("/wp-json/", _looks_like_woocommerce, "woocommerce")
    return bulk_complete or not seen_handles or sitemap_ok


async def _bulk(ctx: StoreContext, emit: Emit, seen: set[str]) -> tuple[bool, bool]:
    """Page through /products.json. Return (complete, first page was a 404).

    Not complete means fall back to the sitemap: a failed request, non-2xx, non-JSON,
    or an empty first page. An empty page after a full one is the end of the catalog.
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
        if not response.ok:
            logger.info(
                "Bulk products.json returned %d for %s, falling back to sitemap%s",
                response.status,
                url,
                # Shopify refuses page > 100, so bulk tops out at 25,000 products
                " (past Shopify's 100-page limit)" if response.status == 400 and not first else "",
            )
            return False, first and response.status == 404
        data = response.json_or_none()
        products = data.get("products") if isinstance(data, dict) else None
        if products == [] and not first:
            # The previous page was full, so the catalog was an exact multiple of the page size
            return True, False
        if not isinstance(products, list) or not products:
            logger.info("Bulk products.json empty for %s, falling back to sitemap", url)
            return False, False

        parsed = urllib.parse.urlsplit(response.url)
        store = parsed.netloc
        base_url = f"{parsed.scheme}://{store}/products/"
        logger.info("Bulk: %d products from %s", len(products), response.url)
        full_page = len(products) >= BULK_PAGE_SIZE
        products = [p for p in products if isinstance(p, dict)]
        handles = {str(p["handle"]).lower() for p in products if p.get("handle")}
        if handles and handles <= seen:
            # A server that ignores `page` would otherwise repeat this page forever
            logger.warning("Bulk page repeated earlier products; stopping at %s", response.url)
            return True, False
        seen |= handles
        for product in products:
            handle = product.get("handle", "")
            await emit(
                {
                    "product": product,
                    "url": f"{base_url}{handle}.json",
                    "store": store,
                    "platform": "shopify",
                    "image_urls": image_urls(product, ctx.images),
                }
            )
        if not full_page:
            return True, False
        url = next_page_url(response.url)
        first = False


async def _sitemap(ctx: StoreContext, emit: Emit, seen: set[str], skip_products: bool) -> bool:
    """Fetch product/collection JSON documents listed in the sitemap.

    After a complete bulk run every product is already emitted, so product entries
    are skipped; after a partial one, only products whose handles weren't seen are
    fetched. Returns whether the top-level sitemap could be read.
    """
    limiter = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(url: str) -> None:
        try:
            response = await ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Request failed: %s", exc)
            return
        finally:
            limiter.release()
        if response is None or not response.ok:
            return
        data = response.json_or_none()
        if not isinstance(data, dict):
            logger.warning("Non-JSON or unexpected response from %s", url)
            return
        data["url"] = response.url
        data["store"] = urllib.parse.urlsplit(response.url).netloc
        data["platform"] = "shopify"
        data["image_urls"] = image_urls(data, ctx.images)
        await emit(data)

    want_products = ctx.products and not skip_products

    def follow(child: str) -> bool:
        """Skip Shopify child sitemaps that can't list anything wanted."""
        name = urllib.parse.urlsplit(child).path.rsplit("/", 1)[-1]
        if name.startswith("sitemap_products_"):
            return want_products
        if name.startswith("sitemap_collections_"):
            return ctx.collections
        return not name.startswith(SKIPPED_CHILD_SITEMAPS)

    walk = sitemap.iter_locs(ctx.session, get_sitemap_url(ctx.url), follow)
    async with asyncio.TaskGroup() as tg:
        async for loc in walk:
            parts = urllib.parse.urlsplit(loc)
            if (parts.hostname or "").lower() not in walk.hosts:
                continue
            if "/products/" in parts.path:
                if not want_products:
                    continue
                handle = product_handle(loc)
                if handle is not None and handle in seen:
                    continue
            elif "/collections/" not in parts.path or not ctx.collections:
                continue
            # Take the slot before creating the task, so a huge sitemap doesn't
            # queue one task per entry
            await limiter.acquire()
            tg.create_task(fetch(loc + ".json"))
    return walk.root_ok


def _looks_like_woocommerce(data: Any) -> bool:
    return isinstance(data, dict) and ("namespaces" in data or "routes" in data)
