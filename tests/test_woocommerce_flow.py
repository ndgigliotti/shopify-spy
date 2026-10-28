"""WooCommerce adapter flows through spy.scrape with a FakeFetcher (plan §3.3)."""

import pytest

from shopify_spy._fetch import FetchConnectionError
from tests.flow_support import STORE, scrape, woo_url


def woo_product(i, images=1):
    return {
        "id": i,
        "name": f"Product {i}",
        "permalink": f"{STORE}/product/p{i}/",
        "images": [{"id": j, "src": f"https://cdn.test/{i}-{j}.jpg"} for j in range(images)],
    }


def page(n, start=0):
    return [woo_product(i) for i in range(start, start + n)]


def scrape_woo(routes, **options):
    return scrape(routes, platform="woocommerce", **options)


def test_paginates_until_empty_page():
    routes = {woo_url(1): page(100), woo_url(2): page(3, 100), woo_url(3): []}
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 103
    assert fetcher.count(woo_url(3)) == 1
    assert fetcher.count(woo_url(4)) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_short_page_does_not_stop_pagination():
    # Unlike Shopify's bulk endpoint, only an empty page ends WooCommerce paging
    routes = {woo_url(1): page(2), woo_url(2): page(1, 2), woo_url(3): []}
    items, _, fetcher = scrape_woo(routes)
    assert len(items) == 3
    assert fetcher.count(woo_url(3)) == 1


def test_item_shape_without_images():
    items, _, _ = scrape_woo({woo_url(1): [woo_product(0)], woo_url(2): []})
    assert items == [{**woo_product(0), "store": "shop.test", "image_urls": []}]
    assert "url" not in items[0]


def test_item_image_urls_from_images_src(tmp_path):
    routes = {woo_url(1): [woo_product(0, images=2)], woo_url(2): []}
    items, _, _ = scrape_woo(routes, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/0-0.jpg", "https://cdn.test/0-1.jpg"]
    assert "url" not in items[0]
    assert items[0]["store"] == "shop.test"


@pytest.mark.parametrize(
    "failure",
    [(500, "boom"), (404, ""), (200, "<html>"), FetchConnectionError("reset")],
    ids=["server_error", "not_found", "non_json", "transport"],
)
def test_error_on_later_page_ends_store(failure):
    routes = {woo_url(1): page(2), woo_url(2): failure, woo_url(3): page(2, 50)}
    items, _, fetcher = scrape_woo(routes)
    assert len(items) == 2
    assert fetcher.count(woo_url(3)) == 0
    assert fetcher.count(f"{STORE}/sitemap.xml") == 0  # no fallback


def test_non_json_first_page_ends_store():
    items, result, fetcher = scrape_woo({woo_url(1): (200, "<html>maintenance</html>")})
    assert items == []
    assert fetcher.count(woo_url(2)) == 0
    assert result.stores[0].status == "empty"


def test_shopify_hint_after_404():
    routes = {woo_url(1): (404, ""), f"{STORE}/products.json": {"products": [{"id": 1}]}}
    items, result, _ = scrape_woo(routes)
    assert items == []
    store = result.stores[0]
    assert store.status == "not_found"
    assert store.hint is not None
    assert "shopify" in store.hint.lower()


def test_no_hint_when_products_json_absent():
    _, result, _ = scrape_woo({woo_url(1): (404, "")})
    assert result.stores[0].hint is None


def test_pages_are_sorted_by_id():
    # The fake answers only the orderby=id URLs that woo_url builds
    items, _, fetcher = scrape_woo({woo_url(1): page(2), woo_url(2): []})
    assert len(items) == 2
    assert fetcher.count(woo_url(2)) == 1


def test_falls_back_to_default_order_when_orderby_id_rejected():
    default = f"{STORE}/wp-json/wc/store/v1/products?per_page=100&page="
    routes = {
        woo_url(1): (400, '{"code": "rest_invalid_param"}'),
        f"{default}1": page(2),
        f"{default}2": page(1, 2),
        f"{default}3": [],
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 3
    assert fetcher.count(f"{default}3") == 1
    assert fetcher.count(woo_url(2)) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_other_400_on_first_page_ends_store():
    # A WAF or bot block, not an unsupported orderby: no second request
    default = f"{STORE}/wp-json/wc/store/v1/products?per_page=100&page=1"
    items, result, fetcher = scrape_woo({woo_url(1): (400, "<html>Bad Request</html>")})
    assert items == []
    assert fetcher.count(default) == 0
    assert result.stores[0].responses_by_status == {400: 1}


def test_400_on_later_page_ends_store():
    routes = {woo_url(1): page(2), woo_url(2): (400, ""), woo_url(3): page(2, 50)}
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 2
    assert fetcher.count(woo_url(3)) == 0
    assert not result.stores[0].complete


def test_limit_stops_exactly():
    routes = {woo_url(1): page(100), woo_url(2): page(100, 100)}
    items, result, fetcher = scrape_woo(routes, max_items_per_store=5)
    assert len(items) == 5
    assert fetcher.count(woo_url(2)) == 0
    assert result.stores[0].finish_reason == "item_limit"
