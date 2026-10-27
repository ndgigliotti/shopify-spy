"""spy.summarize() across platforms (plan §4.3 "Items and summarize()")."""

import copy
from decimal import Decimal

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
