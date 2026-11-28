"""Magento 2.4: the anonymous storefront GraphQL ``products`` query at ``/graphql``.

The query goes in the URL of a GET (``?query=...``), with the page size and page
number written into its text, so each page has its own URL and the session's dedupe,
robots.txt, retries and redirects apply as for any other request. No ``Store``
header is sent, so the default store view and its currency are scraped.

Paging ends when ``currentPage * pageSize`` reaches ``total_count`` or a page comes
back empty. A page-size limit named in a GraphQL error on the first page shrinks the
page size; later pages keep it, since changing it would shift every later offset.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._session import Emit, RequestFailed, StoreContext
from shopify_spy._shopify import _looks_like_woocommerce
from shopify_spy._utils import dicts, normalize_url, origin
from shopify_spy._woocommerce import _looks_like_shopify

__all__ = ["get_api_url", "run"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/graphql"
PAGE_SIZE = 100

# Only fields every 2.4.x release from 2.4.1 (which added uid) has on
# ProductInterface: an unknown field fails the whole query. %d slots, not
# str.format, because of GraphQL's braces
_QUERY = " ".join(
    """
{ products(search: "", pageSize: %d, currentPage: %d) {
    total_count
    page_info { current_page page_size total_pages }
    items {
      __typename uid sku name url_key url_suffix stock_status
      description { html } short_description { html }
      categories { name }
      price_range {
        minimum_price { regular_price { value currency } final_price { value currency } }
        maximum_price { regular_price { value currency } final_price { value currency } }
      }
      media_gallery { url label position disabled }
      ... on ConfigurableProduct {
        configurable_options { attribute_code label values { value_index label } }
        variants {
          attributes { code label value_index }
          product {
            uid sku name stock_status image { url }
            price_range { minimum_price {
              regular_price { value currency } final_price { value currency } } }
          }
        }
      }
    }
} }
""".split()
)

_PAGE_SIZE_ERROR = re.compile(r"page ?size", re.I)
# "currentPage value 5 specified is greater than the 4 page(s) available."
_PAST_END_ERROR = re.compile(r"currentPage.*greater than", re.I)


class MagentoError(Exception):
    """The store's ``/graphql`` answered the first page without products."""


def get_api_url(store_url: str, page: int = 1, page_size: int = PAGE_SIZE) -> str:
    """Build the GraphQL GET URL for the given store, page and page size."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"query": _QUERY % (page_size, page)})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store; return whether it got to the end.

    A first page without products raises MagentoError, so the store reports
    ``error`` with Magento's message rather than ``empty``.
    """
    base = ctx.url
    page, size = 1, PAGE_SIZE
    seen = _Seen()
    while (response := await _get(ctx, get_api_url(base, page, size))) is not None:
        where = f"{_endpoint(response.url)} page {page}"
        products, problem = _read(response)
        if products is None:
            smaller = _smaller_page_size(page, size, problem)
            if smaller is None:
                return await _failed(ctx, response, where, page, problem, size != PAGE_SIZE)
            logger.info("Magento page size limit (%s); retrying with %d", problem, smaller)
            size = smaller
            continue
        if problem is not None:
            if not dicts(products["items"]):
                # Resolver errors null out the items they hit, so this is a failed page
                return await _failed(ctx, response, where, page, problem)
            logger.warning("Magento GraphQL answered with errors for %s: %s", where, problem)
        done = await _take_page(ctx, emit, response, products, seen, where, page * size)
        if done is not None:
            return done
        page += 1
        # Past a redirect (http to https, apex to www), not through it on every page
        base = origin(response.url)
    if page == 1 and ctx.session.stats.robots_disallowed:
        await _note_query_disallowed(ctx, get_api_url(base, page, size))
    if page == 1 and size != PAGE_SIZE:
        raise MagentoError(
            f"{_endpoint(get_api_url(base))}: request failed after a page-size retry"
        )
    return False


async def _get(ctx: StoreContext, url: str) -> Response | None:
    """The response, or None if the request failed or was skipped (robots.txt)."""
    try:
        return await ctx.session.get(url)
    except RequestFailed as exc:
        logger.info("Magento GraphQL request failed: %s", exc)
        return None


async def _note_query_disallowed(ctx: StoreContext, url: str) -> None:
    """Record that robots.txt disallows the query URL but not ``/graphql`` itself,
    as ``Disallow: /*?`` does, for the store's hint."""
    session = ctx.session
    rules = await session.registry.robots(url, session)
    agent = session.robots_agent
    if not rules.allowed(url, agent) and rules.allowed(_endpoint(url), agent):
        ctx.query_disallowed = True


async def _take_page(
    ctx: StoreContext,
    emit: Emit,
    response: Response,
    products: dict[str, Any],
    seen: _Seen,
    where: str,
    read: int,
) -> bool | None:
    """Emit a page's new products; return whether the store got to the end, or None
    to read the next page. ``read`` is ``currentPage * pageSize``."""
    total = products.get("total_count")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    batch = dicts(products["items"])
    if not batch:
        return seen.finish(total, where)
    logger.info("Magento: %d products from %s", len(batch), where)
    fresh = seen.fresh(batch)
    if fresh is None:
        # A server ignoring currentPage would otherwise repeat this page forever
        logger.warning("Page repeated earlier products; stopping at %s", where)
        return False
    for product in fresh:
        await emit(_item(product, response, ctx.images))
    if total is not None and read >= total:
        return seen.finish(total, where)
    return None


