"""Generic JSON-LD platform flows through spy.scrape with a FakeFetcher."""

import asyncio
import re
import time

import pytest

import shopify_spy as spy
from shopify_spy._fetch import FakeFetcher, FetchConnectionError, Seq
from shopify_spy._generic import FETCH_CONCURRENCY, HINT_AFTER, PROBE_PAGES
from shopify_spy._magento import STORE_CONFIG_PATH
from tests.flow_support import (
    FAST,
    STORE,
    product_page,
    redirect,
    requested,
    scrape,
    sitemapindex,
    urlset,
)

ROBOTS = f"{STORE}/robots.txt"
SITEMAP = f"{STORE}/sitemap.xml"
SHOPIFY = {"products": []}
WOOCOMMERCE = {"namespaces": ["wc/store/v1"]}
MAGENTO = {"data": {"storeConfig": {"store_name": "Default Store View"}}}
STORE_CONFIG = f"{STORE}{STORE_CONFIG_PATH}"


def url(i, store=STORE):
    return f"{store}/products/p{i}"


def node(i, store=STORE, **extra):
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"Product {i}",
        "sku": f"SKU{i}",
        "url": url(i, store),
        "image": f"https://cdn.test/{i}.jpg",
        "offers": {
            "@type": "Offer",
            "price": "10.00",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        **extra,
    }


def blank_page():
    return 200, "<html><body><div id='app'></div></body></html>", {"Content-Type": "text/html"}


def store(n, start=0, sitemap_url=SITEMAP, page=None):
    """A flat sitemap listing ``n`` product pages; ``page(i)`` overrides each page."""
    locs = [url(i) for i in range(start, start + n)]
    routes = {sitemap_url: urlset(*locs)}
    for i in range(start, start + n):
        routes[url(i)] = page(i) if page else product_page(node(i))
    return routes


def scrape_ld(routes, **options):
    return scrape(routes, platform="jsonld", **options)


def pages_requested(fetcher):
    return [u for u in requested(fetcher) if "/products/" in u]


def slow(spec, seconds):
    async def respond(request):
        await asyncio.sleep(seconds)
        return spec

    return respond


# --- Finding the sitemaps ---


