"""spy.summarize() across platforms (plan §4.3 "Items and summarize()")."""

import copy
from decimal import Decimal

import pytest

import shopify_spy as spy

SHOPIFY_PRODUCT = {
    "product": {
        "id": 101,
        "title": "Hat",
        "handle": "hat",
        "variants": [{"price": "19.99"}, {"price": "5.00"}, {"price": "120.50"}, {"price": None}],
    },
    "url": "https://shop.test/products/hat.json",
    "store": "shop.test",
    "image_urls": ["https://cdn.test/hat.jpg"],
}

SHOPIFY_COLLECTION = {
    "collection": {"id": 7, "title": "Sale", "handle": "sale"},
    "url": "http://shop.test/collections/sale.json",
    "store": "shop.test",
    "image_urls": [],
}

WOO_PRODUCT = {
    "id": 55,
    "name": "Mug",
    "permalink": "https://woo.test/product/mug/",
    "prices": {
        "price": "1250",
        "currency_code": "EUR",
        "currency_minor_unit": 2,
        "price_range": {"min_amount": "999", "max_amount": "1999"},
    },
    "images": [{"src": "https://woo.test/mug.jpg"}],
    "store": "woo.test",
    "image_urls": ["https://woo.test/mug.jpg"],
}


def test_shopify_product():
    s = spy.summarize(SHOPIFY_PRODUCT)
    assert s.kind == "product"
    assert s.platform == "shopify"
    assert s.id == 101
    assert s.title == "Hat"
    assert s.min_price == Decimal("5.00")
    assert s.max_price == Decimal("120.50")
    assert isinstance(s.min_price, Decimal)
    assert s.currency is None
    assert s.html_url == "https://shop.test/products/hat"
    assert s.store == "shop.test"
    assert s.image_urls == ["https://cdn.test/hat.jpg"]


def test_shopify_product_without_variants():
    item = {**SHOPIFY_PRODUCT, "product": {"id": 1, "handle": "x"}}
    s = spy.summarize(item)
    assert s.min_price is None
    assert s.max_price is None


def test_shopify_prices_that_cant_be_compared_are_skipped():
    variants = [{"price": "NaN"}, {"price": "sNaN"}, {"price": "Infinity"}, {"price": "7"}, 3]
    s = spy.summarize({**SHOPIFY_PRODUCT, "product": {"id": 1, "variants": variants}})
    assert (s.min_price, s.max_price) == (Decimal("7"), Decimal("7"))
    s = spy.summarize({**SHOPIFY_PRODUCT, "product": {"id": 1, "variants": 5}})
    assert (s.min_price, s.max_price) == (None, None)


def test_shopify_collection():
    s = spy.summarize(SHOPIFY_COLLECTION)
    assert s.kind == "collection"
    assert s.platform == "shopify"
    assert s.id == 7
    assert s.title == "Sale"
    assert s.min_price is None
    assert s.html_url == "http://shop.test/collections/sale"


def test_woocommerce_minor_units():
    s = spy.summarize(WOO_PRODUCT)
    assert s.kind == "product"
    assert s.platform == "woocommerce"
    assert s.id == 55
    assert s.title == "Mug"
    assert s.min_price == Decimal("9.99")
    assert s.max_price == Decimal("19.99")
    assert s.currency == "EUR"
    assert s.html_url == "https://woo.test/product/mug/"
    assert s.store == "woo.test"
    assert s.image_urls == ["https://woo.test/mug.jpg"]


def test_woocommerce_title_html_entities_decoded():
    item = {**WOO_PRODUCT, "name": "17&#8243; Canton Box &amp; Lid"}
    assert spy.summarize(item).title == "17″ Canton Box & Lid"
    assert item["name"] == "17&#8243; Canton Box &amp; Lid"  # the item itself is untouched


def test_woocommerce_price_without_range():
    prices = {"price": "500", "currency_code": "JPY", "currency_minor_unit": 0}
    s = spy.summarize({**WOO_PRODUCT, "prices": prices})
    assert s.min_price == Decimal("500")
    assert s.max_price == Decimal("500")
    assert s.currency == "JPY"


def test_summarize_does_not_change_item():
    item = copy.deepcopy(SHOPIFY_PRODUCT)
    spy.summarize(item)
    assert item == SHOPIFY_PRODUCT


