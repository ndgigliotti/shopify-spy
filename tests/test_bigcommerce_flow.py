"""BigCommerce adapter flows through spy.scrape with a FakeFetcher."""

import json
import logging

import pytest

from shopify_spy import _bigcommerce
from shopify_spy._bigcommerce import _QUERY, _VARIANTS_QUERY, PAGE_SIZE, _tokens
from shopify_spy._fetch import FetchConnectionError, Seq
from tests.flow_support import STORE, bc_token, bigcommerce_api, redirect, scrape

TOKEN = bc_token(sid=1)
FRESH = bc_token(sid=2)
API = bigcommerce_api()
STENCIL = "<meta name='platform' content='bigcommerce.stencil' />"


def homepage(*tokens, stencil=True, extra=""):
    settings = "".join(f'<script>var graphQLToken = "{t}";</script>' for t in tokens)
    return 200, f"<html><head>{STENCIL if stencil else ''}{extra}</head>{settings}</html>"


def bc_product(i, **extra):
    return {
        "id": f"UHJvZHVjdDo{i}",
        "entityId": i,
        "sku": f"SKU{i}",
        "name": f"Product {i}",
        "path": f"/p{i}/",
        "prices": {"price": {"value": 10.0, "currencyCode": "USD"}},
        "images": {
            "edges": [
                {"node": {"urlOriginal": f"https://cdn.test/{i}-a.jpg", "isDefault": False}},
                {"node": {"urlOriginal": f"https://cdn.test/{i}-b.jpg", "isDefault": True}},
            ]
        },
        "variants": {"pageInfo": {"hasNextPage": False}, "edges": []},
        **extra,
    }


def answer(nodes, cursor=None, errors=None, has_next=None):
    info = {"hasNextPage": cursor is not None if has_next is None else has_next}
    info["endCursor"] = cursor
    body = {"data": {"site": {"products": {"pageInfo": info, "edges": nodes}}}}
    return body | ({"errors": [{"message": m} for m in errors]} if errors else {})


def page(n, start=0, cursor=None):
    return answer([{"node": bc_product(i)} for i in range(start, start + n)], cursor)


def errors(*messages):
    return {"errors": [{"message": m} for m in messages]}


REJECTED = (401, errors("GraphQL invalid credentials. JWT signature is invalid"))


class GraphQL:
    """A ``/graphql`` route: answers by the ``after`` variable, each a spec or a Seq."""

    def __init__(self, pages):
        self.pages = pages
        self.sent = []  # (token, variables) per request
        self.served: dict = {}

    def __call__(self, request):
        auth = request.headers.get("Authorization", "")
        variables = json.loads(request.body)["variables"]
        self.sent.append((auth.removeprefix("Bearer "), variables))
        spec = self.pages[variables["after"]]
        if isinstance(spec, Seq):
            index = self.served.get(variables["after"], 0)
            self.served[variables["after"]] = index + 1
            spec = spec[min(index, len(spec) - 1)]
        return spec

    @property
    def tokens(self):
        return [t for t, _ in self.sent]


def scrape_bc(pages, home=None, routes=None, **options):
    graphql = pages if isinstance(pages, GraphQL) else GraphQL(pages)
    all_routes = {f"{STORE}/": home or homepage(TOKEN), ("POST", API): graphql, **(routes or {})}
    items, result, fetcher = scrape(all_routes, platform="bigcommerce", **options)
    return items, result, fetcher, graphql


def test_pages_by_cursor_with_variables():
    items, result, fetcher, graphql = scrape_bc(
        {None: page(50, 0, "c1"), "c1": page(50, 50, "c2"), "c2": page(3, 100)}
    )
    assert len(items) == 103
    assert [v for _, v in graphql.sent] == [
        {"first": 50, "after": None},
        {"first": 50, "after": "c1"},
        {"first": 50, "after": "c2"},
    ]
    assert len({i["entityId"] for i in items}) == 103
    assert result.stores[0].status == "ok"
    assert result.stores[0].complete


