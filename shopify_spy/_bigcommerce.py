"""BigCommerce Stencil storefronts: the Storefront GraphQL ``site.products`` query.

Stencil themes put the storefront's public API token, a JWT, in the homepage so the
theme's own scripts can query ``/graphql``. BigCommerce issues these tokens for use
in the browser and scopes them to read-only storefront data. The adapter reads the
token from the homepage and sends it as a bearer token with each query, a POST with
a JSON body (``/graphql`` refuses GET). No channel or currency is chosen, so the
store's default is scraped.

A headless storefront's page can name its real endpoint, a
``store-<hash>.mybigcommerce.com/graphql`` URL, which is queried instead of the
store's own ``/graphql``; product URLs stay on the store's domain.

Paging follows ``pageInfo.endCursor`` until ``hasNextPage`` is false, and a product
with more variants than one page holds pages its ``variants`` the same way. A 401
partway through (tokens expire) reads the homepage once more for a fresh token. A
query complexity error halves the page size, which is safe mid-catalog because
cursors are not offsets.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
from typing import Any

from shopify_spy._fetch import Response
from shopify_spy._magento import _dig, _first_error, _probe_others
from shopify_spy._session import Emit, RequestFailed, RequestKind, StoreContext
from shopify_spy._utils import dicts, normalize_url, origin

__all__ = ["get_api_url", "run", "image_list", "nodes"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/graphql"
PAGE_SIZE = 50  # BigCommerce's maximum for ``first``
# A prerequisite, not the data: its 200 must not make diagnose() call a store whose
# /graphql then fails ``empty``. Not deduped, so a 401 can fetch it again
HOMEPAGE = RequestKind("homepage", dedupe=False, success=False)
EXPIRED = "only expired tokens"

# Three base64url parts; the first two are JSON objects, so they start with "eyJ"
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_STENCIL = re.compile(
    r"""<meta\s+name=["']platform["']\s+content=["']bigcommerce\.stencil["']""", re.I
)
# The documented wording is "The query is too complex as it has a complexity score
# of N out of 10000", but it wasn't seen live, so the match is broad
_COMPLEXITY_ERROR = re.compile(r"complex", re.I)

# A headless storefront's endpoint, as a page's config names it (slashes maybe escaped)
_ENDPOINT = re.compile(r"https:\\?/\\?/(store-[a-z0-9]+)\.mybigcommerce\.com\\?/graphql", re.I)
# The setting a value is assigned to, from the text before it: ``NAME:"``,
# ``"NAME": "``, ``NAME = '`` or an escaped ``\"NAME\":\"``
_SETTING = re.compile(r"""([A-Za-z_$][\w$]*)\\*["']?\s*[:=]\s*\\*["']$""")
# The ``sub`` of the token Stencil issues for the theme (``storefront_api.token``);
# apps and merchant scripts get tokens whose ``sub`` is their client ID
STENCIL_SUB = "BC"
VARIANT_PAGE_SIZE = 250  # BigCommerce's maximum for ``variants(first:)``
# Far above BigCommerce's per-product variant limit, so only a server that keeps
# answering hasNextPage reaches it
MAX_VARIANTS = 2500

_VARIANT_FIELDS = """
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
"""

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
    variants(first: 250) { pageInfo { hasNextPage endCursor } edges { node { VARIANT } } }
  } }
} } }
""".replace("VARIANT", _VARIANT_FIELDS).split()
)
_VARIANTS_QUERY = " ".join(
    """
query Variants($id: Int!, $first: Int!, $after: String) { site { product(entityId: $id) {
  variants(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor } edges { node { VARIANT } }
  }
} } }
""".replace("VARIANT", _VARIANT_FIELDS).split()
)


class BigCommerceError(Exception):
    """The store's homepage had no Storefront API token, or its ``/graphql``
    answered the first page without products."""


def get_api_url(store_url: str) -> str:
    """The store's Storefront GraphQL endpoint."""
    parsed = normalize_url(store_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, API_PATH, "", "", ""))


def find_token(html: str, site: str, now: float | None = None) -> str | None:
    """The page's best unexpired BigCommerce-issued JWT (``iss`` "BC"), or None.

    The claims, not a setting name, pick it out: themes call the setting
    ``graphQLToken``, ``storefrontApiToken`` or ``BC_GraphQL_Token``, and pages can
    carry other JWTs and base64 JSON that also start with "eyJ". See ``_tokens`` for
    the order.
    """
    tokens, _ = _tokens(html, site, time.time() if now is None else now)
    return tokens[0] if tokens else None


