"""BigCommerce Stencil storefronts: the Storefront GraphQL ``site.products`` query.

Stencil themes put the storefront's public API token, a JWT, in the homepage so the
theme's own scripts can query ``/graphql``. BigCommerce issues these tokens for use
in the browser and scopes them to read-only storefront data. The adapter reads the
token from the homepage and sends it as a bearer token with each query, a POST with
a JSON body (``/graphql`` refuses GET). No channel or currency is chosen, so the
store's default is scraped.

Paging follows ``pageInfo.endCursor`` until ``hasNextPage`` is false. A 401 partway
through (tokens expire) reads the homepage once more for a fresh token. A query
complexity error halves the page size, which is safe mid-catalog because cursors
are not offsets.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._magento import _dig, _first_error, _probe_others
from shopify_spy._session import Emit, RequestFailed, RequestKind, StoreContext
from shopify_spy._utils import dicts, normalize_url, origin

__all__ = ["get_api_url", "run", "image_list"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/graphql"
PAGE_SIZE = 50  # BigCommerce's maximum for ``first``
# A prerequisite, not the data: its 200 must not make diagnose() call a store whose
# /graphql then fails ``empty``. Not deduped, so a 401 can fetch it again
HOMEPAGE = RequestKind("homepage", dedupe=False, success=False)

# Three base64url parts; the first two are JSON objects, so they start with "eyJ"
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_STENCIL = re.compile(
    r"""<meta\s+name=["']platform["']\s+content=["']bigcommerce\.stencil["']""", re.I
)
# The documented wording is "The query is too complex as it has a complexity score
# of N out of 10000", but it wasn't seen live, so the match is broad
_COMPLEXITY_ERROR = re.compile(r"complex", re.I)

# Every field was checked live on three stores: an unknown one fails the whole query
_QUERY = " ".join(
    """
query Products($first: Int!, $after: String) { site { products(first: $first, after: $after) {
  pageInfo { hasNextPage endCursor }
  edges { node {
    id entityId sku name path description createdAt { utc }
    brand { name }
    categories(first: 50) { edges { node { name } } }
    prices {
      price { value currencyCode } basePrice { value currencyCode }
      salePrice { value currencyCode } retailPrice { value currencyCode }
      priceRange { min { value currencyCode } max { value currencyCode } }
    }
    inventory { isInStock }
    availabilityV2 { status }
    reviewSummary { numberOfReviews summationOfRatings }
    images(first: 50) { edges { node { urlOriginal altText isDefault } } }
    productOptions(first: 50) { edges { node { entityId displayName isVariantOption } } }
    variants(first: 250) { pageInfo { hasNextPage } edges { node {
      entityId sku isPurchasable
      prices {
        price { value currencyCode } basePrice { value currencyCode }
        salePrice { value currencyCode } retailPrice { value currencyCode }
      }
      inventory { isInStock }
      defaultImage { urlOriginal }
      options(first: 50) { edges { node {
        entityId displayName values(first: 50) { edges { node { entityId label } } }
      } } }
    } } }
  } }
} } }
""".split()
)


class BigCommerceError(Exception):
    """The store's homepage had no Storefront API token, or its ``/graphql``
    answered the first page without products."""