def test_asdict_prices_as_strings():
    d = spy.summarize(SHOPIFY_PRODUCT).asdict()
    assert d["min_price"] == "5.00"
    assert d["max_price"] == "120.50"
    assert d["currency"] is None
    w = spy.summarize(WOO_PRODUCT).asdict()
    assert w["min_price"] == "9.99"
    assert w["max_price"] == "19.99"
    assert set(d) == {
        "kind",
        "platform",
        "id",
        "title",
        "min_price",
        "max_price",
        "currency",
        "html_url",
        "store",
        "image_urls",
    }


def test_woocommerce_zero_min_price_kept():
    # A free variant's 0 is a price, not a missing value
    prices = {
        "price": "500",
        "currency_code": "USD",
        "currency_minor_unit": 2,
        "price_range": {"min_amount": "0", "max_amount": "1000"},
    }
    s = spy.summarize({**WOO_PRODUCT, "prices": prices})
    assert s.min_price == Decimal("0")
    assert s.max_price == Decimal("10")


def test_image_urls_come_from_the_payload_when_images_are_off():
    # Live items have empty image_urls unless images are downloaded
    shopify = copy.deepcopy(SHOPIFY_PRODUCT)
    shopify["image_urls"] = []
    shopify["product"]["images"] = [{"src": "https://cdn.test/a.jpg"}, {"src": None}]
    collection = copy.deepcopy(SHOPIFY_COLLECTION)
    collection["collection"]["image"] = {"src": "https://cdn.test/c.jpg"}
    woo = {**WOO_PRODUCT, "image_urls": []}
    assert spy.summarize(shopify).image_urls == ["https://cdn.test/a.jpg"]
    assert spy.summarize(collection).image_urls == ["https://cdn.test/c.jpg"]
    assert spy.summarize(woo).image_urls == ["https://woo.test/mug.jpg"]
    assert spy.summarize({**woo, "images": None}).image_urls == []


def test_woocommerce_prices_survive_odd_amounts_and_units(caplog):
    from shopify_spy._summary import woo_money

    assert woo_money({"currency_minor_unit": "2.0"})("1990") == Decimal("19.90")
    assert woo_money({"currency_minor_unit": 2})("1e30") == Decimal("1e28")
    assert woo_money({"currency_minor_unit": 2})("Infinity") == Decimal("Infinity")
    assert woo_money({"currency_minor_unit": "x"})("7") == Decimal("7")
    assert "Unreadable currency_minor_unit" in caplog.text


def _money(value, currency="CHF"):
    return {"value": value, "currency": currency}


MAGENTO_PRODUCT = {
    "uid": "MTIz",
    "sku": "TEE",
    "name": "Tee",
    "url_key": "tee",
    "url_suffix": ".html",
    "price_range": {
        "minimum_price": {"regular_price": _money(30), "final_price": _money(19.9)},
        "maximum_price": {"regular_price": _money(30), "final_price": _money(25)},
    },
    "media_gallery": [
        {"url": "https://m.test/b.jpg", "position": 2, "disabled": False},
        {"url": "https://m.test/a.jpg", "position": 1, "disabled": False},
    ],
    "store": "m.test",
    "platform": "magento",
    "html_url": "https://m.test/tee.html",
    "image_urls": [],
}


def test_magento_product():
    s = spy.summarize(MAGENTO_PRODUCT)
    assert (s.kind, s.platform, s.id, s.title) == ("product", "magento", "MTIz", "Tee")
    assert (s.min_price, s.max_price) == (Decimal("19.9"), Decimal("25"))
    assert s.currency == "CHF"
    assert (s.html_url, s.store) == ("https://m.test/tee.html", "m.test")
    # image_urls is empty without images; the gallery stands in, by position
    assert s.image_urls == ["https://m.test/a.jpg", "https://m.test/b.jpg"]


def test_magento_product_without_prices():
    s = spy.summarize({**MAGENTO_PRODUCT, "price_range": {}})
    assert (s.min_price, s.max_price, s.currency) == (None, None, None)


def test_magento_summary_dict_is_not_a_magento_item():
    from shopify_spy._flatten import flatten

    assert flatten(spy.summarize(MAGENTO_PRODUCT).asdict()) is None


def _bc_money(value, currency="GBP"):
    return {"value": value, "currencyCode": currency}


