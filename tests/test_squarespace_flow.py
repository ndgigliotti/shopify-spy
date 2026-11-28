"""Squarespace platform flows through spy.scrape with a FakeFetcher."""

import asyncio
import logging

import pytest

from shopify_spy._fetch import FetchConnectionError
from shopify_spy._squarespace import collection_path, is_product_url
from tests.flow_support import STORE, product_page, requested, scrape, sitemapindex, urlset

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


def store(n=3, robots=SQUARESPACE_ROBOTS, extra_locs=(), page=None, json=None):
    """A Squarespace-like store: a flat sitemap listing blog and info pages, the shop
    page and ``n`` product pages; ``page(slug)`` overrides each product page, and
    ``json`` is ``/shop?format=json``'s answer."""
    slugs = SLUGS[:n]
    routes = {SITEMAP: urlset(*OTHER_PAGES, *map(url, slugs), *extra_locs)}
    if robots is not None:
        routes[ROBOTS] = (200, robots)
    for slug in slugs:
        routes[url(slug)] = page(slug) if page else product_page(node(slug))
    if json is not None:
        routes[json_url("/shop")] = json
    return routes


def json_url(path, query=""):
    return f"{STORE}{path}?{query}&format=json" if query else f"{STORE}{path}?format=json"


def record(slug, collection="shop", **extra):
    """A Squarespace collection item record for a product."""
    return {
        "id": f"id-{slug}",
        "title": slug.title(),
        "urlId": slug,
        "fullUrl": f"/{collection}/p/{slug}",
        "assetUrl": f"https://images.test/{slug}.jpg",
        "items": [{"assetUrl": f"https://images.test/{slug}.jpg"}],
        "structuredContent": {
            "variants": [
                {
                    "id": f"v-{slug}",
                    "sku": f"SKU-{slug}",
                    "attributes": {},
                    "onSale": False,
                    "priceMoney": {"currency": "USD", "value": "20.00"},
                    "unlimited": False,
                    "qtyInStock": 3,
                    "mainImage": {"assetUrl": f"https://images.test/{slug}-v.jpg"},
                }
            ],
        },
        **extra,
    }


def collection(*records, pagination=None):
    return {"collection": {"pageSize": 999}, "items": list(records), "pagination": pagination}


def json_store(n=3, **options):
    """``store`` with ``/shop?format=json`` giving its ``n`` products."""
    return store(n, json=collection(*map(record, SLUGS[:n])), **options)


def scrape_json(routes, **options):
    return scrape_sq(routes, respect_robots_txt=False, **options)


def squarespace_logs(caplog):
    return [
        r.message
        for r in caplog.records
        if r.name == "shopify_spy" and r.levelno == logging.INFO and "Squarespace" in r.message
    ]


def product_pages_requested(fetcher):
    return [u for u in requested(fetcher) if "/p/" in u]


def html(body="<html><body></body></html>"):
    return 200, body, {"Content-Type": "text/html"}


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


@pytest.mark.parametrize(
    ("loc", "path"),
    [(f"{STORE}/shop/p/tee", "/shop"), (f"{STORE}/a/b/p/c/", "/a/b"), (f"{STORE}/x/p/y?q=1", "/x")],
)
def test_collection_path(loc, path):
    assert collection_path(loc) == path


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
    scrape_sq(json_store(), respect_robots_txt=respect)
    [record] = squarespace_logs(caplog)
    assert STORE in record
    assert message in record


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


BOTH_MODES = pytest.mark.parametrize("respect", [True, False], ids=["obeyed", "ignored"])


@BOTH_MODES
def test_no_sitemap_is_not_found(respect):
    _, result, _ = scrape_sq({}, respect_robots_txt=respect)
    assert result.stores[0].status == "not_found"


@BOTH_MODES
def test_no_sitemap_suggests_shopify_when_it_answers_like_shopify(respect):
    routes = {f"{STORE}/products.json": {"products": []}}
    _, result, _ = scrape_sq(routes, respect_robots_txt=respect)
    [s] = result.stores
    assert s.status == "not_found"
    assert s.suggested_platform == "shopify"


@BOTH_MODES
def test_empty_sitemap_is_empty(respect):
    _, result, _ = scrape_sq({SITEMAP: urlset()}, respect_robots_txt=respect)
    [s] = result.stores
    assert s.status == "empty"
    assert s.complete