def test_request_shape():
    _, _, fetcher, _ = scrape_bc({None: page(1)})
    post = next(r for r in fetcher.requests if r.method == "POST")
    assert post.url == f"{STORE}/graphql"
    assert post.headers == {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    assert json.loads(post.body) == {
        "query": _QUERY,
        "variables": {"first": PAGE_SIZE, "after": None},
    }


def test_token_picked_among_decoys():
    shopify_jwt = bc_token(iss="shopify")
    no_iss = bc_token(iss=None)
    blob = "eyJldmVudHMiOltdfQ"  # base64 JSON, not a JWT
    extra = (
        f'<a href="/buyer_flags?t={shopify_jwt}">x</a>'
        f'<script>window.bodl = JSON.parse(decodeBase64("{blob}"));</script>'
        f"<script>var t = '{no_iss}';</script>"
        "<script>var broken = 'eyJ!!!.eyJ###.abc';</script>"
    )
    jscontext = json.dumps(json.dumps({"graphQLToken": TOKEN}))  # escaped, as in jsContext
    home = (200, f"<html>{extra}<script>var jsContext = JSON.parse({jscontext});</script></html>")
    _, result, _, graphql = scrape_bc({None: page(1)}, home=home)
    assert graphql.tokens == [TOKEN]
    assert result.stores[0].status == "ok"


def test_token_whose_cors_names_the_site_is_preferred():
    elsewhere = bc_token(cors=["https://other.test"])
    _, _, _, graphql = scrape_bc({None: page(1)}, home=homepage(elsewhere, TOKEN))
    assert graphql.tokens == [TOKEN]


def jscontext(token):
    """Stencil's escaped jsContext, as the theme renders ``storefront_api.token``."""
    settings = {"storefront_api": {"token": token}}
    return f"<script>var jsContext = JSON.parse({json.dumps(json.dumps(settings))});</script>"


def test_stencil_token_preferred_over_an_earlier_app_token():
    # As on crabplace.com: an app's token, with a client ID ``sub``, comes first
    app = bc_token(sub="plr01xvcbfpin1v9wgvockqmqvv4pzp", cors=[STORE, "https://www.shop.test"])
    home = (200, f"<html><script>window.CPX_GRID_SF_TOKEN='{app}';</script>{jscontext(TOKEN)}")
    _, result, _, graphql = scrape_bc({None: page(1)}, home=home)
    assert graphql.tokens == [TOKEN]
    assert result.stores[0].status == "ok"


def test_app_token_used_without_a_stencil_one():
    app = bc_token(sub="client-id")
    _, _, _, graphql = scrape_bc({None: page(1)}, home=homepage(bc_token(iss="x"), app))
    assert graphql.tokens == [app]


def test_cors_outranks_stencil_sub():
    elsewhere = bc_token(cors=["https://other.test"])
    app = bc_token(sub="client-id")
    assert _tokens(homepage(elsewhere, app)[1], STORE, now=0)[0][0] == app


def test_expired_token_skipped():
    # As on erskinedental.com: a theme script hard-codes a long-expired token
    expired = bc_token(sid=3, eat=1_747_400_000)
    script = f"<script>const storeUrl = '/graphql';\nconst apiToken = '{expired}';</script>"
    home = (200, f"<html>{STENCIL}{script}{jscontext(TOKEN)}</html>")
    _, result, _, graphql = scrape_bc({None: page(1)}, home=home)
    assert graphql.tokens == [TOKEN]
    assert result.stores[0].status == "ok"


def test_expiry_is_checked_against_now():
    token = bc_token(eat=1_000)
    assert _tokens(homepage(token)[1], STORE, now=999) == ([token], 0)
    assert _tokens(homepage(token)[1], STORE, now=1_000) == ([], 1)


def test_only_expired_tokens_is_an_error():
    home = homepage(bc_token(sid=3, eat=1_000), bc_token(sid=4, eat=2_000))
    items, result, fetcher, _ = scrape_bc({}, home=home)
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert "no Storefront API token on the homepage (only expired ones)" in store.errors[-1]
    assert fetcher.count(API, "POST") == 0


def test_refresh_skips_an_expired_token():
    expired = bc_token(sid=3, eat=1_000)
    graphql = GraphQL({None: page(2, 0, "c1"), "c1": Seq([REJECTED, page(2, 2)])})
    home = Seq([homepage(TOKEN), homepage(expired, FRESH)])
    items, result, _, _ = scrape_bc(graphql, home=home)
    assert graphql.tokens == [TOKEN, TOKEN, FRESH]
    assert len(items) == 4
    assert result.stores[0].status == "ok"


def test_refresh_to_the_same_token_stops():
    graphql = GraphQL({None: page(2, 0, "c1"), "c1": REJECTED})
    items, result, _, _ = scrape_bc(graphql, home=homepage(TOKEN))
    assert graphql.tokens == [TOKEN, TOKEN]
    assert len(items) == 2
    assert result.stores[0].status == "partial"


def test_refresh_to_the_same_token_on_the_first_page_is_an_error():
    _, result, _, graphql = scrape_bc({None: REJECTED}, home=homepage(TOKEN))
    assert graphql.tokens == [TOKEN]
    assert "gave the same token" in result.stores[0].errors[-1]


HEADLESS_AU = "https://store-5he0z7o63b.mybigcommerce.com/graphql"
HEADLESS_US = "https://store-d38zsiurdu.mybigcommerce.com/graphql"


def nuxt_homepage(au_token, us_token):
    """A Nuxt storefront's config, as on mudaustralia.com: an endpoint and a token per
    region, the US endpoint first but the AU token first."""
    config = (
        f'{{public:{{URL:"{STORE}",BC_GRAPHQL_US:"{HEADLESS_US}",BC_GRAPHQL_AU:"{HEADLESS_AU}",'
        f'BIGCOMMERCE_ACCESS_TOKEN_AU:"{au_token}",BIGCOMMERCE_ACCESS_TOKEN_US:"{us_token}"}}}}'
    )
    return 200, f"<html><script>window.__NUXT__={{}};window.__NUXT__.config={config}</script>"


def test_headless_endpoint_paired_with_the_token_is_used(caplog):
    au, us = bc_token(sub="client-au"), bc_token(sub="client-us")
    graphql = GraphQL({None: page(1)})
    routes = {f"{STORE}/": nuxt_homepage(au, us), ("POST", HEADLESS_AU): graphql}
    with caplog.at_level(logging.INFO, logger="shopify_spy"):
        items, result, fetcher = scrape(routes, platform="bigcommerce")
    assert graphql.tokens == [au]
    assert fetcher.count(API, "POST") == 0
    assert items[0]["html_url"] == f"{STORE}/p0/"
    assert items[0]["store"] == "shop.test"
    assert result.stores[0].status == "ok"
    assert f"names 2 GraphQL endpoints; using {HEADLESS_AU} (paired" in caplog.text
    assert any(r.url.startswith("https://store-5he0z7o63b") for r in fetcher.requests)


def test_headless_endpoint_without_a_pairing_is_the_first():
    endpoints = json.dumps({"a": HEADLESS_US, "b": HEADLESS_AU}).replace("/", "\\/")
    html = f"<script>var c = {endpoints}; var t = '{TOKEN}';</script>"
    assert _bigcommerce.find_endpoint(html, TOKEN) == HEADLESS_US


def test_headless_endpoint_obeys_its_robots_txt():
    au = bc_token(sub="client-au")
    routes = {
        f"{STORE}/": nuxt_homepage(au, bc_token(sub="client-us")),
        "https://store-5he0z7o63b.mybigcommerce.com/robots.txt": (
            200,
            "User-agent: *\nDisallow: /",
        ),
    }
    items, result, fetcher = scrape(routes, platform="bigcommerce")
    assert items == []
    assert fetcher.count(HEADLESS_AU, "POST") == 0
    assert result.stores[0].status == "robots_blocked"


def test_stencil_page_ignores_a_named_endpoint():
    _, _, fetcher, graphql = scrape_bc(
        {None: page(1)}, home=homepage(TOKEN, extra=f'<script>var u = "{HEADLESS_AU}";</script>')
    )
    assert graphql.tokens == [TOKEN]
    assert fetcher.count(HEADLESS_AU, "POST") == 0


def test_headless_redirect_without_an_endpoint_says_headless():
    items, result, _, _ = scrape_bc(
        {None: redirect("/graphql/", 301)}, home=homepage(TOKEN, stencil=False)
    )
    assert items == []
    error = result.stores[0].errors[-1]
    assert result.stores[0].status == "error"
    assert "looks like a headless storefront" in error
    assert "redirected (301)" in error


def test_api_and_html_url_follow_a_homepage_redirect():
    www = "https://www.shop.test"
    graphql = GraphQL({None: page(1)})
    routes = {
        f"{STORE}/": redirect(f"{www}/"),
        f"{www}/": homepage(bc_token(cors=[www])),
        ("POST", bigcommerce_api(www)): graphql,
    }
    items, result, fetcher = scrape(routes, platform="bigcommerce")
    assert fetcher.count(API, "POST") == 0
    assert items[0]["html_url"] == f"{www}/p0/"
    assert items[0]["store"] == "www.shop.test"
    assert result.stores[0].status == "ok"


def test_item_shape_without_images():
    items, _, _, _ = scrape_bc({None: page(1)})
    assert items == [
        {
            **bc_product(0),
            "store": "shop.test",
            "platform": "bigcommerce",
            "html_url": f"{STORE}/p0/",
            "image_urls": [],
        }
    ]


def test_item_image_urls_default_first(tmp_path):
    items, _, _, _ = scrape_bc({None: page(1)}, images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == ["https://cdn.test/0-b.jpg", "https://cdn.test/0-a.jpg"]


@pytest.mark.parametrize("path", [None, ""])
def test_html_url_without_a_path(path):
    items, _, _, _ = scrape_bc({None: answer([{"node": bc_product(0, path=path)}])})
    assert items[0]["html_url"] is None


@pytest.mark.parametrize(
    ("stencil", "reason"),
    [(True, "a Stencil theme"), (False, "not a Stencil storefront")],
    ids=["stencil", "not_stencil"],
)
def test_no_token_is_an_error(stencil, reason):
    items, result, fetcher, _ = scrape_bc({}, home=homepage(stencil=stencil))
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert "no Storefront API token on the homepage" in store.errors[-1]
    assert reason in store.errors[-1]
    assert fetcher.count(API, "POST") == 0
    assert (fetcher.count(f"{STORE}/products.json") == 0) is stencil


def test_decoy_jwts_only_is_an_error():
    home = homepage(bc_token(iss="shopify"), stencil=False)
    _, result, _, graphql = scrape_bc({}, home=home)
    assert result.stores[0].status == "error"
    assert graphql.sent == []


def test_no_token_on_a_shopify_store_gives_the_hint():
    routes = {f"{STORE}/products.json": {"products": [{"id": 1}]}}
    _, result, _, _ = scrape_bc({}, home=homepage(stencil=False), routes=routes)
    store = result.stores[0]
    assert store.status == "error"
    assert store.suggested_platform == "shopify"


@pytest.mark.parametrize(
    ("home", "expected"),
    [((403, ""), "forbidden"), ((503, ""), "unavailable"), (FetchConnectionError("x"), None)],
    ids=["403", "503", "failed"],
)
def test_homepage_failure(home, expected):
    items, result, fetcher, _ = scrape_bc({}, home=home, retries=0)
    assert items == []
    assert result.stores[0].status == (expected or "no_response")
    assert fetcher.count(API, "POST") == 0


def test_401_refreshes_the_token_and_retries_the_cursor():
    graphql = GraphQL({None: page(2, 0, "c1"), "c1": Seq([REJECTED, page(2, 2)])})
    home = Seq([homepage(TOKEN), homepage(FRESH)])
    items, result, fetcher, _ = scrape_bc(graphql, home=home)
    assert len(items) == 4
    assert graphql.tokens == [TOKEN, TOKEN, FRESH]
    assert [v["after"] for _, v in graphql.sent] == [None, "c1", "c1"]
    assert fetcher.count(f"{STORE}/") == 2
    assert result.stores[0].status == "ok"


def test_two_401s_on_the_first_page_are_an_error():
    home = Seq([homepage(TOKEN), homepage(FRESH)])
    items, result, fetcher, graphql = scrape_bc({None: REJECTED}, home=home)
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert "token rejected (401): GraphQL invalid credentials" in store.errors[-1]
    assert graphql.tokens == [TOKEN, FRESH]
    assert fetcher.count(f"{STORE}/products.json") == 0


def test_two_401s_on_a_later_page_are_partial():
    home = Seq([homepage(TOKEN), homepage(FRESH)])
    items, result, _, graphql = scrape_bc({None: page(2, 0, "c1"), "c1": REJECTED}, home=home)
    assert len(items) == 2
    assert result.stores[0].status == "partial"
    assert len(graphql.sent) == 3


def test_401_without_a_graphql_message():
    _, result, _, _ = scrape_bc({None: (401, "")})
    assert "token rejected (401): no GraphQL message" in result.stores[0].errors[-1]


@pytest.mark.parametrize(
    ("refresh", "why"),
    [
        (homepage(), "no token"),
        ((403, ""), "HTTP 403"),
        (FetchConnectionError("reset"), "request failed"),
    ],
    ids=["no_token", "403", "failed"],
)
def test_failed_refresh(refresh, why, caplog):
    home = Seq([homepage(TOKEN), refresh])
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, fetcher, _ = scrape_bc(
            {None: page(2, 0, "c1"), "c1": REJECTED}, home=home, retries=0
        )
    assert len(items) == 2
    assert result.stores[0].status == "partial"
    assert f"gave no token ({why}" in caplog.text
    assert fetcher.count(f"{STORE}/products.json") == 0
    assert fetcher.count(f"{STORE}/wp-json/") == 0


@pytest.mark.parametrize(
    ("refresh", "why"),
    [(homepage(), "no token"), ((403, ""), "HTTP 403"), (FetchConnectionError("x"), "request")],
    ids=["no_token", "403", "failed"],
)
def test_failed_refresh_on_the_first_page_is_an_error(refresh, why):
    home = Seq([homepage(TOKEN), refresh])
    items, result, fetcher, _ = scrape_bc({None: REJECTED}, home=home, retries=0)
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert "token rejected (401)" in store.errors[-1]
    assert f"gave no token ({why}" in store.errors[-1]
    assert fetcher.count(f"{STORE}/products.json") == 0


def test_redirect_on_the_first_page_is_an_error():
    items, result, fetcher, _ = scrape_bc({None: (302, "", {"Location": "/login"})})
    assert items == []
    store = result.stores[0]
    assert store.status == "error"
    assert "redirected (302) to /login" in store.errors[-1]
    assert fetcher.count(f"{STORE}/login") == 0


def test_redirect_without_a_location():
    _, result, _, _ = scrape_bc({None: (301, "")})
    assert "redirected (301) to an unknown location" in result.stores[0].errors[-1]


def test_redirect_on_a_later_page_is_partial():
    items, result, _, _ = scrape_bc({None: page(2, 0, "c1"), "c1": redirect("/login", 302)})
    assert len(items) == 2
    assert result.stores[0].status == "partial"


def test_complexity_error_halves_the_page_size():
    too_complex = errors(
        "The query is too complex as it has a complexity score of 12000 out of 10000."
    )
    calls = []

    def respond(request):
        variables = json.loads(request.body)["variables"]
        calls.append(variables["first"])
        if variables["first"] > 12:
            return too_complex
        return page(2, 0, "c1") if variables["after"] is None else page(2, 2)

    items, result, _, _ = scrape_bc({None: None}, routes={("POST", API): respond})
    assert len(items) == 4
    assert calls == [50, 25, 12, 12]
    assert result.stores[0].status == "ok"


def test_complexity_error_at_page_size_one_is_an_error():
    items, result, _, graphql = scrape_bc({None: errors("Query is too complex")})
    assert items == []
    assert result.stores[0].status == "error"
    assert "too complex" in result.stores[0].errors[-1]
    assert [v["first"] for _, v in graphql.sent] == [50, 25, 12, 6, 3, 1]


def test_http_failure_after_a_page_size_retry_is_an_error():
    graphql = GraphQL({None: Seq([errors("Query is too complex"), (503, "")])})
    items, result, _, _ = scrape_bc(graphql, retries=0)
    assert items == []
    assert result.stores[0].status == "error"
    assert "HTTP 503 after a page-size retry" in result.stores[0].errors[-1]


def test_http_failure_after_a_400_complexity_error_is_left_to_diagnose():
    too_complex = (400, errors("Query is too complex"))
    graphql = GraphQL({None: Seq([too_complex, (403, "")])})
    items, result, _, _ = scrape_bc(graphql, retries=0)
    assert items == []
    assert result.stores[0].status == "forbidden"


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (errors("Field 'nope' doesn't exist"), "Field 'nope'"),
        ((200, "<html>maintenance</html>"), "not JSON"),
        ({"data": None}, "no products in the answer"),
        ((400, errors("Argument 'first' cannot exceed 50")), "cannot exceed 50"),
        (answer([{"node": None}], errors=["Internal error"]), "Internal error"),
    ],
    ids=["errors_only", "non_json", "null_data", "400_json_errors", "null_nodes_with_errors"],
)
def test_first_page_without_products_is_an_error(spec, reason):
    items, result, _, _ = scrape_bc({None: spec})
    assert items == []
    assert result.stores[0].status == "error"
    assert reason in result.stores[0].errors[-1]


