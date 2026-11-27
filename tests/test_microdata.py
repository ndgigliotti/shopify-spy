"""schema.org microdata on pages without a JSON-LD Product."""

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from shopify_spy._flatten import flatten
from shopify_spy._jsonld import parse_product_page
from shopify_spy._microdata import items

FIXTURES = Path(__file__).parent / "fixtures" / "pages"
PAGE = "https://shop.example/products/tee"


def fixture(name):
    return (FIXTURES / f"{name}.html").read_text()


def parse(page, url=PAGE, **kwargs):
    return parse_product_page(page, url, groups=True, fallback=False, **kwargs)[0]


def row(node, url=PAGE):
    item = node | {"store": "shop.example", "platform": "jsonld", "html_url": url}
    return flatten(item)[0]


def test_shopware_product():
    url = "https://drykorn.com/de-de/10-40-260241-40709-RICC-3600-33x32"
    found = parse(fixture("drykorn-shopware"), url)
    assert found["@type"] == "Product"
    # Shopware puts the name inside the offer, so the product takes it from there
    assert (found["name"], found["brand"], found["gtin13"]) == (
        "RICC",
        {"@type": "Brand", "name": "DRYKORN"},
        "4064908584143",
    )
    assert found["offers"]["price"] == "104.95"
    assert found["image"][0].endswith("RICC_3600_01.webp?ts=1739448100")
    cells = row(found, url)
    assert (cells["title"], cells["vendor"], cells["currency"], cells["price"]) == (
        "RICC",
        "DRYKORN",
        "EUR",
        Decimal("104.95"),
    )


def test_bigcommerce_product():
    url = "https://bohemiantraders.com/swing-tee-in-magenta/"
    found = parse(fixture("bohemiantraders-bigcommerce"), url)
    assert found["name"] == "Swing Tee in Magenta"
    assert found["brand"] == {
        "@type": "Brand",
        "url": "https://bohemiantraders.com/bohemian-traders/",
        "name": "Bohemian Traders",
    }
    assert found["offers"] == {
        "@type": "Offer",
        "priceCurrency": "USD",
        "price": "118",
        "availability": "https://schema.org/InStock",
    }
    # A ratingValue of 0 without a scale (no reviews yet) isn't kept
    assert found["aggregateRating"] == {"@type": "AggregateRating", "reviewCount": "0"}


def test_bigcommerce_product_beside_breadcrumbs():
    url = "https://noveltymfg.com/shop/pots-planters/ella-wall-pot/"
    found = parse(fixture("noveltymfg-bigcommerce"), url)
    assert (found["name"], found["sku"]) == ("Ella Wall Pot", "3505X")
    assert found["description"].startswith("Description The Ella Wall Pot is part of")


def test_magento_luma_product():
    found = parse(fixture("mage-os-luma"), "https://demo.mage-os.org/erika-running-short.html")
    assert (found["name"], found["sku"]) == ("Erika Running Short", "WSH12")
    assert found["image"] == (
        "https://demo.mage-os.org/media/catalog/product/cache/"
        "207e23213cf636ccdef205098cf3c8a3/w/s/wsh12-green_main_1.jpg"
    )
    assert found["offers"] == {"@type": "Offer", "price": "45", "priceCurrency": "USD"}
    # Luma's rating is a percentage, so it's kept with its scale
    assert found["aggregateRating"]["ratingValue"] == "60"
    assert found["aggregateRating"]["bestRating"] == "100"


SYNTHETIC = """
<html><head><title>Tee</title></head><body>
<div itemscope itemtype="https://schema.org/Product" itemid="#tee">
  <h1 itemprop="name">Felt
     &amp; Wool <b>Tee</b></h1>
  <img itemprop="image" src="/media/tee.jpg"><img itemprop="image" src="//cdn.test/b.jpg">
  <a itemprop="url" href="/products/tee?ref=x">link</a>
  <p itemprop="description">Soft. <script>var x = "not text";</script><style>p{}</style>Warm.</p>
  <span itemprop="sku" content="TEE-1">ignored text</span>
  <data itemprop="gtin13" value="0123456789012">barcode</data>
  <time itemprop="releaseDate" datetime="2024-01-02">Jan 2</time>
  <time itemprop="productionDate">2023</time>
  <div itemprop="brand" itemscope itemtype="https://schema.org/Brand">
    <meta itemprop="name" content="Acme">
  </div>
  <div itemprop="image" itemscope itemtype="https://schema.org/ImageObject">
    <link itemprop="contentUrl" href="/media/c.jpg">
  </div>
  <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
    <meter itemprop="ratingValue" value="4.5">4.5</meter> of
    <span itemprop="bestRating">5</span>, <span itemprop="reviewCount">12</span> reviews
  </div>
  <div itemprop="offers" itemscope itemtype="https://schema.org/AggregateOffer">
    <span itemprop="lowPrice">10.00</span> - <span itemprop="highPrice">20.00</span>
    <meta itemprop="priceCurrency" content="USD">
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
      <meta itemprop="price" content="10.00">
      <link itemprop="availability" href="https://schema.org/InStock">
    </div>
  </div>
</div>
<div itemscope itemtype="https://schema.org/Organization"><span itemprop="name">Shop</span></div>
</body></html>
"""


