"""WooCommerce: the public Store API, paginated 100 products at a time.

A product lists each variation only by ID and attribute values. The Store API also
lists variations as products of their own (with their prices, SKU and stock), so each
page's variations are fetched in one batch filtered by parent and merged into it.

When the Store API's first page is unusable (an error status other than a 429 rate
limit, no product list, a failed request or robots.txt), product pages are read
instead: their URLs come from the WordPress REST API's product listing or, failing
that, the product sitemaps, and each page's schema.org JSON-LD becomes a partial
item in the Store API's shape, with a variable product's variations from the page's
variation form.
"""

from __future__ import annotations

import asyncio
import html
import logging
import urllib.parse
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from shopify_spy import _jsonld as jsonld
from shopify_spy import _sitemap as sitemap
from shopify_spy._fetch import Response
from shopify_spy._session import (
    FALLBACK,
    Emit,
    RequestFailed,
    RequestKind,
    ShutdownRequested,
    StoreContext,
)
from shopify_spy._utils import (
    canonicalize_url,
    dicts,
    hostname,
    next_page_url,
    normalize_url,
)

__all__ = ["get_api_url", "get_variations_url", "next_page_url", "run"]

logger = logging.getLogger("shopify_spy")

API_PATH = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
# The default order (newest first) can shift between pages when a plugin overrides the
# ID tiebreak, repeating some products and skipping others; IDs are unique and fixed
BY_ID = {"orderby": "id", "order": "asc"}
# Optional: not counted toward the store's diagnosis (products came back, so the store
# works), and a 403 mustn't swap the user agent the product pages use. Not deduped: a
# page in the default order can repeat the last page's variable products
VARIATIONS = RequestKind("variations", counted=False, dedupe=False, rotate=False)

# Fallback: product pages, listed by the WordPress REST API or the sitemaps
WP_PRODUCTS_PATH = "/wp-json/wp/v2/product"
# WordPress core's index, then Yoast's and Rank Math's
SITEMAP_PATHS = ("/wp-sitemap.xml", "/sitemap_index.xml")
PRODUCT_SITEMAPS = ("wp-sitemap-posts-product-", "product-sitemap")
FETCH_CONCURRENCY = 8  # product pages fetched at once per store (host slots still apply)
PAGE_SOURCE = "product_page"
GONE = frozenset({404, 410})
MAX_PRICE_DECIMALS = 6
OUT_OF_STOCK = frozenset({"OutOfStock", "SoldOut", "Discontinued"})


def get_api_url(store_url: str, page: int = 1, by_id: bool = True) -> str:
    """Build the Store API URL for the given store and page, sorted by product ID."""
    params = {"per_page": PER_PAGE, "page": page}
    return _url(store_url, API_PATH, params | BY_ID if by_id else params)


def get_variations_url(
    store_url: str, parents: list[Any], page: int = 1, by_id: bool = True
) -> str:
    """Build the Store API URL for a page of the given products' variations."""
    parent = ",".join(map(str, parents))
    params = {"type": "variation", "parent": parent, "per_page": PER_PAGE, "page": page}
    return _url(store_url, API_PATH, params | BY_ID if by_id else params)


def _url(store_url: str, path: str, params: dict[str, Any]) -> str:
    parsed = normalize_url(store_url)
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


async def run(ctx: StoreContext, emit: Emit) -> bool:
    """Emit every product from the store; return whether it got to the end.

    A failure after the Store API's first page ends the store: those products are
    already in Store API shape, and mixing in partial ones would hide the gap.
    """
    complete = await _store_api(ctx, emit)
    if complete is not None or ctx.suggested_platform is not None:
        return bool(complete)
    logger.info("Store API unavailable for %s; reading product pages instead", ctx.url)
    return await _product_pages(ctx, emit)


