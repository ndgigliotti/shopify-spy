"""Magento adapter flows through spy.scrape with a FakeFetcher."""

import logging

import pytest

from shopify_spy._fetch import FetchConnectionError
from tests.flow_support import STORE, magento_url, redirect, requested, scrape


def mg_product(i, **extra):
    return {
        "__typename": "SimpleProduct",
        "uid": f"uid{i}",
        "sku": f"SKU{i}",
        "name": f"Product {i}",
        "url_key": f"p{i}",
        "url_suffix": ".html",
        "media_gallery": [
            {"url": f"https://cdn.test/{i}-b.jpg", "position": 2, "disabled": False},
            {"url": f"https://cdn.test/{i}-x.jpg", "position": 0, "disabled": True},
            {"url": f"https://cdn.test/{i}-a.jpg", "position": 1, "disabled": False},
        ],
        **extra,
    }


def answer(items, total, errors=None):
    body = {"data": {"products": {"total_count": total, "items": items}}}
    return body | ({"errors": errors} if errors else {})


def page(n, start=0, total=None):
    items = [mg_product(i) for i in range(start, start + n)]
    return answer(items, start + n if total is None else total)


def errors(*messages):
    return {"errors": [{"message": m} for m in messages]}


def scrape_mg(routes, **options):
    return scrape(routes, platform="magento", **options)


def test_pages_until_total_count():
    routes = {
        magento_url(1): page(100, 0, 205),
        magento_url(2): page(100, 100, 205),
        magento_url(3): page(5, 200, 205),
    }
    items, result, fetcher = scrape_mg(routes)
    assert len(items) == 205
    assert requested(fetcher) == [magento_url(1), magento_url(2), magento_url(3)]
    assert fetcher.count(magento_url(4)) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_empty_page_ends_paging():
    routes = {magento_url(1): page(100, 0, 500), magento_url(2): answer([], 500)}
    items, result, fetcher = scrape_mg(routes)
    assert len(items) == 100
    assert fetcher.count(magento_url(3)) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_item_shape_without_images():
    items, _, _ = scrape_mg({magento_url(1): answer([mg_product(0)], 1)})
    assert items == [
        {
            **mg_product(0),
            "store": "shop.test",
            "platform": "magento",
            "html_url": f"{STORE}/p0.html",
            "image_urls": [],
        }
    ]


def test_item_image_urls_by_position(tmp_path):
    routes = {magento_url(1): answer([mg_product(0)], 1)}
    items, _, _ = scrape_mg(routes, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/0-a.jpg", "https://cdn.test/0-b.jpg"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"url_key": None}, None),
        ({"url_key": ""}, None),
        ({"url_suffix": None}, f"{STORE}/p0"),
    ],
)
def test_html_url(fields, expected):
    items, _, _ = scrape_mg({magento_url(1): answer([mg_product(0, **fields)], 1)})
    assert items[0]["html_url"] == expected


def test_html_url_follows_a_redirect():
    other = "https://www.shop.test"
    routes = {
        magento_url(1): redirect(magento_url(1, store=other)),
        magento_url(1, store=other): page(100, 0, 101),
        magento_url(2, store=other): page(1, 100, 101),
    }
    items, result, fetcher = scrape_mg(routes)
    assert [i["html_url"] for i in items[99:]] == [f"{other}/p99.html", f"{other}/p100.html"]
    assert items[0]["store"] == "www.shop.test"
    assert fetcher.count(magento_url(2)) == 0
    assert result.stores[0].complete


@pytest.mark.parametrize(
    ("message", "size"),
    [("Maximum pageSize is 30", 30), ("pageSize is too large", 50), ("page size 500", 50)],
    ids=["named", "halved", "named_larger"],
)
def test_page_size_error_on_first_page_shrinks_the_page_size(message, size):
    routes = {
        magento_url(1): errors(message),
        magento_url(1, size): page(size, 0, size + 2),
        magento_url(2, size): page(2, size, size + 2),
    }
    items, result, fetcher = scrape_mg(routes)
    assert len(items) == size + 2
    assert fetcher.count(magento_url(2, size)) == 1
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_page_size_error_in_a_500_json_body_shrinks_the_page_size():
    routes = {
        magento_url(1): (500, errors("pageSize must be at most 10")),
        magento_url(1, 10): page(3, 0, 3),
    }
    items, result, _ = scrape_mg(routes)
    assert len(items) == 3
    assert result.stores[0].status == "ok"


def test_page_size_error_on_a_later_page_does_not_shrink():
    routes = {magento_url(1): page(100, 0, 300), magento_url(2): errors("Maximum pageSize is 30")}
    items, result, fetcher = scrape_mg(routes)
    assert len(items) == 100
    assert all("pageSize%3A+100" in url for url in requested(fetcher))
    assert result.stores[0].status == "partial"
    assert not result.stores[0].complete


def test_past_the_end_error_finishes():
    message = "currentPage value 2 specified is greater than the 1 page(s) available."
    routes = {magento_url(1): page(100, 0, 150), magento_url(2): errors(message)}
    items, result, _ = scrape_mg(routes)
    assert len(items) == 100
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (errors("Field 'uid' is not defined"), "Field 'uid' is not defined"),
        ((200, "<html>maintenance</html>"), "not JSON"),
        ({"data": {"products": None}}, "no products in the answer"),
        ((500, errors("Internal server error")), "Internal server error"),
        ((400, errors("Max Aliases in query should be 10")), "Max Aliases"),
    ],
    ids=["errors_only", "non_json", "no_products", "500_json_errors", "400_json_errors"],
)
def test_first_page_without_products_is_an_error(spec, reason):
    items, result, _ = scrape_mg({magento_url(1): spec})
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert reason in store.errors[-1]
    assert store.hint is None
    assert "query=" not in store.errors[-1]


