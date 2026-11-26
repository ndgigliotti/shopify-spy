"""The generic platform: schema.org Product JSON-LD on the product pages that the
store's sitemaps list.

The sitemaps are the robots.txt ``Sitemap:`` URLs on the store's site, else the first
of ``/sitemap.xml``, ``/sitemap_index.xml`` and ``/xmlsitemap.php`` that parses. Of
an index's children, only the product sitemaps are read when there are any. Each
listed page is fetched, and one whose JSON-LD has a Product or ProductGroup for that
page becomes an item: the node as found, plus ``store``, ``platform``, ``html_url``
and ``image_urls``.

A store whose pages have no Product data (it renders products in the browser, say)
raises JsonLdError after ``PROBE_PAGES`` pages, so it reports ``error`` with the
reason rather than ``empty``. Until the first item, ``ctx.no_item_reason`` holds the
same reason, which the runner reports if ``--bail`` stops the store first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from shopify_spy import _jsonld as jsonld
from shopify_spy import _sitemap as sitemap
from shopify_spy._fetch import Response
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._shopify import _looks_like_woocommerce
from shopify_spy._utils import canonicalize_url, hostname
from shopify_spy._woocommerce import _looks_like_shopify

__all__ = ["JsonLdError", "run"]

logger = logging.getLogger("shopify_spy")

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/xmlsitemap.php")
FETCH_CONCURRENCY = 8  # product pages fetched at once per store (host slots still apply)
PROBE_PAGES = 50  # pages back without an item, before the first, that end the store
HINT_AFTER = 5  # pages back without an item, before the first, that start the hint probe
GONE = frozenset({404, 410})
# WordPress taxonomy sitemaps (Yoast's and Rank Math's product_cat-sitemap.xml, core's
# wp-sitemap-taxonomies-product_cat-1.xml) list category, tag and brand pages
_TAXONOMY_FRAGMENTS = (
    "product_cat",
    "product-cat",
    "product_tag",
    "product-tag",
    "product_brand",
    "product-brand",
    "taxonom",
)


class JsonLdError(Exception):
    """The store's sitemaps listed pages, but none gave a schema.org Product, or
    no sitemap could be found."""


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit an item for every listed page with schema.org Product data; return
    whether the listing was read to the end and every page was fetched."""
    listing = _Listing(ctx)
    pages = _Pages(ctx, emit, listing)
    await pages.run()
    if pages.emitted:
        return listing.complete and not pages.failed
    if not listing.roots:
        await pages.probe_others()
        if listing.answered:
            raise JsonLdError(f"no sitemap found at {', '.join(listing.tried)}")
        return False  # diagnose() reports the sitemaps' 404, 403 or robots.txt
    if pages.counts.checked == 0:
        return listing.complete  # no on-site page listed: empty
    await pages.probe_others()
    raise JsonLdError(pages.reason())


def _site(host: str) -> str:
    return host.removeprefix("www.")