async def _store_api(ctx: StoreContext, emit: Emit) -> bool | None:
    """Page through the Store API. Return whether it got to the end, or None when
    the first page was unusable.

    A failed variation listing doesn't end it: the variations keep only their ID and
    attributes, and if the store refused it (4xx, robots.txt, not JSON, filter
    ignored) later pages skip it.
    """
    by_id = True
    variations = True  # until a variation listing fails
    url = get_api_url(ctx.url)
    first = True
    seen_ids: set[Any] = set()
    while True:
        try:
            response = await ctx.session.get(url)
        except RequestFailed as exc:
            logger.info("Store API request failed: %s", exc)
            return None if first else False
        if response is None:
            return None if first else False
        if first and by_id and _rejects_param(response):
            # Store API versions without orderby=id reject it; page in the default order
            logger.info("Store API rejected orderby=id for %s; using its default order", url)
            by_id = False
            url = get_api_url(ctx.url, by_id=False)
            continue
        if not response.ok:
            logger.info("Store API returned %d for %s", response.status, url)
            if first and response.status == 404:
                await ctx.probe_platform("/products.json", _looks_like_shopify, "shopify")
            if response.status == 429:
                # Asked to slow down: a page per product would multiply the load. A 503
                # still falls back, since it's usually the API alone that's down
                return False
            return None if first else False
        products = response.json_or_none()
        if products is None:
            logger.warning("Store API returned non-JSON for %s", url)
            return None if first else False
        if not isinstance(products, list):
            # e.g. {"code": "rest_disabled"} from a security plugin
            logger.warning("Store API returned no product list for %s", url)
            return None if first else False
        if not products:
            return True
        store = urllib.parse.urlsplit(response.url).netloc
        logger.info("WooCommerce: %d products from %s", len(products), response.url)
        products = [p for p in products if isinstance(p, dict)]
        ids = {p["id"] for p in products if p.get("id") is not None}
        if ids and ids <= seen_ids:
            # A server that ignores `page` would otherwise repeat this page forever
            logger.warning("Page repeated earlier products; stopping at %s", response.url)
            return True
        seen_ids |= ids
        if variations:
            # Products past the item limit won't be emitted, so skip their variations
            variations = await _add_variations(ctx, response.url, products[: ctx.items_left], by_id)
        for product in products:
            await emit(_item(product, store, ctx.images))
        url = next_page_url(response.url)
        first = False


async def _add_variations(
    ctx: StoreContext, page_url: str, products: list[dict[str, Any]], by_id: bool
) -> bool:
    """Replace each product's variation entries with the variations' own records,
    keeping the entry's attributes; return False if later pages shouldn't try again.

    The listing goes to the products page's host, past any redirect.
    """
    wanted: set[str] = set()
    parents = []
    for product in products:
        ids = {key for v in dicts(product.get("variations")) if (key := _id(v)) is not None}
        if ids and _id(product) is not None:
            wanted |= ids
            parents.append(product["id"])
    if not wanted:
        return True
    url = get_variations_url(page_url, parents, by_id=by_id)
    records: dict[str, dict[str, Any]] = {}
    # Before the first item, a slow listing must not use up --bail: give it half the
    # time left, then emit the products without it (later pages try again)
    left = ctx.session.bail_time_left()
    budget = asyncio.timeout(None if left is None else left / 2)
    try:
        async with budget:
            reason, refused = await _fetch_variations(ctx, url, wanted, records)
    except TimeoutError:
        if not budget.expired():
            raise
        reason, refused = "too slow before the first item", False
    logger.info("WooCommerce: %d of %d variations from %s", len(records), len(wanted), url)
    if reason is not None:
        logger.warning(
            "Variation listing failed for %s (%s); %d variations keep the product's prices%s",
            url,
            reason,
            len(wanted - records.keys()),
            ", and later pages skip the listing" if refused else "",
        )
    for product in products:
        if isinstance(product.get("variations"), list):
            product["variations"] = [
                records.get(_id(v), {}) | v if isinstance(v, dict) else v
                for v in product["variations"]
            ]
    return not refused


