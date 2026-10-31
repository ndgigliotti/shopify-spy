"""WooCommerce adapter flows through spy.scrape with a FakeFetcher (plan §3.3)."""

import asyncio
import time

import pytest

from shopify_spy._fetch import FetchConnectionError
from shopify_spy._woocommerce import get_variations_url
from tests.flow_support import STORE, redirect, requested, scrape, woo_url


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
    assert items == [
        {**woo_product(0), "store": "shop.test", "platform": "woocommerce", "image_urls": []}
    ]
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


def variable(i, *variation_ids):
    entries = [{"id": v, "attributes": [{"name": "Size", "value": f"s{v}"}]} for v in variation_ids]
    return woo_product(i) | {"type": "variable", "variations": entries}


def variation(v, parent, price="100"):
    return {
        "id": v,
        "parent": parent,
        "type": "variation",
        "sku": f"SKU-{v}",
        "is_in_stock": True,
        "prices": {"price": price, "regular_price": price, "currency_minor_unit": 2},
        "attributes": [],
        "variation": f"Size: s{v}",
    }


def variations_url(*parents, page=1):
    return get_variations_url(STORE, list(parents), page)


def test_variations_replace_their_entries():
    routes = {
        woo_url(1): [variable(1, 10, 11), woo_product(2), variable(3, 30)],
        variations_url(1, 3): [variation(11, 1, "250"), variation(10, 1), variation(30, 3)],
        woo_url(2): [],
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 3
    first = items[0]["variations"]
    # The record, with the listing's attributes in place of its own empty list
    assert first[0] == variation(10, 1) | {"attributes": [{"name": "Size", "value": "s10"}]}
    assert [v["sku"] for v in first] == ["SKU-10", "SKU-11"]
    assert items[2]["variations"][0]["prices"]["price"] == "100"
    assert fetcher.count(variations_url(1, 3)) == 1
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_no_variation_request_without_variations():
    items, _, fetcher = scrape_woo({woo_url(1): page(3), woo_url(2): []})
    assert len(items) == 3
    assert requested(fetcher) == [woo_url(1), woo_url(2)]


def test_variation_listing_pages():
    ids = list(range(1000, 1101))
    routes = {
        woo_url(1): [variable(1, *ids)],
        variations_url(1): [variation(v, 1) for v in ids[:100]],
        variations_url(1, page=2): [variation(ids[100], 1)],
        woo_url(2): [],
    }
    items, _, fetcher = scrape_woo(routes)
    assert all("prices" in v for v in items[0]["variations"])
    assert fetcher.count(variations_url(1, page=2)) == 1
    assert fetcher.count(variations_url(1, page=3)) == 0


def test_variation_listing_ignoring_the_filter_stops():
    # A server that ignores type/parent pages its whole catalog; nothing matches, so stop
    routes = {
        woo_url(1): [variable(1, 10)],
        variations_url(1): page(100, 500),
        variations_url(1, page=2): page(100, 600),
        woo_url(2): [variable(2, 20)],
        variations_url(2): page(100, 500),
        woo_url(3): [],
    }
    items, _, fetcher = scrape_woo(routes)
    assert items[0]["variations"] == variable(1, 10)["variations"]
    assert fetcher.count(variations_url(1, page=2)) == 0
    assert fetcher.count(variations_url(2)) == 0  # and later pages don't ask again


@pytest.mark.parametrize(
    ("failure", "refused"),
    [
        ((403, "<html>Forbidden</html>"), True),
        ((200, "<html>"), True),
        ((200, "{}"), True),
        ((500, ""), False),
        (FetchConnectionError("x"), False),
    ],
    ids=["forbidden", "non_json", "not_a_list", "server_error", "transport"],
)
def test_variation_listing_failure_keeps_the_entries(failure, refused):
    routes = {
        woo_url(1): [variable(1, 10), woo_product(2)],
        variations_url(1): failure,
        woo_url(2): [variable(3, 30)],
        variations_url(3): [variation(30, 3)],
        woo_url(3): [],
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 3
    assert items[0]["variations"] == variable(1, 10)["variations"]
    # A refusal holds for the store; a transient failure leaves later pages to try
    assert fetcher.count(variations_url(3)) == (0 if refused else 1)
    assert ("prices" in items[2]["variations"][0]) is not refused
    store = result.stores[0]
    assert store.status == "ok"
    assert store.complete
    assert store.responses_by_status == {200: 3}  # variation listings aren't counted


def test_refused_variation_listing_is_not_retried_and_keeps_the_user_agent():
    routes = {
        woo_url(1): [variable(1, 10)],
        variations_url(1): (403, "<html>Forbidden</html>"),
        woo_url(2): [],
    }
    _, _, fetcher = scrape_woo(routes)
    assert fetcher.count(variations_url(1)) == 1
    assert len({r.identity.user_agent for r in fetcher.requests}) == 1


def test_variation_listing_pages_past_a_capped_per_page():
    # A store that caps per_page below 100: short pages aren't the last
    routes = {
        woo_url(1): [variable(1, 10, 11, 12)],
        variations_url(1): [variation(10, 1), variation(11, 1)],
        variations_url(1, page=2): [variation(12, 1)],
        woo_url(2): [],
    }
    items, _, fetcher = scrape_woo(routes)
    assert [v["sku"] for v in items[0]["variations"]] == ["SKU-10", "SKU-11", "SKU-12"]
    assert fetcher.count(variations_url(1, page=3)) == 0


def test_repeated_variation_listing_is_fetched_again():
    # Pages in the default order can repeat the last page's variable products
    default = f"{STORE}/wp-json/wc/store/v1/products?per_page=100&page="
    unsorted = get_variations_url(STORE, [1], by_id=False)
    routes = {
        woo_url(1): (400, '{"code": "rest_invalid_param"}'),
        f"{default}1": [variable(1, 10)],
        f"{default}2": [variable(1, 10), woo_product(2)],
        f"{default}3": [],
        unsorted: [variation(10, 1)],
    }
    items, _, fetcher = scrape_woo(routes)
    assert fetcher.count(unsorted) == 2
    assert all("prices" in i["variations"][0] for i in items if i["id"] == 1)


def test_variation_listing_follows_the_product_pages_host():
    other = "https://www.shop.test"
    routes = {
        woo_url(1): redirect(woo_url(1, other)),
        woo_url(1, other): [variable(1, 10)],
        get_variations_url(other, [1]): [variation(10, 1)],
        woo_url(2, other): [],
    }
    items, _, fetcher = scrape_woo(routes)
    assert "prices" in items[0]["variations"][0]
    assert fetcher.count(variations_url(1)) == 0


def test_odd_variation_ids_do_not_fail_the_store():
    product = variable(1, 10) | {"variations": [{"id": [9]}, {"id": 10, "attributes": []}]}
    routes = {
        woo_url(1): [product],
        variations_url(1): [variation({"x": 1}, 1), variation(10, 1)],
        woo_url(2): [],
    }
    items, result, _ = scrape_woo(routes)
    assert result.stores[0].status == "ok"
    assert items[0]["variations"][0] == {"id": [9]}
    assert items[0]["variations"][1]["sku"] == "SKU-10"


def test_variation_listing_blocked_by_robots():
    routes = {
        f"{STORE}/robots.txt": (200, "User-agent: *\nDisallow: /*type=variation"),
        woo_url(1): [variable(1, 10)],
        variations_url(1): [variation(10, 1)],
        woo_url(2): [],
    }
    items, result, fetcher = scrape_woo(routes)
    assert items[0]["variations"] == variable(1, 10)["variations"]
    assert fetcher.count(variations_url(1)) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].robots_disallowed == 0


def test_limit_skips_variations_of_cut_off_products():
    routes = {
        woo_url(1): [variable(1, 10), variable(2, 20), variable(3, 30)],
        variations_url(1, 2): [variation(10, 1), variation(20, 2)],
    }
    items, result, fetcher = scrape_woo(routes, max_items_per_store=2)
    assert len(items) == 2
    assert all("prices" in i["variations"][0] for i in items)
    assert [r for r in requested(fetcher) if "type=variation" in r] == [variations_url(1, 2)]
    assert result.stores[0].finish_reason == "item_limit"


def test_slow_variation_listing_does_not_use_up_bail():
    async def hang(request):
        await asyncio.sleep(30)

    routes = {
        woo_url(1): [variable(1, 10)],
        variations_url(1): hang,
        woo_url(2): [variable(2, 20)],
        variations_url(2): [variation(20, 2)],
        woo_url(3): [],
    }
    started = time.monotonic()
    items, result, _ = scrape_woo(routes, bail=1)
    assert time.monotonic() - started < 5
    assert items[0]["variations"] == variable(1, 10)["variations"]
    # Bail no longer applies once an item is out, so the next page tries again
    assert "prices" in items[1]["variations"][0]
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete
