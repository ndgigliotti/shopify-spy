"""WooCommerce fallback to product pages when the Store API is unavailable (#42)."""

import html
import json
from decimal import Decimal

import pytest

import shopify_spy as spy
from shopify_spy._flatten import COLUMNS, flatten
from shopify_spy._jsonld import offers as read_offers
from shopify_spy._jsonld import parse_product_page
from shopify_spy._jsonld import variation_form as read_form
from shopify_spy._woocommerce import _prices, page_item
from tests.flow_support import STORE, scrape, sitemapindex, urlset, woo_url

WP_LIST = (
    f"{STORE}/wp-json/wp/v2/product?per_page=100&page={{}}&_fields=id%2Clink&orderby=id&order=asc"
)


def wp_list(page):
    return WP_LIST.format(page)


def page_html(name, price="10.00", post_id=7, currency="USD", extra=""):
    ld = {
        "@context": "https://schema.org/",
        "@graph": [
            {"@type": "BreadcrumbList", "itemListElement": []},
            {
                "@type": "Product",
                "@id": f"{STORE}/product/{name}/#product",
                "name": name.title(),
                "sku": f"SKU-{name}",
                "image": f"https://cdn.test/{name}.jpg",
                "offers": [
                    {
                        "@type": "Offer",
                        "price": price,
                        "priceCurrency": currency,
                        "availability": "https://schema.org/InStock",
                    }
                ],
            },
        ],
    }
    return (
        200,
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f'<body class="product-template-default single single-product postid-{post_id}">'
        f"{extra}</body></html>",
        {"Content-Type": "text/html"},
    )


def product_url(name):
    return f"{STORE}/product/{name}/"


def scrape_woo(routes, **options):
    return scrape(routes, platform="woocommerce", **options)


def test_store_api_403_falls_back_to_listed_product_pages():
    routes = {
        woo_url(1): (403, "<html>blocked</html>"),
        wp_list(1): (
            200,
            [{"id": 7, "link": product_url("hat")}, {"id": 8, "link": product_url("cap")}],
            {"X-WP-TotalPages": "1"},
        ),
        product_url("hat"): page_html("hat", "19.90", post_id=7),
        product_url("cap"): page_html("cap", "5", post_id=8),
    }
    items, result, fetcher = scrape_woo(routes)
    by_slug = {item["slug"]: item for item in items}
    assert set(by_slug) == {"hat", "cap"}
    hat = by_slug["hat"]
    assert hat["source"] == "product_page"
    assert hat["id"] == 7
    assert hat["name"] == "Hat"
    assert hat["sku"] == "SKU-hat"
    assert hat["permalink"] == product_url("hat")
    assert hat["store"] == "shop.test"
    assert hat["images"] == [{"src": "https://cdn.test/hat.jpg"}]
    assert hat["prices"]["price"] == "1990"
    assert hat["prices"]["currency_minor_unit"] == 2
    assert hat["is_in_stock"] is True
    assert hat["json_ld"]["@type"] == "Product"
    summary = spy.summarize(hat)
    assert (summary.min_price, summary.currency) == (Decimal("19.90"), "USD")
    store = result.stores[0]
    assert store.status == "ok"
    assert store.complete  # X-WP-TotalPages says page 1 was the last
    assert fetcher.count(f"{STORE}/wp-sitemap.xml") == 0  # the listing was enough


def test_short_listing_page_is_not_the_end():
    # A host may cap per_page below 100; only an empty page ends the listing
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): [{"id": i, "link": product_url(f"p{i}")} for i in range(50)],
        wp_list(2): [{"id": i, "link": product_url(f"p{i}")} for i in range(50, 60)],
        wp_list(3): [],
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(60)},
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 60
    assert fetcher.count(wp_list(4)) == 0
    assert result.stores[0].complete


def test_listing_pages_until_total_pages():
    full = [{"id": i, "link": product_url(f"p{i}")} for i in range(100)]
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): full,
        wp_list(2): (200, [{"id": 100, "link": product_url("p100")}], {"X-WP-TotalPages": "2"}),
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(101)},
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 101
    assert fetcher.count(wp_list(3)) == 0
    assert result.stores[0].complete