def test_later_page_graphql_error_is_partial(caplog):
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _, _ = scrape_bc({None: page(2, 0, "c1"), "c1": errors("Boom")})
    assert len(items) == 2
    assert result.stores[0].status == "partial"
    assert "Boom" in caplog.text


@pytest.mark.parametrize(
    ("spec", "expected"),
    [((404, ""), "not_found"), ((503, ""), "unavailable"), ((429, ""), "rate_limited")],
)
def test_http_failure_on_the_first_page(spec, expected):
    items, result, _, _ = scrape_bc({None: spec}, retries=0)
    assert items == []
    assert result.stores[0].status == expected


def test_404_on_the_first_page_probes_other_platforms():
    routes = {f"{STORE}/wp-json/": {"namespaces": ["wc/store/v1"]}}
    _, result, _, _ = scrape_bc({None: (404, "")}, routes=routes)
    assert result.stores[0].status == "not_found"
    assert result.stores[0].suggested_platform == "woocommerce"


def test_robots_txt_blocks_graphql():
    routes = {f"{STORE}/robots.txt": (200, "User-agent: *\nDisallow: /graphql")}
    items, result, fetcher, _ = scrape_bc({None: page(1)}, routes=routes)
    assert items == []
    assert fetcher.count(API, "POST") == 0
    assert result.stores[0].status == "robots_blocked"


