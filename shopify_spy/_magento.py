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
_QUERY = """
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
"""
QUERY = " ".join(_QUERY.split())

_PAGE_SIZE_ERROR = re.compile(r"page ?size", re.I)
# "currentPage value 5 specified is greater than the 4 page(s) available."
_PAST_END_ERROR = re.compile(r"currentPage.*greater than", re.I)


class MagentoError(Exception):
    """The store's ``/graphql`` answered the first page without products."""


def get_api_url(store_url: str, page: int = 1, page_size: int = PAGE_SIZE) -> str:
    """Build the GraphQL GET URL for the given store, page and page size."""
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode({"query": QUERY % (page_size, page)})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store; return whether it got to the end.

    A first page without products raises MagentoError, so the store reports
    ``error`` with Magento's message rather than ``empty``.
    """
    base = ctx.url
    page, size = 1, PAGE_SIZE
    seen = _Seen()
    while True:
        try:
            response = await ctx.session.get(get_api_url(base, page, size))
        except RequestFailed as exc:
            logger.info("Magento GraphQL request failed: %s", exc)
            return False
        if response is None:
            return False
        products, problem = _read(response)
        if products is None:
            if problem is None:  # an HTTP failure for diagnose() to report
                logger.info("Magento GraphQL returned %d for %s", response.status, response.url)
                if page == 1 and response.status == 404:
                    await _probe_others(ctx)
                return False
            if page == 1 and size > 1 and _PAGE_SIZE_ERROR.search(problem):
                size = _smaller_page_size(size, problem)
                logger.info("Magento page size limit (%s); retrying with %d", problem, size)
                continue
            if _PAST_END_ERROR.search(problem):
                return True
            if page > 1:
                logger.warning("Magento GraphQL failed for %s: %s", response.url, problem)
                return False
            await _probe_others(ctx)
            raise MagentoError(f"{response.url}: {problem}")
        if problem is not None:
            logger.warning("Magento GraphQL answered with errors for %s: %s", response.url, problem)
        total = products.get("total_count")
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        batch = dicts(products["items"])
        if not batch:
            return seen.finish(total, response.url)
        logger.info("Magento: %d products from %s", len(batch), response.url)
        fresh = seen.fresh(batch)
        if fresh is None:
            # A server ignoring currentPage would otherwise repeat this page forever
            logger.warning("Page repeated earlier products; stopping at %s", response.url)
            return False
        for product in fresh:
            await emit(_item(product, response, ctx.images))
        if total is not None and page * size >= total:
            return seen.finish(total, response.url)
        page += 1
        # Past a redirect (http to https, apex to www), not through it on every page
        base = origin(response.url)


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

    def finish(self, total: int | None, url: str) -> bool:
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
            url,
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


def _smaller_page_size(size: int, message: str) -> int:
    """The largest number in the error if it's below ``size``, else half; at least 1."""
    named = max((int(n) for n in re.findall(r"\d+", message)), default=None)
    smaller = named if named is not None and named < size else size // 2
    return max(1, smaller)


async def _probe_others(ctx: StoreContext) -> None:
    """Suggest Shopify or WooCommerce when the store answers like one (``/graphql``
    is also WPGraphQL's default endpoint)."""
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
