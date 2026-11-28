"""Squarespace platform flows through spy.scrape with a FakeFetcher."""

import logging

import pytest

from shopify_spy._squarespace import is_product_url
from tests.flow_support import STORE, product_page, requested, scrape, urlset

ROBOTS = f"{STORE}/robots.txt"
SITEMAP = f"{STORE}/sitemap.xml"
# The rules every Squarespace site serves, trimmed
SQUARESPACE_ROBOTS = f"""\
User-agent: *
Disallow: /config
Disallow: /search
Disallow: /api/
Disallow: /static/
Disallow: /*?format=json
Disallow: /*&format=json
Disallow: /*?format=page-context
Disallow: /*&format=page-context
Disallow: /*?format=json-pretty
Disallow: /*&format=json-pretty
Sitemap: {SITEMAP}
"""
OTHER_PAGES = [f"{STORE}/blog/post-1", f"{STORE}/shop", f"{STORE}/about"]
SLUGS = ["tee", "mug", "cap", "hat", "bag"]


def url(slug):
    return f"{STORE}/shop/p/{slug}"


def node(slug):
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": slug.title(),
        "url": url(slug),
        "image": f"https://images.test/{slug}.jpg",
        "offers": {"@type": "Offer", "price": "20.00", "priceCurrency": "USD"},
    }


def store(n=3, robots=SQUARESPACE_ROBOTS, extra_locs=(), page=None):
    """A Squarespace-like store: a flat sitemap listing blog and info pages, the shop
    page and ``n`` product pages; ``page(slug)`` overrides each product page."""
    slugs = SLUGS[:n]
    routes = {SITEMAP: urlset(*OTHER_PAGES, *map(url, slugs), *extra_locs)}
    if robots is not None:
        routes[ROBOTS] = (200, robots)
    for slug in slugs:
        routes[url(slug)] = page(slug) if page else product_page(node(slug))
    return routes


def scrape_sq(routes, **options):
    return scrape(routes, platform="squarespace", **options)


def assert_no_format_json(fetcher):
    assert not [u for u in requested(fetcher) if "format=" in u]


# --- Product URLs ---


@pytest.mark.parametrize(
    ("loc", "kept"),
    [
        (f"{STORE}/shop/p/tee", True),
        (f"{STORE}/store/p/mug/", True),
        (f"{STORE}/a/b/p/c", True),
        (f"{STORE}/shop/p/tee?color=red", True),
        (f"{STORE}/shop/p/tee?format=json", False),
        (f"{STORE}/shop/p/tee?color=red&format=json-pretty", False),
        (f"{STORE}/shop/p/tee?format=", False),
        (f"{STORE}/shop", False),
        (f"{STORE}/p/tee", False),
        (f"{STORE}/shop/p", False),
        (f"{STORE}/blog/post-1", False),
        ("http://[::1", False),
    ],
)
def test_is_product_url(loc, kept):
    assert is_product_url(loc) is kept


# --- Product pages (robots.txt obeyed) ---


def test_reads_only_the_listed_product_pages():
    items, result, fetcher = scrape_sq(store())
    assert sorted(i["html_url"] for i in items) == sorted(map(url, SLUGS[:3]))
    assert requested(fetcher) == [SITEMAP, *map(url, SLUGS[:3])]
    assert_no_format_json(fetcher)
    [s] = result.stores
    assert s.status == "ok"
    assert s.complete is True


def test_format_json_locs_left_out_when_robots_txt_is_missing():
    # With robots.txt at 404 the session's rules allow everything, so only the
    # adapter's own filter keeps these out
    extra = [f"{STORE}/shop/p/tee?format=json", f"{STORE}/shop?format=json"]
    items, result, fetcher = scrape_sq(store(robots=None, extra_locs=extra))
    assert len(items) == 3
    assert_no_format_json(fetcher)
    assert not any(page in requested(fetcher) for page in OTHER_PAGES)
    [s] = result.stores
    assert s.status == "ok"
    assert s.complete is True