def test_request_failure_on_the_first_page_is_an_error():
    items, result, _, _ = scrape_bc({None: FetchConnectionError("reset")}, retries=0)
    assert items == []
    assert result.stores[0].status == "error"
    assert "reset" in result.stores[0].errors[-1]


def test_request_failure_on_a_later_page_is_partial():
    pages = {None: page(2, 0, "c1"), "c1": FetchConnectionError("reset")}
    items, result, _, _ = scrape_bc(pages, retries=0)
    assert len(items) == 2
    assert result.stores[0].status == "partial"


@pytest.mark.parametrize(
    "second",
    [page(2, 2, "c1"), answer([{"node": bc_product(2)}], has_next=True)],
    ids=["repeated", "missing"],
)
def test_bad_end_cursor_stops(second, caplog):
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _, graphql = scrape_bc({None: page(2, 0, "c1"), "c1": second})
    assert len(graphql.sent) == 2
    assert len(items) in (3, 4)
    assert result.stores[0].status == "partial"
    assert "no new endCursor" in caplog.text


def test_null_nodes_are_skipped():
    body = answer([{"node": None}, None, {"node": bc_product(1)}])
    items, result, _, _ = scrape_bc({None: body})
    assert [i["entityId"] for i in items] == [1]
    assert result.stores[0].status == "ok"