def test_synthetic_product_values():
    found = parse(SYNTHETIC)
    assert found["@type"] == "Product"
    assert found["@id"] == "#tee"
    assert found["name"] == "Felt & Wool Tee"
    assert found["image"] == [
        "https://shop.example/media/tee.jpg",
        "https://cdn.test/b.jpg",
        {"@type": "ImageObject", "contentUrl": "https://shop.example/media/c.jpg"},
    ]
    assert found["url"] == "https://shop.example/products/tee?ref=x"
    assert found["description"] == "Soft. Warm."
    assert (found["sku"], found["gtin13"]) == ("TEE-1", "0123456789012")
    assert (found["releaseDate"], found["productionDate"]) == ("2024-01-02", "2023")
    assert found["brand"] == {"@type": "Brand", "name": "Acme"}
    assert found["aggregateRating"] == {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "bestRating": "5",
        "reviewCount": "12",
    }
    assert found["offers"]["offers"] == {
        "@type": "Offer",
        "price": "10.00",
        "availability": "https://schema.org/InStock",
    }
    cells = row(found)
    assert (cells["min_price"], cells["max_price"], cells["available"]) == (
        Decimal("10.00"),
        Decimal("20.00"),
        True,
    )
    assert cells["image_url"] == "https://shop.example/media/tee.jpg"


def test_top_level_items_in_document_order():
    found = items(SYNTHETIC, PAGE)
    assert [i["@type"] for i in found] == ["Product", "Organization"]


def test_json_ld_product_wins_over_microdata():
    ld = {"@type": "Product", "name": "From JSON-LD"}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>{SYNTHETIC}'
    assert parse(page) == ld


def test_microdata_when_json_ld_has_no_product():
    ld = {"@type": "Organization", "name": "Shop"}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>{SYNTHETIC}'
    assert parse(page)["name"] == "Felt & Wool Tee"


def test_microdata_listing_page_is_skipped():
    product = '<div itemscope itemtype="http://schema.org/Product"><b itemprop="name">{}</b></div>'
    page = product.format("A") + product.format("B")
    assert parse(page, "https://shop.example/collections/all") is None
    assert parse_product_page(page, PAGE)[0]["name"] == "A"


def test_product_group_with_variants():
    page = """
    <div itemscope itemtype="https://schema.org/ProductGroup">
      <span itemprop="name">Tee</span><meta itemprop="variesBy" content="size">
      <div itemprop="hasVariant" itemscope itemtype="https://schema.org/Product">
        <meta itemprop="sku" content="S"><meta itemprop="size" content="Small">
        <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="9">
        </div>
      </div>
    </div>"""
    found = parse(page)
    assert found["hasVariant"]["sku"] == "S"
    cells = row(found)
    assert (cells["variant_count"], cells["option1"], cells["price"]) == (1, "Small", Decimal("9"))


@pytest.mark.parametrize(
    "markup",
    [
        # Unclosed elements end with what holds them
        '<div itemscope itemtype="http://schema.org/Product"><p itemprop="name">Tee<p>x</div>',
        # A stray end tag is ignored
        '<div itemscope itemtype="http://schema.org/Product"></span><b itemprop="name">Tee</b>',
        # Self-closing syntax on a normal element
        '<div itemscope itemtype="http://schema.org/Product"><span itemprop="sku"/>'
        '<i itemprop="name">Tee</i></div>',
    ],
)
def test_sloppy_markup(markup):
    assert parse(markup)["name"] == "Tee"


def test_rating_without_a_scale_is_left_out():
    page = """<div itemscope itemtype="http://schema.org/Product"><b itemprop="name">Tee</b>
      <div itemprop="aggregateRating" itemscope itemtype="http://schema.org/AggregateRating">
        <span itemprop="ratingValue">60</span><span itemprop="reviewCount">3</span>
      </div></div>"""
    assert parse(page)["aggregateRating"] == {"@type": "AggregateRating", "reviewCount": "3"}


def test_pages_without_a_product_type_are_not_parsed():
    page = '<div itemscope itemtype="https://schema.org/Organization">x</div>'
    assert items(page, PAGE) == []


def test_hostile_pages_parse_quickly():
    product = '<div itemscope itemtype="http://schema.org/Product">'
    pages = [
        product + '<span itemprop="name">' * 20_000,
        product * 20_000,
        product + "<div>" * 50_000 + "</span>" * 50_000,
        product + '<p itemprop="description">' + "x " * 500_000,
    ]
    start = time.perf_counter()
    for page in pages:
        parse(page)
    assert time.perf_counter() - start < 5
