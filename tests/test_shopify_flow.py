"""Shopify adapter flows through spy.scrape with a FakeFetcher (plan §3.2, §3.10, §4.7)."""

import asyncio
import logging

import pytest

import shopify_spy as spy
from shopify_spy import _shopify
from shopify_spy._fetch import FakeFetcher, FetchConnectionError
from shopify_spy._shopify import COLLECTION_CONCURRENCY, YIELD_WINDOW
from tests.flow_support import (
    FAST,
    STORE,
    bulk_url,
    collection_url,
    product,
    products,
    redirect,
    requested,
    scrape,
    sitemapindex,
    urlset,
)

SITEMAP = f"{STORE}/sitemap.xml"


# --- Bulk /products.json ---


def test_bulk_full_page_requests_next_page():
    routes = {bulk_url(1): products(250), bulk_url(2): products(3, start=250)}
    items, result, fetcher = scrape(routes)
    assert len(items) == 253
    assert fetcher.count(bulk_url(2)) == 1
    assert fetcher.count(bulk_url(3)) == 0
    assert fetcher.count(SITEMAP) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_bulk_empty_page_after_full_page_ends_the_catalog():
    # A catalog of exactly 500 products: page 3 is empty, and the sitemap isn't needed
    routes = {bulk_url(1): products(250), bulk_url(2): products(250, 250), bulk_url(3): products(0)}
    items, result, fetcher = scrape(routes)
    assert len(items) == 500
    assert fetcher.count(SITEMAP) == 0
    assert result.stores[0].complete


def test_bulk_short_page_stops():
    items, _, fetcher = scrape({bulk_url(1): products(249)})
    assert len(items) == 249
    assert fetcher.count(bulk_url(2)) == 0
    assert fetcher.count(SITEMAP) == 0


def test_bulk_item_shape():
    items, _, _ = scrape({bulk_url(1): products(1)})
    assert items == [
        {
            "product": product(0),
            "url": f"{STORE}/products/p0.json",
            "store": "shop.test",
            "platform": "shopify",
            "image_urls": [],
        }
    ]


def test_bulk_item_uses_post_redirect_host():
    www = "https://www.shop.test"
    routes = {bulk_url(1): redirect(bulk_url(1, www)), bulk_url(1, www): products(2)}
    items, _, _ = scrape(routes)
    assert [i["url"] for i in items] == [f"{www}/products/p0.json", f"{www}/products/p1.json"]
    assert {i["store"] for i in items} == {"www.shop.test"}


def test_bulk_image_urls_when_images_enabled(tmp_path):
    p = product(
        0, variants=[{"id": 1, "price": "1", "featured_image": {"src": "https://cdn.test/v.jpg"}}]
    )
    items, _, _ = scrape({bulk_url(1): {"products": [p]}}, images=True, images_dir=tmp_path)
    assert sorted(items[0]["image_urls"]) == ["https://cdn.test/0.jpg", "https://cdn.test/v.jpg"]


# --- Fallback to the sitemap ---

FALLBACK_TRIGGERS = {
    "request_failure": FetchConnectionError("refused"),
    "non_2xx": (500, "oops"),
    "not_found": (404, ""),
    "non_json": (200, "<html>not json</html>"),
    "json_list": [],
    "json_string": (200, '"hello"'),
    "empty_products": {"products": []},
    "missing_products": {"other": 1},
    "products_not_list": {"products": {"a": 1}},
}


@pytest.mark.parametrize("trigger", FALLBACK_TRIGGERS.values(), ids=FALLBACK_TRIGGERS.keys())
def test_bulk_fallback_triggers(trigger):
    routes = {
        bulk_url(1): trigger,
        SITEMAP: urlset(f"{STORE}/products/hat"),
        f"{STORE}/products/hat.json": {"product": {"id": 7, "handle": "hat"}},
    }
    items, result, fetcher = scrape(routes)
    assert fetcher.count(SITEMAP) == 1
    # Only a 404 is tried on the store's myshopify.com domain
    assert fetcher.count(f"{STORE}/") == (trigger == (404, ""))
    assert items == [
        {
            "product": {"id": 7, "handle": "hat"},
            "url": f"{STORE}/products/hat.json",
            "store": "shop.test",
            "platform": "shopify",
            "image_urls": [],
        }
    ]
    assert result.stores[0].status == "ok"


def test_bulk_request_failure_is_retried_before_fallback():
    routes = {bulk_url(1): FetchConnectionError("refused"), SITEMAP: urlset()}
    _, _, fetcher = scrape(routes, retries=2)
    assert fetcher.count(bulk_url(1)) == 3
    assert fetcher.count(SITEMAP) == 1