def _is_product_sitemap(url: str) -> bool:
    """Whether a sitemap's file name or query names products, and it isn't a
    taxonomy's (categories, tags, brands)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    name = parts.path.rsplit("/", 1)[-1].lower()
    if any(fragment in name for fragment in _TAXONOMY_FRAGMENTS):
        return False
    return "product" in name or "product" in parts.query.lower()


def _prefer_products(urls: list[str]) -> list[str]:
    """The product sitemaps among ``urls`` if there are any, else all of them."""
    products = [url for url in urls if _is_product_sitemap(url)]
    return products or urls


@dataclass
class _Sitemap:
    url: str  # where it was read, past redirects
    root: str  # "urlset" or "sitemapindex"
    locs: list[str]


class _Listing:
    """Page URLs from the store's sitemaps, on the store's site.

    ``roots`` are the top-level sitemaps that parsed; ``answered`` is whether any
    candidate answered 2xx; ``complete`` becomes True once every sitemap read was
    readable and no off-site URL was left out.
    """

    def __init__(self, ctx: StoreContext) -> None:
        self.ctx = ctx
        self.sites = {_site(hostname(ctx.url))}
        self.roots: list[str] = []
        self.tried: list[str] = []
        self.answered = False
        self.complete = False
        self._dropped = 0

    def _on_site(self, url: str) -> bool:
        if _site(hostname(url)) in self.sites:
            return True
        self._dropped += 1
        return False

    async def locs(self) -> AsyncIterator[str]:
        found, complete = await self._top_level()
        if not found:
            return
        # Product sitemaps: the top-level urlsets and the indexes' children, together
        sources: list[str] = []
        read: dict[str, _Sitemap] = {}
        for doc in found:
            if doc.root == "urlset":
                sources.append(doc.url)
                read[doc.url] = doc
            else:
                sources += [loc for loc in doc.locs if self._on_site(loc)]
        for source in _prefer_products(sources):
            if source in read:
                for loc in read[source].locs:
                    if self._on_site(loc):
                        yield loc
                continue
            walk = sitemap.iter_locs(self.ctx.session, source)
            async for loc in walk:
                self.sites |= {_site(host) for host in walk.hosts}
                if self._on_site(loc):
                    yield loc
            complete = complete and walk.root_ok
        if self._dropped:
            logger.info("Sitemaps listed %d off-site links", self._dropped)
        self.complete = complete and not self._dropped

    async def _top_level(self) -> tuple[list[_Sitemap], bool]:
        """The robots.txt-listed sitemaps that parse, else the first well-known one,
        and whether every robots.txt-listed one did."""
        session = self.ctx.session
        robots_url = f"{self.ctx.url}/robots.txt"
        rules = await session.registry.robots(self.ctx.url, session)
        listed = []
        for value in rules.sitemaps:
            try:
                url = urllib.parse.urljoin(robots_url, value)
            except ValueError:
                continue
            if url not in listed and self._on_site(url):
                listed.append(url)
        self._dropped = 0  # an off-site Sitemap: line isn't a page left out
        found = [doc for url in listed if (doc := await self._read(url)) is not None]
        if found:
            if len(found) < len(listed):
                logger.info("Some robots.txt sitemaps were unreadable for %s", self.ctx.url)
            return found, len(found) == len(listed)
        for path in SITEMAP_PATHS:
            if (doc := await self._read(f"{self.ctx.url}{path}")) is not None:
                return [doc], True
        return [], False

    async def _read(self, url: str) -> _Sitemap | None:
        self.tried.append(url)
        try:
            response = await self.ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Sitemap request failed: %s", exc)
            return None
        if response is None or not response.ok:
            return None
        self.answered = True
        root, locs = sitemap.parse(response.body)
        if root is None:
            logger.info("Not a sitemap: %s", response.url)
            return None
        self.sites.add(_site(hostname(response.url)))
        self.roots.append(response.url)
        return _Sitemap(response.url, root, locs)


@dataclass
class _Counts:
    """Pages that came back without an item, by outcome."""

    checked: int = 0
    no_product: int = 0
    skipped: int = 0  # robots.txt-disallowed, or a redirect to a page already fetched
    failed: int = 0  # no response after retries
    statuses: Counter[int] = field(default_factory=Counter)


class _Pages:
    """Fetches the listed pages and emits their Products, up to ``PROBE_PAGES``
    pages without an item before the first."""

    def __init__(self, ctx: StoreContext, emit: Emit, listing: _Listing) -> None:
        self.ctx = ctx
        self._emit = emit
        self.listing = listing
        self.counts = _Counts()
        self.emitted = False
        self.failed = False  # a page that should have been read wasn't
        self._limiter = asyncio.Semaphore(FETCH_CONCURRENCY)
        self._fetches: set[asyncio.Task[None]] = set()
        self._probe: asyncio.Task[None] | None = None
        self._tg: asyncio.TaskGroup | None = None
        self._robots_before = 0

    async def run(self) -> None:
        self._robots_before = self.ctx.session.stats.robots_disallowed
        seen: set[str] = set()
        async with (
            asyncio.TaskGroup() as tg,
            contextlib.aclosing(self.listing.locs()) as locs,
        ):
            self._tg = tg
            async for url in locs:
                try:
                    key = canonicalize_url(url)
                except ValueError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                if not await self._take_slot():
                    break
                task = tg.create_task(self._fetch(url.split("#", 1)[0]))
                self._fetches.add(task)
                task.add_done_callback(self._fetches.discard)

    def _gave_up(self) -> bool:
        return not self.emitted and self.counts.checked >= PROBE_PAGES

    async def _take_slot(self) -> bool:
        """Take a fetch slot; False once ``PROBE_PAGES`` pages came back without an
        item and the pages still in flight didn't give one either.

        The slot is taken before the task is created, so a long listing doesn't
        queue one task per page.
        """
        await self._limiter.acquire()
        if not self._gave_up():
            return True
        self._limiter.release()
        if self._fetches:
            await asyncio.wait(set(self._fetches))
        if self._gave_up():
            logger.info(
                "No schema.org Product on %d pages from %s; giving up",
                self.counts.checked,
                self.ctx.url,
            )
            return False
        await self._limiter.acquire()
        return True

    async def _fetch(self, url: str) -> None:
        # The slot is held until the item is handed off, which bounds the pages
        # held in memory while the consumer is slow
        try:
            try:
                response = await self.ctx.session.get(url)
            except RequestFailed as exc:
                logger.info("Request failed: %s", exc)
                self.failed = True
                self.counts.failed += 1
                self._missed()
                return
            if response is None:
                self.counts.skipped += 1
                self._missed()
            elif (node := self._node(response)) is not None:
                await self._emit_item(_item(node, response, self.ctx.images))
        finally:
            self._limiter.release()

    def _node(self, response: Response) -> dict[str, Any] | None:
        """The page's Product node, counting the page if it has none."""
        if not response.ok:
            # A 404 or 410 is a listed page that no longer exists
            self.failed = self.failed or response.status not in GONE
            self.counts.statuses[response.status] += 1
        else:
            node, _ = jsonld.parse_product_page(
                response.text, response.url, groups=True, fallback=False
            )
            if node is not None:
                return node
            logger.info("No schema.org Product on %s", response.url)
            self.counts.no_product += 1
        self._missed()
        return None

    def _missed(self) -> None:
        if self.emitted:
            return
        self.counts.checked += 1
        self.ctx.no_item_reason = self.reason()
        if self.counts.checked >= HINT_AFTER and self._probe is None and self._tg is not None:
            # Started early, so it can finish before --bail on a slow store
            self._probe = self._tg.create_task(_probe_others(self.ctx))

    async def _emit_item(self, item: dict[str, Any]) -> None:
        if not self.emitted:
            self.emitted = True
            self.ctx.no_item_reason = None
            # A suggestion it already made stays: the store answers like that platform
            if self._probe is not None:
                self._probe.cancel()
        await self._emit(item)

    async def probe_others(self) -> None:
        """Run the hint probe unless it already ran during the pages."""
        if self._probe is None:
            await _probe_others(self.ctx)

    def reason(self) -> str:
        """Why the store has no item: the pages checked and their outcomes."""
        counts = self.counts
        robots = self.ctx.session.stats.robots_disallowed - self._robots_before
        robots = min(robots, counts.skipped)
        outcomes = [f"{n} answered {status}" for status, n in sorted(counts.statuses.items())]
        if counts.failed:
            outcomes.append(f"{counts.failed} failed to respond")
        if robots:
            outcomes.append(f"{robots} disallowed by robots.txt")
        if counts.skipped > robots:
            outcomes.append(f"{counts.skipped - robots} redirected to a page already fetched")
        where = ", ".join(self.listing.roots)
        reason = (
            f"no schema.org Product JSON-LD on {counts.no_product} of {counts.checked} "
            f"product pages checked ({where})"
        )
        return f"{reason}: {', '.join(outcomes)}" if outcomes else reason


async def _probe_others(ctx: StoreContext) -> None:
    """Suggest another platform when the store answers like Shopify or WooCommerce."""
    await ctx.probe_platform("/products.json", _looks_like_shopify, "shopify")
    if ctx.suggested_platform is None:
        await ctx.probe_platform("/wp-json/", _looks_like_woocommerce, "woocommerce")


def _item(node: dict[str, Any], response: Response, images: bool) -> dict[str, Any]:
    node["store"] = urllib.parse.urlsplit(response.url).netloc
    node["platform"] = "jsonld"
    node["html_url"] = response.url.split("#", 1)[0]
    node["image_urls"] = _image_urls(node) if images else []
    return node


def _image_urls(node: dict[str, Any]) -> list[str]:
    """The node's images, then its variants' images not already listed."""
    urls = jsonld.image_urls(node.get("image"))
    for variant in node.get("hasVariant") or []:
        if isinstance(variant, dict):
            urls += [u for u in jsonld.image_urls(variant.get("image")) if u not in urls]
    return urls
