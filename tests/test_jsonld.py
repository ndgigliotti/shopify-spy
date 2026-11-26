import json

from shopify_spy._jsonld import OUT_OF_STOCK, parse_product_page

STORE = "https://shop.example"
PAGE = f"{STORE}/products/tee"


def ld_page(*objects):
    scripts = "".join(
        f'<script type="application/ld+json">{o if isinstance(o, str) else json.dumps(o)}</script>'
        for o in objects
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def variant(sku, size):
    return {"@type": "Product", "sku": sku, "size": size, "url": f"{PAGE}?variant={sku}"}


GROUP = {
    "@type": "ProductGroup",
    "name": "Tee",
    "productGroupID": "TEE",
    "variesBy": ["https://schema.org/size"],
    "hasVariant": [variant("TEE-S", "S"), variant("TEE-M", "M")],
}


def parse(html, url=PAGE, **kwargs):
    return parse_product_page(html, url, **kwargs)[0]


def test_product_group_found_only_with_groups():
    html = ld_page(GROUP)
    assert parse(html) is None
    assert parse(html, groups=True) == GROUP


def test_product_group_list_type():
    group = {**GROUP, "@type": ["ProductGroup"]}
    assert parse(ld_page(group), groups=True) == group


def test_product_group_in_graph():
    html = ld_page({"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, GROUP]})
    assert parse(html, groups=True) == GROUP


def test_page_url_match_wins_over_group():
    related = {**GROUP, "name": "Other", "url": f"{STORE}/products/other"}
    product = {"@type": "Product", "name": "Tee", "url": f"{PAGE}/"}
    assert parse(ld_page(related, product), groups=True) == product


def test_group_matching_the_page_wins_over_related_product():
    group = {**GROUP, "@id": f"{PAGE}#group"}
    related = {"@type": "Product", "name": "Related", "url": f"{STORE}/products/other"}
    assert parse(ld_page(related, group), groups=True) == group


def test_variants_stay_inside_the_group():
    # The variant names the page, so it would win if it were a candidate
    group = {**GROUP, "hasVariant": [{"@type": "Product", "sku": "TEE-S", "url": PAGE}]}
    assert parse(ld_page(group), groups=True) == group


def category_page():
    return ld_page(
        *({"@type": "Product", "name": name, "url": f"{STORE}/products/{name}"} for name in "abc")
    )


def test_no_fallback_skips_a_listing_page():
    html = category_page()
    url = f"{STORE}/collections/all"
    assert parse(html, url, groups=True, fallback=False) is None


def test_default_fallback_returns_the_first_node_on_a_listing_page():
    found = parse(category_page(), f"{STORE}/collections/all")
    assert found["name"] == "a"


def test_no_fallback_keeps_a_single_unmatched_product():
    product = {"@type": "Product", "name": "Tee"}
    assert parse(ld_page(product), groups=True, fallback=False) == product


def test_no_fallback_keeps_a_group_beside_its_variants():
    html = ld_page(GROUP, *GROUP["hasVariant"])
    assert parse(html, groups=True, fallback=False) == GROUP


def test_no_fallback_skips_two_unmatched_groups():
    other = {**GROUP, "name": "Other"}
    assert parse(ld_page(GROUP, other), groups=True, fallback=False) is None


def test_no_fallback_still_prefers_the_page_match():
    html = category_page()
    url = f"{STORE}/products/b"
    assert parse(html, url, groups=True, fallback=False)["name"] == "b"


def test_no_products():
    html = ld_page({"@type": "Organization"}, "{not json")
    assert parse(html, groups=True, fallback=False) is None


def test_out_of_stock_still_importable_from_woocommerce():
    from shopify_spy import _woocommerce

    assert _woocommerce.OUT_OF_STOCK is OUT_OF_STOCK
    assert {"OutOfStock", "SoldOut", "Discontinued"} == OUT_OF_STOCK