def test_format_json_locs_count_as_filtered_out():
    locs = [f"{STORE}/shop/p/tee?format=json", f"{STORE}/shop?format=json"]
    _, result, fetcher = scrape_sq({SITEMAP: urlset(*locs)})
    [s] = result.stores
    assert s.status == "error"
    assert "among the 2 pages listed" in s.errors[0]
    assert_no_format_json(fetcher)


def test_items_are_jsonld_items_labelled_squarespace():
    items, _, _ = scrape_sq(store())
    product_locs = {SITEMAP: urlset(*map(url, SLUGS[:3]))}
    jsonld_items, _, _ = scrape({**store(), **product_locs}, platform="jsonld")
    by_url = {i["html_url"]: i for i in items}
    assert len(by_url) == 3
    for expected in jsonld_items:
        item = by_url[expected["html_url"]]
        assert item == {**expected, "platform": "squarespace", "source": "product_page"}
        assert item["store"] == "shop.test"
        assert item["image_urls"] == []
    assert "source" not in jsonld_items[0]


def test_items_list_images_when_downloading(tmp_path):
    items, _, _ = scrape_sq(store(1), images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://images.test/tee.jpg"]


@pytest.mark.parametrize(
    ("respect", "message"),
    [
        (True, "robots.txt is obeyed and disallows ?format=json; reading product pages"),
        (False, "robots.txt is ignored"),
    ],
)
def test_logs_the_source_once_per_store(caplog, respect, message):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    scrape_sq(store(), respect_robots_txt=respect)
    records = [
        r
        for r in caplog.records
        if r.name == "shopify_spy" and r.levelno == logging.INFO and "Squarespace" in r.message
    ]
    assert len(records) == 1
    assert STORE in records[0].message
    assert message in records[0].message


def test_pages_without_product_data_are_an_error():
    blank = (200, "<html><body></body></html>", {"Content-Type": "text/html"})
    _, result, _ = scrape_sq(store(page=lambda slug: blank))
    [s] = result.stores
    assert s.status == "error"
    assert (
        "no schema.org Product JSON-LD or microdata on 3 of 3 product pages checked"
        in (s.errors[0])
    )


# --- Store outcomes ---


def test_no_sitemap_is_not_found():
    _, result, _ = scrape_sq({})
    assert result.stores[0].status == "not_found"


def test_no_sitemap_suggests_shopify_when_it_answers_like_shopify():
    _, result, _ = scrape_sq({f"{STORE}/products.json": {"products": []}})
    [s] = result.stores
    assert s.status == "not_found"
    assert s.suggested_platform == "shopify"


def test_empty_sitemap_is_empty():
    _, result, _ = scrape_sq({SITEMAP: urlset()})
    [s] = result.stores
    assert s.status == "empty"
    assert s.complete


def test_sitemap_without_product_pages_is_an_error():
    _, result, fetcher = scrape_sq({SITEMAP: urlset(*OTHER_PAGES)})
    [s] = result.stores
    assert s.status == "error"
    assert (
        f"no product page (/<collection>/p/<slug>) among the 3 pages listed ({SITEMAP})"
        in s.errors[0]
    )
    assert requested(fetcher)[0] == SITEMAP
    assert not any(page in requested(fetcher) for page in OTHER_PAGES)


@pytest.mark.parametrize(("respect", "status"), [(True, "robots_blocked"), (False, "ok")])
def test_robots_txt_disallowing_everything(respect, status):
    routes = store(robots=f"User-agent: *\nDisallow: /\nSitemap: {SITEMAP}\n")
    items, result, _ = scrape_sq(routes, respect_robots_txt=respect)
    assert result.stores[0].status == status
    assert len(items) == (3 if respect is False else 0)


def test_max_items_per_store():
    items, result, _ = scrape_sq(store(5), max_items_per_store=2)
    assert len(items) == 2
    assert result.stores[0].items == 2