def test_listing_past_last_page_is_the_end():
    full = [{"id": i, "link": product_url(f"p{i}")} for i in range(100)]
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): full,
        wp_list(2): (400, {"code": "rest_post_invalid_page_number"}),
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(100)},
    }
    items, result, _ = scrape_woo(routes)
    assert len(items) == 100
    assert result.stores[0].complete


def test_listing_cut_short_is_filled_from_sitemaps():
    full = [{"id": i, "link": product_url(f"p{i}")} for i in range(100)]
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): full,
        wp_list(2): (500, ""),
        f"{STORE}/wp-sitemap.xml": sitemapindex(f"{STORE}/wp-sitemap-posts-product-1.xml"),
        f"{STORE}/wp-sitemap-posts-product-1.xml": urlset(product_url("p0"), product_url("p100")),
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(101)},
    }
    items, result, fetcher = scrape_woo(routes)
    assert len(items) == 101
    assert fetcher.count(product_url("p0")) == 1  # listed by both
    assert result.stores[0].complete


def test_listing_cut_short_without_sitemaps_is_partial():
    full = [{"id": i, "link": product_url(f"p{i}")} for i in range(100)]
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): full,
        wp_list(2): (500, ""),
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(100)},
    }
    items, result, _ = scrape_woo(routes)
    assert len(items) == 100
    assert result.stores[0].status == "partial"


def test_falls_back_to_product_sitemaps_without_listing():
    routes = {
        woo_url(1): (200, "<html>not json</html>"),
        wp_list(1): (401, {"code": "rest_forbidden"}),
        f"{STORE}/wp-sitemap.xml": sitemapindex(
            f"{STORE}/wp-sitemap-posts-page-1.xml",
            f"{STORE}/wp-sitemap-posts-product-1.xml",
            f"{STORE}/wp-sitemap-taxonomies-product_cat-1.xml",
            "https://elsewhere.test/wp-sitemap-posts-product-1.xml",
        ),
        f"{STORE}/wp-sitemap-posts-product-1.xml": urlset(
            f"{STORE}/shop/", product_url("hat"), "https://elsewhere.test/product/x/"
        ),
        f"{STORE}/shop/": (200, "<html><body>archive</body></html>"),
        product_url("hat"): page_html("hat"),
    }
    items, result, fetcher = scrape_woo(routes)
    assert [item["slug"] for item in items] == ["hat"]
    assert fetcher.count(f"{STORE}/wp-sitemap-posts-page-1.xml") == 0
    assert fetcher.count(f"{STORE}/wp-sitemap-taxonomies-product_cat-1.xml") == 0
    assert fetcher.count("https://elsewhere.test/product/x/") == 0
    # The off-store sitemap and loc may list products this run didn't get
    assert result.stores[0].status == "partial"


def test_yoast_sitemap_index_after_core_sitemap_missing():
    routes = {
        woo_url(1): (404, ""),
        f"{STORE}/sitemap_index.xml": sitemapindex(
            f"{STORE}/post-sitemap.xml",
            f"{STORE}/product-sitemap.xml",
            f"{STORE}/product_cat-sitemap.xml",
        ),
        f"{STORE}/product-sitemap.xml": urlset(product_url("hat")),
        product_url("hat"): page_html("hat"),
    }
    items, _, fetcher = scrape_woo(routes)
    assert [item["slug"] for item in items] == ["hat"]
    assert fetcher.count(f"{STORE}/post-sitemap.xml") == 0
    assert fetcher.count(f"{STORE}/product_cat-sitemap.xml") == 0


def test_flat_sitemap_is_not_crawled():
    # A urlset root doesn't say which pages are products
    routes = {
        woo_url(1): (404, ""),
        f"{STORE}/wp-sitemap.xml": urlset(product_url("hat"), f"{STORE}/about/"),
        product_url("hat"): page_html("hat"),
    }
    items, result, fetcher = scrape_woo(routes)
    assert items == []
    assert fetcher.count(product_url("hat")) == 0
    assert result.stores[0].status == "not_found"


def test_failed_fallback_keeps_the_store_api_diagnosis():
    # The fallback's own 404s don't turn a forbidden store into a not-found one
    items, result, _ = scrape_woo({woo_url(1): (403, "")})
    assert items == []
    store = result.stores[0]
    assert store.status == "forbidden"
    assert set(store.responses_by_status) == {403}


def test_robots_blocked_api_falls_back_to_allowed_pages():
    routes = {
        f"{STORE}/robots.txt": (200, "User-agent: *\nDisallow: /wp-json/\n"),
        f"{STORE}/wp-sitemap.xml": sitemapindex(f"{STORE}/wp-sitemap-posts-product-1.xml"),
        f"{STORE}/wp-sitemap-posts-product-1.xml": urlset(product_url("hat")),
        product_url("hat"): page_html("hat"),
    }
    items, _, fetcher = scrape_woo(routes)
    assert [item["slug"] for item in items] == ["hat"]
    assert fetcher.count(woo_url(1)) == 0
    assert fetcher.count(wp_list(1)) == 0
    # The same API under another URL is still what robots.txt disallowed
    assert not any("rest_route" in r.url for r in fetcher.requests)


def test_no_fallback_after_shopify_hint():
    routes = {woo_url(1): (404, ""), f"{STORE}/products.json": {"products": [{"id": 1}]}}
    _, result, fetcher = scrape_woo(routes)
    assert result.stores[0].hint is not None
    assert fetcher.count(wp_list(1)) == 0
    assert fetcher.count(f"{STORE}/wp-sitemap.xml") == 0


def test_no_fallback_for_empty_catalog():
    _, result, fetcher = scrape_woo({woo_url(1): []})
    assert fetcher.count(wp_list(1)) == 0
    assert result.stores[0].complete


def test_pages_without_product_data_are_skipped():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): [{"id": 1, "link": product_url("a")}, {"id": 2, "link": product_url("b")}],
        product_url("a"): (200, "<html><body>no structured data</body></html>"),
        product_url("b"): page_html("b"),
    }
    items, _, _ = scrape_woo(routes)
    assert [item["slug"] for item in items] == ["b"]


def test_fallback_respects_item_limit():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): [{"id": i, "link": product_url(f"p{i}")} for i in range(20)],
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(20)},
    }
    items, result, _ = scrape_woo(routes, max_items_per_store=3)
    assert len(items) == 3
    assert result.stores[0].finish_reason == "item_limit"


def test_listing_link_off_host_skipped():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): [{"id": 1, "link": "https://elsewhere.test/product/a/"}],
    }
    items, _, fetcher = scrape_woo(routes)
    assert items == []
    assert fetcher.count("https://elsewhere.test/product/a/") == 0


def test_page_item_csv_row_is_marked():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): [{"id": 7, "link": product_url("hat")}],
        product_url("hat"): page_html("hat", "19.90"),
    }
    items, _, _ = scrape_woo(routes)
    assert items[0]["platform"] == "woocommerce"
    (row,) = flatten(items[0])
    assert set(row) <= set(COLUMNS)
    assert {k: v for k, v in row.items() if v not in (None, "", 0)} == {
        "platform": "woocommerce",
        "kind": "product",
        "store": "shop.test",
        "id": 7,
        "handle": "hat",
        "title": "Hat",
        "html_url": product_url("hat"),
        "image_url": "https://cdn.test/hat.jpg",
        "image_count": 1,
        "currency": "USD",
        "min_price": Decimal("19.90"),
        "max_price": Decimal("19.90"),
        "sku": "SKU-hat",
        "price": Decimal("19.90"),
        "available": True,
        "source": "product_page",
    }


def test_store_api_csv_row_source():
    (row,) = flatten({"id": 1, "name": "A", "permalink": "x", "prices": {}})
    assert row["source"] == "store_api"


# --- JSON-LD parsing --------------------------------------------------------------


def ld_page(*objects, body_class="single-product postid-42"):
    scripts = "".join(
        f'<script type="application/ld+json">{o if isinstance(o, str) else json.dumps(o)}</script>'
        for o in objects
    )
    return f'<html><head>{scripts}</head><body class="{body_class}"></body></html>'


def test_parse_picks_the_page_product_over_related_ones():
    url = f"{STORE}/product/hat/"
    html = ld_page(
        {"@type": "Product", "name": "Related", "url": f"{STORE}/product/other/"},
        {"@type": ["Product", "Thing"], "name": "Hat", "@id": f"{url}#product"},
    )
    product, post_id = parse_product_page(html, url)
    assert product["name"] == "Hat"
    assert post_id == 42


def test_parse_skips_broken_scripts_and_non_products():
    html = ld_page("{not json", {"@type": "Organization"}, body_class="page postid-x")
    assert parse_product_page(html, STORE) == (None, None)


def test_parse_handles_deep_nesting():
    deep = "[" * 100_000 + "]" * 100_000
    assert parse_product_page(ld_page(deep), STORE) == (None, 42)


def offer(**fields):
    return {"@type": "Offer", **fields}


@pytest.mark.parametrize(
    ("offers", "expected"),
    [
        # Plain offer
        (
            [offer(price="8.99", priceCurrency="GBP")],
            {"price": "899", "regular_price": "899", "price_range": None, "minor": 2},
        ),
        # Newer WooCommerce: price only in priceSpecification, sale shown as ListPrice
        (
            offer(
                priceSpecification=[
                    {"price": "20", "priceCurrency": "USD"},
                    {"price": "45", "priceType": "https://schema.org/ListPrice"},
                ]
            ),
            {"price": "20", "regular_price": "45", "price_range": None, "minor": 0},
        ),
        # Variable product
        (
            {"@type": "AggregateOffer", "lowPrice": "295.00", "highPrice": "5750.5"},
            {
                "price": "29500",
                "regular_price": "29500",
                "price_range": {"min_amount": "29500", "max_amount": "575050"},
                "minor": 2,
            },
        ),
        # Yoast's merged specification: a dict with a "0" key
        (
            offer(priceSpecification={"0": {"price": "19.00"}, "price": "19.00"}),
            {"price": "1900", "regular_price": "1900", "price_range": None, "minor": 2},
        ),
        # Numbers, and junk that is ignored
        (
            [offer(price=1210), offer(price="free"), offer(price=-1), offer(price=True)],
            {"price": "1210", "regular_price": "1210", "price_range": None, "minor": 0},
        ),
        # No offers
        (None, {"price": None, "regular_price": None, "price_range": None, "minor": 0}),
    ],
    ids=["offer", "spec_with_list_price", "aggregate", "yoast_dict", "numbers", "none"],
)
def test_prices_from_offers(offers, expected):
    found = read_offers(offers)
    prices = _prices(found.current, found.listed, found.bounds, found.currency)
    assert prices["price"] == expected["price"]
    assert prices["sale_price"] == expected["price"]
    assert prices["regular_price"] == expected["regular_price"]
    assert prices["price_range"] == expected["price_range"]
    assert prices["currency_minor_unit"] == expected["minor"]


def test_page_item_sale_and_stock():
    product = {
        "@type": "Product",
        "name": "Hat",
        "image": [
            {"@type": "ImageObject", "url": "https://cdn.test/a.jpg"},
            "https://cdn.test/b.jpg",
        ],
        "offers": offer(
            priceSpecification=[
                {"price": "20.00", "priceCurrency": "EUR"},
                {"price": "45.00", "priceType": "https://schema.org/StrikethroughPrice"},
            ],
            availability="http://schema.org/OutOfStock",
        ),
    }
    item = page_item(product, f"{STORE}/product/hat%C3%A9/", None)
    assert item["slug"] == "haté"
    assert item["id"] is None
    assert item["on_sale"] is True
    assert item["is_in_stock"] is False
    assert item["prices"]["currency_code"] == "EUR"
    assert [i["src"] for i in item["images"]] == [
        "https://cdn.test/a.jpg",
        "https://cdn.test/b.jpg",
    ]
    summary = spy.summarize(item)
    assert (summary.min_price, summary.max_price) == (Decimal("20.00"), Decimal("20.00"))


def test_page_item_without_offers():
    item = page_item({"@type": "Product", "name": "Hat"}, product_url("hat"), 3)
    assert item["on_sale"] is None
    assert item["is_in_stock"] is None
    assert spy.summarize(item).min_price is None


def test_parse_tag_variants():
    html = (
        "<SCRIPT TYPE='application/ld+json' id=x>"
        '{"@type": "Product", "name": "Hat"}</script >'
        '<script type="text/javascript">{"@type": "Product", "name": "No"}</script>'
        "<body id='b' class='single postid-9 woocommerce'>"
    )
    product, post_id = parse_product_page(html, STORE)
    assert product["name"] == "Hat"
    assert post_id == 9


# --- Review findings --------------------------------------------------------------


def listed(*names, total_pages=1):
    return (
        200,
        [{"id": i + 1, "link": product_url(n)} for i, n in enumerate(names)],
        {"X-WP-TotalPages": str(total_pages)},
    )


def test_xml_export_of_page_items_is_well_formed(tmp_path):
    from xml.etree import ElementTree

    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("hat"),
        product_url("hat"): page_html("hat"),
    }
    items, _, _ = scrape_woo(routes)
    path = tmp_path / "out.xml"
    spy.write_items(items, path)
    root = ElementTree.parse(path).getroot()
    assert root.find("item/json_ld/_type").text == "Product"


def test_failed_page_fetches_leave_store_partial():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("a", "b"),
        product_url("a"): page_html("a"),
        product_url("b"): (503, ""),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["a"]
    assert result.stores[0].status == "partial"


def test_repeated_listing_page_falls_through_to_sitemaps():
    full = [{"id": i, "link": product_url(f"p{i}")} for i in range(100)]
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): full,
        wp_list(2): full,
        f"{STORE}/wp-sitemap.xml": sitemapindex(f"{STORE}/wp-sitemap-posts-product-1.xml"),
        f"{STORE}/wp-sitemap-posts-product-1.xml": urlset(product_url("p100")),
        **{product_url(f"p{i}"): page_html(f"p{i}", post_id=i) for i in range(101)},
    }
    items, result, _ = scrape_woo(routes)
    assert len(items) == 101
    assert result.stores[0].complete


def test_www_links_belong_to_the_bare_domain_store():
    www = "https://www.shop.test/product/hat/"
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): (200, [{"id": 1, "link": www}], {"X-WP-TotalPages": "1"}),
        www: page_html("hat"),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["permalink"] for i in items] == [www]
    assert result.stores[0].complete


@pytest.mark.parametrize(
    "bad_page",
    [
        page_html("bad", price="1e5000"),
        page_html("bad", price="1E+1000000"),
        page_html("bad", post_id="9" * 5000),
    ],
    ids=["huge_exponent", "overflow", "huge_post_id"],
)
def test_hostile_page_does_not_end_the_store(bad_page):
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("bad", "good"),
        product_url("bad"): bad_page,
        product_url("good"): page_html("good"),
    }
    items, result, _ = scrape_woo(routes)
    assert sorted(i["slug"] for i in items) == ["bad", "good"]
    assert result.stores[0].status == "ok"


def test_malformed_listing_link_is_skipped():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): (
            200,
            [{"id": 1, "link": product_url("a")}, {"id": 2, "link": "http://[::1"}],
            {"X-WP-TotalPages": "1"},
        ),
        product_url("a"): page_html("a"),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["a"]
    assert result.stores[0].status == "partial"


def test_store_api_json_without_product_list_falls_back():
    routes = {
        woo_url(1): {"code": "rest_disabled", "message": "REST API disabled"},
        wp_list(1): listed("hat"),
        product_url("hat"): page_html("hat"),
    }
    items, _, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["hat"]


def test_rate_limited_store_api_does_not_fall_back():
    _, _, fetcher = scrape_woo({woo_url(1): (429, "")})
    assert fetcher.count(wp_list(1)) == 0
    assert fetcher.count(f"{STORE}/wp-sitemap.xml") == 0


def test_unavailable_store_api_falls_back():
    # A 503 is usually the API alone being down; the pages still answer
    routes = {
        woo_url(1): (503, ""),
        wp_list(1): listed("hat"),
        product_url("hat"): page_html("hat"),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["hat"]
    assert result.stores[0].status == "ok"


def test_listing_id_used_without_post_id_class():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("hat"),
        product_url("hat"): (
            200,
            '<script type="application/ld+json">{"@type": "Product", "name": "Hat"}</script>'
            '<body class="single-product">',
        ),
    }
    items, _, _ = scrape_woo(routes)
    assert items[0]["id"] == 1