def _tokens(html: str, site: str, now: float) -> tuple[list[str], int]:
    """The page's unexpired BigCommerce tokens, best first, and how many expired.

    One whose ``cors`` names ``site`` comes first, then Stencil's own (``sub`` "BC")
    over an app's or a merchant script's, which can be scoped differently; ties keep
    page order. A token past its ``eat`` (seconds) is left out: themes can hard-code
    one that has long expired beside the one Stencil renders.
    """
    ranked: dict[str, tuple[bool, bool]] = {}
    expired: set[str] = set()
    for match in _JWT.finditer(html):
        jwt = match.group()
        claims = _claims(jwt)
        if jwt in ranked or claims is None or claims.get("iss") != "BC":
            continue
        eat = claims.get("eat")
        if isinstance(eat, (int, float)) and not isinstance(eat, bool) and eat <= now:
            expired.add(jwt)
            continue
        cors = claims.get("cors")
        named = isinstance(cors, list) and site in cors
        ranked[jwt] = (not named, claims.get("sub") != STENCIL_SUB)
    return sorted(ranked, key=ranked.__getitem__), len(expired)


def find_endpoint(html: str, token: str) -> str | None:
    """The ``store-<hash>.mybigcommerce.com/graphql`` endpoint a headless page names.

    With several (one per region, say), the one whose setting name ends like the
    token's (``BC_GRAPHQL_AU`` for ``BIGCOMMERCE_ACCESS_TOKEN_AU``), else the first.
    """
    endpoints: dict[str, set[str]] = {}
    for match in _ENDPOINT.finditer(html):
        url = f"https://{match.group(1).lower()}.mybigcommerce.com{API_PATH}"
        endpoints.setdefault(url, set()).add(_setting_suffix(html, match.start()))
    if not endpoints:
        return None
    suffixes = {_setting_suffix(html, m.start()) for m in re.finditer(re.escape(token), html)}
    suffixes.discard("")
    paired = [url for url, ends in endpoints.items() if ends & suffixes]
    chosen = (paired or list(endpoints))[0]
    if len(endpoints) > 1:
        how = "paired with the token" if paired else "the first"
        logger.info(
            "BigCommerce: the page names %d GraphQL endpoints; using %s (%s)",
            len(endpoints),
            chosen,
            how,
        )
    return chosen


def _setting_suffix(html: str, start: int) -> str:
    """The last ``_`` part of the setting name a value at ``start`` is assigned to,
    lowercased, or "" without a name of two or more parts."""
    match = _SETTING.search(html, max(0, start - 100), start)
    parts = match.group(1).split("_") if match else []
    return parts[-1].lower() if len(parts) > 1 else ""