async def _failed(
    ctx: StoreContext,
    response: Response,
    where: str,
    page: int,
    problem: str | None,
    resized: bool = False,
) -> bool:
    """Whether the store got to the end, given an answer without products.

    Raises MagentoError for a first page that isn't an HTTP failure, which
    diagnose() would otherwise call ``empty``. So does an HTTP failure after a
    ``resized`` first page: the page-size error's 200 already counted as a success.
    """
    if problem is None:  # an HTTP failure for diagnose() to report
        logger.info("Magento GraphQL returned %d for %s", response.status, where)
        if page == 1 and resized:
            raise MagentoError(
                f"{_endpoint(response.url)}: HTTP {response.status} after a page-size retry"
            )
        if page == 1 and response.status == 404:
            await _probe_others(ctx)
        return False
    if _PAST_END_ERROR.search(problem):
        return True
    if page > 1:
        logger.warning("Magento GraphQL failed for %s: %s", where, problem)
        return False
    await _probe_others(ctx)
    raise MagentoError(f"{_endpoint(response.url)}: {problem}")


def _endpoint(url: str) -> str:
    """A GraphQL URL without its query string, which is most of it."""
    return url.split("?", 1)[0]


class _Seen:
    """Products emitted so far, by ``uid`` (or ``sku``), and whether any repeated."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.unkeyed = 0
        self.repeats = False

    def fresh(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """The products not emitted before; None if every one was."""
        keys = [_key(p) for p in batch]
        if all(k is not None and k in self.keys for k in keys):
            return None
        fresh = []
        for key, product in zip(keys, batch, strict=True):
            if key is None:
                self.unkeyed += 1
            elif key in self.keys:
                self.repeats = True
                continue
            else:
                self.keys.add(key)
            fresh.append(product)
        return fresh

    def finish(self, total: int | None, where: str) -> bool:
        """Whether the store got to the end, now that paging has.

        Fewer products than ``total_count`` can be ones the storefront hides (such
        as out-of-stock ones); with a repeat, the page order shifted between pages,
        so products were likely missed.
        """
        unique = len(self.keys) + self.unkeyed
        if total is None or unique >= total:
            return True
        logger.warning(
            "Magento: %d products, fewer than total_count %d, from %s%s",
            unique,
            total,
            where,
            "; some repeated, so the page order shifted" if self.repeats else "",
        )
        return not self.repeats


def _key(product: dict[str, Any]) -> str | None:
    for field in ("uid", "sku"):
        value = product.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value != "":
            return f"{field}:{value}"
    return None


def _read(response: Response) -> tuple[dict[str, Any] | None, str | None]:
    """The answer's ``data.products`` (when its ``items`` is a list) and its first
    GraphQL error, or why there are no products.

    A non-2xx answer gives (None, None), unless it's a 400 or 5xx with a GraphQL
    ``errors`` body: Magento answers 500 for errors raised outside resolvers, such
    as query complexity and depth limits.
    """
    data = response.json_or_none()
    error = _first_error(data)
    if not response.ok:
        graphql_error = response.status == 400 or response.status >= 500
        return None, error if graphql_error else None
    if data is None:
        return None, "not JSON"
    products = _dig(data, "data", "products")
    if isinstance(products, dict) and isinstance(products.get("items"), list):
        return products, error
    return None, error or "no products in the answer"


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _first_error(data: Any) -> str | None:
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    message = first.get("message") if isinstance(first, dict) else None
    return message if isinstance(message, str) and message else str(first)


def _smaller_page_size(page: int, size: int, problem: str | None) -> int | None:
    """The page size to retry the first page with after a page-size error: the
    largest number in the error below ``size`` (a message can echo the size asked
    for beside the limit), else half, at least 1.

    None when this isn't one, or it's too late: changing the page size after the
    first page would shift every later offset.
    """
    if problem is None or page > 1 or size <= 1 or not _PAGE_SIZE_ERROR.search(problem):
        return None
    named = max((n for n in map(int, re.findall(r"\d+", problem)) if n < size), default=None)
    return max(1, size // 2 if named is None else named)


async def _probe_others(ctx: StoreContext) -> None:
    """Suggest another platform when a Magento or BigCommerce run's store answers like
    Shopify or WooCommerce (``/graphql`` is also WPGraphQL's default endpoint)."""
    await ctx.probe_platform("/products.json", _looks_like_shopify, "shopify")
    if ctx.suggested_platform is None:
        await ctx.probe_platform("/wp-json/", _looks_like_woocommerce, "woocommerce")


def _item(product: dict[str, Any], response: Response, images: bool) -> dict[str, Any]:
    site = origin(response.url)
    url_key, suffix = product.get("url_key"), product.get("url_suffix")
    product["store"] = urllib.parse.urlsplit(response.url).netloc
    product["platform"] = "magento"
    product["html_url"] = (
        f"{site}/{url_key}{suffix if isinstance(suffix, str) else ''}"
        if isinstance(url_key, str) and url_key
        else None
    )
    product["image_urls"] = gallery_urls(product) if images else []
    return product


def gallery_urls(product: dict[str, Any]) -> list[str]:
    """The product's ``media_gallery`` URLs ordered by position, without disabled ones."""
    entries = [
        e
        for e in dicts(product.get("media_gallery"))
        if not e.get("disabled") and isinstance(e.get("url"), str) and e["url"]
    ]
    entries.sort(key=_position)
    return [e["url"] for e in entries]


def _position(entry: dict[str, Any]) -> float:
    position = entry.get("position")
    if isinstance(position, (int, float)) and not isinstance(position, bool):
        return position
    return float("inf")