async def _fetch_variations(
    ctx: StoreContext, url: str, wanted: set[str], records: dict[str, dict[str, Any]]
) -> tuple[str | None, bool]:
    """Page the variation listing into ``records``; return why it failed, if it did,
    and whether the store refused it (so later pages shouldn't ask).

    Only the first page can refuse: a later one failing (say, past the last page)
    doesn't mean the store won't serve the listing.
    """
    page = 1
    while True:
        try:
            response = await ctx.session.get(url, kind=VARIATIONS)
        except RequestFailed as exc:
            return str(exc), False
        except ShutdownRequested:  # still emit the page already fetched
            return "shutting down", False
        if response is None:
            return "blocked by robots.txt", page == 1
        batch = _variation_batch(response)
        if isinstance(batch, str):
            return batch, page == 1 and _refusal(response.status)
        missing = wanted - records.keys()
        new = {key: v for v in dicts(batch) if (key := _id(v)) in missing}
        if batch and not new and not records:
            # Not one of them on the first page: the store ignores type or parent
            return "the store ignored the variation filter", True
        records |= new
        # The header says when this was the last page; without it, page until nothing
        # new, like the product pages (a store may cap per_page)
        last = _int(response.headers.get("x-wp-totalpages"))
        if not new or records.keys() >= wanted or (last is not None and page >= last):
            return None, False
        url = next_page_url(response.url)
        page += 1


def _refusal(status: int) -> bool:
    """Whether a failed status means the store won't serve the listing at all."""
    return status < 500 and status not in (408, 429)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _variation_batch(response: Response) -> list[Any] | str:
    """A variation listing page's records, or why the response is unusable."""
    if not response.ok:
        return f"status {response.status}"
    data = response.json_or_none()
    return data if isinstance(data, list) else "not a JSON list"


def _id(record: dict[str, Any]) -> str | None:
    """A record's ID as a key, so ``10`` and ``"10"`` match (a plugin can return
    anything); None if it's neither."""
    value = record.get("id")
    return str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _item(product: dict[str, Any], store: str, images: bool) -> dict[str, Any]:
    product["store"] = store
    product["platform"] = "woocommerce"
    images_field = product.get("images") or []  # may be null
    product["image_urls"] = (
        [img["src"] for img in images_field if isinstance(img, dict) and "src" in img]
        if images
        else []
    )
    return product


def _rejects_param(response: Response) -> bool:
    if response.status != 400:
        return False
    data = response.json_or_none()
    return isinstance(data, dict) and data.get("code") == "rest_invalid_param"


def _looks_like_shopify(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("products"), list)


# --- Fallback: product pages -------------------------------------------------------


async def _product_pages(ctx: StoreContext, emit: Emit) -> bool:
    """Emit a partial item for every listed product page with schema.org Product data.

    Returns whether the listing was read to the end and every page was fetched.
    """
    limiter = asyncio.Semaphore(FETCH_CONCURRENCY)
    failed = False

    async def fetch(url: str, listed_id: int | None) -> None:
        nonlocal failed
        # The slot is held until the item is handed off, which bounds the pages
        # held in memory while the consumer is slow
        try:
            try:
                response = await ctx.session.get(url, kind=FALLBACK)
            except RequestFailed as exc:
                logger.info("Request failed: %s", exc)
                failed = True
                return
            # None: robots.txt disallows the page, or it redirected to one already
            # fetched. A 404 or 410 is a listed product that no longer exists.
            if response is None or response.status in GONE:
                return
            if not response.ok:
                failed = True
                return
            text = response.text  # decoded on each access
            product, post_id = jsonld.parse_product_page(text, response.url)
            if product is None:
                logger.info("No schema.org Product on %s", response.url)
                return
            post_id = post_id or listed_id
            form = jsonld.variation_form(text, post_id)
            item = page_item(product, response.url, post_id, form, jsonld.site_name(text))
            store = urllib.parse.urlsplit(response.url).netloc
            await emit(_item(item, store, ctx.images))
        finally:
            limiter.release()

    listing = _Listing(ctx)
    seen: set[str] = set()
    async with asyncio.TaskGroup() as tg:
        async for url, listed_id in listing.urls():
            try:
                key = canonicalize_url(url)
            except ValueError:
                continue
            if key in seen:
                continue
            seen.add(key)
            # Take the slot before creating the task, so a long listing doesn't
            # queue one task per product
            await limiter.acquire()
            tg.create_task(fetch(url.split("#", 1)[0], listed_id))
    return listing.complete and not failed


def _site(host: str) -> str:
    """A host without ``www.``: WordPress's site address and its REST API can
    differ only by that."""
    return host.removeprefix("www.")