def _claims(jwt: str) -> dict[str, Any] | None:
    payload = jwt.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:  # includes binascii.Error and UnicodeDecodeError
        return None
    return claims if isinstance(claims, dict) else None


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store; return whether it got to the end.

    A homepage without a token, or a first page without products, raises
    BigCommerceError, so the store reports ``error`` with the reason rather than
    ``empty``.
    """
    response, token, why = await _homepage(ctx)
    if response is None or not response.ok:
        return False  # diagnose() reports the homepage's status
    site = origin(response.url)  # past apex-to-www and http-to-https redirects
    stencil = _STENCIL.search(response.text) is not None
    if token is None:
        if why == EXPIRED:
            reason = " (only expired ones)"
        elif stencil:
            reason = " (a Stencil theme that doesn't expose one)"
        else:
            reason = (
                " (not a Stencil storefront: a headless one such as Catalyst, or not BigCommerce)"
            )
            await _probe_others(ctx)
        raise BigCommerceError(f"{site}/: no Storefront API token on the homepage{reason}")
    api = None if stencil else find_endpoint(response.text, token)
    return await _Pager(ctx, emit, site, token, api=api, headless=not stencil).run()


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
    tokens, expired = _tokens(response.text, site or origin(response.url), time.time())
    if tokens:
        return response, tokens[0], ""
    return response, None, EXPIRED if expired else "no token"


class _Pager:
    """One store's walk through ``site.products``."""

    def __init__(
        self,
        ctx: StoreContext,
        emit: Emit,
        site: str,
        token: str,
        *,
        api: str | None = None,
        headless: bool = False,
    ) -> None:
        self.ctx = ctx
        self.emit = emit
        self.site = site  # product URLs stay here, wherever ``api`` is
        self.api = api or get_api_url(site)
        self.headless = headless  # not a Stencil storefront
        self.token = token
        self.variant_first = VARIANT_PAGE_SIZE
        self.first = PAGE_SIZE
        self.cursor: str | None = None
        self.cursors: set[str] = set()
        self.page = 1
        # The page size was halved on this page after a 2xx, which counted as a success
        self.resized = False
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
            if self.headless and self.api == get_api_url(self.site):
                return self._failed(
                    f"{self.api}: redirected ({response.status}); the homepage isn't a "
                    "Stencil storefront and names no store-<hash>.mybigcommerce.com/graphql "
                    "endpoint, so this looks like a headless storefront"
                )
            location = response.location or "an unknown location"
            return self._failed(f"{self.api}: redirected ({response.status}) to {location}")
        products, problem = _read(response)
        if products is not None:
            return await self._take(products, problem)
        if problem is None:
            return await self._http_failure(response)
        if _COMPLEXITY_ERROR.search(problem) and self.first > 1:
            self.first //= 2
            self.resized = self.resized or response.ok
            logger.info("BigCommerce query too complex (%s); retrying with %d", problem, self.first)
            return None
        return self._failed(f"{self.api}: {problem}")

    async def _unauthorized(self, response: Response) -> bool | None:
        rejected = _rejected(self.api, response)
        why = await self._refresh()
        return None if why is None else self._failed(f"{rejected}{why}")

    async def _refresh(self) -> str | None:
        """Swap in a fresh token from the homepage; None if it did, else why not
        (to append to the 401's message)."""
        if self.refreshed:
            return ""  # a second 401 in a row
        self.refreshed = True
        # No probes and no no-token error here: this store is BigCommerce
        _, token, why = await _homepage(self.ctx, self.site)
        if token is None:
            return f"; fetching the homepage again gave no token ({why})"
        if token == self.token:
            return "; fetching the homepage again gave the same token"
        logger.info("BigCommerce token rejected at %s; retrying with a new one", self.where)
        self.token = token
        return None

    async def _http_failure(self, response: Response) -> bool:
        """Leave an HTTP failure to diagnose(), unless a 2xx was already counted."""
        logger.info("BigCommerce GraphQL returned %d for %s", response.status, self.where)
        if self.page == 1 and self.resized:
            # The complexity error's 2xx means diagnose() would call this empty
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
        found = nodes(products)
        if problem is not None:
            if not found:
                return self._failed(f"{self.api}: {problem}")
            logger.warning(
                "BigCommerce GraphQL answered with errors for %s: %s", self.where, problem
            )
        logger.info("BigCommerce: %d products from %s", len(found), self.where)
        self.refreshed = self.resized = False
        for node in found:
            if _dig(node, "variants", "pageInfo", "hasNextPage") is True:
                await self._more_variants(node)
            await self.emit(_item(node, self.site, self.ctx.images))
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

    async def _more_variants(self, node: dict[str, Any]) -> None:
        """Page the product's ``variants`` past its first page, into ``node``.

        A failure keeps the variants read so far, with ``hasNextPage`` still true and
        a warning, rather than failing the store.
        """
        connection = node["variants"]
        edges = connection.get("edges")
        product = node.get("entityId")
        if not isinstance(edges, list) or not isinstance(product, int):
            return
        info = connection["pageInfo"]
        cursors: set[str] = set()
        stopped = None
        while info.get("hasNextPage") is True:
            cursor = info.get("endCursor")
            if len(edges) >= MAX_VARIANTS:
                stopped = f"stopped at {MAX_VARIANTS}"
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                stopped = "no new endCursor"
                break
            variables = {"id": product, "first": self.variant_first, "after": cursor}
            body = json.dumps({"query": _VARIANTS_QUERY, "variables": variables}).encode()
            try:
                response = await self.ctx.session.post(self.api, body, headers=self._headers())
            except RequestFailed as exc:
                stopped = f"request failed: {exc}"
                break
            if response is None:
                stopped = "robots.txt"
                break
            if response.status == 401:
                why = await self._refresh()
                if why is None:
                    continue
                stopped = f"{_rejected(self.api, response)}{why}"
                break
            data = response.json_or_none()
            more = _dig(data, "data", "site", "product", "variants")
            page = more.get("edges") if isinstance(more, dict) else None
            if not response.ok or not isinstance(page, list):
                status = f"HTTP {response.status}" if not response.ok else None
                problem = _first_error(data) or status or "no variants in the answer"
                if _COMPLEXITY_ERROR.search(problem) and self.variant_first > 1:
                    self.variant_first //= 2
                    logger.info(
                        "BigCommerce variants query too complex (%s); retrying with %d",
                        problem,
                        self.variant_first,
                    )
                    continue
                stopped = problem
                break
            cursors.add(cursor)
            self.refreshed = False
            edges.extend(page)
            more_info = more.get("pageInfo")
            info = connection["pageInfo"] = more_info if isinstance(more_info, dict) else {}
        if stopped is not None:
            logger.warning(
                "BigCommerce product %s: only the first %d variants (%s)",
                product,
                len(edges),
                stopped,
            )


def _rejected(api: str, response: Response) -> str:
    message = _first_error(response.json_or_none()) or "no GraphQL message"
    return f"{api}: Storefront API token rejected (401): {message}"


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
        for n in nodes(node.get("images"))
        if isinstance(n.get("urlOriginal"), str) and n["urlOriginal"]
    ]
    entries.sort(key=lambda n: n.get("isDefault") is not True)
    return [n["urlOriginal"] for n in entries]


def nodes(connection: Any) -> list[dict[str, Any]]:
    """The node dicts of a GraphQL connection (``{"edges": [{"node": ...}]}``)."""
    edges = connection.get("edges") if isinstance(connection, dict) else None
    return [n for e in dicts(edges) if isinstance(n := e.get("node"), dict)]
