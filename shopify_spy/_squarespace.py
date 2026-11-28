"""Squarespace: the product pages its sitemap lists, read for schema.org Product data.

Every Squarespace site serves the same robots.txt, which disallows ``?format=json``
(and ``&format=json``, ``format=page-context``, ``format=json-pretty``) for every
user agent but allows ``/sitemap.xml`` and the product pages. So with robots.txt
obeyed, the default, no ``format=json`` URL is ever requested: the
``/<collection>/p/<slug>`` pages the sitemap lists are read with the ``generic``
platform's reader, and each item is the page's Product node as ``generic`` gives it,
with ``platform="squarespace"`` and ``source="product_page"``.

Sitemap locs that aren't product pages are left out, and so is any loc with a
``format`` query parameter, so the guarantee holds even when robots.txt can't be
read (the session's rules are then permissive).

With robots.txt ignored (``respect_robots_txt=False``), the collections are the
parents of the listed product pages, and each collection's ``?format=json`` pages
are read, following ``pagination.nextPageUrl``. Each Squarespace product record
(one with ``structuredContent.variants``) becomes an item: the record unchanged,
plus ``store``, ``platform``, ``html_url``, ``image_urls`` and
``source="format_json"``. When no collection gives a product, the listed product
pages are read instead, as in the default mode. When some do, each listed product
page whose path no record's ``fullUrl`` matches is then read as in the default mode
(``source="product_page"``), and the store is complete only if every one of those
gives an item or no longer exists.
"""

from __future__ import annotations

import contextlib
import logging
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from shopify_spy import _generic as generic
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._utils import canonicalize_url, dicts, hostname
from shopify_spy._woocommerce import GONE

__all__ = [
    "SquarespaceError",
    "collection_path",
    "image_urls",
    "is_product_url",
    "run",
]

logger = logging.getLogger("shopify_spy")

PRODUCT_SHAPE = "/<collection>/p/<slug>"


class SquarespaceError(Exception):
    """With robots.txt ignored, the sitemap answered but no item came from the
    collections or the product pages; or no sitemap could be found."""


def is_product_url(url: str) -> bool:
    """Whether ``url`` is a ``/<collection>/p/<slug>`` page with no ``format`` query
    parameter."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 3 or segments[-2] != "p":
        return False
    return "format" not in urllib.parse.parse_qs(parts.query, keep_blank_values=True)


def collection_path(url: str) -> str:
    """The collection a product page URL is under: ``/shop`` for ``/shop/p/tee``."""
    segments = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    return "/" + "/".join(segments[:-2])


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit an item for every product found; return whether the listing was read to
    the end and nothing that should have been read was missed."""
    if ctx.session.registry.respect_robots_txt:
        logger.info(
            "Squarespace %s: robots.txt is obeyed and disallows ?format=json; "
            "reading product pages' structured data",
            ctx.url,
        )
        return await generic.read_pages(
            ctx,
            emit,
            platform="squarespace",
            source="product_page",
            keep=is_product_url,
            product_shape=PRODUCT_SHAPE,
        )
    logger.info(
        "Squarespace %s: robots.txt is ignored; reading ?format=json collection pages", ctx.url
    )
    return await _JsonSource(ctx, emit).run()


@dataclass
class _Counts:
    """Collection pages that gave no item, by outcome, and records left out."""

    checked: int = 0  # collection pages requested
    failed: int = 0  # no response after retries
    statuses: Counter[int] = field(default_factory=Counter)
    not_product: int = 0  # records without structuredContent.variants
    unreadable: list[str] = field(default_factory=list)  # 2xx without collection data