class _Listing:
    """Product page URLs, with the product ID when known: the WordPress REST API's
    product listing, then the product sitemaps if the listing couldn't be read to
    the end.

    ``complete`` becomes True once either source was read to the end with no
    off-store URL left out.
    """

    def __init__(self, ctx: StoreContext) -> None:
        self.ctx = ctx
        self.complete = False
        self.sites = {_site(hostname(ctx.url))}
        self._dropped = 0  # off-store URLs left out of the current source

    def _on_store(self, url: str) -> bool:
        if _site(hostname(url)) in self.sites:
            return True
        self._dropped += 1
        return False

    async def urls(self) -> AsyncIterator[tuple[str, int | None]]:
        async for item in self._rest():
            yield item
        if self._dropped:
            logger.info("Product listing had %d off-store links", self._dropped)
            self.complete = False
        if not self.complete:
            self._dropped = 0
            async for loc in self._sitemaps():
                yield loc, None
            self.complete = self.complete and not self._dropped

    async def _rest(self) -> AsyncIterator[tuple[str, int | None]]:
        page = 1
        params = {"per_page": PER_PAGE, "page": page, "_fields": "id,link"} | BY_ID
        url = _url(self.ctx.url, WP_PRODUCTS_PATH, params)
        seen: set[str] = set()
        while True:
            try:
                response = await self.ctx.session.get(url, kind=FALLBACK)
            except RequestFailed as exc:
                logger.info("Product listing request failed: %s", exc)
                return
            if response is None:
                return
            data = response.json_or_none()
            if not response.ok:
                # WordPress answers 400 for a page past the last one
                past_end = (
                    isinstance(data, dict) and data.get("code") == "rest_post_invalid_page_number"
                )
                self.complete = past_end and page > 1
                logger.info("Product listing returned %d for %s", response.status, url)
                return
            if not isinstance(data, list):
                logger.info("Product listing returned non-list for %s", url)
                return
            if not data:
                self.complete = True
                return
            if page == 1:
                self.sites.add(_site(hostname(response.url)))
            # Ordered: the listing is sorted by product ID
            links = {
                p["link"]: p.get("id") if isinstance(p.get("id"), int) else None
                for p in data
                if isinstance(p, dict) and isinstance(p.get("link"), str)
            }
            if links and links.keys() <= seen:
                # A server that ignores `page`; the sitemaps may list the rest
                logger.warning("Product listing repeated a page; stopping at %s", response.url)
                return
            seen |= links.keys()
            logger.info("Product listing: %d products from %s", len(links), response.url)
            for link, listed_id in links.items():
                if self._on_store(link):
                    yield link, listed_id
            # A short page isn't the end: a host may cap per_page below 100
            last = _int(response.headers.get("x-wp-totalpages"))
            if last is not None and page >= last:
                self.complete = True
                return
            url = next_page_url(response.url)
            page += 1

    async def _sitemaps(self) -> AsyncIterator[str]:
        """Locs of the product sitemaps in the first sitemap index that has any.

        Read here rather than by ``SitemapWalk``, which would also crawl a flat
        sitemap that doesn't say which pages are products.
        """
        session = self.ctx.session
        for path in SITEMAP_PATHS:
            try:
                response = await session.get(f"{self.ctx.url}{path}", kind=FALLBACK)
            except RequestFailed as exc:
                logger.info("Sitemap request failed: %s", exc)
                continue
            if response is None or not response.ok:
                continue
            root, locs = sitemap.parse(response.body)
            if root != "sitemapindex":
                continue
            self.sites.add(_site(hostname(response.url)))
            children = [loc for loc in locs if _is_product_sitemap(loc) and self._on_store(loc)]
            if not children:
                continue
            complete = True
            for child in children:
                walk = sitemap.iter_locs(session, child, kind=FALLBACK)
                async for loc in walk:
                    if self._on_store(loc):
                        yield loc
                complete = complete and walk.root_ok
            self.complete = complete
            return


def _is_product_sitemap(url: str) -> bool:
    try:
        name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return False
    return name.startswith(PRODUCT_SITEMAPS)