def test_errors_alongside_products_are_logged(caplog):
    body = answer([{"node": bc_product(0)}], errors=["Could not resolve image"])
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _, _ = scrape_bc({None: body})
    assert len(items) == 1
    assert result.stores[0].status == "ok"
    assert "Could not resolve image" in caplog.text


def variant(i):
    return {"node": {"entityId": i, "sku": f"V{i}"}}


def variants_answer(start, n, cursor=None):
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    edges = [variant(i) for i in range(start, start + n)]
    return {"data": {"site": {"product": {"variants": {"pageInfo": info, "edges": edges}}}}}


def many_variants(n, cursor="v1"):
    """A product whose first page of variants holds ``n`` and says there are more."""
    info = {"hasNextPage": True, "endCursor": cursor}
    edges = [variant(i) for i in range(n)]
    return bc_product(0, variants={"pageInfo": info, "edges": edges})


def scrape_variants(respond_variants, **options):
    """Scrape one product of ``many_variants(250)``; ``respond_variants`` answers the
    variants queries, given their variables."""
    sent = []

    def respond(request):
        body = json.loads(request.body)
        if body["query"] == _QUERY:
            return answer([{"node": many_variants(250)}])
        assert body["query"] == _VARIANTS_QUERY
        sent.append(body["variables"])
        return respond_variants(body["variables"])

    items, result, _ = scrape(
        {f"{STORE}/": homepage(TOKEN), ("POST", API): respond}, platform="bigcommerce", **options
    )
    return items, result, sent