BIGCOMMERCE_PRODUCT = {
    "entityId": 77,
    "sku": "TEE",
    "name": "Tee",
    "path": "/tee/",
    "prices": {
        "price": _bc_money(19.5),
        "priceRange": {"min": _bc_money(19.5), "max": _bc_money(25)},
    },
    "images": {
        "edges": [
            {"node": {"urlOriginal": "https://bc.test/a.jpg", "isDefault": False}},
            {"node": {"urlOriginal": "https://bc.test/b.jpg", "isDefault": True}},
        ]
    },
    "store": "bc.test",
    "platform": "bigcommerce",
    "html_url": "https://bc.test/tee/",
    "image_urls": [],
}


def test_bigcommerce_product():
    s = spy.summarize(BIGCOMMERCE_PRODUCT)
    assert (s.kind, s.platform, s.id, s.title) == ("product", "bigcommerce", 77, "Tee")
    assert (s.min_price, s.max_price) == (Decimal("19.5"), Decimal("25"))
    assert s.currency == "GBP"
    assert (s.html_url, s.store) == ("https://bc.test/tee/", "bc.test")
    # image_urls is empty without images; the images connection stands in, default first
    assert s.image_urls == ["https://bc.test/b.jpg", "https://bc.test/a.jpg"]


def test_bigcommerce_product_without_a_price_range_uses_the_price():
    prices = {"price": _bc_money(8, "USD")}
    s = spy.summarize({**BIGCOMMERCE_PRODUCT, "prices": prices})
    assert (s.min_price, s.max_price, s.currency) == (Decimal("8"), Decimal("8"), "USD")


def test_bigcommerce_product_without_prices():
    s = spy.summarize({**BIGCOMMERCE_PRODUCT, "prices": None})
    assert (s.min_price, s.max_price, s.currency) == (None, None, None)


def test_bigcommerce_summary_dict_is_not_a_bigcommerce_item():
    from shopify_spy._flatten import flatten, item_platform

    summary = spy.summarize(BIGCOMMERCE_PRODUCT).asdict()
    assert flatten(summary) is None
    assert item_platform(summary) is None


GENERIC_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Hat &amp; Scarf",
    "sku": "HAT-1",
    "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "12.50",
        "highPrice": "20",
        "priceCurrency": "USD",
    },
    "image": ["https://j.test/hat.jpg"],
    "store": "j.test",
    "platform": "generic",
    "html_url": "https://j.test/products/hat",
    "image_urls": [],
}


def test_generic_product():
    s = spy.summarize(GENERIC_PRODUCT)
    assert (s.kind, s.platform, s.id, s.title) == ("product", "generic", "HAT-1", "Hat & Scarf")
    assert (s.min_price, s.max_price, s.currency) == (Decimal("12.50"), Decimal("20"), "USD")
    assert (s.html_url, s.store) == ("https://j.test/products/hat", "j.test")
    assert s.image_urls == ["https://j.test/hat.jpg"]


def test_generic_image_urls_do_not_depend_on_images():
    variant = {"@type": "Product", "sku": "S", "image": "/hat-s.jpg"}
    stub = {"@type": "Product", "url": "https://j.test/products/red", "image": "/red.jpg"}
    group = {**GENERIC_PRODUCT, "@type": "ProductGroup", "hasVariant": [variant, stub]}
    expected = ["https://j.test/hat.jpg", "https://j.test/hat-s.jpg"]
    assert spy.summarize(group).image_urls == expected
    assert spy.summarize({**group, "image_urls": [*expected, "x"]}).image_urls == expected


def test_generic_product_group_prices_span_its_variants():
    group = {
        **GENERIC_PRODUCT,
        "@type": "ProductGroup",
        "productGroupID": "G1",
        "offers": None,
        "hasVariant": [
            {"@type": "Product", "offers": {"price": "30", "priceCurrency": "EUR"}},
            {"@type": "Product", "offers": [{"price": "25"}, {"price": "40"}]},
        ],
    }
    s = spy.summarize(group)
    assert (s.id, s.min_price, s.max_price, s.currency) == (
        "G1",
        Decimal("25"),
        Decimal("40"),
        "EUR",
    )


def test_generic_product_without_prices():
    s = spy.summarize({**GENERIC_PRODUCT, "offers": None})
    assert (s.min_price, s.max_price, s.currency) == (None, None, None)