def test_errors_only_with_wordpress_gives_the_woocommerce_hint():
    routes = {
        magento_url(1): errors("Cannot query field products"),
        f"{STORE}/wp-json/": {"namespaces": ["wp/v2"]},
    }
    _, result, _ = scrape_mg(routes)
    store = result.stores[0]
    assert store.status == "error"
    assert store.hint == "looks like WooCommerce; pass platform='woocommerce'"


@pytest.mark.parametrize(
    "spec",
    [errors("Something broke"), (200, "<html>"), (500, errors("Internal server error"))],
    ids=["errors_only", "non_json", "500_json_errors"],
)
def test_later_page_without_products_is_partial(spec, caplog):
    routes = {magento_url(1): page(100, 0, 300), magento_url(2): spec}
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, fetcher = scrape_mg(routes)
    assert len(items) == 100
    assert fetcher.count(magento_url(3)) == 0
    assert result.stores[0].status == "partial"
    assert "Magento GraphQL failed" in caplog.text


def test_errors_alongside_items_are_logged(caplog):
    body = answer([mg_product(0)], 1, errors=[{"message": "Could not resolve image"}])
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _ = scrape_mg({magento_url(1): body})
    assert len(items) == 1
    assert result.stores[0].status == "ok"
    assert "Could not resolve image" in caplog.text


@pytest.mark.parametrize(
    ("probe", "hint"),
    [
        ({"/products.json": {"products": [{"id": 1}]}}, "shopify"),
        ({"/wp-json/": {"namespaces": ["wc/store/v1"]}}, "woocommerce"),
        ({}, None),
    ],
    ids=["shopify", "woocommerce", "neither"],
)
def test_404_on_first_page(probe, hint):
    routes = {magento_url(1): (404, "")} | {f"{STORE}{p}": spec for p, spec in probe.items()}
    items, result, _ = scrape_mg(routes)
    assert items == []
    store = result.stores[0]
    assert store.status == "not_found"
    assert store.suggested_platform == hint


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, "forbidden"), (429, "rate_limited"), (503, "unavailable")],
)
def test_http_failure_on_first_page(status, expected):
    items, result, _ = scrape_mg({magento_url(1): (status, "")}, retries=0)
    assert items == []
    assert result.stores[0].status == expected


def test_connection_failure_on_first_page():
    items, result, _ = scrape_mg({magento_url(1): FetchConnectionError("reset")}, retries=0)
    assert items == []
    assert result.stores[0].status == "no_response"


@pytest.mark.parametrize(
    "failure",
    [FetchConnectionError("reset"), (503, ""), (404, "")],
    ids=["transport", "unavailable", "not_found"],
)
def test_failure_on_a_later_page_is_partial(failure):
    routes = {magento_url(1): page(100, 0, 300), magento_url(2): failure}
    items, result, fetcher = scrape_mg(routes, retries=0)
    assert len(items) == 100
    assert fetcher.count(magento_url(3)) == 0
    assert result.stores[0].status == "partial"
    assert not result.stores[0].complete


def test_robots_txt_blocks_graphql():
    routes = {
        f"{STORE}/robots.txt": (200, "User-agent: *\nDisallow: /graphql"),
        magento_url(1): page(3),
    }
    items, result, fetcher = scrape_mg(routes)
    assert items == []
    assert fetcher.count(magento_url(1)) == 0
    assert result.stores[0].status == "robots_blocked"


def test_repeated_page_stops(caplog):
    routes = {magento_url(1): page(100, 0, 300), magento_url(2): page(100, 0, 300)}
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, fetcher = scrape_mg(routes)
    assert len(items) == 100
    assert fetcher.count(magento_url(3)) == 0
    assert result.stores[0].status == "partial"
    assert "repeated" in caplog.text


def test_shortfall_without_repeats_stays_ok(caplog):
    routes = {magento_url(1): page(100, 0, 250), magento_url(2): answer([], 250)}
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _ = scrape_mg(routes)
    assert len(items) == 100
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete
    assert "100 products, fewer than total_count 250" in caplog.text


def test_shortfall_with_a_repeat_is_partial(caplog):
    # Page 2 repeats one product from page 1: the order shifted, so one was missed
    second = answer([mg_product(99)] + [mg_product(i) for i in range(100, 199)], 200)
    routes = {magento_url(1): page(100, 0, 200), magento_url(2): second}
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _ = scrape_mg(routes)
    assert len(items) == 199
    assert len({i["uid"] for i in items}) == 199
    assert result.stores[0].status == "partial"
    assert not result.stores[0].complete
    assert "199 products, fewer than total_count 200" in caplog.text


def test_limit_stops_exactly():
    routes = {magento_url(1): page(100, 0, 300), magento_url(2): page(100, 100, 300)}
    items, result, fetcher = scrape_mg(routes, max_items_per_store=150)
    assert len(items) == 150
    assert fetcher.count(magento_url(3)) == 0
    assert result.stores[0].finish_reason == "item_limit"
