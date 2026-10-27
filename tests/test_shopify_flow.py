"""Shopify adapter flows through spy.scrape with a FakeFetcher (plan §3.2, §3.10, §4.7)."""

import pytest

from shopify_spy.fetch import FetchConnectionError
from tests.flow_support import (
    STORE,
    bulk_url,
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
    assert items == [
        {
            "product": {"id": 7, "handle": "hat"},
            "url": f"{STORE}/products/hat.json",
            "store": "shop.test",
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


def test_products_false_goes_straight_to_sitemap():
    routes = {
        SITEMAP: urlset(f"{STORE}/products/a", f"{STORE}/collections/c"),
        f"{STORE}/products/a.json": {"product": {"handle": "a"}},
        f"{STORE}/collections/c.json": {"collection": {"handle": "c"}},
    }
    items, _, fetcher = scrape(routes, products=False, collections=True)
    assert fetcher.count(bulk_url(1)) == 0
    assert fetcher.count(f"{STORE}/products/a.json") == 0
    assert [i["collection"]["handle"] for i in items] == ["c"]


# --- Collections ---


def _collections_routes():
    return {
        bulk_url(1): products(2),
        SITEMAP: urlset(
            f"{STORE}/products/p0", f"{STORE}/products/extra", f"{STORE}/collections/c"
        ),
        f"{STORE}/products/extra.json": {"product": {"handle": "extra"}},
        f"{STORE}/collections/c.json": {"collection": {"id": 3, "handle": "c"}},
    }


def test_collections_off_by_default_no_sitemap_after_complete_bulk():
    items, _, fetcher = scrape(_collections_routes())
    assert len(items) == 2
    assert fetcher.count(SITEMAP) == 0


def test_collections_fetched_after_complete_bulk_and_products_skipped():
    items, _, fetcher = scrape(_collections_routes(), collections=True)
    assert fetcher.count(SITEMAP) == 1
    assert fetcher.count(f"{STORE}/products/extra.json") == 0
    assert fetcher.count(f"{STORE}/products/p0.json") == 0
    collections = [i for i in items if "collection" in i]
    assert collections == [
        {
            "collection": {"id": 3, "handle": "c"},
            "url": f"{STORE}/collections/c.json",
            "store": "shop.test",
            "image_urls": [],
        }
    ]
    assert len(items) == 3


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