def test_variants_past_the_first_page_are_paged_by_cursor(caplog):
    pages = {"v1": variants_answer(250, 250, "v2"), "v2": variants_answer(500, 100)}
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, sent = scrape_variants(lambda v: pages[v["after"]])
    assert sent == [{"id": 0, "first": 250, "after": "v1"}, {"id": 0, "first": 250, "after": "v2"}]
    connection = items[0]["variants"]
    assert [e["node"]["entityId"] for e in connection["edges"]] == list(range(600))
    assert connection["pageInfo"]["hasNextPage"] is False
    assert result.stores[0].status == "ok"
    assert "variants" not in caplog.text


def test_variants_stop_at_the_bound(monkeypatch, caplog):
    monkeypatch.setattr(_bigcommerce, "MAX_VARIANTS", 750)
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, sent = scrape_variants(lambda v: variants_answer(0, 250, v["after"] + "x"))
    assert len(sent) == 2
    assert len(items[0]["variants"]["edges"]) == 750
    assert items[0]["variants"]["pageInfo"]["hasNextPage"] is True
    assert result.stores[0].status == "ok"
    assert "only the first 750 variants (stopped at 750)" in caplog.text


def test_variants_query_too_complex_halves_its_page_size():
    def respond(variables):
        if variables["first"] > 100:
            return errors("Query is too complex")
        return variants_answer(250, variables["first"])

    items, result, sent = scrape_variants(respond)
    assert [v["first"] for v in sent] == [250, 125, 62]
    assert len(items[0]["variants"]["edges"]) == 312
    assert result.stores[0].status == "ok"


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (errors("Boom"), "Boom"),
        ((503, ""), "HTTP 503"),
        (variants_answer(250, 1, "v1"), "no new endCursor"),
        ({"data": {"site": {"product": None}}}, "no variants in the answer"),
    ],
    ids=["graphql_error", "503", "repeated_cursor", "null_product"],
)
def test_variants_failure_keeps_what_was_read(spec, reason, caplog):
    with caplog.at_level(logging.WARNING, logger="shopify_spy"):
        items, result, _ = scrape_variants(lambda v: spec, retries=0)
    assert len(items) == 1
    assert items[0]["variants"]["pageInfo"]["hasNextPage"] is True
    assert result.stores[0].status == "ok"
    assert "BigCommerce product 0: only the first " in caplog.text
    assert reason in caplog.text