def test_robots_sitemap_read_before_well_known_paths():
    routes = {
        ROBOTS: (200, f"User-agent: *\nSitemap: {STORE}/maps/main.xml\n"),
        f"{STORE}/maps/main.xml": urlset(url(0)),
        url(0): product_page(node(0)),
        **store(2, start=10),
    }
    items, result, fetcher = scrape_ld(routes)
    assert [i["name"] for i in items] == ["Product 0"]
    assert fetcher.count(SITEMAP) == 0
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_relative_robots_sitemap_resolved_against_robots_url():
    routes = {
        ROBOTS: (200, "User-agent: *\nSitemap: /sitemaps/index.xml\n"),
        f"{STORE}/sitemaps/index.xml": sitemapindex(f"{STORE}/sitemaps/products.xml"),
        **store(2, sitemap_url=f"{STORE}/sitemaps/products.xml"),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 2
    assert requested(fetcher)[:2] == [
        f"{STORE}/sitemaps/index.xml",
        f"{STORE}/sitemaps/products.xml",
    ]
    assert result.stores[0].complete


def test_robots_sitemaps_read_without_respecting_robots_txt():
    routes = {
        ROBOTS: (200, "User-agent: *\nDisallow: /\nSitemap: /maps/main.xml\n"),
        **store(2, sitemap_url=f"{STORE}/maps/main.xml"),
    }
    items, result, fetcher = scrape_ld(routes, respect_robots_txt=False)
    assert len(items) == 2
    assert fetcher.count(SITEMAP) == 0


def test_off_site_robots_sitemap_ignored():
    routes = {
        ROBOTS: (200, "Sitemap: https://elsewhere.test/sitemap.xml\n"),
        **store(1),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert fetcher.count("https://elsewhere.test/sitemap.xml") == 0
    assert result.stores[0].complete


def test_unparseable_robots_sitemap_falls_back_to_well_known_paths():
    routes = {
        ROBOTS: (200, "Sitemap: /maps/main.xml\n"),
        f"{STORE}/maps/main.xml": (200, "<html>not a sitemap</html>"),
        **store(1),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert requested(fetcher)[:2] == [f"{STORE}/maps/main.xml", SITEMAP]


def test_robots_sitemap_failing_to_respond_is_skipped():
    routes = {
        ROBOTS: (200, "Sitemap: /maps/a.xml\nSitemap: /maps/b.xml\n"),
        f"{STORE}/maps/a.xml": FetchConnectionError("connection reset"),
        **store(1, sitemap_url=f"{STORE}/maps/b.xml"),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert fetcher.count(SITEMAP) == 0
    # A listed sitemap went unread, so products may be missing
    assert result.stores[0].status == "partial"


def test_every_robots_sitemap_failing_falls_back_partial():
    routes = {
        ROBOTS: (200, "Sitemap: /maps/a.xml\nSitemap: /maps/b.xml\n"),
        f"{STORE}/maps/a.xml": (503, ""),
        f"{STORE}/maps/b.xml": FetchConnectionError("connection reset"),
        **store(1),
    }
    items, result, _ = scrape_ld(routes)
    assert len(items) == 1
    assert result.stores[0].status == "partial"


@pytest.mark.parametrize("path", ["/sitemap.xml", "/sitemap_index.xml", "/xmlsitemap.php"])
def test_well_known_paths_tried_in_order(path):
    routes = store(1, sitemap_url=f"{STORE}{path}")
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    tried = [u for u in requested(fetcher) if "/products/" not in u]
    order = ["/sitemap.xml", "/sitemap_index.xml", "/xmlsitemap.php"]
    assert tried == [f"{STORE}{p}" for p in order[: order.index(path) + 1]]
    assert result.stores[0].status == "ok"


def test_sitemap_redirecting_to_www():
    www = "https://www.shop.test"
    routes = {
        SITEMAP: redirect(f"{www}/sitemap.xml"),
        f"{www}/sitemap.xml": urlset(url(0, www), url(1, www)),
        url(0, www): product_page(node(0, www)),
        url(1, www): product_page(node(1, www)),
    }
    items, result, _ = scrape_ld(routes)
    assert [i["store"] for i in items] == ["www.shop.test", "www.shop.test"]
    assert result.stores[0].complete


# --- Product sitemaps ---


def index_store(children, read):
    """An index at /sitemap.xml over ``children``; those in ``read`` list a product."""
    routes = {SITEMAP: sitemapindex(*children)}
    for n, child in enumerate(children):
        routes[child] = urlset(url(n))
        routes[url(n)] = product_page(node(n))
    return routes


@pytest.mark.parametrize(
    ("children", "read"),
    [
        (
            [
                f"{STORE}/product-sitemap.xml",
                f"{STORE}/product_cat-sitemap.xml",
                f"{STORE}/product_tag-sitemap.xml",
                f"{STORE}/product_brand-sitemap.xml",
                f"{STORE}/page-sitemap.xml",
            ],
            [0],
        ),
        (
            [
                f"{STORE}/wp-sitemap-posts-product-1.xml",
                f"{STORE}/wp-sitemap-taxonomies-product_cat-1.xml",
                f"{STORE}/wp-sitemap-posts-page-1.xml",
            ],
            [0],
        ),
        (
            [
                f"{STORE}/xmlsitemap.php?type=categories&page=1",
                f"{STORE}/xmlsitemap.php?type=products&page=1",
                f"{STORE}/xmlsitemap.php?type=pages&page=1",
            ],
            [1],
        ),
        (
            [f"{STORE}/sitemap_Products_1.xml", f"{STORE}/sitemap_pages_1.xml"],
            [0],
        ),
        ([f"{STORE}/sitemap-a.xml", f"{STORE}/sitemap-b.xml"], [0, 1]),
    ],
    ids=["yoast", "wordpress-core", "bigcommerce", "case-insensitive", "no-product-children"],
)
def test_index_reads_only_product_sitemaps(children, read):
    items, result, fetcher = scrape_ld(index_store(children, read))
    assert sorted(i["name"] for i in items) == [f"Product {n}" for n in read]
    for n, child in enumerate(children):
        assert fetcher.count(child) == (n in read), child
    assert result.stores[0].complete


def test_off_site_child_sitemap_not_read():
    routes = {
        SITEMAP: sitemapindex("https://elsewhere.test/products.xml", f"{STORE}/products-1.xml"),
        f"{STORE}/products-1.xml": urlset(url(0)),
        url(0): product_page(node(0)),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert fetcher.count("https://elsewhere.test/products.xml") == 0
    assert not result.stores[0].complete


def test_unreadable_child_sitemap_is_partial():
    routes = {
        SITEMAP: sitemapindex(f"{STORE}/products-1.xml", f"{STORE}/products-2.xml"),
        f"{STORE}/products-1.xml": urlset(url(0)),
        f"{STORE}/products-2.xml": (503, ""),
        url(0): product_page(node(0)),
    }
    items, result, _ = scrape_ld(routes)
    assert len(items) == 1
    assert result.stores[0].status == "partial"


def test_off_site_non_product_child_sitemap_keeps_the_listing_complete():
    routes = {
        SITEMAP: sitemapindex(f"{STORE}/product-sitemap.xml", "https://blog.test/post-sitemap.xml"),
        **store(1, sitemap_url=f"{STORE}/product-sitemap.xml"),
    }
    items, result, _ = scrape_ld(routes)
    assert len(items) == 1
    assert result.stores[0].complete


def test_unreadable_non_product_robots_sitemap_keeps_the_listing_complete():
    routes = {
        ROBOTS: (200, "Sitemap: /product-sitemap.xml\nSitemap: /news-sitemap.xml\n"),
        f"{STORE}/news-sitemap.xml": (404, ""),
        **store(1, sitemap_url=f"{STORE}/product-sitemap.xml"),
    }
    items, result, _ = scrape_ld(routes)
    assert len(items) == 1
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_unreadable_product_robots_sitemap_is_partial():
    routes = {
        ROBOTS: (200, "Sitemap: /product-sitemap-1.xml\nSitemap: /product-sitemap-2.xml\n"),
        f"{STORE}/product-sitemap-2.xml": (503, ""),
        **store(1, sitemap_url=f"{STORE}/product-sitemap-1.xml"),
    }
    items, result, _ = scrape_ld(routes)
    assert len(items) == 1
    assert result.stores[0].status == "partial"


def test_product_preference_across_robots_sitemaps():
    routes = {
        ROBOTS: (200, "Sitemap: /sitemap-pages.xml\nSitemap: /sitemap-products.xml\n"),
        f"{STORE}/sitemap-pages.xml": urlset(f"{STORE}/about"),
        f"{STORE}/about": blank_page(),
        **store(2, sitemap_url=f"{STORE}/sitemap-products.xml"),
    }
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 2
    assert fetcher.count(f"{STORE}/about") == 0
    assert result.stores[0].complete


def test_product_preference_across_robots_sitemap_indexes():
    routes = {
        ROBOTS: (200, "Sitemap: /a.xml\nSitemap: /b.xml\n"),
        f"{STORE}/a.xml": sitemapindex(f"{STORE}/page-sitemap.xml"),
        f"{STORE}/b.xml": sitemapindex(f"{STORE}/product-sitemap.xml"),
        f"{STORE}/page-sitemap.xml": urlset(f"{STORE}/about"),
        **store(1, sitemap_url=f"{STORE}/product-sitemap.xml"),
    }
    items, _, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert fetcher.count(f"{STORE}/page-sitemap.xml") == 0


# --- Pages ---


def test_item_shape_without_images():
    items, result, _ = scrape_ld(store(1))
    assert items == [
        {
            **node(0),
            "store": "shop.test",
            "platform": "jsonld",
            "html_url": url(0),
            "image_urls": [],
        }
    ]
    assert result.stores[0].platform == "jsonld"


def test_html_url_drops_the_fragment():
    routes = {SITEMAP: urlset(f"{url(0)}#reviews"), url(0): product_page(node(0))}
    items, _, fetcher = scrape_ld(routes)
    assert items[0]["html_url"] == url(0)
    assert pages_requested(fetcher) == [url(0)]


def test_product_group_with_variants(tmp_path):
    group = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Tee",
        "url": url(0),
        "image": "https://cdn.test/tee.jpg",
        "variesBy": ["https://schema.org/size"],
        "hasVariant": [
            {"@type": "Product", "sku": "T-S", "size": "S", "image": "https://cdn.test/s.jpg"},
            {"@type": "Product", "sku": "T-M", "size": "M", "image": "https://cdn.test/tee.jpg"},
        ],
    }
    routes = {SITEMAP: urlset(url(0)), url(0): product_page(group)}
    items, _, _ = scrape_ld(routes, images=True, images_dir=tmp_path)
    [item] = items
    assert item["@type"] == "ProductGroup"
    assert item["hasVariant"] == group["hasVariant"]
    assert item["image_urls"] == ["https://cdn.test/tee.jpg", "https://cdn.test/s.jpg"]


def test_lone_variant_images_listed(tmp_path):
    group = {
        "@type": "ProductGroup",
        "url": url(0),
        "image": "https://cdn.test/tee.jpg",
        "hasVariant": {"@type": "Product", "image": "https://cdn.test/s.jpg"},
    }
    routes = {SITEMAP: urlset(url(0)), url(0): product_page(group)}
    items, _, _ = scrape_ld(routes, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/tee.jpg", "https://cdn.test/s.jpg"]


def test_images_on(tmp_path):
    items, _, _ = scrape_ld(store(1), images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/0.jpg"]


def test_relative_images_resolved_against_the_page(tmp_path):
    page = node(0, image=["/media/0.jpg", "//cdn.test/0b.jpg", "https://cdn.test/0c.jpg"])
    routes = {SITEMAP: urlset(url(0)), url(0): product_page(page)}
    items, _, _ = scrape_ld(routes, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == [
        f"{STORE}/media/0.jpg",
        "https://cdn.test/0b.jpg",
        "https://cdn.test/0c.jpg",
    ]
    assert items[0]["image"] == page["image"]


def test_page_without_product_gives_no_item():
    routes = store(3, page=lambda i: product_page(node(i)) if i else blank_page())
    items, result, _ = scrape_ld(routes)
    assert [i["name"] for i in items] == ["Product 1", "Product 2"]
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_unparseable_json_ld_skipped():
    broken = (200, '<script type="application/ld+json">{"@type": "Product",</script>')
    routes = store(2, page=lambda i: product_page(node(i)) if i else broken)
    items, result, _ = scrape_ld(routes)
    assert [i["name"] for i in items] == ["Product 1"]
    assert result.stores[0].status == "ok"


def category_page():
    return product_page(*(node(i, url=f"{STORE}/products/x{i}") for i in range(3)))


def test_listing_page_skipped():
    routes = {
        SITEMAP: urlset(f"{STORE}/collections/all", url(0), url(1)),
        f"{STORE}/collections/all": category_page(),
        url(0): product_page(node(0)),
        url(1): product_page(node(1)),
    }
    items, result, _ = scrape_ld(routes)
    assert sorted(i["name"] for i in items) == ["Product 0", "Product 1"]
    assert result.stores[0].status == "ok"


def test_listing_page_counts_as_a_page_without_an_item():
    routes = {
        SITEMAP: urlset(f"{STORE}/collections/all"),
        f"{STORE}/collections/all": category_page(),
    }
    _, result, _ = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert "no schema.org Product JSON-LD on 1 of 1 product pages checked" in s.errors[0]


def test_single_unmatched_product_kept():
    routes = {SITEMAP: urlset(url(0)), url(0): product_page(node(0, url=None))}
    items, _, _ = scrape_ld(routes)
    assert items[0]["name"] == "Product 0"


def test_group_beside_its_variants_kept():
    group = {"@type": "ProductGroup", "name": "Tee", "hasVariant": [{"@type": "Product"}]}
    variants = [{"@type": "Product", "name": f"Tee {s}"} for s in "SM"]
    routes = {SITEMAP: urlset(url(0)), url(0): product_page(group, *variants)}
    items, _, _ = scrape_ld(routes)
    assert [i["@type"] for i in items] == ["ProductGroup"]


def test_off_site_loc_never_requested():
    routes = {**store(1), SITEMAP: urlset(url(0), "https://elsewhere.test/products/p1")}
    items, result, fetcher = scrape_ld(routes)
    assert len(items) == 1
    assert fetcher.count("https://elsewhere.test/products/p1") == 0
    assert not result.stores[0].complete


def test_url_listed_twice_fetched_once():
    routes = {
        SITEMAP: sitemapindex(f"{STORE}/products-1.xml", f"{STORE}/products-2.xml"),
        f"{STORE}/products-1.xml": urlset(url(0), url(0)),
        f"{STORE}/products-2.xml": urlset(url(0), url(1)),
        url(0): product_page(node(0)),
        url(1): product_page(node(1)),
    }
    items, _, fetcher = scrape_ld(routes)
    assert len(items) == 2
    assert fetcher.count(url(0)) == 1


def test_max_items_per_store():
    items, result, fetcher = scrape_ld(store(30), max_items_per_store=3)
    assert len(items) == 3
    assert result.stores[0].finish_reason == "item_limit"
    assert len(pages_requested(fetcher)) < 30


def test_robots_disallowed_page_skipped():
    routes = {ROBOTS: (200, "User-agent: *\nDisallow: /products/p1\n"), **store(3)}
    items, result, fetcher = scrape_ld(routes)
    assert sorted(i["name"] for i in items) == ["Product 0", "Product 2"]
    assert fetcher.count(url(1)) == 0
    assert result.stores[0].status == "ok"


@pytest.mark.parametrize("status", [404, 410])
def test_gone_page_skipped(status):
    routes = store(3, page=lambda i: (status, "") if i == 1 else product_page(node(i)))
    items, result, _ = scrape_ld(routes)
    assert len(items) == 2
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_server_error_page_after_items_is_partial():
    routes = store(3, page=lambda i: (503, "") if i == 2 else product_page(node(i)))
    items, result, _ = scrape_ld(routes)
    assert len(items) == 2
    assert result.stores[0].status == "partial"


def test_transport_error_page_after_items_is_partial():
    err = FetchConnectionError("connection reset")
    routes = store(3, page=lambda i: err if i == 2 else product_page(node(i)))
    items, result, _ = scrape_ld(routes)
    assert len(items) == 2
    assert result.stores[0].status == "partial"


# --- No item: fast exits under the default bail ---


def assert_fast_exit(s):
    assert s.duration < 5
    assert not any("--bail" in e for e in s.errors)


def test_no_product_gives_up_after_probe_pages():
    routes = store(PROBE_PAGES + 50, page=lambda i: blank_page())
    items, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert items == []
    assert s.status == "error"
    assert s.finish_reason == "error"
    # Pages already in flight at the PROBE_PAGES mark are counted too
    found = re.search(r"no schema.org Product JSON-LD on (\d+) of (\d+) product pages", s.errors[0])
    assert found
    assert found[1] == found[2]
    assert PROBE_PAGES <= int(found[1]) < PROBE_PAGES + FETCH_CONCURRENCY
    assert f"product pages checked ({SITEMAP})" in s.errors[0]
    assert len(pages_requested(fetcher)) <= PROBE_PAGES + FETCH_CONCURRENCY
    assert_fast_exit(s)


def test_items_after_many_pages_without_product():
    """Pages in flight at the PROBE_PAGES mark can still give the first item."""
    n = PROBE_PAGES - 1
    routes = store(n + 20, page=lambda i: blank_page() if i < n else product_page(node(i)))
    items, result, _ = scrape_ld(routes)
    assert len(items) == 20
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ((403, ""), "12 answered 403"),
        ((429, ""), "12 answered 429"),
        ((503, ""), "12 answered 503"),
        (FetchConnectionError("connection refused"), "12 failed to respond"),
    ],
    ids=["403", "429", "503", "transport-error"],
)
def test_every_page_failing_is_an_error(spec, message):
    _, result, _ = scrape_ld(store(12, page=lambda i: spec))
    [s] = result.stores
    assert s.status == "error"
    assert f"on 0 of 12 product pages checked ({SITEMAP}): {message}" in s.errors[0]
    assert_fast_exit(s)


def test_short_all_429_listing_is_an_error():
    """Retries finish before --bail, so the store doesn't end rate_limited."""
    _, result, _ = scrape_ld(store(3, page=lambda i: (429, "")))
    [s] = result.stores
    assert s.status == "error"
    assert "3 answered 429" in s.errors[0]
    assert s.rate_limited_requests == 3
    assert_fast_exit(s)


def test_every_page_disallowed_by_robots_is_an_error():
    routes = {ROBOTS: (200, "User-agent: *\nDisallow: /products/\n"), **store(12)}
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert "on 0 of 12 product pages checked" in s.errors[0]
    assert "12 disallowed by robots.txt" in s.errors[0]
    assert pages_requested(fetcher) == []
    assert_fast_exit(s)


def test_no_on_site_page_is_empty():
    routes = {SITEMAP: urlset("https://elsewhere.test/a", "https://elsewhere.test/b")}
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "empty"
    assert s.errors == []
    assert pages_requested(fetcher) == []
    assert_fast_exit(s)


@pytest.mark.parametrize("spec", [(403, ""), (503, "")], ids=["403", "503"])
def test_unreadable_product_sitemap_is_an_error(spec):
    child = f"{STORE}/sitemap_products_1.xml"
    routes = {
        SITEMAP: sitemapindex(child, f"{STORE}/sitemap_pages_1.xml"),
        child: spec,
        f"{STORE}/sitemap_pages_1.xml": urlset(f"{STORE}/about"),
    }
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert f"no page listed; unreadable sitemaps: {child}" in s.errors[0]
    assert fetcher.count(f"{STORE}/about") == 0
    assert_fast_exit(s)


def test_empty_sitemap_is_empty():
    _, result, _ = scrape_ld({SITEMAP: urlset()})
    [s] = result.stores
    assert s.status == "empty"
    assert s.complete


# --- No sitemap ---


@pytest.mark.parametrize(
    ("routes", "status"),
    [
        ({}, "not_found"),
        (
            {
                SITEMAP: (403, ""),
                f"{STORE}/sitemap_index.xml": (403, ""),
                f"{STORE}/xmlsitemap.php": (403, ""),
            },
            "forbidden",
        ),
        ({ROBOTS: (200, "User-agent: *\nDisallow: /\n")}, "robots_blocked"),
    ],
    ids=["404", "403", "robots"],
)
def test_no_readable_sitemap(routes, status):
    _, result, _ = scrape_ld(routes)
    assert result.stores[0].status == status


def test_html_catch_all_is_no_sitemap_found():
    fetcher = FakeFetcher({}, default_status=200)
    _, result, _ = scrape_ld(fetcher)
    [s] = result.stores
    assert s.status == "error"
    assert f"no sitemap found at {SITEMAP}" in s.errors[0]


# --- Bail ---


def test_bail_with_pages_checked_is_an_error():
    routes = store(60, page=lambda i: slow(blank_page(), 0.1))
    _, result, fetcher = scrape_ld(routes, bail=0.4)
    [s] = result.stores
    assert s.status == "error"
    assert s.finish_reason == "bail"
    assert "product pages checked" in s.errors[0]
    assert "stopped at --bail (0.4s); raise --bail to check more pages" in s.errors[0]
    assert len(pages_requested(fetcher)) < 60


def test_bail_with_no_page_back_times_out():
    routes = store(60, page=lambda i: slow(blank_page(), 5))
    _, result, _ = scrape_ld(routes, bail=0.3)
    [s] = result.stores
    assert s.status == "timed_out"
    assert s.errors == []


def test_bail_with_rate_limited_pages_is_rate_limited():
    routes = store(60, page=lambda i: slow((429, ""), 0.2))
    _, result, _ = scrape_ld(routes, bail=0.5, retries=0)
    [s] = result.stores
    assert s.status == "rate_limited"
    assert s.finish_reason == "bail"
    assert s.errors == []


def test_retry_after_wait_outlasts_bail():
    routes = {
        SITEMAP: urlset(url(0)),
        url(0): Seq([(429, "", {"Retry-After": "1"}), slow(product_page(node(0)), 0.1)]),
    }
    items, result, _ = scrape_ld(routes, bail=0.3, _backoff=True, retries=1)
    assert result.stores[0].status == "ok"
    assert len(items) == 1


# --- Hint probe ---


def test_hint_after_probe_pages():
    routes = store(PROBE_PAGES + 10, page=lambda i: blank_page())
    routes[f"{STORE}/products.json"] = SHOPIFY
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert s.suggested_platform == "shopify"
    assert fetcher.count(f"{STORE}/products.json") == 1
    assert fetcher.count(f"{STORE}/wp-json/") == 0
    assert fetcher.count(STORE_CONFIG) == 0


def test_magento_hint():
    routes = {**store(3, page=lambda i: blank_page()), STORE_CONFIG: MAGENTO}
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert s.suggested_platform == "magento"
    assert s.hint == "looks like Magento; pass platform='magento'"
    assert fetcher.count(f"{STORE}/products.json") == 1
    assert fetcher.count(f"{STORE}/wp-json/") == 1
    assert fetcher.count(STORE_CONFIG) == 1


@pytest.mark.parametrize(
    "answer",
    [
        (404, "<html>Not Found</html>"),
        {"errors": [{"message": "Cannot query field storeConfig"}]},
        {"data": {"storeConfig": None}},
    ],
)
def test_no_magento_hint_without_store_config(answer):
    routes = {**store(3, page=lambda i: blank_page()), STORE_CONFIG: answer}
    _, result, fetcher = scrape_ld(routes)
    assert result.stores[0].suggested_platform is None
    assert fetcher.count(STORE_CONFIG) == 1


def test_magento_probe_obeys_robots_txt():
    routes = {
        **store(3, page=lambda i: blank_page()),
        ROBOTS: (200, "User-agent: *\nDisallow: /*?"),
        STORE_CONFIG: MAGENTO,
    }
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert s.suggested_platform is None
    assert fetcher.count(STORE_CONFIG) == 0


def test_hint_at_the_end_of_a_short_listing():
    assert HINT_AFTER > 3
    routes = {**store(3, page=lambda i: blank_page()), f"{STORE}/products.json": SHOPIFY}
    _, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert s.status == "error"
    assert s.suggested_platform == "shopify"
    assert fetcher.count(f"{STORE}/products.json") == 1


def test_hint_on_bail():
    routes = store(60, page=lambda i: slow(blank_page(), 0.1))
    routes[f"{STORE}/products.json"] = SHOPIFY
    _, result, _ = scrape_ld(routes, bail=0.5)
    [s] = result.stores
    assert s.status == "error"
    assert s.finish_reason == "bail"
    assert s.suggested_platform == "shopify"


def test_hint_with_no_sitemap():
    _, result, _ = scrape_ld({f"{STORE}/wp-json/": WOOCOMMERCE})
    [s] = result.stores
    assert s.status == "not_found"
    assert s.suggested_platform == "woocommerce"


def test_hint_with_html_catch_all():
    fetcher = FakeFetcher({f"{STORE}/products.json": SHOPIFY}, default_status=200)
    _, result, _ = scrape_ld(fetcher)
    assert result.stores[0].status == "error"
    assert result.stores[0].suggested_platform == "shopify"


def test_hanging_hint_probe_on_bail():
    routes = store(60, page=lambda i: slow(blank_page(), 0.05))
    routes[f"{STORE}/products.json"] = slow(SHOPIFY, 5)
    _, result, _ = scrape_ld(routes, bail=0.5)
    [s] = result.stores
    assert s.status == "error"
    assert "--bail" in s.errors[0]
    assert s.suggested_platform is None
    assert s.duration < 3


def test_item_after_the_hint_probe_started():
    n = HINT_AFTER + 2

    def page(i):
        # The products answer after the blank pages, so the probe is running by then
        return blank_page() if i < n else slow(product_page(node(i)), 0.2)

    routes = store(n + 3, page=page)
    routes[f"{STORE}/products.json"] = slow(SHOPIFY, 5)
    items, result, fetcher = scrape_ld(routes)
    [s] = result.stores
    assert len(items) == 3
    assert s.status == "ok"
    assert s.suggested_platform is None
    assert fetcher.count(f"{STORE}/products.json") == 1
    # Cancelled with the first item, not waited for
    assert s.duration < 3


def test_no_hint_probe_when_items_come_first():
    routes = {**store(3), f"{STORE}/products.json": SHOPIFY}
    _, result, fetcher = scrape_ld(routes)
    assert result.stores[0].suggested_platform is None
    assert fetcher.count(f"{STORE}/products.json") == 0


# --- Shutdown ---


async def test_request_shutdown_during_pages():
    fetcher = FakeFetcher(store(200, page=lambda i: slow(product_page(node(i)), 0.01)))
    async with asyncio.timeout(6):
        run = spy.scrape_async(STORE, platform="jsonld", fetcher_factory=lambda: fetcher, **FAST)
        start = time.monotonic()
        count = 0
        async for _ in run:
            count += 1
            if count == 1:
                run.request_shutdown()
        elapsed = time.monotonic() - start
    [s] = run.result.stores
    assert s.finish_reason == "shutdown"
    assert s.status in ("partial", "ok")
    assert count < 200
    assert elapsed < 5