def test_permalink_has_no_fragment():
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): (
            200,
            [{"id": 1, "link": product_url("hat") + "#reviews"}],
            {"X-WP-TotalPages": "1"},
        ),
        product_url("hat"): page_html("hat"),
    }
    items, _, _ = scrape_woo(routes)
    assert items[0]["permalink"] == product_url("hat")


def test_parse_allows_raw_newlines_in_strings():
    html = ld_page('{"@type": "Product", "description": "line1\nline2"}')
    product, _ = parse_product_page(html, STORE)
    assert product["description"] == "line1\nline2"


def test_redirect_to_a_fetched_page_is_not_a_failure():
    from tests.flow_support import redirect

    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("old", "hat"),
        product_url("old"): redirect(product_url("hat")),
        product_url("hat"): page_html("hat"),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["hat"]
    assert result.stores[0].status == "ok"


@pytest.mark.parametrize("status", [404, 410])
def test_deleted_listed_product_is_not_a_failure(status):
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("gone", "hat"),
        product_url("gone"): (status, ""),
        product_url("hat"): page_html("hat"),
    }
    items, result, _ = scrape_woo(routes)
    assert [i["slug"] for i in items] == ["hat"]
    assert result.stores[0].status == "ok"


# --- Variation form ---------------------------------------------------------------


def variation_form(records, selects=None):
    """WooCommerce's variation form, as cornforthimages.com renders it."""
    selects = selects or [
        ("pa_size", "Size", [("small", "Small"), ("large", "Large")]),
        ("paper-mounting", "Print Styles", [("Metal", "Metal"), ("Acrylic", "Acrylic")]),
    ]
    data = records if isinstance(records, str) else json.dumps(records)
    rows = "".join(
        f'<tr><th class="label"><label for="{field}">{label}</label></th><td class="value">'
        f'<select id="{field}" name="attribute_{field}" data-attribute_name="attribute_{field}">'
        '<option value="">Choose an option</option>'
        + "".join(f'<option value="{v}" >{t}</option>' for v, t in options)
        + "</select></td></tr>"
        for field, label, options in selects
    )
    return (
        '<form class="variations_form cart" method="post" data-product_id="7" '
        f'data-product_variations="{html.escape(data)}">'
        f'<table class="variations"><tbody>{rows}</tbody></table></form>'
    )


def variation(vid, size, style, price, regular=None, sku="", in_stock=True):
    return {
        "variation_id": vid,
        "attributes": {"attribute_pa_size": size, "attribute_paper-mounting": style},
        "display_price": price,
        "display_regular_price": regular if regular is not None else price,
        "sku": sku,
        "is_in_stock": in_stock,
        "variation_is_active": True,
    }


def variable_page(form):
    return page_html("print", price="12.50", post_id=7, extra=form)


def test_variation_form_becomes_variation_rows():
    form = variation_form(
        [
            variation(101, "small", "Metal", 12.5, sku="P-S-M"),
            variation(102, "large", "Acrylic", 30, regular=45, in_stock=False),
            variation(103, "large", "", 28),  # "any" print style
        ]
    )
    routes = {
        woo_url(1): (500, ""),
        wp_list(1): listed("print"),
        product_url("print"): variable_page(form),
    }
    items, _, _ = scrape_woo(routes)
    (item,) = items
    assert [a["name"] for a in item["attributes"]] == ["Size", "Print Styles"]
    assert item["variations"][0] == {
        "id": 101,
        "attributes": [
            {"name": "Size", "value": "small"},
            {"name": "Print Styles", "value": "Metal"},
        ],
        "prices": {
            "price": "125",
            "regular_price": "125",
            "sale_price": "125",
            "price_range": None,
            "currency_code": "USD",
            "currency_minor_unit": 1,
        },
        "sku": "P-S-M",
        "is_in_stock": True,
    }
    rows = flatten(item)
    assert [
        (r["variant_id"], r["variant_title"], r["option1"], r["option2"], r["sku"], r["price"])
        for r in rows
    ] == [
        (101, "Small / Metal", "Small", "Metal", "P-S-M", Decimal("12.5")),
        (102, "Large / Acrylic", "Large", "Acrylic", None, Decimal("30")),
        (103, "Large", "Large", None, None, Decimal("28")),
    ]
    assert rows[0]["options"] == "Size, Print Styles"
    assert rows[1]["compare_at_price"] == Decimal("45")
    assert rows[1]["available"] is False
    assert {r["source"] for r in rows} == {"product_page"}