class _JsonSource:
    """The collections' ``?format=json`` pages, falling back to the listed product
    pages when none gives a product."""

    def __init__(self, ctx: StoreContext, emit: Emit) -> None:
        self.ctx = ctx
        self._emit = emit
        self.listing = generic._Listing(ctx, keep=is_product_url)
        self.product_urls: list[str] = []
        self.collections: list[str] = []
        self.counts = _Counts()
        self.incomplete = False  # a collection page that should have been read wasn't
        self.emitted = False
        self.emitted_ids: set[str | int] = set()
        self.emitted_paths: set[str] = set()  # the emitted records' fullUrl paths

    async def run(self) -> bool:
        await self._list()
        if not self.listing.roots:
            await generic._probe_others(self.ctx)
            if self.listing.answered:
                raise SquarespaceError(f"no sitemap found at {', '.join(self.listing.tried)}")
            return False  # diagnose() reports the sitemap's 404, 403 or robots.txt
        if not self.collections:
            if (reason := self.listing.no_page_reason(PRODUCT_SHAPE)) is not None:
                await generic._probe_others(self.ctx)
                raise SquarespaceError(reason)
            return self.listing.complete  # an empty urlset: empty
        for path in self.collections:
            await self._read_collection(path)
        if self.emitted:
            return await self._read_missing()
        return await self._fallback()

    async def _list(self) -> None:
        """Drain the listing into product page URLs and their collections, in
        first-seen order."""
        seen: set[str] = set()
        async with contextlib.aclosing(self.listing.locs()) as locs:
            async for loc in locs:
                url = loc.split("#", 1)[0]
                try:
                    key = canonicalize_url(url)
                except ValueError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                self.product_urls.append(url)
                if (path := collection_path(url)) not in self.collections:
                    self.collections.append(path)

    async def _read_collection(self, path: str) -> None:
        """Read a collection's ``?format=json`` pages until one has no next page."""
        url: str | None = f"{self.ctx.url}{path}?format=json"
        while url is not None:
            url = await self._read_page(url)

    async def _read_page(self, url: str) -> str | None:
        """Emit a collection page's products; return the next page's URL, if any."""
        self.counts.checked += 1
        try:
            response = await self.ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Request failed: %s", exc)
            self.counts.failed += 1
            self.incomplete = True
            self._missed()
            return None
        if response is None:  # a page already read: a pagination loop
            self.counts.checked -= 1
            return None
        if not response.ok:
            logger.info("%s answered %d", url, response.status)
            # A 404 or 410 is a collection that no longer exists
            self.incomplete = self.incomplete or response.status not in GONE
            self.counts.statuses[response.status] += 1
            self._missed()
            return None
        data = response.json_or_none()
        if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
            logger.info("%s answered without Squarespace collection data", url)
            self.counts.unreadable.append(url)
            self._missed()
            return None
        before = self.emitted
        for record in data["items"]:
            await self._emit_record(record, url)
        if not before and not self.emitted:
            self._missed()
        return self._next_url(data.get("pagination"))

    def _missed(self) -> None:
        """A collection page without an item: until the first item, say why."""
        if not self.emitted:
            self.ctx.no_item_reason = self.reason()

    async def _emit_record(self, record: Any, url: str) -> None:
        if not _is_product(record):
            logger.debug("Not a Squarespace product record on %s", url)
            self.counts.not_product += 1
            return
        # Before the id check: a product in two collections has a fullUrl in each
        if isinstance(full_url := record.get("fullUrl"), str):
            self.emitted_paths.add(_path_key(full_url))
        key = record.get("id")
        if isinstance(key, (str, int)):
            if key in self.emitted_ids:
                return
            self.emitted_ids.add(key)
        self.emitted = True
        self.ctx.no_item_reason = None
        await self._emit(_item(record, self.ctx))

    def _next_url(self, pagination: Any) -> str | None:
        """The next page's ``?format=json`` URL from a page's ``pagination``; a next
        page that can't be followed leaves the collection incomplete."""
        if not isinstance(pagination, dict) or not pagination.get("nextPage"):
            return None
        next_url = pagination.get("nextPageUrl")
        if (url := _format_json_url(self.ctx.url, next_url)) is None:
            logger.info("Not following a collection's next page on %s: %r", self.ctx.url, next_url)
            self.incomplete = True
        return url

    def reason(self) -> str:
        """Why no collection gave an item: an unreadable page, else the outcomes."""
        counts = self.counts
        if counts.unreadable:
            return f"{counts.unreadable[0]} answered without Squarespace collection data"
        outcomes = [f"{n} answered {status}" for status, n in sorted(counts.statuses.items())]
        if counts.failed:
            outcomes.append(f"{counts.failed} failed to respond")
        if counts.not_product:
            outcomes.append(f"{counts.not_product} records without structuredContent.variants")
        pages = "page" if counts.checked == 1 else "pages"
        reason = (
            f"no Squarespace product from {counts.checked} collection {pages} "
            f"({', '.join(self.collections)})"
        )
        return f"{reason}: {', '.join(outcomes)}" if outcomes else reason

    async def _read_missing(self) -> bool:
        """Read the listed product pages no emitted record's ``fullUrl`` matches;
        return whether the store is complete: every collection page was read and
        every missing page gave an item or no longer exists."""
        listed: dict[str, str] = {}
        for url in self.product_urls:
            listed.setdefault(_path_key(url), url)
        missing = [url for key, url in listed.items() if key not in self.emitted_paths]
        if missing or self.counts.not_product:
            logger.info(
                "Squarespace %s: %d of %d listed product pages not in the ?format=json "
                "collections%s; %d records without structuredContent.variants",
                self.ctx.url,
                len(missing),
                len(listed),
                ", reading them from product pages" if missing else "",
                self.counts.not_product,
            )
        pages_read = True
        if missing:
            pages = generic._Pages(
                self.ctx,
                self._emit,
                self.listing,
                platform="squarespace",
                source="product_page",
                urls=missing,
            )
            # The store's first item came from the collections, so no missed page
            # counts toward PROBE_PAGES or the no-item reason, and no other platform
            # is probed
            pages.emitted = True
            await pages.run()
            pages_read = not pages.failed and not pages.counts.no_product
        return (
            self.listing.complete
            and not self.incomplete
            and not self.counts.unreadable
            and pages_read
        )

    async def _fallback(self) -> bool:
        """Read the listed product pages, as with robots.txt obeyed."""
        json_reason = self.reason()
        logger.info(
            "Squarespace %s: no product from ?format=json (%s); reading product pages instead",
            self.ctx.url,
            json_reason,
        )
        pages = _FallbackPages(
            self.ctx,
            self._emit,
            self.listing,
            platform="squarespace",
            source="product_page",
            urls=self.product_urls,
            json_reason=json_reason,
        )
        await pages.run()
        if pages.emitted:
            return self.listing.complete and not pages.failed
        # Never return False here: the sitemap's 2xx would make diagnose() say empty
        await pages.probe_others()
        raise SquarespaceError(pages.reason())


