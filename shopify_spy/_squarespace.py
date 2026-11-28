"""Squarespace: the product pages its sitemap lists, read for schema.org Product data.

Every Squarespace site serves the same robots.txt, which disallows ``?format=json``
(and ``&format=json``, ``format=page-context``, ``format=json-pretty``) for every
user agent but allows ``/sitemap.xml`` and the product pages. So with robots.txt
obeyed, the default, no ``format=json`` URL is ever requested: the
``/<collection>/p/<slug>`` pages the sitemap lists are read with the ``jsonld``
platform's reader, and each item is the page's Product node as ``jsonld`` gives it,
with ``platform="squarespace"`` and ``source="product_page"``.

Sitemap locs that aren't product pages are left out, and so is any loc with a
``format`` query parameter, so the guarantee holds even when robots.txt can't be
read (the session's rules are then permissive).
"""

from __future__ import annotations

import logging
import urllib.parse

from shopify_spy import _generic as generic
from shopify_spy._session import Emit, StoreContext

__all__ = ["PRODUCT_SHAPE", "is_product_url", "run"]

logger = logging.getLogger("shopify_spy")

PRODUCT_SHAPE = "/<collection>/p/<slug>"


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


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit an item for every listed product page with schema.org Product data;
    return whether the listing was read to the end and every page was fetched."""
    if ctx.session.registry.respect_robots_txt:
        logger.info(
            "Squarespace %s: robots.txt is obeyed and disallows ?format=json; "
            "reading product pages' structured data",
            ctx.url,
        )
    else:
        # TODO(?format=json collection reader not built yet): read collections here
        logger.info(
            "Squarespace %s: robots.txt is ignored; reading product pages' structured data",
            ctx.url,
        )
    return await _product_pages(ctx, emit)


async def _product_pages(ctx: StoreContext, emit: Emit) -> bool:
    return await generic.read_pages(
        ctx,
        emit,
        platform="squarespace",
        source="product_page",
        keep=is_product_url,
        product_shape=PRODUCT_SHAPE,
    )