def test_sitemap_item_image_urls(tmp_path):
    payload = {"product": {"id": 7, "handle": "hat", "images": [{"src": "https://cdn.test/h.jpg"}]}}
    routes = {
        bulk_url(1): (404, ""),
        SITEMAP: urlset(f"{STORE}/products/hat"),
        f"{STORE}/products/hat.json": payload,
    }
    items, _, _ = scrape(routes, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/h.jpg"]
    assert items[0]["product"] == payload["product"]


def test_sitemap_index_children_followed():
    child = f"{STORE}/sitemap_products_1.xml"
    routes = {
        bulk_url(1): (404, ""),
        SITEMAP: sitemapindex(child),
        child: urlset(f"{STORE}/products/a", f"{STORE}/products/b"),
        f"{STORE}/products/a.json": {"product": {"handle": "a"}},
        f"{STORE}/products/b.json": {"product": {"handle": "b"}},
    }
    items, _, _ = scrape(routes)
    assert sorted(i["product"]["handle"] for i in items) == ["a", "b"]


def test_non_2xx_product_json_dropped_silently():
    routes = {
        bulk_url(1): (404, ""),
        SITEMAP: urlset(f"{STORE}/products/a", f"{STORE}/products/gone"),
        f"{STORE}/products/a.json": {"product": {"handle": "a"}},
        f"{STORE}/products/gone.json": (410, "gone"),
    }
    items, result, _ = scrape(routes)
    assert [i["product"]["handle"] for i in items] == ["a"]
    assert result.stores[0].status == "ok"
    assert result.stores[0].errors == []


# --- Collections ---


def collection_list(*counts: tuple[str, int | None]) -> dict:
    return {"collections": [{"handle": h, "products_count": n} for h, n in counts]}


COLLECTIONS = f"{STORE}/collections.json?limit=250&page=1"


def per_handle_requests(fetcher) -> list[str]:
    """Requests for /collections/<handle>.json, which Shopify answers 429 (issue #64)."""
    return [u for u in requested(fetcher) if "/collections/" in u and "/products" not in u]


def test_products_false_reads_only_the_collection_list():
    routes = {COLLECTIONS: {"collections": [{"id": 3, "handle": "c"}]}}
    items, _, fetcher = scrape(routes, products=False, collections=True)
    assert requested(fetcher) == [COLLECTIONS]
    assert [i["collection"]["handle"] for i in items] == ["c"]


def _collections_routes():
    return {
        bulk_url(1): products(2),
        COLLECTIONS: {"collections": [{"id": 3, "handle": "c"}]},
        SITEMAP: urlset(
            f"{STORE}/products/p0", f"{STORE}/products/extra", f"{STORE}/collections/c"
        ),
        f"{STORE}/collections/c.json": {"collection": {"id": 3, "handle": "c"}},
    }


def test_collections_off_by_default_no_sitemap_after_complete_bulk():
    items, _, fetcher = scrape(_collections_routes())
    assert len(items) == 2
    assert fetcher.count(SITEMAP) == 0
    assert fetcher.count(COLLECTIONS) == 0


def test_collections_come_from_the_list_after_complete_bulk():
    items, _, fetcher = scrape(_collections_routes(), collections=True)
    assert fetcher.count(SITEMAP) == 0
    assert per_handle_requests(fetcher) == []
    assert items[2:] == [
        {
            "collection": {"id": 3, "handle": "c"},
            "url": f"{STORE}/collections/c.json",
            "store": "shop.test",
            "platform": "shopify",
            "image_urls": [],
        }
    ]
    assert len(items) == 3


def test_collection_items_use_the_lists_post_redirect_host():
    www = "https://www.shop.test"
    routes = {
        COLLECTIONS: redirect(f"{www}/collections.json?limit=250&page=1"),
        f"{www}/collections.json?limit=250&page=1": collection_list(("a b", 1)),
    }
    items, _, _ = scrape(routes, products=False, collections=True)
    assert [(i["url"], i["store"]) for i in items] == [
        (f"{www}/collections/a%20b.json", "www.shop.test")
    ]


def test_collection_items_from_every_page_once_each():
    first = collection_list(*((f"c{i}", 1) for i in range(250)))
    routes = {
        COLLECTIONS: first,
        # A handle repeated on a later page is emitted once; an entry without one is skipped
        COLLECTIONS[:-1] + "2": {"collections": [{"handle": "c0"}, {"id": 9}, {"handle": "z"}]},
    }
    items, _, fetcher = scrape(routes, products=False, collections=True)
    assert [i["collection"]["handle"] for i in items] == [f"c{i}" for i in range(250)] + ["z"]
    assert fetcher.count(COLLECTIONS[:-1] + "3") == 0


def test_collection_image_urls_when_images_enabled(tmp_path):
    entry = {"handle": "c", "image": {"src": "https://cdn.test/c.jpg"}}
    routes = {COLLECTIONS: {"collections": [entry]}}
    items, _, _ = scrape(routes, products=False, collections=True, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/c.jpg"]


@pytest.mark.parametrize("failure", [(500, "boom"), (404, ""), (200, "<html>")])
def test_collection_list_failure_gives_no_collection_items(failure):
    routes = {**_collections_routes(), COLLECTIONS: failure}
    items, result, fetcher = scrape(routes, collections=True)
    assert [i for i in items if "collection" in i] == []
    assert len(items) == 2
    assert per_handle_requests(fetcher) == []  # no per-handle fallback
    assert fetcher.count(SITEMAP) == 0
    assert (result.stores[0].status, result.stores[0].complete) == (spy.StoreStatus.partial, False)


# --- Partial bulk run (§3.10 fix) ---


def test_partial_bulk_fetches_only_unseen_handles():
    unseen = ["new-one", "another"]
    routes = {
        bulk_url(1): products(250),
        bulk_url(2): (500, "boom"),
        SITEMAP: urlset(
            "https://www.shop.test/products/p1",  # other host
            f"{STORE}/en-ca/products/p2",  # locale prefix
            f"{STORE}/products/P3",  # case
            f"{STORE}/products/p4?variant=1",  # query
            *(f"{STORE}/products/{h}" for h in unseen),
        ),
        **{f"{STORE}/products/{h}.json": {"product": {"handle": h}} for h in unseen},
    }
    items, _, fetcher = scrape(routes)
    fetched = [u for u in requested(fetcher) if u.endswith(".json") and "/products/" in u]
    assert sorted(fetched) == sorted(f"{STORE}/products/{h}.json" for h in unseen)
    assert len(items) == 252
    assert sorted(i["product"]["handle"] for i in items[250:]) == sorted(unseen)


# --- Past the bulk limit: collections ---


@pytest.fixture
def capped(monkeypatch):
    """Routes for a bulk endpoint at Shopify's page limit, lowered to 3 pages (p0-p749)."""
    monkeypatch.setattr(_shopify, "MAX_PAGES", 3)
    return {bulk_url(n): products(250, (n - 1) * 250) for n in (1, 2, 3)}


def test_bulk_at_page_100_pages_collections_without_requesting_page_101():
    routes = {bulk_url(n): products(250, (n - 1) * 250) for n in range(1, 101)}
    _, _, fetcher = scrape(routes)
    assert fetcher.count(bulk_url(101)) == 0
    assert fetcher.count(collection_url("all", 1)) == 1


@pytest.mark.parametrize("status", [400, 500])
def test_bulk_failing_before_the_limit_does_not_page_collections(status):
    routes = {bulk_url(1): products(250), bulk_url(2): (status, ""), SITEMAP: urlset()}
    _, _, fetcher = scrape(routes)
    assert not [u for u in requested(fetcher) if "/collections" in u]


def test_capped_bulk_pages_collections_then_sitemap(capped):
    routes = {
        **capped,
        COLLECTIONS: collection_list(("small", 3), ("big", 300), ("empty", 0), ("all", 999)),
        collection_url("big", 1): products(250, 0),  # nothing new
        collection_url("big", 2): products(50, 750),  # 50 new, then the end
        collection_url("small", 1): products(3, 800),
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(805))),
        f"{STORE}/products/p803.json": {"product": {"handle": "p803"}},
        f"{STORE}/products/p804.json": {"product": {"handle": "p804"}},
    }
    items, result, fetcher = scrape(routes)
    handles = [i["product"]["handle"] for i in items]
    assert len(handles) == len(set(handles)) == 805
    fetched = requested(fetcher)
    assert fetched.index(collection_url("big", 1)) < fetched.index(collection_url("small", 1))
    assert fetcher.count(collection_url("empty", 1)) == 0
    assert fetcher.count(collection_url("all", 1)) == 1  # not paged again from the list
    assert [u for u in fetched if u.endswith(".json") and "/products/" in u] == [
        f"{STORE}/products/p803.json",
        f"{STORE}/products/p804.json",
    ]
    from_collection = next(i for i in items if i["product"]["handle"] == "p802")
    assert from_collection == {
        "product": product(802),
        "url": f"{STORE}/products/p802.json",
        "store": "shop.test",
        "platform": "shopify",
        "image_urls": [],
    }
    assert result.stores[0].complete