def test_generic_summary_dict_is_not_a_generic_item():
    from shopify_spy._flatten import flatten

    assert flatten(spy.summarize(GENERIC_PRODUCT).asdict()) is None


def _sq_variant(vid, price, *, sale=None, quantity=3, **attributes):
    variant = {
        "id": vid,
        "sku": f"SKU-{vid}",
        "attributes": attributes,
        "onSale": sale is not None,
        "priceMoney": {"currency": "USD", "value": price},
        "unlimited": False,
        "qtyInStock": quantity,
    }
    if sale is not None:
        variant["salePriceMoney"] = {"currency": "USD", "value": sale}
    return variant


SQUARESPACE_PRODUCT = {
    "id": "abc123",
    "title": "Tee",
    "urlId": "tee",
    "fullUrl": "/shop/p/tee",
    "items": [{"assetUrl": "https://images.test/tee.jpg"}],
    "structuredContent": {
        "variants": [
            _sq_variant("v1", "20.00", sale="15.00", Color="Red", Size="S"),
            _sq_variant("v2", "22.00", quantity=0, Color="Red", Size="M"),
        ]
    },
    "store": "sq.test",
    "platform": "squarespace",
    "html_url": "https://sq.test/shop/p/tee",
    "image_urls": [],
    "source": "format_json",
}


def test_squarespace_format_json_product():
    s = spy.summarize(SQUARESPACE_PRODUCT)
    assert (s.kind, s.platform, s.id, s.title) == ("product", "squarespace", "abc123", "Tee")
    # The sale price is the selling price
    assert (s.min_price, s.max_price, s.currency) == (Decimal("15.00"), Decimal("22.00"), "USD")
    assert (s.html_url, s.store) == ("https://sq.test/shop/p/tee", "sq.test")
    assert s.image_urls == ["https://images.test/tee.jpg"]


def test_squarespace_product_page_item_is_a_generic_product():
    page_item = GENERIC_PRODUCT | {"platform": "squarespace", "source": "product_page"}
    s = spy.summarize(page_item)
    assert (s.kind, s.platform, s.id, s.title) == ("product", "squarespace", "HAT-1", "Hat & Scarf")
    assert (s.min_price, s.max_price, s.currency) == (Decimal("12.50"), Decimal("20"), "USD")
    assert (s.html_url, s.store, s.image_urls) == (
        "https://j.test/products/hat",
        "j.test",
        ["https://j.test/hat.jpg"],
    )


def test_squarespace_currency_comes_from_any_variant():
    variants = [{"priceMoney": {"value": "9"}}, _sq_variant("v2", "22.00")]
    s = spy.summarize(SQUARESPACE_PRODUCT | {"structuredContent": {"variants": variants}})
    assert (s.min_price, s.max_price, s.currency) == (Decimal("9"), Decimal("22.00"), "USD")


@pytest.mark.parametrize(
    "variant",
    [
        {"priceMoney": None},
        {"priceMoney": {"value": "abc"}},
        {"onSale": True, "salePriceMoney": {"value": "abc"}, "priceMoney": {"value": "9"}},
        {"priceMoney": {"value": "NaN"}},
        "not a variant",
    ],
)
def test_squarespace_odd_prices_never_raise(variant):
    item = SQUARESPACE_PRODUCT | {"structuredContent": {"variants": [variant]}}
    s = spy.summarize(item)
    expected = Decimal("9") if isinstance(variant, dict) and variant.get("onSale") else None
    assert (s.min_price, s.max_price) == (expected, expected)


def test_squarespace_summary_dict_is_not_a_squarespace_item():
    from shopify_spy._flatten import flatten, item_platform

    summary = spy.summarize(SQUARESPACE_PRODUCT).asdict()
    assert flatten(summary) is None
    assert item_platform(summary) is None


def test_squarespace_item_types_are_exported():
    from typing import get_args

    assert "SquarespaceProductItem" in spy.__all__
    hints = spy.SquarespaceProductItem.__annotations__
    assert get_args(hints["platform"]) == ("squarespace",)
    assert get_args(hints["source"]) == ("format_json",)
    generic_hints = spy.GenericProductItem.__annotations__
    assert get_args(generic_hints["platform"]) == ("generic", "squarespace")
    assert get_args(generic_hints["source"]) == ("product_page",)
