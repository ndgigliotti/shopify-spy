import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from shopify_spy._jsonld import OUT_OF_STOCK, amount, offers, parse_product_page

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


def test_references_resolved_across_scripts():
    image = {"@type": "ImageObject", "@id": f"{PAGE}#primaryimage", "url": "https://cdn.test/a.jpg"}
    webpage = {"@type": "WebPage", "@id": PAGE, "image": {"@id": image["@id"]}}
    product = {
        "@type": "Product",
        "name": "Tee",
        "image": [{"@id": image["@id"]}, {"@id": "#missing"}],
        "brand": {"@type": "Brand", "@id": "#brand"},
        "mainEntityOfPage": {"@id": PAGE},
    }
    brand = {"@type": "Brand", "@id": "#brand", "name": "Acme"}
    html = ld_page({"@graph": [webpage, image]}, {"@graph": [product, brand]})
    found = parse(html)
    assert found["image"] == [image, {"@id": "#missing"}]
    assert found["brand"] == brand  # a relative @id names the same node
    assert found["mainEntityOfPage"] == {"@id": PAGE}  # the page isn't inlined
    assert product["image"][0] == {"@id": image["@id"]}  # the page's data is left as is


def test_reference_cycles_stay_references():
    a = {"@type": "Product", "@id": "#a", "name": "A", "isRelatedTo": {"@id": "#b"}}
    b = {"@type": "Thing", "@id": "#b", "name": "B", "isRelatedTo": {"@id": "#a"}}
    found = parse(ld_page({"@graph": [a, b]}))
    assert found["isRelatedTo"] == b | {"isRelatedTo": {"@id": "#a"}}


def test_shared_references_resolve_quickly():
    # Each level refers twice to the next, so a full expansion would be 2**40 nodes
    nodes = [{"@id": f"#n{i}", "x": [{"@id": f"#n{i + 1}"}] * 2} for i in range(40)]
    product = {"@type": "Product", "name": "Tee", "x": {"@id": "#n0"}}
    start = time.perf_counter()
    assert parse(ld_page({"@graph": [product, *nodes]}))["name"] == "Tee"
    assert time.perf_counter() - start < 1


FIXTURES = Path(__file__).parent / "fixtures" / "pages"
ANDOVER = "https://andoverfabrics.com/color-camp-dusk-by-alison-glass-tangel-lighter-pool/"


def test_empty_values_are_null():
    # BigCommerce's template leaves "description": , when the product has none
    found = parse((FIXTURES / "andoverfabrics-empty-value.html").read_text(), ANDOVER)
    assert (found["sku"], found["description"]) == ("A-10420-B", None)


def test_empty_values_before_a_brace_or_bracket():
    script = '{"@type": "Product", "name": "Tee", "a": [{"b": }], "c": ,}'
    assert parse(ld_page(script)) == {
        "@type": "Product",
        "name": "Tee",
        "a": [{"b": None}],
        "c": None,
    }


def test_empty_value_markers_inside_strings_are_kept():
    script = '{"@type": "Product", "name": "a: , b: } c: ]", "description": }'
    found = parse(ld_page(script))
    assert (found["name"], found["description"]) == ("a: , b: } c: ]", None)


def test_lenient_pass_is_linear_on_hostile_scripts():
    start = time.perf_counter()
    assert parse(ld_page("{" + ": " * 200_000, '{"a": "' + '\\"' * 200_000)) is None
    assert time.perf_counter() - start < 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("19.99", "19.99"),
        (19.9, "19.9"),
        ("1.234,56", "1234.56"),
        ("1,299.00", "1299.00"),
        ("$19.99", "19.99"),
        ("19,99", "19.99"),
        ("1,299", "1299"),
        ("1.234.567", "1234567"),
        ("1 234,56 €", "1234.56"),
        ("1'234.50", "1234.50"),
        ("1,23,456.00", "123456.00"),
        ("EUR 12", "12"),
        ("19,-", "19"),
        ("", None),
        ("Call us", None),
        ("10-20", None),
        ("-5", None),
        ("1.2.3", None),
        ("12,34,56", None),
        ("1,234.567,89", None),
        ("1" * 100, None),
    ],
)
def test_amount_reads_formatted_prices(value, expected):
    assert amount(value) == (None if expected is None else Decimal(expected))


def test_aggregate_offer_takes_what_it_lacks_from_its_offers():
    nested = [
        {"@type": "Offer", "price": "10", "availability": "https://schema.org/OutOfStock"},
        {"@type": "Offer", "price": "30", "priceCurrency": "USD", "availability": "InStock"},
    ]
    found = offers({"@type": "AggregateOffer", "offers": nested})
    assert (found.current, found.currency, found.availability) == (
        [Decimal(10), Decimal(30)],
        "USD",
        "InStock",
    )
    # Its own bounds and availability stand
    own = {"@type": "AggregateOffer", "lowPrice": "5", "availability": "SoldOut", "offers": nested}
    found = offers(own)
    assert (found.current, found.bounds, found.availability) == ([], [Decimal(5)], "SoldOut")


def test_availability_terms_without_namespace():
    for written in ("schema:OutOfStock", "http://schema.org/OutOfStock/", "OutOfStock"):
        assert offers({"availability": written}).availability == "OutOfStock"


