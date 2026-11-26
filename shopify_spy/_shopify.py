"""Shopify: bulk /products.json first, then collections past the 25,000 limit, then the sitemap.

A store whose own /products.json is a 404 (a headless storefront) is tried on the
*.myshopify.com domain its homepage names before the sitemap.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import urllib.parse
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple

from shopify_spy import _sitemap as sitemap
from shopify_spy._session import (
    FALLBACK,
    MYSHOPIFY,
    STORE,
    Emit,
    RequestFailed,
    RequestKind,
    StoreContext,
)
from shopify_spy._utils import find_all_values, hostname, next_page_url, normalize_url, origin

logger = logging.getLogger("shopify_spy")

BULK_PAGE_SIZE = 250
# Shopify answers 400 past page 100 of any product list: 25,000 products
MAX_PAGES = 100
# Collections paged at once after bulk stops at the limit
COLLECTION_CONCURRENCY = 4
# Collections stop once the last this many together found fewer new products than
# they cost in requests, the sitemap's rate of one product per request
YIELD_WINDOW = 10
# Sitemap documents fetched at once per store (the session's slots still apply)
FETCH_CONCURRENCY = 32

# Shopify child sitemaps that never list products; collections come from
# /collections.json, since /collections/<handle>.json answers 429 (issue #64)
SKIPPED_CHILD_SITEMAPS = (
    "sitemap_pages_",
    "sitemap_blogs_",
    "sitemap_agentic_discovery",
    "sitemap_collections_",
)

# A shop's Shopify domain, as a headless frontend's HTML names it (checkout links,
# Shop Pay or analytics config)
MYSHOPIFY_HANDLE = re.compile(r"([a-z0-9-]+)\.myshopify\.com")

next_bulk_page_url = next_page_url


def get_sitemap_url(url: str) -> str:
    """Infer the sitemap URL from a store URL, normalizing if needed."""
    parsed = normalize_url(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "sitemap.xml", "", "", ""))


def get_bulk_products_url(store_url: str, page: int = 1, path: str = "/products.json") -> str:
    """Build the bulk products JSON URL (or another paged list's) for a store and page."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"limit": BULK_PAGE_SIZE, "page": page})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


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


class Paging(enum.Enum):
    """How paging a product list ended."""

    COMPLETE = enum.auto()  # reached the end of the list
    CAPPED = enum.auto()  # a full page 100: Shopify's 25,000-item limit
    NOT_FOUND = enum.auto()  # the first page was a 404
    FAILED = enum.auto()  # anything else that stopped it partway


class ListedCollection(NamedTuple):
    collection: dict[str, Any]  # an entry of /collections.json
    handle: str
    page_url: str  # the list page it came from, after redirects


class Paged(NamedTuple):
    outcome: Paging
    origin: str  # where the last response came from, after redirects
    requests: int
    found: int  # products not seen before


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit a store's products (and collections, if enabled).

    Returns whether the catalog was covered: False only when the bulk endpoint
    stopped partway and the sitemap couldn't be read to fill the gap.
    """
    seen_handles: set[str] = set()
    covered = True
    own_bulk = Paging.FAILED  # the store's own /products.json
    base = ctx.url
    # Lists read from the myshopify.com domain, once it found products, use its kind
    # and emit items under the store's own domain
    kind = STORE
    listing_emit = emit
    collections: list[ListedCollection] | None = None
    if ctx.products:
        paged = await _page_products(
            ctx, emit, seen_handles, get_bulk_products_url(ctx.url), "Bulk"
        )
        own_bulk = paged.outcome
        if own_bulk is Paging.NOT_FOUND and not hostname(ctx.url).endswith(".myshopify.com"):
            rehosted = await _myshopify_bulk(ctx, emit, seen_handles, paged.origin)
            if rehosted is not None:
                paged, listing_emit = rehosted
                kind = MYSHOPIFY
        base = paged.origin
        if paged.outcome is not Paging.COMPLETE:
            covered, collections = await _past_bulk(
                ctx, emit, seen_handles, paged, kind=kind, listing_emit=listing_emit
            )
    if ctx.collections:
        if collections is None:
            collections = await _collection_list(ctx, base, kind=kind)
        await _emit_collections(ctx, listing_emit, collections)
    if ctx.items == 0 and own_bulk is Paging.NOT_FOUND:
        await ctx.probe_platform("/wp-json/", _looks_like_woocommerce, "woocommerce")
    return covered


async def _myshopify_bulk(
    ctx: StoreContext, emit: Emit, seen: set[str], own_origin: str
) -> tuple[Paged, Emit] | None:
    """Page /products.json on the myshopify.com domain the homepage names (issue #92).

    Returns how paging went and the emit that puts items under ``own_origin``, or None
    if it found no products. With --bail, the wait for its first item is limited to
    half the store's remaining bail time, since the shared myshopify.com slot may be
    busy or paused by other stores; the rest is left to the sitemap.
    """
    handle = await _myshopify_handle(ctx, own_origin)
    if handle is None:
        return None
    host = f"{handle}.myshopify.com"
    scheme = urllib.parse.urlsplit(ctx.url).scheme
    left = ctx.session.bail_time_left()
    budget = None if left is None else left / 2
    deadline: asyncio.Timeout | None = None

    def on_first() -> None:
        ctx.products_from = host
        if deadline is not None:
            deadline.reschedule(None)  # never cut short a paging that produces items

    listing_emit = _rehosted(emit, own_origin, on_first)
    url = get_bulk_products_url(f"{scheme}://{host}")
    try:
        async with asyncio.timeout(budget) as deadline:
            paged = await _page_products(
                ctx, listing_emit, seen, url, "myshopify.com bulk", kind=MYSHOPIFY
            )
    except TimeoutError:
        if deadline is None or not deadline.expired():
            raise
        logger.info("%s: no answer from %s within %.1fs, using the sitemap", ctx.url, host, budget)
        return None
    return (paged, listing_emit) if paged.found > 0 else None


async def _myshopify_handle(ctx: StoreContext, base: str) -> str | None:
    """The one myshopify.com handle the homepage names, or None if it names none or several."""
    url = f"{base}/"
    try:
        response = await ctx.session.get(url, kind=FALLBACK)
    except RequestFailed as exc:
        logger.info("Homepage failed for %s: %s", url, exc)
        return None
    if response is None or not response.ok:
        status = "skipped" if response is None else f"returned {response.status}"
        logger.info("Homepage %s for %s", status, url)
        return None
    handles = sorted(set(MYSHOPIFY_HANDLE.findall(response.text.lower())))
    if len(handles) != 1:
        named = ", ".join(f"{h}.myshopify.com" for h in handles) or "none"
        logger.info("Homepage %s names %d myshopify.com domains (%s)", url, len(handles), named)
        return None
    logger.info("Trying %s.myshopify.com for the products of %s", handles[0], base)
    return handles[0]


def _rehosted(emit: Emit, own_origin: str, on_first: Callable[[], None]) -> Emit:
    """Emit items under ``own_origin``, whatever host they were read from.

    ``on_first`` runs before the first item is emitted, so what it records survives
    the StopStore or ShutdownRequested that emitting may raise.
    """
    own = urllib.parse.urlsplit(own_origin)
    first = True

    async def rehost(item: dict[str, Any]) -> None:
        nonlocal first
        parts = urllib.parse.urlsplit(item["url"])
        item["url"] = urllib.parse.urlunsplit((own.scheme, own.netloc, *parts[2:]))
        item["store"] = own.netloc
        if first:
            first = False
            on_first()
        await emit(item)

    return rehost


async def _past_bulk(
    ctx: StoreContext,
    emit: Emit,
    seen: set[str],
    paged: Paged,
    *,
    kind: RequestKind = STORE,
    listing_emit: Emit | None = None,
) -> tuple[bool, list[ListedCollection] | None]:
    """Find the products bulk didn't reach: collections past the limit, then the sitemap.

    Collections are read with ``kind`` and emitted with ``listing_emit`` (default
    ``emit``); the sitemap is always the store's own.

    Returns whether the catalog was covered, and the collection list if it was read.
    """
    listing_emit = listing_emit or emit
    collections = None
    if paged.outcome is Paging.CAPPED and ctx.item_limit is None:
        # List the sitemap first, so collections know which products are missing
        walk = _sitemap_walk(ctx)
        locs = [loc async for loc in _product_locs(walk)]
        listed = {h for loc in locs if (h := _listed_handle(loc))} if walk.root_ok else None
        collections = await _collections(ctx, listing_emit, seen, paged.origin, listed, kind=kind)
        await _fetch_documents(ctx, emit, _each(locs), seen)
        return walk.root_ok, collections
    if paged.outcome is Paging.CAPPED:
        # With an item limit the catalog may be far bigger than needed (millions of
        # products), so don't read the whole sitemap up front; it's streamed below
        collections = await _collections(ctx, listing_emit, seen, paged.origin, None, kind=kind)
    walk = _sitemap_walk(ctx)
    await _fetch_documents(ctx, emit, _product_locs(walk), seen)
    return not seen or walk.root_ok, collections


async def _page_products(
    ctx: StoreContext,
    emit: Emit,
    seen: set[str],
    url: str,
    label: str,
    *,
    overlapping: bool = False,
    remaining: set[str] | None = None,
    kind: RequestKind = STORE,
) -> Paged:
    """Page through a product list from its first page.

    A failed request, non-2xx, non-JSON or an empty first page stops paging
    partway. An empty page after a full one is the end of the list, and a full
    page 100 is as far as Shopify goes (page 101 answers 400).

    An ``overlapping`` list (a collection) shares products with lists paged before
    it, so only products not seen yet are emitted. It is paged to the end even
    when its first pages add nothing: a collection in the bulk order repeats the
    bulk window first, and its new products come after. It stops early once
    ``remaining``, the products still being looked for, is empty.
    """
    base = origin(url)
    pages = found = 0
    previous: set[str] = set()

    def done(outcome: Paging) -> Paged:
        return Paged(outcome, base, pages, found)

    while True:
        try:
            response = await ctx.session.get(url, kind=kind)
        except RequestFailed as exc:
            pages += 1
            logger.info("%s failed for %s: %s", label, url, exc)
            return done(Paging.FAILED)
        if response is None:  # not sent: robots.txt or an already fetched redirect target
            logger.info("%s skipped for %s", label, url)
            return done(Paging.FAILED)
        pages += 1
        first = pages == 1
        base = origin(response.url)
        if urllib.parse.urlsplit(response.url).path == "/password":
            logger.info("%s is password protected at %s", label, response.url)
            return done(Paging.FAILED)
        if not response.ok:
            logger.info("%s returned %d for %s", label, response.status, url)
            return done(Paging.NOT_FOUND if first and response.status == 404 else Paging.FAILED)
        data = response.json_or_none()
        products = data.get("products") if isinstance(data, dict) else None
        if products == [] and not first:
            # The previous page was full, so the list was an exact multiple of the page size
            return done(Paging.COMPLETE)
        if not isinstance(products, list) or not products:
            logger.info("%s empty for %s", label, url)
            return done(Paging.FAILED)

        parsed = urllib.parse.urlsplit(response.url)
        store = parsed.netloc
        base_url = f"{parsed.scheme}://{store}/products/"
        full_page = len(products) >= BULK_PAGE_SIZE
        products = [p for p in products if isinstance(p, dict)]
        handles = {str(p["handle"]).lower() for p in products if p.get("handle")}
        new = handles - seen
        logger.info(
            "%s: %d products (%d new) from %s", label, len(products), len(new), response.url
        )
        # A server that ignores `page` would otherwise repeat this page forever
        if handles and (handles == previous if overlapping else not new):
            logger.warning("%s page repeated earlier products; stopping at %s", label, response.url)
            return done(Paging.COMPLETE)
        previous = handles
        seen |= handles
        found += len(new)
        if remaining is not None:
            remaining -= new
        if overlapping:
            # Without a handle a product can't be told apart from one already emitted
            by_handle = {str(p.get("handle") or "").lower(): p for p in products}
            products = [p for h, p in by_handle.items() if h in new]
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
        if not full_page or remaining == set():
            return done(Paging.COMPLETE)
        if pages >= MAX_PAGES:
            logger.info("%s reached Shopify's 100-page limit at %s", label, response.url)
            return done(Paging.CAPPED)
        url = next_page_url(response.url)


async def _collections(
    ctx: StoreContext,
    emit: Emit,
    seen: set[str],
    base: str,
    listed: set[str] | None,
    *,
    kind: RequestKind = STORE,
) -> list[ListedCollection] | None:
    """Get past the bulk limit through collections, each with its own 25,000 limit.

    /collections/all lists the catalog in another order than /products.json, then
    collections from /collections.json are paged, largest first, a few at a time.
    The sitemap fetches each missing product with one request, so a list is paged
    only while it could save requests: while more of the sitemap's products
    (``listed``, None if it couldn't be read) are missing than the list has pages,
    and until the last YIELD_WINDOW collections found less than a product per
    page. Products in no collection are left to the sitemap.

    Returns /collections.json's entries, or None if it wasn't read. The session
    requests a URL only once, so collection items are built from this list.
    """

    remaining = None if listed is None else listed - seen

    def cheaper_one_by_one(pages: int) -> bool:
        return remaining is not None and len(remaining) <= pages

    if cheaper_one_by_one(MAX_PAGES):
        logger.info("Bulk stopped at the 25,000-product limit; %d missing", len(remaining or ()))
        return None
    logger.info("Bulk stopped at Shopify's 25,000-product limit; paging collections")

    async def page(path: str, label: str) -> Paged:
        url = get_bulk_products_url(base, path=path)
        return await _page_products(
            ctx, emit, seen, url, label, overlapping=True, remaining=remaining, kind=kind
        )

    await page("/collections/all/products.json", "Collection all")
    if remaining == set():
        logger.info("Collections found every product in the sitemap")
        return None
    collections = await _collection_list(ctx, base, kind=kind)
    queue = deque(_collection_handles(collections))
    recent: deque[Paged] = deque(maxlen=YIELD_WINDOW)

    def worthwhile() -> bool:
        if remaining == set():
            return False
        if len(recent) < YIELD_WINDOW:
            return True
        return sum(p.found for p in recent) >= sum(p.requests for p in recent)

    async def worker() -> None:
        while queue and worthwhile():
            handle, count = queue.popleft()
            # A missing count is a single page until proven otherwise
            if cheaper_one_by_one(min(MAX_PAGES, max(1, -(-count // BULK_PAGE_SIZE)))):
                continue
            path = f"/collections/{urllib.parse.quote(handle)}/products.json"
            recent.append(await page(path, f"Collection {handle}"))

    async with asyncio.TaskGroup() as tg:
        for _ in range(COLLECTION_CONCURRENCY):
            tg.create_task(worker())
    if remaining == set():
        logger.info("Collections found every product in the sitemap")
    elif queue:
        logger.info("Collections stopped paying off; skipped the last %d", len(queue))
    return collections


async def _collection_list(
    ctx: StoreContext, base: str, *, kind: RequestKind = STORE
) -> list[ListedCollection]:
    """Every entry of the paged /collections.json, once per handle.

    A failed page is logged and ends the list with what was read so far.
    """
    collections: dict[str, ListedCollection] = {}
    previous: list[Any] = []
    url = get_bulk_products_url(base, path="/collections.json")
    for _ in range(MAX_PAGES):
        fetched = await _collection_page(ctx, url, kind=kind)
        if fetched is None:
            break
        page, page_url = fetched
        if not page or page == previous:
            break  # the end, or a server that ignores `page`
        previous = page
        for collection in page:
            if isinstance(collection, dict) and collection.get("handle"):
                handle = str(collection["handle"])
                collections.setdefault(handle, ListedCollection(collection, handle, page_url))
        if len(page) < BULK_PAGE_SIZE:
            break
        url = next_page_url(page_url)
    logger.info("Collection list: %d collections", len(collections))
    return list(collections.values())


async def _collection_page(
    ctx: StoreContext, url: str, *, kind: RequestKind = STORE
) -> tuple[list[Any], str] | None:
    """One page of /collections.json and its URL after redirects, or None if it failed."""
    try:
        response = await ctx.session.get(url, kind=kind)
    except RequestFailed as exc:
        logger.info("Collection list failed for %s: %s", url, exc)
        return None
    if response is None or not response.ok:
        status = "skipped" if response is None else f"returned {response.status}"
        logger.info("Collection list %s for %s", status, url)
        return None
    data = response.json_or_none()
    page = data.get("collections") if isinstance(data, dict) else None
    return (page, response.url) if isinstance(page, list) else None


def _collection_handles(collections: list[ListedCollection]) -> list[tuple[str, int]]:
    """(handle, products count) of the collections worth paging, largest first.

    Empty collections and "all" are left out; a missing count is -1.
    """
    handles = []
    for collection, handle, _ in collections:
        count = collection.get("products_count")
        # A missing count sorts last but is still paged
        count = count if isinstance(count, int) else -1
        if count != 0 and handle.lower() != "all":
            handles.append((handle, count))
    return sorted(handles, key=lambda hn: -hn[1])


async def _emit_collections(
    ctx: StoreContext, emit: Emit, collections: list[ListedCollection]
) -> None:
    """Emit each listed collection; /collections/<handle>.json answers 429 (issue #64),
    so the list entry is used instead."""
    for collection, handle, page_url in collections:
        parts = urllib.parse.urlsplit(page_url)
        handle = urllib.parse.quote(handle)
        await emit(
            {
                "collection": collection,
                "url": f"{parts.scheme}://{parts.netloc}/collections/{handle}.json",
                "store": parts.netloc,
                "platform": "shopify",
                "image_urls": image_urls(collection, ctx.images),
            }
        )


def _listed_handle(loc: str) -> str | None:
    """A sitemap product's handle, unless it's a localized URL (/fr/products/...),
    whose handle may be translated and never match a product's own."""
    path = urllib.parse.urlsplit(loc).path
    return product_handle(loc) if path.startswith("/products/") else None


def _sitemap_walk(ctx: StoreContext) -> sitemap.SitemapWalk:
    def follow(child: str) -> bool:
        """Skip Shopify child sitemaps that can't list products."""
        name = urllib.parse.urlsplit(child).path.rsplit("/", 1)[-1]
        return not name.startswith(SKIPPED_CHILD_SITEMAPS)

    return sitemap.iter_locs(ctx.session, get_sitemap_url(ctx.url), follow)


async def _product_locs(walk: sitemap.SitemapWalk) -> AsyncIterator[str]:
    """The store's own product entries."""
    async for loc in walk:
        parts = urllib.parse.urlsplit(loc)
        if (parts.hostname or "").lower() in walk.hosts and "/products/" in parts.path:
            yield loc


async def _each(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


async def _fetch_documents(
    ctx: StoreContext, emit: Emit, locs: AsyncIterator[str], seen: set[str]
) -> None:
    """Fetch each entry's JSON document, skipping products whose handles were seen."""
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

    async with asyncio.TaskGroup() as tg:
        async for loc in locs:
            handle = product_handle(loc)
            if handle is not None and handle in seen:
                continue
            # Take the slot before creating the task, so a huge sitemap doesn't
            # queue one task per entry
            await limiter.acquire()
            tg.create_task(fetch(loc + ".json"))


def _looks_like_woocommerce(data: Any) -> bool:
    return isinstance(data, dict) and ("namespaces" in data or "routes" in data)