def test_few_missing_products_are_fetched_one_by_one_instead(capped):
    # 2 products missing: fewer than /collections/all's 3 pages would cost
    routes = {
        **capped,
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(752))),
        **{f"{STORE}/products/p{i}.json": {"product": {"handle": f"p{i}"}} for i in (750, 751)},
    }
    items, result, fetcher = scrape(routes)
    assert len(items) == 752
    assert not [u for u in requested(fetcher) if "/collections" in u]
    assert result.stores[0].complete


def test_collections_stop_once_the_sitemaps_products_are_all_seen(capped, monkeypatch):
    monkeypatch.setattr(_shopify, "COLLECTION_CONCURRENCY", 1)
    routes = {
        **capped,
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(760))),
        COLLECTIONS: collection_list(*((f"c{i}", 600 - i) for i in range(5))),
        collection_url("c0", 1): products(10, 750),
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 760
    assert fetcher.count(collection_url("c1", 1)) == 0
    assert not [u for u in requested(fetcher) if "/products/p" in u]


def test_a_short_collections_all_still_leaves_the_collection_list(capped):
    # A merchant's own "all" collection can list far fewer products than the store has
    routes = {
        **capped,
        collection_url("all", 1): products(10, 750),
        COLLECTIONS: collection_list(("c", 1000)),
        collection_url("c", 1): products(5, 760),
    }
    items, _, _ = scrape(routes)
    assert len(items) == 765