@pytest.mark.parametrize(
    "kind", ["http://schema.org/Product", "https://schema.org/Product", "schema:Product"]
)
def test_type_as_an_iri_or_prefixed(kind):
    found = parse(ld_page({"@type": kind, "schema:name": "Pan", "http://schema.org/sku": "P-1"}))
    assert found == {"@type": "Product", "name": "Pan", "sku": "P-1"}


def test_unprefixed_property_wins_over_prefixed():
    found = parse(ld_page({"@type": "Product", "schema:name": "B", "name": "A"}))
    assert found["name"] == "A"


def test_product_under_main_entity():
    product = {"@type": "Product", "name": "Kayak"}
    page = {"@type": "ItemPage", "mainEntity": product}
    assert parse(ld_page(page), groups=True, fallback=False) == product


def test_variants_naming_their_group_become_its_variants():
    group = {"@type": "ProductGroup", "@id": "#group", "name": "Tee"}
    small = {"@type": "Product", "isVariantOf": {"@id": "#group"}, "sku": "S"}
    medium = {"@type": "Product", "isVariantOf": {"@id": f"{PAGE}#group"}, "sku": "M"}
    for fallback in (True, False):
        found = parse(ld_page({"@graph": [small, group, medium]}), groups=True, fallback=fallback)
        assert found == group | {"hasVariant": [small, medium]}


def test_variants_naming_their_group_join_its_own_variants():
    lone = {"@type": "Product", "isVariantOf": {"@id": "#g"}, "sku": "X"}
    group = GROUP | {"@id": "#g"}
    found = parse(ld_page(group, lone), groups=True, fallback=False)
    assert found == group | {"hasVariant": [*GROUP["hasVariant"], lone]}


@pytest.mark.parametrize(
    "script",
    [
        '<!-- {"@type": "Product", "name": "Lamp"} -->',
        '\n<!--\n{"@type": "Product", "name": "Lamp"}\n-->\n',
        '<![CDATA[{"@type": "Product", "name": "Lamp"}]]>',
    ],
)
def test_json_in_an_html_comment(script):
    assert parse(ld_page(script)) == {"@type": "Product", "name": "Lamp"}


def test_page_matched_but_for_the_query_string():
    # The page lists the product under its first variant's URL, beside a related one
    main = {"@type": "Product", "name": "Boot", "url": f"{PAGE}?variant=1"}
    related = {"@type": "Product", "name": "Lace", "url": f"{STORE}/products/lace"}
    assert parse(ld_page([main, related]), groups=True, fallback=False) == main


def test_relative_product_url_matches_the_page():
    main = {"@type": "Product", "name": "Boot", "url": "/products/tee"}
    related = {"@type": "Product", "name": "Lace", "url": "/products/lace"}
    assert parse(ld_page([related, main]), groups=True, fallback=False) == main


def test_several_query_string_matches_are_not_the_page():
    products = [{"@type": "Product", "name": n, "url": f"{PAGE}?variant={n}"} for n in "ab"]
    assert parse(ld_page(products), groups=True, fallback=False) is None


def test_bigcommerce_gtin12_in_the_row():
    from shopify_spy._flatten import flatten

    found = parse((FIXTURES / "andoverfabrics-empty-value.html").read_text(), ANDOVER)
    (row,) = flatten(found | {"store": "andoverfabrics.com", "platform": "generic"})
    assert (row["gtin"], row["sku"], row["available"]) == ("199716017379", "A-10420-B", False)


def test_has_variant_and_is_variant_of_are_merged():
    small = {"@type": "Product", "@id": "#s", "isVariantOf": {"@id": "#g"}, "sku": "S"}
    large = {"@type": "Product", "@id": "#l", "isVariantOf": {"@id": "#g"}, "sku": "L"}
    group = {"@type": "ProductGroup", "@id": "#g", "name": "Tee", "hasVariant": [{"@id": "#s"}]}
    found = parse(ld_page({"@graph": [group, small, large]}), groups=True, fallback=False)
    assert [v["sku"] for v in found["hasVariant"]] == ["S", "L"]


def test_merged_variants_are_deduplicated_by_sku():
    inline = {"@type": "Product", "sku": "S", "offers": {"price": "1"}}
    small = {"@type": "Product", "isVariantOf": {"@id": "#g"}, "sku": "S"}
    large = {"@type": "Product", "isVariantOf": {"@id": "#g"}, "sku": "L"}
    group = {"@type": "ProductGroup", "@id": "#g", "hasVariant": inline}
    found = parse(ld_page(group, small, large), groups=True, fallback=False)
    assert found["hasVariant"] == [inline, large]


def test_variants_sharing_their_group_sku_are_all_kept():
    group = {"@type": "ProductGroup", "@id": "#g", "name": "Tee", "sku": "TEE"}
    sizes = [
        {"@type": "Product", "isVariantOf": {"@id": "#g"}, "sku": "TEE", "size": s} for s in "SML"
    ]
    found = parse(ld_page({"@graph": [group, *sizes]}), groups=True, fallback=False)
    assert [v["size"] for v in found["hasVariant"]] == ["S", "M", "L"]


def test_merged_variants_match_ids_across_http_and_https():
    inline = {"@id": PAGE.replace("https://", "http://") + "#s", "offers": {"price": "1"}}
    small = {"@type": "Product", "@id": f"{PAGE}#s", "isVariantOf": {"@id": "#g"}}
    group = {"@type": "ProductGroup", "@id": "#g", "hasVariant": [inline]}
    found = parse(ld_page(group, small), groups=True, fallback=False)
    assert found["hasVariant"] == [inline]