def get_api_url(store_url: str) -> str:
    """The store's Storefront GraphQL endpoint."""
    parsed = normalize_url(store_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", "", ""))


def find_token(html: str, site: str) -> str | None:
    """The page's BigCommerce-issued JWT (``iss`` "BC"), preferring one whose
    ``cors`` names ``site``.

    The claims, not a setting name, pick it out: themes call the setting
    ``graphQLToken``, ``storefrontApiToken`` or ``BC_GraphQL_Token``, and pages can
    carry other JWTs and base64 JSON that also start with "eyJ".
    """
    issued = []
    for match in _JWT.finditer(html):
        claims = _claims(match.group())
        if claims is None or claims.get("iss") != "BC":
            continue
        cors = claims.get("cors")
        if isinstance(cors, list) and site in cors:
            return match.group()
        issued.append(match.group())
    return issued[0] if issued else None


def _claims(jwt: str) -> dict[str, Any] | None:
    payload = jwt.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:  # includes binascii.Error and UnicodeDecodeError
        return None
    return claims if isinstance(claims, dict) else None


def _is_stencil(html: str) -> bool:
    return _STENCIL.search(html) is not None


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store; return whether it got to the end.

    A homepage without a token, or a first page without products, raises
    BigCommerceError, so the store reports ``error`` with the reason rather than
    ``empty``.
    """
    response, token, _ = await _homepage(ctx)
    if response is None or not response.ok:
        return False  # diagnose() reports the homepage's status
    site = origin(response.url)  # past apex-to-www and http-to-https redirects
    if token is None:
        if _is_stencil(response.text):
            reason = " (a Stencil theme that doesn't expose one)"
        else:
            reason = (
                " (not a Stencil storefront: a headless one such as Catalyst, or not BigCommerce)"
            )
            await _probe_others(ctx)
        raise BigCommerceError(f"{site}/: no Storefront API token on the homepage{reason}")
    return await _Pager(ctx, emit, site, token).run()


async def _homepage(
    ctx: StoreContext, site: str | None = None
) -> tuple[Response | None, str | None, str]:
    """The homepage's response, its token, and why there is no token ("" if there is).

    The response is None if robots.txt disallowed it or the request failed. ``site``
    is the origin whose token to prefer, by default the response's own.
    """
    try:
        response = await ctx.session.get(f"{ctx.url}/", kind=HOMEPAGE)
    except RequestFailed as exc:
        return None, None, f"request failed: {exc}"
    if response is None:
        return None, None, "robots.txt"
    if not response.ok:
        return response, None, f"HTTP {response.status}"
    token = find_token(response.text, site or origin(response.url))
    return response, token, "" if token else "no token"


class _Pager:
    """One store's walk through ``site.products``."""

    def __init__(self, ctx: StoreContext, emit: Emit, site: str, token: str) -> None:
        self.ctx = ctx
        self.emit = emit
        self.site = site
        self.api = get_api_url(site)
        self.token = token
        self.first = PAGE_SIZE
        self.cursor: str | None = None
        self.cursors: set[str] = set()
        self.page = 1
        self.resized = False  # the page size was halved on this page
        self.refreshed = False  # the token was refreshed since the last good page

    @property
    def where(self) -> str:
        return f"{self.api} page {self.page}"

    async def run(self) -> bool:
        while True:
            try:
                response = await self.ctx.session.post(
                    self.api, self._body(), headers=self._headers()
                )
            except RequestFailed as exc:
                if self.page == 1:
                    raise  # else diagnose() calls it empty after the homepage's 200
                logger.warning("BigCommerce GraphQL request failed for %s: %s", self.where, exc)
                return False
            if response is None:  # robots.txt
                return False
            done = await self._answer(response)
            if done is not None:
                return done

    def _body(self) -> bytes:
        variables = {"first": self.first, "after": self.cursor}
        return json.dumps({"query": _QUERY, "variables": variables}).encode()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _answer(self, response: Response) -> bool | None:
        """Whether the store got to the end, or None to send the next request."""
        if response.status == 401:
            return await self._unauthorized(response)
        if 300 <= response.status < 400:
            # diagnose() has no redirect status and would call this empty
            location = response.location or "an unknown location"
            return self._failed(f"{self.api}: redirected ({response.status}) to {location}")
        products, problem = _read(response)
        if products is not None:
            return await self._take(products, problem)
        if problem is None:
            return await self._http_failure(response)
        if _COMPLEXITY_ERROR.search(problem) and self.first > 1:
            self.first //= 2
            self.resized = True
            logger.info("BigCommerce query too complex (%s); retrying with %d", problem, self.first)
            return None
        return self._failed(f"{self.api}: {problem}")

    async def _unauthorized(self, response: Response) -> bool | None:
        message = _first_error(response.json_or_none()) or "no GraphQL message"
        rejected = f"{self.api}: Storefront API token rejected (401): {message}"
        if self.refreshed:
            return self._failed(rejected)
        self.refreshed = True
        # No probes and no no-token error here: this store is BigCommerce
        _, token, why = await _homepage(self.ctx, self.site)
        if token is None:
            return self._failed(f"{rejected}; fetching the homepage again gave no token ({why})")
        logger.info("BigCommerce token rejected at %s; retrying with a new one", self.where)
        self.token = token
        return None

    async def _http_failure(self, response: Response) -> bool:
        """Leave an HTTP failure to diagnose(), unless a 2xx was already counted."""
        logger.info("BigCommerce GraphQL returned %d for %s", response.status, self.where)
        if self.page == 1 and self.resized:
            # The complexity error's 2xx counted as a success, so this would read empty
            raise BigCommerceError(f"{self.api}: HTTP {response.status} after a page-size retry")
        if self.page == 1 and response.status == 404:
            await _probe_others(self.ctx)
        return False

    def _failed(self, message: str) -> bool:
        """Fail the store on the first page; after it, stop with what it has."""
        if self.page == 1:
            raise BigCommerceError(message)
        logger.warning("BigCommerce stopped at %s: %s", self.where, message)
        return False

    async def _take(self, products: dict[str, Any], problem: str | None) -> bool | None:
        nodes = [e["node"] for e in dicts(products["edges"]) if isinstance(e.get("node"), dict)]
        if problem is not None:
            if not nodes:
                return self._failed(f"{self.api}: {problem}")
            logger.warning(
                "BigCommerce GraphQL answered with errors for %s: %s", self.where, problem
            )
        logger.info("BigCommerce: %d products from %s", len(nodes), self.where)
        for node in nodes:
            if _dig(node, "variants", "pageInfo", "hasNextPage") is True:
                logger.warning(
                    "BigCommerce product %s: only the first 250 variants", node.get("entityId")
                )
            await self.emit(_item(node, self.site, self.ctx.images))
        self.refreshed = self.resized = False
        if _dig(products, "pageInfo", "hasNextPage") is not True:
            return True
        cursor = _dig(products, "pageInfo", "endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in self.cursors:
            # A server repeating a cursor would otherwise page forever
            logger.warning("BigCommerce: no new endCursor at %s; stopping", self.where)
            return False
        self.cursors.add(cursor)
        self.cursor = cursor
        self.page += 1
        return None


def _read(response: Response) -> tuple[dict[str, Any] | None, str | None]:
    """The answer's ``data.site.products`` (when its ``edges`` is a list) and its first
    GraphQL error, or why there are no products.

    A non-2xx answer gives (None, None), unless it's a 400 or 5xx with a GraphQL
    ``errors`` body, such as a validation error.
    """
    data = response.json_or_none()
    error = _first_error(data)
    if not response.ok:
        graphql_error = response.status == 400 or response.status >= 500
        return None, error if graphql_error else None
    if data is None:
        return None, "not JSON"
    products = _dig(data, "data", "site", "products")
    if isinstance(products, dict) and isinstance(products.get("edges"), list):
        return products, error
    return None, error or "no products in the answer"


def _item(node: dict[str, Any], site: str, images: bool) -> dict[str, Any]:
    path = node.get("path")
    node["store"] = urllib.parse.urlsplit(site).netloc
    node["platform"] = "bigcommerce"
    node["html_url"] = f"{site}{path}" if isinstance(path, str) and path else None
    node["image_urls"] = image_list(node) if images else []
    return node


def image_list(node: dict[str, Any]) -> list[str]:
    """The product's ``images`` ``urlOriginal`` values, the default image first."""
    entries = [
        n
        for e in dicts(_dig(node, "images", "edges"))
        if isinstance(n := e.get("node"), dict)
        and isinstance(n.get("urlOriginal"), str)
        and n["urlOriginal"]
    ]
    entries.sort(key=lambda n: n.get("isDefault") is not True)
    return [n["urlOriginal"] for n in entries]