@BOTH_MODES
def test_sitemap_without_product_pages_is_an_error(respect):
    _, result, fetcher = scrape_sq({SITEMAP: urlset(*OTHER_PAGES)}, respect_robots_txt=respect)
    [s] = result.stores
    assert s.status == "error"
    assert (
        f"no product page (/<collection>/p/<slug>) among the 3 pages listed ({SITEMAP})"
        in s.errors[0]
    )
    assert requested(fetcher)[0] == SITEMAP
    assert not any(page in requested(fetcher) for page in OTHER_PAGES)


@BOTH_MODES
def test_unreadable_child_sitemap_is_an_error(respect):
    child = f"{STORE}/sitemap-products.xml"
    routes = {SITEMAP: sitemapindex(child), child: (503, "")}
    _, result, _ = scrape_sq(routes, respect_robots_txt=respect)
    [s] = result.stores
    assert s.status == "error"
    assert f"no page listed; unreadable sitemaps: {child}" in s.errors[0]


@pytest.mark.parametrize(("respect", "status"), [(True, "robots_blocked"), (False, "ok")])
def test_robots_txt_disallowing_everything(respect, status):
    routes = json_store(robots=f"User-agent: *\nDisallow: /\nSitemap: {SITEMAP}\n")
    items, result, _ = scrape_sq(routes, respect_robots_txt=respect)
    assert result.stores[0].status == status
    assert len(items) == (3 if respect is False else 0)


@BOTH_MODES
def test_max_items_per_store(respect):
    items, result, _ = scrape_sq(json_store(5), max_items_per_store=2, respect_robots_txt=respect)
    assert len(items) == 2
    assert result.stores[0].items == 2


# --- ?format=json collections (robots.txt ignored) ---


ADDED_KEYS = {"store", "platform", "html_url", "image_urls", "source"}


def slow(spec, seconds):
    async def respond(request):
        await asyncio.sleep(seconds)
        return spec

    return respond


def test_reads_the_collection_json_instead_of_product_pages():
    items, result, fetcher = scrape_json(json_store())
    assert [i["source"] for i in items] == ["format_json"] * 3
    assert fetcher.count(json_url("/shop")) == 1
    assert product_pages_requested(fetcher) == []
    [s] = result.stores
    assert s.status == "ok"
    assert s.complete is True


def test_json_items_are_the_records_plus_added_keys():
    items, _, _ = scrape_json(json_store())
    for item, slug in zip(items, SLUGS, strict=False):
        rec = record(slug)
        assert set(item) - set(rec) == ADDED_KEYS
        assert {k: item[k] for k in rec} == rec
        assert item["store"] == "shop.test"
        assert item["platform"] == "squarespace"
        assert item["html_url"] == url(slug)
        assert item["image_urls"] == []