def page_item(
    product: dict[str, Any],
    url: str,
    post_id: int | None,
    form: jsonld.VariationForm | None = None,
    site: str | None = None,
) -> dict[str, Any]:
    """A Store API-shaped product from a page's schema.org Product.

    Only the fields JSON-LD carries are set, plus a variable product's
    ``attributes`` and ``variations`` from the page's variation form; ``source``
    marks the item as partial and ``json_ld`` keeps the original.
    """
    found = jsonld.offers(product.get("offers"))
    prices = _prices(found.current, found.listed, found.bounds, found.currency)
    return {
        "id": post_id,
        "name": _name(jsonld.text(product.get("name")), site),
        "slug": urllib.parse.unquote(
            urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        ),
        "permalink": url,
        # With no SKU, WooCommerce's JSON-LD gives the product ID in its place
        "sku": sku if (sku := jsonld.text(product.get("sku"))) != str(post_id) else None,
        "description": jsonld.text(product.get("description")),
        "images": [{"src": src} for src in jsonld.image_urls(product.get("image"))],
        "prices": prices,
        "on_sale": (
            prices["sale_price"] != prices["regular_price"] if prices["price"] is not None else None
        ),
        "is_in_stock": (
            None if found.availability is None else found.availability not in OUT_OF_STOCK
        ),
        **_variations(form, found.currency, prices),
        "source": PAGE_SOURCE,
        "json_ld": product,
    }


def _variations(
    form: jsonld.VariationForm | None, currency: str | None, product_prices: dict[str, Any]
) -> dict[str, Any]:
    """A variable product's Store API ``attributes`` and ``variations`` from its
    page's variation form; empty when there's none to read.

    Names are HTML-escaped, as the Store API returns them.
    """
    if form is None:
        return {}
    variations = []
    for record in dicts(form.records):
        values = record.get("attributes")
        if not isinstance(values, dict):
            continue
        stock = record.get("is_in_stock")
        variations.append(
            {
                "id": record.get("variation_id"),
                "attributes": [
                    {"name": _escape(form.name(field)), "value": value}
                    for field, value in values.items()
                    if isinstance(value, str)
                ],
                # Without its own price, a variation sells at the product's
                "prices": _variation_prices(record, currency) or product_prices,
                "sku": jsonld.text(record.get("sku")) or None,
                "is_in_stock": stock if isinstance(stock, bool) else None,
            }
        )
    if not variations:
        return {}
    attributes = [
        {
            "name": _escape(found.name),
            "has_variations": True,
            "terms": [{"slug": v, "name": _escape(t)} for v, t in found.terms.items()],
        }
        for found in form.fields.values()
    ]
    return {"attributes": attributes, "variations": variations}


def _name(name: str | None, site: str | None) -> str | None:
    """The product's name, escaped once like the Store API's, without the site name
    an SEO plugin may have added to the JSON-LD's (``Hat - Shop``).

    JSON-LD names should be plain text but come escaped, sometimes twice
    (``&amp;amp;``), so they're unescaped until they stop changing.
    """
    if name is None:
        return None
    for _ in range(3):
        if (plain := html.unescape(name)) == name:
            break
        name = plain
    return _escape(jsonld.strip_site_name(name, site) or name)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _variation_prices(record: dict[str, Any], currency: str | None) -> dict[str, Any] | None:
    price = jsonld.amount(record.get("display_price"))
    if price is None:
        return None
    regular = jsonld.amount(record.get("display_regular_price"))
    listed = [regular] if regular is not None and regular > price else []
    return _prices([price], listed, [], currency)


def _prices(
    current: list[Decimal], listed: list[Decimal], bounds: list[Decimal], currency: str | None
) -> dict[str, Any]:
    """Store API ``prices``, amounts in minor units.

    The minor unit is the most decimals any amount was written with, so the
    Store API's arithmetic (amount / 10**currency_minor_unit) gives them back.
    """
    amounts = current + listed + bounds
    decimals = min(MAX_PRICE_DECIMALS, max([0, *(-int(a.as_tuple().exponent) for a in amounts)]))
    unit = Decimal(10) ** decimals

    def minor(amount: Decimal | None) -> str | None:
        return None if amount is None else str(int((amount * unit).to_integral_value()))

    candidates = current or bounds
    low, high = (min(candidates), max(candidates)) if candidates else (None, None)
    regular = max(listed) if listed else low
    return {
        "price": minor(low),
        "regular_price": minor(regular),
        "sale_price": minor(low),
        "price_range": (
            {"min_amount": minor(low), "max_amount": minor(high)} if low != high else None
        ),
        "currency_code": currency,
        "currency_minor_unit": decimals,
    }
