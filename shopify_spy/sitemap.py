"""Sitemap parsing for untrusted input.

Sitemaps come from arbitrary servers, so parsing uses defusedxml, decompressed size
is capped, index nesting is limited, and child sitemaps must stay on the store's
host.
"""

from __future__ import annotations

import io
import logging
import urllib.parse
import zlib
from collections.abc import AsyncIterator

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from shopify_spy.fetch import FetchTooLarge, decompress
from shopify_spy.session import RequestFailed, StoreSession

logger = logging.getLogger("shopify_spy")

MAX_SITEMAP_SIZE = 50 * 1024 * 1024  # sitemaps.org limit, uncompressed
MAX_DEPTH = 3


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(body: bytes) -> tuple[str | None, list[str]]:
    """Parse a sitemap document; return (root type, [loc, ...]).

    The root type is "sitemapindex", "urlset" or None if the body isn't a sitemap.
    Gzipped bodies are detected by their magic bytes.
    """
    if body[:2] == b"\x1f\x8b":
        try:
            body = decompress(body, zlib.MAX_WBITS | 16, MAX_SITEMAP_SIZE)
        except (zlib.error, FetchTooLarge) as exc:
            logger.warning("Bad gzipped sitemap: %s", exc)
            return None, []
    if len(body) > MAX_SITEMAP_SIZE:
        logger.warning("Sitemap larger than %d bytes ignored", MAX_SITEMAP_SIZE)
        return None, []
    root_type: str | None = None
    locs: list[str] = []
    try:
        for event, elem in ElementTree.iterparse(io.BytesIO(body), events=("start", "end")):
            name = _local(elem.tag)
            if event == "start" and root_type is None:
                root_type = name if name in ("sitemapindex", "urlset") else ""
            elif event == "end" and name == "loc" and elem.text:
                locs.append(elem.text.strip())
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        logger.warning("Unparseable sitemap: %s", exc)
    return root_type or None, locs


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class SitemapWalk:
    """Async-iterable page URLs from a sitemap, following indexes on the same host.

    The store's host is the sitemap URL's host plus wherever it redirected to.
    ``root_ok`` becomes True once the top-level sitemap has been read.
    """

    def __init__(self, session: StoreSession, sitemap_url: str) -> None:
        self.session = session
        self.sitemap_url = sitemap_url
        self.root_ok = False
        self._hosts = {_host(sitemap_url)}
        self._visited: set[str] = set()

    def __aiter__(self) -> AsyncIterator[str]:
        return self._walk(self.sitemap_url, 0)

    async def _walk(self, url: str, depth: int) -> AsyncIterator[str]:
        if url in self._visited or depth > MAX_DEPTH:
            return
        self._visited.add(url)
        try:
            response = await self.session.get(url)
        except RequestFailed as exc:
            logger.info("Sitemap request failed: %s", exc)
            return
        if response is None or not response.ok:
            return
        root, locs = parse(response.body)
        if depth == 0:
            self._hosts.add(_host(response.url))
            self.root_ok = root is not None
        if root == "sitemapindex":
            for loc in locs:
                if _host(loc) in self._hosts:
                    async for page in self._walk(loc, depth + 1):
                        yield page
                else:
                    logger.info("Skipping off-host child sitemap %s", loc)
        elif root == "urlset":
            for loc in locs:
                yield loc


def iter_locs(session: StoreSession, sitemap_url: str) -> SitemapWalk:
    """Walk a sitemap: ``async for loc in iter_locs(session, url)``."""
    return SitemapWalk(session, sitemap_url)