def test_json_items_list_images_when_downloading(tmp_path):
    items, _, _ = scrape_json(json_store(1), images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == [
        "https://images.test/tee.jpg",
        "https://images.test/tee-v.jpg",
    ]


def test_follows_pagination_to_the_last_page():
    routes = store(3)
    routes[json_url("/shop")] = collection(
        record("tee"),
        record("mug"),
        pagination={"nextPage": True, "nextPageUrl": "/shop?offset=123", "nextPageOffset": 123},
    )
    routes[json_url("/shop", "offset=123")] = collection(
        record("cap"), pagination={"nextPage": False}
    )
    items, result, fetcher = scrape_json(routes)
    assert [i["urlId"] for i in items] == ["tee", "mug", "cap"]
    assert fetcher.count(json_url("/shop", "offset=123")) == 1
    assert result.stores[0].status == "ok"


def test_pagination_pointing_back_ends_the_collection():
    routes = store(2)
    routes[json_url("/shop")] = collection(
        record("tee"), pagination={"nextPage": True, "nextPageUrl": "/shop?offset=1"}
    )
    routes[json_url("/shop", "offset=1")] = collection(
        record("mug"), pagination={"nextPage": True, "nextPageUrl": "/shop"}
    )
    items, result, fetcher = scrape_json(routes)
    assert len(items) == 2
    assert fetcher.count(json_url("/shop")) == 1
    assert fetcher.count(json_url("/shop", "offset=1")) == 1
    assert result.stores[0].status == "ok"


def test_pagination_to_another_host_is_not_followed():
    routes = store(1)
    routes[json_url("/shop")] = collection(
        record("tee"), pagination={"nextPage": True, "nextPageUrl": "https://other.test/shop"}
    )
    _, result, fetcher = scrape_json(routes)
    assert not [u for u in requested(fetcher) if "other.test" in u]
    assert result.stores[0].status == "ok"


def test_a_product_in_two_collections_is_emitted_once():
    routes = store(2, extra_locs=[f"{STORE}/sale/p/cap"])
    routes[json_url("/shop")] = collection(record("tee"), record("mug"))
    routes[json_url("/sale")] = collection(record("cap", "sale"), record("tee"))
    items, result, _ = scrape_json(routes)
    assert sorted(i["urlId"] for i in items) == ["cap", "mug", "tee"]
    assert result.stores[0].status == "ok"


def test_logs_the_fallback_after_the_source(caplog):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    scrape_json(store(json=(403, "")))
    first, fallback = squarespace_logs(caplog)
    assert "robots.txt is ignored; reading ?format=json collection pages" in first
    assert STORE in fallback
    assert (
        "no product from ?format=json (no Squarespace product from 1 collection page "
        "(/shop): 1 answered 403); reading product pages instead"
    ) in fallback


# A second collection beside /shop, which gives every /shop product
SALE = f"{STORE}/sale/p/hat"


@pytest.mark.parametrize(
    "spec",
    [html(), {"collection": {}}, (503, "")],
    ids=["html", "json-without-items", "503"],
)
def test_one_collection_failing_beside_items_is_partial(caplog, spec):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    routes = json_store(extra_locs=[SALE])
    routes[json_url("/sale")] = spec
    items, result, fetcher = scrape_json(routes)
    assert len(items) == 3
    assert product_pages_requested(fetcher) == []
    [s] = result.stores
    assert s.status == "partial"
    assert s.complete is False
    if spec != (503, ""):
        assert (
            f"{json_url('/sale')} answered without Squarespace collection data" in caplog.messages
        )


@pytest.mark.parametrize(
    "spec",
    [html(), {"collection": {}}, (503, ""), FetchConnectionError("connection refused")],
    ids=["html", "json-without-items", "503", "no-response"],
)
def test_a_failing_later_page_with_every_listed_product_read_is_partial(spec):
    # Every listed product comes from the first page, so only the failed page
    # itself can make the store partial
    routes = store(2)
    routes[json_url("/shop")] = collection(
        record("tee"),
        record("mug"),
        pagination={"nextPage": True, "nextPageUrl": "/shop?offset=2"},
    )
    routes[json_url("/shop", "offset=2")] = spec
    items, result, fetcher = scrape_json(routes)
    assert len(items) == 2
    assert product_pages_requested(fetcher) == []
    [s] = result.stores
    assert s.status == "partial"
    assert s.complete is False


def test_a_gone_collection_leaves_its_listed_products_missing(caplog):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    items, result, fetcher = scrape_json(json_store(extra_locs=[SALE]))  # /sale 404s
    assert len(items) == 3
    assert product_pages_requested(fetcher) == []
    [s] = result.stores
    assert s.status == "partial"
    assert any("1 of 4 listed product pages not in" in m for m in squarespace_logs(caplog))


VARIANTLESS = {"id": "x", "fullUrl": "/shop/p/tee", "structuredContent": {"productType": 2}}
FALLBACK_CASES = pytest.mark.parametrize(
    ("spec", "json_reason"),
    [
        (html(), f"{json_url('/shop')} answered without Squarespace collection data"),
        ((403, ""), "no Squarespace product from 1 collection page (/shop): 1 answered 403"),
        (
            FetchConnectionError("connection refused"),
            "no Squarespace product from 1 collection page (/shop): 1 failed to respond",
        ),
        (
            collection(VARIANTLESS, VARIANTLESS, VARIANTLESS),
            "no Squarespace product from 1 collection page (/shop): "
            "3 records without structuredContent.variants",
        ),
    ],
    ids=["unreadable", "403", "no-response", "variantless"],
)


@FALLBACK_CASES
def test_no_item_from_the_collections_falls_back_to_product_pages(spec, json_reason):
    items, result, fetcher = scrape_json(store(json=spec))
    assert sorted(i["html_url"] for i in items) == sorted(map(url, SLUGS[:3]))
    assert {i["source"] for i in items} == {"product_page"}
    assert {i["platform"] for i in items} == {"squarespace"}
    assert all("@type" in i for i in items)
    assert fetcher.count(SITEMAP) == 1
    assert all(fetcher.count(url(slug)) == 1 for slug in SLUGS[:3])
    [s] = result.stores
    assert s.status == "ok"


def test_a_failing_product_page_in_the_fallback_is_partial():
    routes = store(json=(403, ""))
    routes[url("mug")] = (503, "")
    items, result, _ = scrape_json(routes)
    assert sorted(i["html_url"] for i in items) == [url("cap"), url("tee")]
    [s] = result.stores
    assert s.status == "partial"
    assert s.complete is False


@FALLBACK_CASES
def test_no_item_from_collections_or_pages_is_an_error_naming_both(spec, json_reason):
    _, result, _ = scrape_json(store(json=spec, page=lambda slug: (403, "")))
    [s] = result.stores
    assert s.status == "error"
    assert (
        f"{json_reason}; no schema.org Product JSON-LD or microdata on 0 of 3 product pages "
        f"checked ({SITEMAP}): 3 answered 403"
    ) in s.errors[0]


def test_failed_fallback_suggests_shopify_when_it_answers_like_shopify():
    routes = store(json=(403, ""), page=lambda slug: (403, ""))
    routes[f"{STORE}/products.json"] = {"products": []}
    _, result, _ = scrape_json(routes)
    [s] = result.stores
    assert s.status == "error"
    assert s.suggested_platform == "shopify"


def test_bail_before_an_item_reports_the_collections_outcome():
    routes = store(json=(403, ""), extra_locs=[SALE])
    routes[json_url("/sale")] = slow((403, ""), 5)
    _, result, _ = scrape_json(routes, bail=0.3)
    [s] = result.stores
    assert s.finish_reason == "bail"
    assert "no Squarespace product from" in s.errors[0]
    assert "1 answered 403" in s.errors[0]


def test_listed_products_missing_from_the_collections_is_partial(caplog):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    items, result, fetcher = scrape_json(store(5, json=collection(*map(record, SLUGS[:3]))))
    assert len(items) == 3
    assert product_pages_requested(fetcher) == []
    [s] = result.stores
    assert s.status == "partial"
    assert s.complete is False
    [_, coverage] = squarespace_logs(caplog)
    assert STORE in coverage
    assert "2 of 5 listed product pages not in the ?format=json collections" in coverage


def test_a_listed_variantless_record_is_partial(caplog):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    cap = {**record("cap"), "structuredContent": {}}
    items, result, _ = scrape_json(store(3, json=collection(record("tee"), record("mug"), cap)))
    assert len(items) == 2
    assert result.stores[0].status == "partial"
    [_, coverage] = squarespace_logs(caplog)
    assert "1 of 3 listed product pages not in" in coverage
    assert "1 records without structuredContent.variants" in coverage


def test_an_unlisted_variantless_record_is_only_logged(caplog):
    caplog.set_level(logging.INFO, logger="shopify_spy")
    gift = {"id": "gift", "fullUrl": "/shop/p/gift-card", "structuredContent": {}}
    routes = store(3, json=collection(*map(record, SLUGS[:3]), gift))
    items, result, _ = scrape_json(routes)
    assert len(items) == 3
    assert result.stores[0].status == "ok"
    [_, coverage] = squarespace_logs(caplog)
    assert "0 of 3 listed product pages not in" in coverage
    assert "1 records without structuredContent.variants" in coverage


def test_listed_paths_match_records_after_unquoting_and_trailing_slashes():
    routes = store(0, extra_locs=[f"{STORE}/shop/p/caf%C3%A9/", f"{STORE}/shop/p/tee"])
    routes[json_url("/shop")] = collection(
        {**record("tee"), "fullUrl": "/shop/p/tee/"}, {**record("x"), "fullUrl": "/shop/p/café"}
    )
    _, result, _ = scrape_json(routes)
    assert result.stores[0].status == "ok"


def test_malformed_records_never_raise(tmp_path):
    bad_variants = {
        **record("mug"),
        "items": "not a list",
        "assetUrl": 5,
        "structuredContent": {
            "variants": [None, {"mainImage": "x"}, {"mainImage": {"assetUrl": 3}}]
        },
    }
    odd = [None, "x", 3, {"structuredContent": []}, {"structuredContent": {"variants": {}}}]
    routes = store(2, json=collection(record("tee"), bad_variants, *odd))
    items, result, _ = scrape_json(routes, images=True, images_dir=tmp_path)
    by_slug = {i["urlId"]: i for i in items}
    assert sorted(by_slug) == ["mug", "tee"]
    assert by_slug["mug"]["image_urls"] == []
    assert result.stores[0].status == "ok"


def test_a_record_without_a_full_url_has_no_html_url():
    rec = {k: v for k, v in record("tee").items() if k != "fullUrl"}
    items, result, _ = scrape_json(store(1, json=collection(rec)))
    assert items[0]["html_url"] is None
    assert result.stores[0].status == "partial"  # the listed page wasn't matched