def test_collection_list_skipped_once_collections_all_finds_every_listed_product(capped):
    routes = {
        **capped,
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(760))),
        collection_url("all", 1): products(250, 510),  # p750-p759 are new
        collection_url("all", 2): products(250, 760),
        COLLECTIONS: collection_list(("c", 1000)),
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 760
    assert fetcher.count(collection_url("all", 2)) == 0  # stopped mid-list
    assert fetcher.count(COLLECTIONS) == 0


def test_localized_sitemap_handles_are_not_counted_as_missing(capped):
    # Translated handles never match a product's own, so only 2 products are missing
    locs = [f"{STORE}/products/p{i}" for i in range(752)]
    locs += [f"{STORE}/fr/products/produit-{i}" for i in range(10)]
    routes = {**capped, SITEMAP: urlset(*locs)}
    _, _, fetcher = scrape(routes)
    assert not [u for u in requested(fetcher) if "/collections" in u]


def test_item_limit_skips_reading_the_sitemap_first(capped):
    routes = {
        **capped,
        collection_url("all", 1): products(250, 750),
        collection_url("all", 2): products(250, 1000),
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(2000))),
    }
    items, _, fetcher = scrape(routes, max_items_per_store=800)
    assert len(items) == 800
    assert fetcher.count(SITEMAP) == 0


def test_collection_list_stops_when_pages_repeat(capped):
    page = collection_list(*((f"c{i}", 1) for i in range(250)))
    _, _, fetcher = scrape({**capped, COLLECTIONS: page, COLLECTIONS[:-1] + "2": page})
    assert fetcher.count(COLLECTIONS[:-1] + "3") == 0


def test_repeated_collection_list_page_makes_the_store_partial():
    page = collection_list(*((f"c{i}", 1) for i in range(250)))
    routes = {COLLECTIONS: page, COLLECTIONS[:-1] + "2": page}
    items, result, _ = scrape(routes, products=False, collections=True)
    assert len(items) == 250
    assert (result.stores[0].status, result.stores[0].complete) == (spy.StoreStatus.partial, False)


def test_collection_product_with_a_null_handle_does_not_hide_one_named_none(capped):
    page = {"products": [{"id": 1, "handle": "none"}, {"id": 2, "handle": None}]}
    items, _, _ = scrape({**capped, collection_url("all", 1): page})
    assert {"id": 1, "handle": "none"} in [i["product"] for i in items]