def test_variants_401_refreshes_the_token():
    home = Seq([homepage(TOKEN), homepage(FRESH)])
    tokens = []

    def respond(request):
        body = json.loads(request.body)
        tokens.append(request.headers["Authorization"].removeprefix("Bearer "))
        if body["query"] == _QUERY:
            return answer([{"node": many_variants(250)}])
        if tokens[-1] == TOKEN:
            return REJECTED
        return variants_answer(250, 10)

    items, result, _ = scrape({f"{STORE}/": home, ("POST", API): respond}, platform="bigcommerce")
    assert tokens == [TOKEN, TOKEN, FRESH]
    assert len(items[0]["variants"]["edges"]) == 260
    assert result.stores[0].status == "ok"


def test_variants_401_after_a_products_page_refresh_refreshes_again():
    home = Seq([homepage(TOKEN), homepage(FRESH), homepage(bc_token(sid=3))])
    tokens = []

    def respond(request):
        body = json.loads(request.body)
        tokens.append(request.headers["Authorization"].removeprefix("Bearer "))
        if body["query"] == _QUERY:
            if tokens[-1] == TOKEN:
                return REJECTED
            return answer([{"node": many_variants(250)}])
        if tokens[-1] == FRESH:
            return REJECTED
        return variants_answer(250, 10)

    items, result, _ = scrape({f"{STORE}/": home, ("POST", API): respond}, platform="bigcommerce")
    assert tokens == [TOKEN, FRESH, FRESH, bc_token(sid=3)]
    assert len(items[0]["variants"]["edges"]) == 260
    assert result.stores[0].status == "ok"


def test_token_never_logged(caplog):
    home = Seq([homepage(TOKEN), homepage(FRESH)])
    pages = {None: page(2, 0, "c1"), "c1": REJECTED}
    with caplog.at_level(logging.DEBUG, logger="shopify_spy"):
        _, result, _, _ = scrape_bc(pages, home=home)
    assert "c1" in caplog.text or "page 2" in caplog.text
    for token in (TOKEN, FRESH):
        assert token not in caplog.text
        assert all(token not in e for e in result.stores[0].errors)