class _FallbackPages(generic._Pages):
    """The product pages read after the collections, whose reason for having no
    item, also the one the runner reports on --bail, starts with the collections'."""

    def __init__(self, *args: Any, json_reason: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.json_reason = json_reason

    def reason(self) -> str:
        return f"{self.json_reason}; {super().reason()}"


def _format_json_url(base: str, next_url: Any) -> str | None:
    """``next_url`` resolved against ``base`` with ``format=json``, or None if it isn't
    a URL on ``base``'s host."""
    if not isinstance(next_url, str):
        return None
    try:
        url = urllib.parse.urljoin(base, next_url)
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if hostname(url) != hostname(base):
        return None
    if "format" in urllib.parse.parse_qs(parts.query, keep_blank_values=True):
        return url
    query = f"{parts.query}&format=json" if parts.query else "format=json"
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _is_product(record: Any) -> bool:
    """Whether a collection record is a product: it has ``structuredContent.variants``."""
    if not isinstance(record, dict):
        return False
    content = record.get("structuredContent")
    return isinstance(content, dict) and isinstance(content.get("variants"), list)


def _path_key(url: str) -> str:
    """A URL's path, unquoted and without a trailing slash, to compare sitemap locs
    with records' ``fullUrl``."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return url
    return urllib.parse.unquote(path).rstrip("/")


def _item(record: dict[str, Any], ctx: StoreContext) -> dict[str, Any]:
    full_url = record.get("fullUrl")
    html_url = None
    if isinstance(full_url, str):
        with contextlib.suppress(ValueError):
            html_url = urllib.parse.urljoin(ctx.url, full_url)
    return record | {
        "store": urllib.parse.urlsplit(ctx.url).netloc,
        "platform": "squarespace",
        "html_url": html_url,
        "image_urls": image_urls(record) if ctx.images else [],
        "source": "format_json",
    }


def image_urls(record: dict[str, Any], *, variants: bool = True) -> list[str]:
    """The record's images (``items``, else ``assetUrl``), then, with ``variants``, its
    variants' ``mainImage`` not already listed."""
    images = record.get("items")
    urls = [
        image["assetUrl"]
        for image in (images if isinstance(images, list) else [])
        if isinstance(image, dict) and isinstance(image.get("assetUrl"), str)
    ]
    if not urls and isinstance(record.get("assetUrl"), str):
        urls = [record["assetUrl"]]
    content = record.get("structuredContent")
    for variant in dicts(content.get("variants")) if variants and isinstance(content, dict) else []:
        image = variant.get("mainImage")
        url = image.get("assetUrl") if isinstance(image, dict) else None
        if isinstance(url, str) and url not in urls:
            urls.append(url)
    return urls