def test_collections_all_at_the_limit_goes_on_to_the_collection_list(capped):
    routes = {
        **capped,
        **{collection_url("all", n): products(250, 500 + n * 250) for n in (1, 2, 3)},
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 1500
    assert fetcher.count(collection_url("all", 4)) == 0
    assert fetcher.count(COLLECTIONS) == 1


def test_collection_paged_past_leading_pages_with_nothing_new(capped):
    # A collection in the bulk order repeats the bulk window before its new products
    routes = {
        **capped,
        COLLECTIONS: collection_list(("c", 1000)),
        collection_url("c", 1): products(250, 0),
        collection_url("c", 2): products(250, 250),
        collection_url("c", 3): products(10, 750),
    }
    items, _, _ = scrape(routes)
    assert len(items) == 760


def test_collection_paging_stops_when_pages_repeat(capped):
    routes = {
        **capped,
        collection_url("all", 1): products(250, 750),
        collection_url("all", 2): products(250, 750),
        collection_url("all", 3): products(250, 1000),
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 1000
    assert fetcher.count(collection_url("all", 3)) == 0


def test_collection_page_listing_a_product_twice_emits_it_once(capped):
    page = {"products": [product(750), product(750), *products(248, 751)["products"]]}
    items, _, _ = scrape({**capped, collection_url("all", 1): page})
    handles = [i["product"]["handle"] for i in items]
    assert len(handles) == len(set(handles)) == 999


def test_collections_stop_once_they_find_less_than_a_product_per_request(capped):
    handles = [f"c{i}" for i in range(30)]
    routes = {
        **capped,
        COLLECTIONS: collection_list(*((h, 100 - i) for i, h in enumerate(handles))),
        **{collection_url(h, 1): products(1) for h in handles},  # nothing new
    }
    _, _, fetcher = scrape(routes)
    paged = [h for h in handles if fetcher.count(collection_url(h, 1))]
    assert paged == handles[: len(paged)]  # largest first
    assert len(paged) <= YIELD_WINDOW + COLLECTION_CONCURRENCY


@pytest.mark.parametrize("images", [False, True])
def test_collections_continue_while_they_find_a_product_per_request(capped, images, tmp_path):
    # Image downloads share the session but don't count against the collections
    handles = [f"c{i}" for i in range(30)]
    routes = {
        **capped,
        COLLECTIONS: collection_list(*((h, 1) for h in handles)),
        **{collection_url(h, 1): products(1, 750 + i) for i, h in enumerate(handles)},
    }
    items, _, _ = scrape(routes, images=images, images_dir=tmp_path if images else None)
    assert len(items) == 780


def test_collection_handle_is_quoted_and_missing_count_still_paged(capped):
    routes = {
        **capped,
        COLLECTIONS: collection_list(("été", None)),
        collection_url("%C3%A9t%C3%A9", 1): products(1, 750),
    }
    items, _, _ = scrape(routes)
    assert len(items) == 751


def test_collections_use_the_post_redirect_host(capped):
    www = "https://www.shop.test"
    routes = {
        bulk_url(1): redirect(bulk_url(1, www)),
        **{bulk_url(n, www): products(250, (n - 1) * 250) for n in (1, 2, 3)},
        collection_url("all", 1, www): products(1, 750),
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 751
    assert not [u for u in requested(fetcher) if u.startswith(f"{STORE}/collections")]


def test_max_items_per_store_stops_in_collections(capped):
    routes = {
        **capped,
        collection_url("all", 1): products(250, 750),
        collection_url("all", 2): products(250, 1000),
    }
    items, result, fetcher = scrape(routes, max_items_per_store=800)
    assert len(items) == 800
    assert fetcher.count(collection_url("all", 2)) == 0
    assert fetcher.count(COLLECTIONS) == 0
    assert result.stores[0].finish_reason == "item_limit"


# --- Wrong-platform hint ---


def test_woocommerce_hint_after_404_and_no_sitemap_products():
    routes = {
        bulk_url(1): (404, ""),
        f"{STORE}/wp-json/": {"namespaces": ["wc/store/v1", "wp/v2"]},
    }
    items, result, _ = scrape(routes)
    assert items == []
    store = result.stores[0]
    assert store.status == "not_found"
    assert store.hint is not None
    assert "woocommerce" in store.hint.lower()


def test_no_hint_when_wp_json_absent():
    _, result, _ = scrape({bulk_url(1): (404, "")})
    assert result.stores[0].hint is None


# --- Item limit ---


def test_max_items_per_store_bulk_stops_exactly():
    items, result, fetcher = scrape(
        {bulk_url(1): products(250), bulk_url(2): products(250, 250)}, max_items_per_store=3
    )
    assert len(items) == 3
    assert fetcher.count(bulk_url(2)) == 0
    assert fetcher.count(SITEMAP) == 0
    assert result.stores[0].finish_reason == "item_limit"
    assert not result.stores[0].complete


def test_max_items_per_store_sitemap_stops_exactly():
    handles = [f"h{i}" for i in range(10)]
    routes = {
        bulk_url(1): (404, ""),
        SITEMAP: urlset(*(f"{STORE}/products/{h}" for h in handles)),
        **{f"{STORE}/products/{h}.json": {"product": {"handle": h}} for h in handles},
    }
    items, result, _ = scrape(routes, max_items_per_store=2)
    assert len(items) == 2
    assert result.stores[0].finish_reason == "item_limit"


def test_capped_store_with_collections_reads_the_list_once(capped):
    routes = {
        **capped,
        COLLECTIONS: collection_list(("small", 3), ("empty", 0)),
        collection_url("small", 1): products(3, 750),
        SITEMAP: urlset(
            *(f"{STORE}/products/p{i}" for i in range(755)), f"{STORE}/collections/small"
        ),
        **{f"{STORE}/products/p{i}.json": {"product": {"handle": f"p{i}"}} for i in (753, 754)},
    }
    items, result, fetcher = scrape(routes, collections=True)
    assert fetcher.count(COLLECTIONS) == 1
    assert per_handle_requests(fetcher) == []
    # Products first, then the collections as a block, empty ones included
    assert [i["collection"]["handle"] for i in items[755:]] == ["small", "empty"]
    assert len(items) == 757
    assert result.stores[0].complete


def test_capped_store_reads_the_list_for_items_when_paging_skipped_it(capped):
    # Few products missing, so collections aren't paged and the list isn't read for them
    routes = {
        **capped,
        COLLECTIONS: collection_list(("c", 5)),
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(751))),
        f"{STORE}/products/p750.json": {"product": {"handle": "p750"}},
    }
    items, _, fetcher = scrape(routes, collections=True)
    assert fetcher.count(collection_url("all", 1)) == 0
    assert fetcher.count(COLLECTIONS) == 1
    assert [i["collection"]["handle"] for i in items if "collection" in i] == ["c"]


# --- Headless stores: /products.json on the myshopify.com domain (issue #92) ---

ACME = "https://acme.myshopify.com"
HOMEPAGE = f"{STORE}/"
HAT = f"{STORE}/products/hat.json"


def homepage(*hosts: str) -> tuple[int, str, dict[str, str]]:
    links = "".join(f'<a href="https://{h}/cart">cart</a>' for h in hosts)
    return 200, f"<html><body>{links}</body></html>", {"Content-Type": "text/html"}


def headless(**extra):
    """A store whose own /products.json is a 404 and whose homepage names acme."""
    return {
        bulk_url(1): (404, ""),
        HOMEPAGE: homepage("ACME.myshopify.com", "acme.myshopify.com"),
        bulk_url(1, ACME): products(3),
        SITEMAP: urlset(f"{STORE}/products/hat"),
        HAT: {"product": {"id": 7, "handle": "hat"}},
        **extra,
    }


def myshopify_requests(fetcher: FakeFetcher) -> list[str]:
    return [r.url for r in fetcher.requests if ".myshopify.com" in r.url]


def test_myshopify_products_keep_the_stores_own_domain():
    items, result, fetcher = scrape(headless())
    assert [(i["url"], i["store"]) for i in items] == [
        (f"{STORE}/products/p{i}.json", "shop.test") for i in range(3)
    ]
    assert fetcher.count(SITEMAP) == 0
    store = result.stores[0]
    assert (store.status, store.complete) == ("ok", True)
    assert store.products_from == "acme.myshopify.com"
    assert store.hint is not None and "acme.myshopify.com" in store.hint


def test_myshopify_paging_stays_on_the_myshopify_domain():
    routes = headless(
        **{bulk_url(1, ACME): products(250), bulk_url(2, ACME): products(2, start=250)}
    )
    items, result, fetcher = scrape(routes)
    assert fetcher.count(bulk_url(2, ACME)) == 1
    assert len(items) == 252
    assert {i["store"] for i in items} == {"shop.test"}
    assert all(i["url"].startswith(f"{STORE}/products/") for i in items)
    assert result.stores[0].products_from == "acme.myshopify.com"


def test_myshopify_redirect_to_another_host_keeps_the_stores_own_domain():
    other = "https://other.test"
    routes = headless(
        **{bulk_url(1, ACME): redirect(bulk_url(1, other)), bulk_url(1, other): products(3)}
    )
    items, result, fetcher = scrape(routes)
    assert [(i["url"], i["store"]) for i in items] == [
        (f"{STORE}/products/p{i}.json", "shop.test") for i in range(3)
    ]
    assert result.stores[0].products_from == "acme.myshopify.com"
    assert fetcher.count(SITEMAP) == 0


def test_password_protected_myshopify_domain_falls_back_to_the_sitemap(caplog):
    routes = headless(
        **{
            bulk_url(1, ACME): redirect(f"{ACME}/password", 302),
            f"{ACME}/password": (200, "<html>Enter password</html>"),
        }
    )
    with caplog.at_level(logging.INFO, logger="shopify_spy"):
        items, result, fetcher = scrape(routes)
    assert [i["url"] for i in items] == [HAT]
    assert fetcher.count(SITEMAP) == 1
    assert result.stores[0].products_from is None
    assert "password protected" in caplog.text


def test_escaped_myshopify_references_name_the_same_handle():
    page = (
        '<a href="/account?return_to=https%3A%2F%2Facme.myshopify.com">in</a>'
        '<script>{"shop":"https:\\u002F\\u002Facme.myshopify.com"}</script>'
        '<a href="https://acme.myshopify.com/cart">cart</a>'
    )
    items, result, _ = scrape(headless(**{HOMEPAGE: (200, page, {"Content-Type": "text/html"})}))
    assert len(items) == 3
    assert result.stores[0].products_from == "acme.myshopify.com"


ROBOTS_HIDE_HOMEPAGE = (200, "User-agent: *\nDisallow: /$\n")
HOMEPAGE_FAILURES = {
    "two_handles": homepage("acme.myshopify.com", "other.myshopify.com"),
    "no_handle": homepage("shop.test"),
    "not_found": (404, "acme.myshopify.com"),
    "connection_error": FetchConnectionError("refused"),
    "robots_disallowed": None,
}


@pytest.mark.parametrize("page", HOMEPAGE_FAILURES.values(), ids=HOMEPAGE_FAILURES.keys())
def test_homepage_without_one_handle_goes_straight_to_the_sitemap(page):
    routes = headless(**{HOMEPAGE: page})
    if page is None:
        routes[HOMEPAGE] = homepage("acme.myshopify.com")
        routes[f"{STORE}/robots.txt"] = ROBOTS_HIDE_HOMEPAGE
    items, result, fetcher = scrape(routes)
    assert myshopify_requests(fetcher) == []
    assert [i["url"] for i in items] == [HAT]
    assert fetcher.count(SITEMAP) == 1
    assert result.stores[0].products_from is None


MYSHOPIFY_FAILURES = {
    "not_found": (404, ""),
    "unauthorized": (401, ""),
    "rate_limited": (429, ""),
    "connection_error": FetchConnectionError("refused"),
    "non_json": (200, "<html>not json</html>"),
    "back_to_own_domain": redirect(bulk_url(1)),
}


@pytest.mark.parametrize("answer", MYSHOPIFY_FAILURES.values(), ids=MYSHOPIFY_FAILURES.keys())
def test_failed_myshopify_bulk_falls_back_to_the_sitemap(answer):
    items, result, fetcher = scrape(headless(**{bulk_url(1, ACME): answer}), retries=0)
    assert fetcher.count(bulk_url(1, ACME)) == 1
    assert [i["url"] for i in items] == [HAT]
    assert fetcher.count(SITEMAP) == 1
    assert result.stores[0].status == "ok"
    assert result.stores[0].products_from is None


def test_myshopify_store_url_never_requests_its_homepage():
    routes = {
        bulk_url(1, ACME): (404, ""),
        f"{ACME}/": homepage("other.myshopify.com"),
        f"{ACME}/sitemap.xml": urlset(),
    }
    _, _, fetcher = scrape(routes, urls=ACME)
    assert fetcher.count(f"{ACME}/") == 0
    assert "other.myshopify.com" not in " ".join(r.url for r in fetcher.requests)


def test_woocommerce_hint_survives_a_homepage_without_a_handle():
    routes = {
        bulk_url(1): (404, ""),
        HOMEPAGE: homepage("shop.test"),
        f"{STORE}/wp-json/": {"namespaces": ["wc/store/v1", "wp/v2"]},
    }
    _, result, _ = scrape(routes)
    assert result.stores[0].status == "not_found"
    assert "woocommerce" in (result.stores[0].hint or "").lower()


def test_myshopify_requests_of_all_stores_go_one_at_a_time():
    two = "https://two.test"
    in_flight = peak = 0

    async def slow(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if request.url.endswith("/robots.txt"):
            return 404, ""
        return products(2)

    other = "https://other.myshopify.com"
    routes = {
        bulk_url(1): (404, ""),
        bulk_url(1, two): (404, ""),
        HOMEPAGE: homepage("acme.myshopify.com"),
        f"{two}/": homepage("other.myshopify.com"),
        **{u: slow for h in (ACME, other) for u in (f"{h}/robots.txt", bulk_url(1, h))},
    }
    items, result, fetcher = scrape(routes, urls=[STORE, two], respect_robots_txt=True)
    assert len(myshopify_requests(fetcher)) == 4
    assert peak == 1
    assert len(items) == 4
    assert [s.products_from for s in result.stores] == [
        "acme.myshopify.com",
        "other.myshopify.com",
    ]


@pytest.mark.parametrize("refused", ["robots", "products"])
@pytest.mark.parametrize("status", [403, 429])
def test_myshopify_refusal_keeps_the_stores_user_agent(status, refused):
    target = f"{ACME}/robots.txt" if refused == "robots" else bulk_url(1, ACME)
    routes = headless(**{target: (status, "")})
    if refused == "robots":
        routes[bulk_url(1, ACME)] = (404, "")
    _, _, fetcher = scrape(routes, retries=1)
    agents = {r.identity.user_agent for r in fetcher.requests}
    assert len(agents) == 1


def test_partial_myshopify_bulk_is_completed_from_the_sitemap():
    routes = headless(
        **{
            bulk_url(1, ACME): products(250),
            bulk_url(2, ACME): (500, ""),
            SITEMAP: urlset(f"{STORE}/products/p0", f"{STORE}/products/hat"),
        }
    )
    items, result, fetcher = scrape(routes, retries=0)
    assert fetcher.count(SITEMAP) == 1
    assert fetcher.count(f"{STORE}/products/p0.json") == 0
    assert len(items) == 251
    assert items[-1]["url"] == HAT
    store = result.stores[0]
    assert store.status == "ok"
    assert store.products_from == "acme.myshopify.com"


def test_item_limit_during_myshopify_paging_keeps_products_from():
    items, result, _ = scrape(headless(), max_items_per_store=2)
    assert len(items) == 2
    store = result.stores[0]
    assert store.finish_reason == "item_limit"
    assert store.products_from == "acme.myshopify.com"


def test_shutdown_during_myshopify_paging_keeps_products_from():
    routes = headless(
        **{bulk_url(1, ACME): products(250), bulk_url(2, ACME): products(250, start=250)}
    )
    fetcher = FakeFetcher(routes)
    run = spy.scrape(STORE, fetcher_factory=lambda: fetcher, **FAST)
    for _ in run:
        run.request_shutdown()
    store = run.result.stores[0]
    assert store.finish_reason == "shutdown"
    assert store.products_from == "acme.myshopify.com"


def test_myshopify_wait_is_cut_to_half_the_bail_left():
    """A store stuck behind another store's 429 pause on the shared slot moves on to
    its sitemap instead of timing out."""
    two = "https://two.test"
    other = "https://other.myshopify.com"
    paused = asyncio.Event()
    order: list[str] = []

    def rate_limited(request):
        order.append("A 429")
        paused.set()
        return 429, "", {"Retry-After": "5"}

    async def b_homepage(request):
        await asyncio.wait_for(paused.wait(), 1)
        order.append("B homepage")
        return homepage("other.myshopify.com")

    routes = {
        **headless(**{bulk_url(1, ACME): rate_limited}),
        bulk_url(1, two): (404, ""),
        f"{two}/": b_homepage,
        bulk_url(1, other): products(3),
        f"{two}/sitemap.xml": urlset(f"{two}/products/cap"),
        f"{two}/products/cap.json": {"product": {"id": 8, "handle": "cap"}},
    }
    items, result, fetcher = scrape(routes, urls=[STORE, two], bail=2, retries=0, _backoff=True)
    assert order == ["A 429", "B homepage"]
    assert fetcher.count(bulk_url(1, other)) == 0
    assert f"{two}/products/cap.json" in [i["url"] for i in items]
    b = result.stores[1]
    assert b.status == "ok"
    assert b.products_from is None


def test_slow_homepage_is_cut_to_half_the_bail_left():
    async def slow_homepage(request):
        await asyncio.sleep(30)
        return homepage("acme.myshopify.com")

    items, result, fetcher = scrape(headless(**{HOMEPAGE: slow_homepage}), bail=2, retries=0)
    assert myshopify_requests(fetcher) == []
    assert [i["url"] for i in items] == [HAT]
    assert result.stores[0].status == "ok"


def test_collections_come_from_the_myshopify_domain():
    routes = headless(**{f"{ACME}/collections.json?limit=250&page=1": collection_list(("c", 1))})
    items, _, fetcher = scrape(routes, collections=True)
    assert fetcher.count(COLLECTIONS) == 0
    assert [(i["url"], i["store"]) for i in items if "collection" in i] == [
        (f"{STORE}/collections/c.json", "shop.test")
    ]


# --- Completeness: a store missing part of its catalog is partial ---


def test_repeated_bulk_page_falls_back_to_the_sitemap():
    # A server that ignores ?page= repeats page 1: product p250 is only in the sitemap
    routes = {
        bulk_url(1): products(250),
        bulk_url(2): products(250),
        SITEMAP: urlset(*(f"{STORE}/products/p{i}" for i in range(251))),
        f"{STORE}/products/p250.json": {"product": product(250)},
    }
    items, result, fetcher = scrape(routes)
    assert fetcher.count(bulk_url(3)) == 0
    assert len(items) == 251
    assert (result.stores[0].status, result.stores[0].complete) == ("ok", True)


def test_failed_collection_list_page_makes_the_store_partial():
    routes = {
        COLLECTIONS: collection_list(*((f"c{i}", 1) for i in range(250))),
        COLLECTIONS[:-1] + "2": (503, "busy"),
    }
    items, result, _ = scrape(routes, products=False, collections=True)
    assert len(items) == 250
    store = result.stores[0]
    assert (store.status, store.complete) == (spy.StoreStatus.partial, False)
