"""The generic platform: schema.org Product JSON-LD (or microdata) on the product
pages that the store's sitemaps list.

A store whose pages have no Product data (it renders products in the browser, say)
raises GenericError after ``PROBE_PAGES`` pages, so it reports ``error`` with the
reason rather than ``empty``; ``ctx.no_item_reason`` holds that reason for the
runner in case ``--bail`` stops the store first.

``read_pages`` is the same flow for another platform's adapter: it can read only the
pages a ``keep`` test accepts and label the items with that platform and a ``source``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from shopify_spy import _jsonld as jsonld
from shopify_spy import _magento as magento
from shopify_spy import _sitemap as sitemap
from shopify_spy._fetch import Response
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._utils import canonicalize_url, hostname
from shopify_spy._woocommerce import FETCH_CONCURRENCY, GONE, _site

__all__ = ["GenericError", "read_pages", "run"]

logger = logging.getLogger("shopify_spy")

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/xmlsitemap.php")
PROBE_PAGES = 50  # pages back without an item, before the first, that end the store
HINT_AFTER = 5  # pages back without an item, before the first, that start the hint probe
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


class GenericError(Exception):
    """The store's sitemaps listed pages, but none gave a schema.org Product; no
    sitemap could be found; or the sitemaps to read for pages couldn't be read."""


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit an item for every listed page with schema.org Product data; return
    whether the listing was read to the end and every page was fetched."""
    return await read_pages(ctx, emit)


async def read_pages(
    ctx: StoreContext,
    emit: Emit,
    *,
    platform: str = "generic",
    source: str | None = None,
    keep: Callable[[str], bool] | None = None,
    product_shape: str | None = None,
) -> bool:
    """``run`` for any platform: items get ``platform`` and, when given, ``source``.

    With ``keep``, only the listed pages it accepts are fetched, and a sitemap that
    lists pages but none it accepts raises GenericError naming ``product_shape``,
    the URL shape ``keep`` looks for (a filter that misses the store's product
    URLs would otherwise look like an empty store). Every page ``keep`` accepts is
    a product page, so one without a Product makes the store incomplete.
    """
    if keep is not None and product_shape is None:
        raise ValueError("read_pages needs product_shape with keep")
    listing = _Listing(ctx, keep)
    pages = _Pages(
        ctx, emit, listing, platform=platform, source=source, products_only=keep is not None
    )
    await pages.run()
    if pages.emitted:
        return listing.complete and not pages.failed
    if not listing.roots:
        await pages.probe_others()
        if listing.answered:
            raise GenericError(f"no sitemap found at {', '.join(listing.tried)}")
        return False  # diagnose() reports the sitemaps' 404, 403 or robots.txt
    if pages.counts.checked == 0:
        if (reason := listing.no_page_reason(product_shape)) is not None:
            await pages.probe_others()
            raise GenericError(reason)
        return listing.complete  # no on-site page listed: empty
    await pages.probe_others()
    raise GenericError(f"{pages.reason()}; {pages.stopped_by()}")


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
    candidate answered 2xx; ``complete`` becomes True once every sitemap chosen for
    pages was readable and no off-site URL among them was left out; ``unread``
    lists those that weren't readable; ``filtered`` counts the page locs that
    ``keep`` turned down.
    """

    def __init__(self, ctx: StoreContext, keep: Callable[[str], bool] | None = None) -> None:
        self.ctx = ctx
        self.keep = keep
        self.filtered = 0
        self.sites = {_site(hostname(ctx.url))}
        self.roots: list[str] = []
        self.tried: list[str] = []
        self.answered = False
        self.complete = False
        self._dropped = 0
        self.unread: list[str] = []  # chosen sitemaps that couldn't be read

    def no_page_reason(self, product_shape: str | None) -> str | None:
        """Why the sitemaps that answered gave no page to read, once drained: some
        couldn't be read, or ``keep`` turned every page down; None if neither (the
        store is empty).

        Callers raise it rather than return False, which diagnose() would report
        as empty: the sitemap's 2xx is a success.
        """
        if self.unread:
            return f"no page listed; unreadable sitemaps: {', '.join(self.unread)}"
        if self.filtered:
            return (
                f"no product page ({product_shape}) among the {self.filtered} pages "
                f"listed ({', '.join(self.roots)})"
            )
        return None

    def _on_site(self, url: str) -> bool:
        if _site(hostname(url)) in self.sites:
            return True
        self._dropped += 1
        return False

    def _wanted(self, url: str) -> bool:
        """Whether to yield a page loc: ``keep`` accepts it and it's on the site."""
        # keep is tested first, so an off-site page that wouldn't be read anyway
        # doesn't make the listing incomplete
        if self.keep is not None and not self.keep(url):
            self.filtered += 1
            return False
        return self._on_site(url)

    async def locs(self) -> AsyncIterator[str]:
        found, missing = await self._top_level()
        read = {doc.url: doc for doc in found if doc.root == "urlset"}
        # Products are chosen before the off-site and unread ones are counted, so a
        # sitemap that wouldn't have been read doesn't make the listing incomplete
        for source in _prefer_products(self._sources(found) + missing):
            if source in missing:
                self.unread.append(source)
            elif self._on_site(source):
                async for loc in self._source_locs(source, read):
                    if self._wanted(loc):
                        yield loc
        if self._dropped:
            logger.info("Sitemaps listed %d off-site links", self._dropped)
        self.complete = bool(found) and not self.unread and not self._dropped

    def _sources(self, found: list[_Sitemap]) -> list[str]:
        """The sitemaps to choose product sitemaps from: the top-level urlsets and
        the indexes' children, together."""
        sources: list[str] = []
        for doc in found:
            sources += [doc.url] if doc.root == "urlset" else doc.locs
        return sources

    async def _source_locs(self, source: str, read: dict[str, _Sitemap]) -> AsyncIterator[str]:
        """A sitemap's locs, from ``read`` if it was already fetched."""
        if source in read:
            for loc in read[source].locs:
                yield loc
            return
        walk = sitemap.iter_locs(self.ctx.session, source)
        async for loc in walk:
            self.sites |= {_site(host) for host in walk.hosts}
            yield loc
        if not walk.root_ok:
            self.unread.append(source)

    async def _top_level(self) -> tuple[list[_Sitemap], list[str]]:
        """The robots.txt-listed sitemaps that parse, else the first well-known one,
        and the robots.txt-listed ones that didn't."""
        listed = await self._robots_sitemaps()
        found: list[_Sitemap] = []
        unread: list[str] = []
        for url in listed:
            if (doc := await self._read(url)) is None:
                unread.append(url)
            else:
                found.append(doc)
        if found:
            if unread:
                logger.info("Some robots.txt sitemaps were unreadable for %s", self.ctx.url)
            return found, unread
        for path in SITEMAP_PATHS:
            if (doc := await self._read(f"{self.ctx.url}{path}")) is not None:
                return [doc], unread
        return [], unread

    async def _robots_sitemaps(self) -> list[str]:
        """The on-site ``Sitemap:`` URLs in robots.txt, in order, without repeats."""
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
        return listed

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
    pages without an item before the first.

    Given ``urls``, it reads those instead of the listing's locs (a caller that
    already drained the listing); the listing then only names the sitemaps in
    ``reason()``.

    With ``products_only``, every page read is a product page, so one that answers
    without a Product is a missed product. Otherwise it may be one of the other
    pages a generic store's sitemaps list (categories, blog posts), and isn't.
    """

    def __init__(
        self,
        ctx: StoreContext,
        emit: Emit,
        listing: _Listing,
        *,
        platform: str = "generic",
        source: str | None = None,
        urls: list[str] | None = None,
        products_only: bool = False,
    ) -> None:
        self.ctx = ctx
        self.products_only = products_only
        self._emit = emit
        self.listing = listing
        self.urls = urls
        self.platform = platform
        self.source = source
        self.counts = _Counts()
        self.emitted = False
        self.failed = False  # a page that should have been read wasn't
        self.capped = False  # stopped at PROBE_PAGES rather than the listing's end
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
            contextlib.aclosing(self._locs()) as locs,
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

    async def _locs(self) -> AsyncIterator[str]:
        if self.urls is None:
            async with contextlib.aclosing(self.listing.locs()) as locs:
                async for url in locs:
                    yield url
        else:
            for url in self.urls:
                yield url

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
            self.capped = True
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
                item = _item(node, response, self.ctx.images, self.platform, self.source)
                await self._emit_item(item)
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
            self.failed = self.failed or self.products_only
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

    def stopped_by(self) -> str:
        """Which limit ended the pages, for a store without an item (``--bail``'s
        is added by the runner)."""
        if self.capped:
            return f"stopped at the {PROBE_PAGES}-page limit for pages without a Product"
        return "no more pages listed"

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
            f"no schema.org Product JSON-LD or microdata on {counts.no_product} of "
            f"{counts.checked} product pages checked ({where})"
        )
        return f"{reason}: {', '.join(outcomes)}" if outcomes else reason


async def _probe_others(ctx: StoreContext) -> None:
    """Suggest a platform with its own adapter when the store answers like Shopify,
    WooCommerce or Magento (one request each, while none has matched)."""
    await magento.probe_others(ctx)
    if ctx.suggested_platform is None:
        await ctx.probe_platform(magento.STORE_CONFIG_PATH, magento.looks_like_magento, "magento")


def _item(
    node: dict[str, Any],
    response: Response,
    images: bool,
    platform: str = "generic",
    source: str | None = None,
) -> dict[str, Any]:
    node["store"] = urllib.parse.urlsplit(response.url).netloc
    node["platform"] = platform
    node["html_url"] = response.url.split("#", 1)[0]
    node["image_urls"] = jsonld.product_image_urls(node, response.url) if images else []
    if source is not None:
        node["source"] = source
    return node