@pytest.mark.parametrize(
    "records",
    ["false", "[not json", '{"not": "a list"}'],
    ids=["too_many_to_embed", "broken_json", "not_a_list"],
)
def test_unreadable_variation_form_keeps_one_row(records):
    item = page_item(
        {"@type": "Product", "name": "Print", "offers": offer(price="12.50")},
        product_url("print"),
        7,
        read_form(variation_form(records), 7),
    )
    assert "variations" not in item
    assert len(flatten(item)) == 1


def test_variation_form_without_labels_uses_field_names():
    form = variation_form([variation(101, "small", "Metal", 10)]).replace("<label", "<span")
    item = page_item(
        {"@type": "Product", "name": "Print"}, product_url("print"), 7, read_form(form, 7)
    )
    assert [a["name"] for a in item["variations"][0]["attributes"]] == [
        "size",
        "paper-mounting",
    ]


def test_variation_without_price_keeps_product_price():
    record = variation(101, "small", "Metal", None, sku="P-S", in_stock=False)
    form = variation_form([record | {"display_price": ""}])
    item = page_item(
        {"@type": "Product", "name": "Print", "offers": offer(price="12.50")},
        product_url("print"),
        7,
        read_form(form, 7),
    )
    (row,) = flatten(item)
    assert row["variant_id"] == 101
    assert row["price"] == Decimal("12.50")
    # Its own SKU and stock still apply
    assert (row["sku"], row["available"]) == ("P-S", False)


def test_related_products_variation_form_is_not_used():
    mine = variation_form([variation(101, "small", "Metal", 10)])
    related = mine.replace('data-product_id="7"', 'data-product_id="99"').replace("101", "991")
    form = read_form(related + mine, 7)
    assert [r["variation_id"] for r in form.records] == [101]
    # A related product's form alone isn't this product's
    assert read_form(related, 7) is None


def test_two_forms_without_product_ids_are_ambiguous():
    plain = variation_form([variation(101, "small", "Metal", 10)]).replace(
        ' data-product_id="7"', ""
    )
    assert read_form(plain, 7) is not None
    assert read_form(plain + plain, 7) is None


def test_form_attributes_match_exact_names():
    form = variation_form(
        [variation(101, "small", "Metal", 10)],
        selects=[("pa_size", "Size", [("small", "Small")])],
    )
    form = form.replace('<select id="pa_size"', '<select data-id="other" id="pa_size"').replace(
        '<option value="small"', '<option data-value="#f00" value="small"'
    )
    found = read_form(form, 7)
    assert found.fields["attribute_pa_size"].name == "Size"
    assert found.fields["attribute_pa_size"].terms == {"small": "Small"}


def test_unlabeled_global_and_local_attributes_keep_distinct_names():
    record = {
        "variation_id": 1,
        "attributes": {"attribute_pa_size": "s", "attribute_size": "tall"},
        "display_price": 5,
    }
    form = variation_form(
        [record],
        selects=[("pa_size", "", [("s", "S")]), ("size", "", [("tall", "Tall")])],
    ).replace("<label", "<span")
    found = read_form(form, 7)
    assert [f.name for f in found.fields.values()] == ["pa_size", "size"]


def test_escaped_names_are_unescaped_once():
    form = variation_form(
        [variation(101, "rd", "Metal", 10)],
        selects=[("pa_size", "Size &amp; Fit", [("rd", "R&amp;D"), ("lt", "&amp;lt;3")])],
    )
    item = page_item(
        {"@type": "Product", "name": "Print"}, product_url("print"), 7, read_form(form, 7)
    )
    (row,) = flatten(item)
    assert row["options"].startswith("Size & Fit")
    assert row["option1"] == "R&D"
    assert item["attributes"][0]["terms"][1]["name"] == "&amp;lt;3"  # shows as "&lt;3"


def test_hostile_unclosed_tags_parse_quickly():
    import time

    form = variation_form([variation(101, "small", "Metal", 10)])
    hostile = form.replace("</form>", "") + "<label for=x>a" * 20_000 + "<select>" * 2_000
    started = time.perf_counter()
    read_form(hostile, 7)
    assert time.perf_counter() - started < 1
