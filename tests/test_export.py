"""Writers match Scrapy's feed exporters byte for byte (JSONL/JSON/XML fixtures were
captured from Scrapy 2.14 before it was removed; CSV differs on purpose, D2)."""

import csv
import json
import os
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from shopify_spy._export import (
    ItemWriter,
    format_for,
    write_items,
    write_status,
)
from shopify_spy._flatten import COLUMNS
from shopify_spy._results import StoreResult, StoreStatus

FIXTURES = Path(__file__).parent / "fixtures" / "exporters"
ITEMS = json.loads((FIXTURES / "items.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fmt", ["jsonl", "json", "xml"])
def test_matches_scrapy_exporter(tmp_path, fmt):
    path = tmp_path / f"out.{fmt}"
    assert write_items(ITEMS, path) == len(ITEMS)
    assert path.read_bytes() == (FIXTURES / f"scrapy.{fmt}").read_bytes()


def test_csv_matches_golden_file(tmp_path):
    # D2: CSV deliberately differs from Scrapy's (a row per variant, scalar cells),
    # so the golden file comes from this writer
    path = tmp_path / "out.csv"
    write_items(ITEMS, path)
    assert path.read_bytes() == (FIXTURES / "expected.csv").read_bytes()


def read_csv(path):
    return list(csv.DictReader(path.open(newline="", encoding="utf-8")))


SHOPIFY_PRODUCT = {
    "product": {
        "id": 7,
        "title": "Hat",
        "handle": "hat",
        "vendor": "Acme",
        "product_type": "Hats",
        "tags": ["wool", "winter"],
        "body_html": "<p>Warm &amp; <b>soft</b></p>\n<script>x()</script><p>Wool</p>",
        "options": [{"name": "Color"}, {"name": "Size"}],
        "images": [{"id": 1, "src": "https://cdn/a.jpg"}, {"id": 2, "src": "https://cdn/b.jpg"}],
        "variants": [
            {
                "id": 71,
                "title": "Red / S",
                "option1": "Red",
                "option2": "S",
                "price": "10.00",
                "compare_at_price": None,
                "available": True,
                "featured_image": None,
                "image_id": 2,
            },
            {
                "id": 72,
                "title": "Blue / M",
                "option1": "Blue",
                "option2": "M",
                "price": "12.50",
                "compare_at_price": "15.00",
                "available": False,
                "featured_image": {"src": "https://cdn/c.jpg"},
            },
        ],
    },
    "url": "https://a.com/products/hat.json",
    "store": "a.com",
    "image_urls": [],
}


def test_csv_writes_a_row_per_variant(tmp_path):
    path = tmp_path / "out.csv"
    assert write_items([SHOPIFY_PRODUCT], path) == 1
    rows = read_csv(path)
    assert len(rows) == 2
    first, second = rows
    # Product columns repeat on every variant row
    for row in rows:
        assert (row["platform"], row["kind"]) == ("shopify", "product")
        assert row["id"] == "7"
        assert row["title"] == "Hat"
        assert row["tags"] == "wool, winter"
        assert row["options"] == "Color, Size"
        assert row["description"] == "Warm & soft Wool"
        assert row["html_url"] == "https://a.com/products/hat"
        assert row["image_url"] == "https://cdn/a.jpg"
        assert row["image_count"] == "2"
        assert row["variant_count"] == "2"
        # The product's price span across its variants; Shopify has no currency
        assert (row["min_price"], row["max_price"], row["currency"]) == ("10.00", "12.50", "")
    assert (first["variant_id"], first["option1"], first["option2"]) == ("71", "Red", "S")
    assert (first["price"], first["compare_at_price"], first["available"]) == ("10.00", "", "true")
    assert first["variant_image_url"] == "https://cdn/b.jpg"
    assert (second["price"], second["compare_at_price"]) == ("12.50", "15.00")
    assert second["available"] == "false"
    assert second["variant_image_url"] == "https://cdn/c.jpg"
    assert "product" not in first


def test_csv_product_without_variants_gets_one_row(tmp_path):
    path = tmp_path / "out.csv"
    item = {"product": {"id": 1, "title": "Gift card", "tags": "a, b"}, "store": "a.com"}
    write_items([item], path)
    (row,) = read_csv(path)
    assert (row["title"], row["tags"], row["variant_count"], row["variant_id"]) == (
        "Gift card",
        "a, b",
        "0",
        "",
    )
    assert (row["min_price"], row["max_price"]) == ("", "")


def test_csv_shopify_price_range_skips_variants_without_prices(tmp_path):
    path = tmp_path / "out.csv"
    variants = [{"id": 1, "price": "20"}, {"id": 2, "price": None}, {"id": 3, "price": "5.00"}]
    write_items([{"product": {"id": 1, "variants": variants}, "store": "a.com"}], path)
    rows = read_csv(path)
    assert [(r["min_price"], r["max_price"]) for r in rows] == [("5.00", "20")] * 3

    # Prices that can't be compared are skipped instead of failing the write
    odd = [{"id": 4, "price": ""}, {"id": 5, "price": "NaN"}, {"id": 6, "price": "Infinity"}]
    for name, variants in [("unpriced", odd), ("not-a-list", 5)]:
        path = tmp_path / f"{name}.csv"
        write_items([{"product": {"id": 2, "variants": variants}}], path)
        assert {(r["min_price"], r["max_price"]) for r in read_csv(path)} == {("", "")}
    path = tmp_path / "mixed.csv"
    write_items([{"product": {"id": 3, "variants": [*odd, {"id": 7, "price": "8"}]}}], path)
    assert {(r["min_price"], r["max_price"]) for r in read_csv(path)} == {("8", "8")}


def test_csv_collection_row(tmp_path):
    path = tmp_path / "out.csv"
    item = {
        "collection": {
            "id": 9,
            "handle": "sale",
            "title": "Sale",
            "description": "<p>Big</p>",
            "image": {"src": "https://cdn/s.jpg"},
            "products_count": 4,
        },
        "url": "http://a.com/collections/sale.json",
        "store": "a.com",
    }
    write_items([SHOPIFY_PRODUCT, item], path)
    row = read_csv(path)[-1]
    assert row["kind"] == "collection"
    assert (row["description"], row["image_url"], row["products_count"]) == (
        "Big",
        "https://cdn/s.jpg",
        "4",
    )
    assert row["html_url"] == "http://a.com/collections/sale"
    assert (row["variant_id"], row["min_price"], row["max_price"]) == ("", "", "")


WOO_PRODUCT = {
    "id": 5,
    "name": "17&#8243; Pan",
    "slug": "pan",
    "type": "variable",
    "permalink": "https://w.com/product/pan/",
    "sku": "P1",
    "short_description": "<p>Short</p>",
    "description": "<p>Long</p>",
    "on_sale": True,
    "prices": {
        "price": "1999",
        "regular_price": "2499",
        "sale_price": "1999",
        "price_range": {"min_amount": "1999", "max_amount": "2999"},
        "currency_code": "USD",
        "currency_minor_unit": 2,
    },
    "images": [{"src": "https://w.com/p.jpg"}],
    "categories": [{"name": "Kitchen &amp; Home"}, {"name": "Pans"}],
    "tags": [],
    "attributes": [{"name": "Size", "terms": [{"name": "S"}, {"name": "L"}]}],
    "variations": [
        {"id": 51, "attributes": [{"name": "Size", "value": "S"}]},
        {"id": 52, "attributes": [{"name": "Size", "value": "L"}]},
    ],
    "is_in_stock": True,
    "store": "w.com",
    "image_urls": [],
}


def test_csv_woocommerce_rows(tmp_path):
    path = tmp_path / "out.csv"
    write_items([WOO_PRODUCT], path)
    rows = read_csv(path)
    assert [r["variant_id"] for r in rows] == ["51", "52"]
    assert [(r["variant_title"], r["option1"]) for r in rows] == [("S", "S"), ("L", "L")]
    row = rows[0]
    assert (row["platform"], row["kind"], row["handle"]) == ("woocommerce", "product", "pan")
    assert row["title"] == "17″ Pan"
    assert row["html_url"] == "https://w.com/product/pan/"
    assert (row["price"], row["compare_at_price"]) == ("19.99", "24.99")
    assert "sale_price" not in row
    assert (row["min_price"], row["max_price"]) == ("19.99", "29.99")
    assert row["currency"] == "USD"
    assert row["categories"] == "Kitchen & Home, Pans"
    assert (row["options"], row["product_type"], row["url"]) == ("Size", "", "")
    assert (row["short_description"], row["description"]) == ("Short", "Long")
    assert (row["on_sale"], row["available"], row["image_count"]) == ("true", "true", "1")
    assert row["variant_count"] == "2"
    assert "prices" not in row


def test_csv_woocommerce_compare_at_price_only_below_regular(tmp_path):
    path = tmp_path / "out.csv"
    prices = {"price": "2499", "regular_price": "2499", "currency_minor_unit": 2}
    write_items([WOO_PRODUCT | {"prices": prices, "variations": []}], path)
    (row,) = read_csv(path)
    assert (row["price"], row["compare_at_price"]) == ("24.99", "")


def test_csv_woocommerce_variation_records_set_their_rows(tmp_path):
    path = tmp_path / "out.csv"
    record = {
        "type": "variation",
        "sku": "P1-L",
        "is_in_stock": False,
        "prices": {"price": "2999", "regular_price": "3499", "currency_minor_unit": 2},
    }
    small, large = WOO_PRODUCT["variations"]
    # The scrape merged the large variation's record; the small one's listing failed
    write_items([WOO_PRODUCT | {"variations": [small, record | large]}], path)
    rows = read_csv(path)
    offers = [(r["sku"], r["price"], r["compare_at_price"], r["available"]) for r in rows]
    assert offers == [("P1", "19.99", "24.99", "true"), ("P1-L", "29.99", "34.99", "false")]
    assert [r["variant_title"] for r in rows] == ["S", "L"]
    assert {(r["min_price"], r["max_price"]) for r in rows} == {("19.99", "29.99")}


def test_csv_woocommerce_options_skip_descriptive_attributes(tmp_path):
    path = tmp_path / "out.csv"
    item = WOO_PRODUCT | {
        "attributes": [
            {"name": "Material", "has_variations": False, "terms": [{"name": "Steel"}]},
            {"name": "Color", "has_variations": True, "terms": [{"name": "Red"}]},
            {"name": "Size", "has_variations": True, "terms": [{"name": "S"}]},
        ],
        # Values follow the product's attribute order, not the variation's
        "variations": [
            {
                "id": 1,
                "attributes": [{"name": "Size", "value": "S"}, {"name": "Color", "value": "Red"}],
            }
        ],
    }
    write_items([item], path)
    (row,) = read_csv(path)
    assert row["options"] == "Color, Size"
    assert (row["option1"], row["option2"], row["option3"]) == ("Red", "S", "")
    assert row["variant_title"] == "Red / S"


def test_csv_holds_both_platforms(tmp_path):
    path = tmp_path / "out.csv"
    write_items([WOO_PRODUCT, SHOPIFY_PRODUCT], path)
    rows = read_csv(path)
    assert [r["platform"] for r in rows] == ["woocommerce"] * 2 + ["shopify"] * 2
    assert [r["variant_id"] for r in rows] == ["51", "52", "71", "72"]
    assert [r["title"] for r in rows] == ["17″ Pan"] * 2 + ["Hat"] * 2


def test_csv_append_other_platform(tmp_path):
    path = tmp_path / "out.csv"
    write_items([SHOPIFY_PRODUCT], path)
    write_items([WOO_PRODUCT], path, append=True)
    rows = read_csv(path)
    assert [r["platform"] for r in rows] == ["shopify"] * 2 + ["woocommerce"] * 2


def test_csv_image_paths_with_scraped_images(tmp_path):
    path = tmp_path / "out.csv"
    item = WOO_PRODUCT | {"variations": [], "scraped_images": [{"path": "full/a.jpg"}]}
    write_items([item], path)
    (row,) = read_csv(path)
    assert row["image_paths"] == "full/a.jpg"


def test_csv_other_dicts_get_a_column_per_key(tmp_path):
    path = tmp_path / "out.csv"
    write_items([{"title": "a", "image_urls": ["x", "y"], "meta": {"k": 1}}], path)
    (row,) = read_csv(path)
    assert row == {"title": "a", "image_urls": "x,y", "meta": '{"k": 1}'}


def test_csv_fields_pick_columns(tmp_path):
    path = tmp_path / "out.csv"
    write_items([SHOPIFY_PRODUCT], path, fields=["title", "variant_id", "price"])
    assert read_csv(path) == [
        {"title": "Hat", "variant_id": "71", "price": "10.00"},
        {"title": "Hat", "variant_id": "72", "price": "12.50"},
    ]


def test_append_keeps_single_csv_header(tmp_path):
    path = tmp_path / "out.csv"
    write_items(ITEMS[:1], path)
    write_items(ITEMS[:1], path, append=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("platform,")
    assert sum(line.startswith("platform,") for line in lines) == 1
    assert len(read_csv(path)) == 2


def test_append_jsonl(tmp_path):
    path = tmp_path / "out.jsonl"
    write_items(ITEMS, path)
    write_items(ITEMS, path, append=True)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4


def test_append_rejected_for_json(tmp_path):
    with pytest.raises(ValueError, match="append"):
        ItemWriter(tmp_path / "x.json", append=True)


def test_no_items_no_file(tmp_path):
    path = tmp_path / "out.json"
    assert write_items([], path) == 0
    assert not path.exists()


def test_streams_from_generator(tmp_path):
    path = tmp_path / "out.jsonl"
    write_items((i for i in ITEMS), path)
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == ITEMS


def test_format_detection():
    assert format_for("a.JSONL") == "jsonl"
    assert format_for("a.txt", "csv") == "csv"
    with pytest.raises(ValueError):
        format_for("a.txt")
    with pytest.raises(ValueError):
        format_for("a.json", "parquet")


def test_status_file(tmp_path):
    store = StoreResult(
        input_url="a.com",
        url="https://a.com",
        store="a.com",
        platform="shopify",
        items=0,
        status=StoreStatus.forbidden,
        complete=True,
        finish_reason="finished",
    )
    path = tmp_path / "status" / "s.json"
    write_status(
        path,
        [store],
        finish_reason="finished",
        duration=1.23,
        log_file=None,
        skipped_urls=["bad url"],
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["urls"] == [
        {
            "url": "a.com",
            "items": 0,
            "status": "403 Forbidden",
            "code": "forbidden",
            "products_from": None,
        },
        {
            "url": "bad url",
            "items": 0,
            "status": "invalid URL",
            "code": "invalid_url",
            "products_from": None,
        },
    ]
    assert data["duration_seconds"] == 1.2
    assert data["output_error"] is None
    assert data["items_written"] is None


def test_csv_description_keeps_inline_markup_together(tmp_path):
    path = tmp_path / "out.csv"
    body = "<p>See our <a href='x'>guide</a>. <strong>100%</strong> wo<b>ol</b></p><p>Next</p>"
    write_items([{"product": {"id": 1, "body_html": body}}], path)
    (row,) = read_csv(path)
    assert row["description"] == "See our guide. 100% wool Next"


def test_csv_woocommerce_prices_keep_currency_decimals(tmp_path):
    path = tmp_path / "out.csv"
    prices = {"price": "1990", "regular_price": "500", "currency_minor_unit": 2}
    write_items([WOO_PRODUCT | {"prices": prices, "variations": []}], path)
    (row,) = read_csv(path)
    assert (row["price"], row["compare_at_price"], row["min_price"]) == ("19.90", "", "19.90")


def test_csv_woocommerce_options_skip_missing_and_unescape(tmp_path):
    path = tmp_path / "out.csv"
    item = WOO_PRODUCT | {
        "attributes": [
            {"name": "Size &amp; Fit", "terms": [{"name": "S"}]},
            {"name": None, "terms": [{"name": "X"}]},
        ],
        "variations": [
            {"id": 1, "attributes": [{"name": "Size &amp; Fit", "value": "S"}]},
            {"id": 2, "attributes": [{"name": "Size", "value": None}]},
        ],
    }
    write_items([item], path)
    rows = read_csv(path)
    assert rows[0]["options"] == "Size & Fit"
    assert [r["option1"] for r in rows] == ["S", ""]
    assert [r["variant_title"] for r in rows] == ["S", ""]


def test_csv_picked_fields_dont_warn_about_the_rest(tmp_path, caplog):
    write_items([SHOPIFY_PRODUCT], tmp_path / "out.csv", fields=["title", "price"])
    assert "dropping" not in caplog.text


def test_csv_unknown_fields_warn(tmp_path, caplog):
    path = tmp_path / "out.csv"
    write_items([SHOPIFY_PRODUCT], path, fields=["product", "title"])
    assert "left empty: product" in caplog.text
    assert [r["product"] for r in read_csv(path)] == ["", ""]


def test_csv_append_refuses_old_format_file(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("product,url,store,image_urls\n{},u,a.com,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="0.3 or earlier"):
        write_items([SHOPIFY_PRODUCT], path, append=True)
    assert path.read_text(encoding="utf-8") == "product,url,store,image_urls\n{},u,a.com,\n"


def test_csv_keeps_long_cells_whole(tmp_path):
    path = tmp_path / "out.csv"
    write_items([{"product": {"id": 1, "body_html": "<p>" + "x" * 40_000 + "</p>"}}], path)
    write_items([{"meta": {"k": "y" * 40_000}}], tmp_path / "other.csv")
    csv.field_size_limit(10**6)
    assert read_csv(path)[0]["description"] == "x" * 40_000
    assert json.loads(read_csv(tmp_path / "other.csv")[0]["meta"]) == {"k": "y" * 40_000}


def test_csv_variation_attributes_show_term_names(tmp_path):
    path = tmp_path / "out.csv"
    item = WOO_PRODUCT | {
        "attributes": [{"name": "Color", "terms": [{"name": "Light Blue", "slug": "light-blue"}]}],
        "variations": [{"id": 1, "attributes": [{"name": "Color", "value": "light-blue"}]}],
    }
    write_items([item], path)
    (row,) = read_csv(path)
    assert (row["option1"], row["variant_title"]) == ("Light Blue", "Light Blue")


def test_csv_append_refuses_old_format_file_with_fields(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("product,url,store,image_urls\n", encoding="utf-8")
    with pytest.raises(ValueError, match="0.3 or earlier"):
        write_items([SHOPIFY_PRODUCT], path, append=True, fields=["id", "title"])


@pytest.mark.parametrize("fmt", ["jsonl", "json", "xml"])
def test_native_is_the_default_and_explicit_native_matches(tmp_path, fmt):
    path = tmp_path / f"out.{fmt}"
    write_items(ITEMS, path, schema="native")
    assert path.read_bytes() == (FIXTURES / f"scrapy.{fmt}").read_bytes()


def test_csv_is_always_unified(tmp_path):
    with pytest.raises(ValueError, match="CSV is always unified"):
        write_items([SHOPIFY_PRODUCT], tmp_path / "out.csv", schema="native")
    write_items(ITEMS, tmp_path / "out.csv", schema="unified")
    assert (tmp_path / "out.csv").read_bytes() == (FIXTURES / "expected.csv").read_bytes()


def test_unknown_schema(tmp_path):
    with pytest.raises(ValueError, match="unknown schema"):
        write_items([SHOPIFY_PRODUCT], tmp_path / "out.jsonl", schema="nested")


def test_unified_jsonl_writes_a_record_per_row(tmp_path):
    path = tmp_path / "out.jsonl"
    assert write_items([SHOPIFY_PRODUCT, WOO_PRODUCT], path, schema="unified") == 2
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["platform"], r["variant_id"]) for r in rows] == [
        ("shopify", 71),
        ("shopify", 72),
        ("woocommerce", 51),
        ("woocommerce", 52),
    ]
    # Fixed keys in COLUMNS order, without image_paths when no images were downloaded
    assert list(rows[0]) == list(rows[2]) == [c for c in COLUMNS if c != "image_paths"]
    assert (rows[0]["available"], rows[0]["compare_at_price"]) == (True, None)
    # Decimal prices become strings, like Shopify's
    assert (rows[2]["price"], rows[2]["compare_at_price"]) == ("19.99", "24.99")
    assert (rows[0]["min_price"], rows[0]["max_price"]) == ("10.00", "12.50")


def test_unified_json_and_fields(tmp_path):
    path = tmp_path / "out.json"
    write_items([SHOPIFY_PRODUCT], path, schema="unified", fields=["title", "price"])
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"title": "Hat", "price": "10.00"},
        {"title": "Hat", "price": "12.50"},
    ]


def test_unified_xml_has_an_item_per_row(tmp_path):
    path = tmp_path / "out.xml"
    write_items([SHOPIFY_PRODUCT], path, schema="unified", fields=["platform", "available", "sku"])
    assert path.read_text(encoding="utf-8") == (
        '<?xml version="1.0" encoding="utf8"?>\n<items>\n'
        "<item><platform>shopify</platform><available>true</available><sku></sku></item>\n"
        "<item><platform>shopify</platform><available>false</available><sku></sku></item>\n"
        "</items>"
    )


def test_unified_jsonl_append(tmp_path):
    path = tmp_path / "out.jsonl"
    write_items([SHOPIFY_PRODUCT], path, schema="unified")
    write_items([WOO_PRODUCT], path, schema="unified", append=True)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["platform"] for r in rows] == ["shopify"] * 2 + ["woocommerce"] * 2


def test_csv_woocommerce_any_value_keeps_option_columns_aligned(tmp_path):
    path = tmp_path / "out.csv"
    item = WOO_PRODUCT | {
        "attributes": [
            {"name": "Color", "has_variations": True, "terms": [{"name": "Red"}]},
            {"name": "Size", "has_variations": True, "terms": [{"name": "Small", "slug": "s"}]},
        ],
        "variations": [
            {
                "id": 1,
                "attributes": [{"name": "Color", "value": ""}, {"name": "Size", "value": "s"}],
            }
        ],
    }
    write_items([item], path)
    (row,) = read_csv(path)
    assert (row["options"], row["option1"], row["option2"]) == ("Color, Size", "", "Small")
    assert row["variant_title"] == "Small"


def test_csv_woocommerce_nan_price_doesnt_crash(tmp_path):
    path = tmp_path / "out.csv"
    prices = {"price": "NaN", "regular_price": "2499", "currency_minor_unit": 2}
    write_items([WOO_PRODUCT | {"prices": prices, "variations": []}], path)
    (row,) = read_csv(path)
    assert (row["price"], row["compare_at_price"]) == ("NaN", "")


def test_csv_repeated_fields_stay_aligned(tmp_path):
    path = tmp_path / "out.csv"
    write_items([SHOPIFY_PRODUCT], path, fields=["id", "id", "title"])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["id,id,title", "7,7,Hat"]


def test_non_json_values_still_raise(tmp_path):
    with pytest.raises(TypeError):
        write_items([{"a": {1, 2}}], tmp_path / "out.jsonl")


def test_fields_with_native_schema_warn(tmp_path, caplog):
    write_items([SHOPIFY_PRODUCT], tmp_path / "out.jsonl", fields=["title"])
    assert "only applies to the unified schema" in caplog.text


@pytest.mark.parametrize(("first", "then"), [("native", "unified"), ("unified", "native")])
def test_jsonl_append_refuses_the_other_schema(tmp_path, first, then):
    path = tmp_path / "out.jsonl"
    write_items([SHOPIFY_PRODUCT], path, schema=first)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=f"holds {first} items"):
        write_items([WOO_PRODUCT], path, schema=then, append=True)
    assert path.read_bytes() == before


class _FailingFile:
    """Wraps a writer's file; once armed, a write stores half its bytes and raises."""

    def __init__(self, file):
        self.file = file
        self.armed = False

    def write(self, data):
        if self.armed:
            self.file.write(bytes(data[: len(data) // 2]))
            raise OSError("disk full")
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


@pytest.mark.parametrize(
    ("fmt", "schema"),
    [("jsonl", None), ("json", None), ("csv", None), ("xml", None)]
    + [(f, "unified") for f in ("jsonl", "json", "xml")],
)
def test_failed_write_leaves_whole_items_only(tmp_path, fmt, schema):
    path = tmp_path / f"out.{fmt}"
    with ItemWriter(path, schema=schema) as writer:
        writer.write(ITEMS[0])
        writer._file = failing = _FailingFile(writer._file)
        failing.armed = True
        with pytest.raises(OSError, match="disk full"):
            writer.write(ITEMS[1])
        failing.armed = False
        assert writer.count == 1
    # The file is what writing only the first item gives
    write_items(ITEMS[:1], tmp_path / f"expected.{fmt}", schema=schema)
    assert path.read_bytes() == (tmp_path / f"expected.{fmt}").read_bytes()


def test_lone_surrogates_become_replacement_characters(tmp_path):
    # json.loads accepts a broken escape, which UTF-8 can't encode
    bad = json.loads('{"title": "bad \\ud83d", "ok": "\\ud83d\\ude00"}')
    for fmt in ("jsonl", "json", "csv"):
        path = tmp_path / f"out.{fmt}"
        write_items([bad], path)
        text = path.read_text(encoding="utf-8")
        assert "bad \ufffd" in text and "\U0001f600" in text, fmt


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_writes_to_a_pipe(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    received = []
    reader = threading.Thread(target=lambda: received.append(fifo.read_bytes()))
    reader.start()
    write_items(ITEMS, fifo, format="jsonl")
    reader.join(timeout=10)
    assert received == [(FIXTURES / "scrapy.jsonl").read_bytes()]


def test_failed_header_write_leaves_no_file(tmp_path, monkeypatch):
    path = tmp_path / "out.csv"
    original = ItemWriter._write_all
    calls = []

    def fail_first(self, data):
        calls.append(data)
        if len(calls) == 1:
            raise OSError("disk full")
        original(self, data)

    monkeypatch.setattr(ItemWriter, "_write_all", fail_first)
    with ItemWriter(path) as writer:
        with pytest.raises(OSError):
            writer.write(SHOPIFY_PRODUCT)
        assert not path.exists()
        # The next write starts the file again, header first
        writer.write(SHOPIFY_PRODUCT)
    assert path.read_text(encoding="utf-8").startswith("platform,kind,")


def _mg_price(final, regular=None, currency="EUR"):
    regular = final if regular is None else regular
    money = {
        "regular_price": {"value": regular, "currency": currency},
        "final_price": {"value": final, "currency": currency},
    }
    return {"minimum_price": money, "maximum_price": money}


def _mg_variant(uid, sku, color, size, price, stock="IN_STOCK"):
    return {
        # Listed out of configurable_options order on purpose
        "attributes": [
            {"code": "size", "label": size, "value_index": 2},
            {"code": "color", "label": color, "value_index": 1},
        ],
        "product": {
            "uid": uid,
            "sku": sku,
            "stock_status": stock,
            "image": {"url": f"https://m.test/{sku}.jpg"},
            "price_range": {"minimum_price": _mg_price(price, 30)["minimum_price"]},
        },
    }


MAGENTO_CONFIGURABLE = {
    "__typename": "ConfigurableProduct",
    "uid": "MTA=",
    "sku": "TEE",
    "name": "Tee",
    "url_key": "tee",
    "url_suffix": ".html",
    "stock_status": "IN_STOCK",
    "description": {"html": "<p>Soft</p><p>cotton</p>"},
    "short_description": {"html": "Short"},
    "categories": [{"name": "Men"}, {"name": "Tops"}],
    "price_range": {
        "minimum_price": _mg_price(20, 30)["minimum_price"],
        "maximum_price": _mg_price(30)["maximum_price"],
    },
    "media_gallery": [
        {"url": "https://m.test/2.jpg", "position": 2, "disabled": False},
        {"url": "https://m.test/0.jpg", "position": 0, "disabled": True},
        {"url": "https://m.test/1.jpg", "position": 1, "disabled": False},
    ],
    "configurable_options": [
        {"attribute_code": "color", "label": "Color", "values": []},
        {"attribute_code": "size", "label": "Size", "values": []},
    ],
    "variants": [
        _mg_variant("MTE=", "TEE-R-S", "Red", "S", 20),
        _mg_variant("MTI=", "TEE-B-L", "Blue", "L", 30, stock="OUT_OF_STOCK"),
    ],
    "store": "m.test",
    "platform": "magento",
    "html_url": "https://m.test/tee.html",
    "image_urls": [],
}

MAGENTO_SIMPLE = {
    "__typename": "SimpleProduct",
    "uid": "OTk=",
    "sku": "MUG",
    "name": "Mug",
    "url_key": "mug",
    "stock_status": "OUT_OF_STOCK",
    "price_range": _mg_price(8, 10),
    "media_gallery": [],
    "store": "m.test",
    "platform": "magento",
    "html_url": "https://m.test/mug",
    "image_urls": [],
}


def test_csv_magento_configurable_rows(tmp_path):
    path = tmp_path / "out.csv"
    write_items([MAGENTO_CONFIGURABLE], path)
    rows = read_csv(path)
    assert [r["variant_id"] for r in rows] == ["MTE=", "MTI="]
    assert [r["sku"] for r in rows] == ["TEE-R-S", "TEE-B-L"]
    assert [(r["variant_title"], r["option1"], r["option2"]) for r in rows] == [
        ("Red / S", "Red", "S"),
        ("Blue / L", "Blue", "L"),
    ]
    assert [(r["price"], r["compare_at_price"]) for r in rows] == [("20", "30"), ("30", "")]
    assert [r["available"] for r in rows] == ["true", "false"]
    assert rows[0]["variant_image_url"] == "https://m.test/TEE-R-S.jpg"
    row = rows[0]
    assert (row["platform"], row["kind"], row["store"]) == ("magento", "product", "m.test")
    assert (row["id"], row["handle"], row["title"]) == ("MTA=", "tee", "Tee")
    assert (row["categories"], row["options"]) == ("Men, Tops", "Color, Size")
    assert (row["description"], row["short_description"]) == ("Soft cotton", "Short")
    assert row["html_url"] == "https://m.test/tee.html"
    assert (row["image_url"], row["image_count"]) == ("https://m.test/1.jpg", "2")
    assert (row["currency"], row["min_price"], row["max_price"]) == ("EUR", "20", "30")
    assert row["variant_count"] == "2"
    assert (row["vendor"], row["url"], row["source"]) == ("", "", "")


def test_csv_magento_variant_without_price_keeps_product_price(tmp_path):
    variant = _mg_variant("MTE=", "TEE-R-S", "Red", "S", 20)
    del variant["product"]["price_range"]
    path = tmp_path / "out.csv"
    write_items([MAGENTO_CONFIGURABLE | {"variants": [variant]}], path)
    (row,) = read_csv(path)
    assert (row["sku"], row["price"], row["compare_at_price"]) == ("TEE-R-S", "20", "30")


def test_csv_magento_option_without_label_keeps_its_place(tmp_path):
    options = [
        {"attribute_code": "color", "label": None},
        {"attribute_code": "size", "label": "Size"},
    ]
    path = tmp_path / "out.csv"
    write_items([MAGENTO_CONFIGURABLE | {"configurable_options": options}], path)
    row = read_csv(path)[0]
    assert (row["options"], row["option1"], row["option2"]) == ("color, Size", "Red", "S")


def test_csv_magento_simple_product_row(tmp_path):
    path = tmp_path / "out.csv"
    write_items([MAGENTO_SIMPLE], path)
    (row,) = read_csv(path)
    assert (row["sku"], row["price"], row["compare_at_price"]) == ("MUG", "8", "10")
    assert (row["available"], row["variant_count"], row["variant_id"]) == ("false", "0", "")
    assert (row["image_url"], row["image_count"]) == ("", "0")


def test_csv_holds_every_platform(tmp_path):
    path = tmp_path / "out.csv"
    write_items([WOO_PRODUCT, SHOPIFY_PRODUCT, MAGENTO_SIMPLE], path)
    rows = read_csv(path)
    assert [r["platform"] for r in rows] == ["woocommerce"] * 2 + ["shopify"] * 2 + ["magento"]


def test_item_platform_names_magento():
    from shopify_spy._flatten import item_platform

    assert item_platform(MAGENTO_SIMPLE) == "magento"
    assert item_platform(WOO_PRODUCT) == "woocommerce"
    assert item_platform(SHOPIFY_PRODUCT) == "shopify"
    assert item_platform({"platform": "magento", "name": "x"}) is None


def _bc_money(value, currency="USD"):
    return {"value": value, "currencyCode": currency}


def _bc_option(entity_id, name, value):
    values = {"edges": [{"node": {"entityId": entity_id * 10, "label": value}}]}
    return {"node": {"entityId": entity_id, "displayName": name, "values": values}}


def _bc_variant(entity_id, sku, color, size, price, base=30, in_stock=True):
    return {
        "node": {
            "entityId": entity_id,
            "sku": sku,
            "isPurchasable": in_stock,
            "prices": {"price": _bc_money(price), "basePrice": _bc_money(base)},
            "inventory": {"isInStock": in_stock},
            "defaultImage": {"urlOriginal": f"https://bc.test/{sku}.jpg"},
            # Listed out of productOptions order on purpose
            "options": {"edges": [_bc_option(2, "Size", size), _bc_option(1, "Color", color)]},
        }
    }


BIGCOMMERCE_PRODUCT = {
    "id": "UHJvZHVjdDo3Nw==",
    "entityId": 77,
    "sku": "TEE",
    "name": "Tee",
    "path": "/tee/",
    "description": "<p>Soft</p><p>cotton</p>",
    "createdAt": {"utc": "2026-01-02T03:04:05Z"},
    "brand": {"name": "Acme"},
    "categories": {"edges": [{"node": {"name": "Men"}}, {"node": {"name": "Tops"}}]},
    "prices": {
        "price": _bc_money(20),
        "basePrice": _bc_money(30),
        "priceRange": {"min": _bc_money(20), "max": _bc_money(30)},
    },
    "inventory": {"isInStock": True},
    "reviewSummary": {"numberOfReviews": 3, "summationOfRatings": 13},
    "images": {
        "edges": [
            {"node": {"urlOriginal": "https://bc.test/1.jpg", "isDefault": False}},
            {"node": {"urlOriginal": "https://bc.test/2.jpg", "isDefault": True}},
        ]
    },
    "productOptions": {
        "edges": [
            {"node": {"entityId": 1, "displayName": "Color", "isVariantOption": True}},
            {"node": {"entityId": 3, "displayName": "Extended Warranty", "isVariantOption": False}},
            {"node": {"entityId": 2, "displayName": "Size", "isVariantOption": True}},
        ]
    },
    "variants": {
        "pageInfo": {"hasNextPage": False},
        "edges": [
            _bc_variant(101, "TEE-R-S", "Red", "S", 20),
            _bc_variant(102, "TEE-B-L", "Blue", "L", 30, in_stock=False),
        ],
    },
    "store": "bc.test",
    "platform": "bigcommerce",
    "html_url": "https://bc.test/tee/",
    "image_urls": [],
}

BIGCOMMERCE_SIMPLE = {
    "entityId": 9,
    "sku": "MUG",
    "name": "Mug",
    "path": "/mug/",
    "prices": {
        "price": _bc_money(8),
        "basePrice": _bc_money(8),
        "retailPrice": _bc_money(10),
    },
    "inventory": {"isInStock": False},
    "reviewSummary": {"numberOfReviews": 0, "summationOfRatings": 0},
    "images": {"edges": []},
    "variants": {"edges": []},
    "store": "bc.test",
    "platform": "bigcommerce",
    "html_url": "https://bc.test/mug/",
    "image_urls": [],
}


def test_csv_bigcommerce_variant_rows(tmp_path):
    path = tmp_path / "out.csv"
    write_items([BIGCOMMERCE_PRODUCT], path)
    rows = read_csv(path)
    assert [r["variant_id"] for r in rows] == ["101", "102"]
    assert [r["sku"] for r in rows] == ["TEE-R-S", "TEE-B-L"]
    assert [(r["variant_title"], r["option1"], r["option2"]) for r in rows] == [
        ("Red / S", "Red", "S"),
        ("Blue / L", "Blue", "L"),
    ]
    assert [(r["price"], r["compare_at_price"]) for r in rows] == [("20", "30"), ("30", "")]
    assert [(r["available"], r["is_purchasable"]) for r in rows] == [
        ("true", "true"),
        ("false", "false"),
    ]
    assert rows[0]["variant_image_url"] == "https://bc.test/TEE-R-S.jpg"
    row = rows[0]
    assert (row["platform"], row["kind"], row["store"]) == ("bigcommerce", "product", "bc.test")
    assert (row["id"], row["handle"], row["title"], row["vendor"]) == ("77", "tee", "Tee", "Acme")
    assert (row["categories"], row["options"]) == ("Men, Tops", "Color, Size")
    assert (row["created_at"], row["description"]) == ("2026-01-02T03:04:05Z", "Soft cotton")
    assert row["html_url"] == "https://bc.test/tee/"
    assert (row["image_url"], row["image_count"]) == ("https://bc.test/2.jpg", "2")
    assert (row["currency"], row["min_price"], row["max_price"]) == ("USD", "20", "30")
    assert (row["average_rating"], row["review_count"]) == ("4.33", "3")
    assert row["variant_count"] == "2"
    assert (row["option3"], row["url"], row["source"]) == ("", "", "")


def test_csv_bigcommerce_gtin_and_mpn(tmp_path):
    variants = [
        _bc_variant(101, "TEE-R-S", "Red", "S", 20),
        _bc_variant(102, "TEE-B-L", "Blue", "L", 30),
        _bc_variant(103, "TEE-B-S", "Blue", "S", 30),
    ]
    variants[0]["node"] |= {"gtin": "4006381333931", "mpn": "M-RS"}
    variants[1]["node"] |= {"gtin": "", "upc": "012345678905"}
    product = BIGCOMMERCE_PRODUCT | {
        "gtin": "10012345678902",
        "mpn": "M",
        "variants": {"edges": variants},
    }
    path = tmp_path / "out.csv"
    write_items([product], path)
    assert [(r["gtin"], r["mpn"]) for r in read_csv(path)] == [
        ("4006381333931", "M-RS"),
        ("012345678905", "M"),
        ("10012345678902", "M"),
    ]
    write_items([BIGCOMMERCE_SIMPLE | {"upc": "012345678905", "mpn": " "}], path)
    (row,) = read_csv(path)
    assert (row["gtin"], row["mpn"]) == ("012345678905", "")


def test_csv_bigcommerce_product_without_variants(tmp_path):
    path = tmp_path / "out.csv"
    write_items([BIGCOMMERCE_SIMPLE], path)
    (row,) = read_csv(path)
    # basePrice equals the price, so the retail price (MSRP) is the "was" price
    assert (row["sku"], row["price"], row["compare_at_price"]) == ("MUG", "8", "10")
    assert (row["available"], row["variant_count"], row["variant_id"]) == ("false", "0", "")
    assert (row["average_rating"], row["review_count"]) == ("", "0")
    assert (row["image_url"], row["image_count"], row["handle"]) == ("", "0", "mug")


def test_bigcommerce_variant_without_a_price_takes_the_products():
    from shopify_spy._flatten import flatten

    variant = _bc_variant(101, "TEE-R-S", "Red", "S", 20)
    variant["node"]["prices"] = None
    item = {**BIGCOMMERCE_PRODUCT, "variants": {"edges": [variant]}}
    (row,) = flatten(item)
    assert (row["price"], row["compare_at_price"]) == (Decimal("20"), Decimal("30"))


def test_bigcommerce_variant_price_keeps_its_own_was_price():
    from shopify_spy._flatten import flatten

    variant = _bc_variant(101, "TEE-R-S", "Red", "S", 20)
    variant["node"]["prices"] = {"price": _bc_money(20), "basePrice": None}
    item = {**BIGCOMMERCE_PRODUCT, "variants": {"edges": [variant]}}
    (row,) = flatten(item)
    # The product's basePrice of 30 is another variant's price, not this one's "was"
    assert (row["price"], row["compare_at_price"]) == (Decimal("20"), None)


def test_csv_holds_all_four_platforms(tmp_path):
    path = tmp_path / "out.csv"
    write_items([WOO_PRODUCT, SHOPIFY_PRODUCT, MAGENTO_SIMPLE, BIGCOMMERCE_SIMPLE], path)
    text = path.read_text(encoding="utf-8")
    assert text.count("platform,kind,") == 1
    rows = read_csv(path)
    assert [r["platform"] for r in rows] == (
        ["woocommerce"] * 2 + ["shopify"] * 2 + ["magento", "bigcommerce"]
    )


def test_item_platform_names_bigcommerce():
    from shopify_spy._flatten import item_platform

    assert item_platform(BIGCOMMERCE_SIMPLE) == "bigcommerce"
    assert item_platform({"platform": "bigcommerce", "name": "x"}) is None


JSONLD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Felt Hat",
    "sku": "HAT-1",
    "brand": {"@type": "Brand", "name": "Acme"},
    "category": "Apparel > Hats",
    "description": "<p>Warm <b>felt</b></p>",
    "image": ["https://j.test/hat.jpg", {"@type": "ImageObject", "url": "https://j.test/2.jpg"}],
    "offers": {
        "@type": "Offer",
        "price": "20.00",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
        "priceSpecification": {"priceType": "https://schema.org/ListPrice", "price": "30.00"},
    },
    "store": "j.test",
    "platform": "jsonld",
    "html_url": "https://j.test/products/felt%20hat",
    "image_urls": [],
}


def _jl_variant(sku, size, price, availability="InStock"):
    return {
        "@type": "Product",
        "sku": sku,
        "name": f"Tee {size}",
        "size": size,
        "image": f"https://j.test/{sku}.jpg",
        "offers": {"@type": "Offer", "price": price, "availability": availability},
    }


JSONLD_GROUP = {
    "@context": "https://schema.org",
    "@type": "ProductGroup",
    "name": "Tee",
    "productGroupID": "TEE",
    "variesBy": ["https://schema.org/size"],
    "hasVariant": [
        _jl_variant("TEE-S", "S", "10", "OutOfStock"),
        _jl_variant("TEE-L", "L", "12"),
    ],
    "store": "j.test",
    "platform": "jsonld",
    "html_url": "https://j.test/tee/",
    "image_urls": [],
}


def test_csv_jsonld_product_row(tmp_path):
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT], path)
    (row,) = read_csv(path)
    assert (row["platform"], row["kind"], row["store"]) == ("jsonld", "product", "j.test")
    assert (row["id"], row["handle"], row["title"]) == ("HAT-1", "felt hat", "Felt Hat")
    assert (row["sku"], row["vendor"], row["categories"]) == ("HAT-1", "Acme", "Apparel > Hats")
    assert row["description"] == "Warm felt"
    assert row["html_url"] == "https://j.test/products/felt%20hat"
    assert (row["image_url"], row["image_count"]) == ("https://j.test/hat.jpg", "2")
    assert (row["currency"], row["min_price"], row["max_price"]) == ("USD", "20.00", "20.00")
    assert (row["price"], row["compare_at_price"], row["available"]) == ("20.00", "30.00", "true")
    assert (row["variant_count"], row["variant_id"], row["options"]) == ("0", "", "")


def test_csv_jsonld_compare_at_price_only_above_price(tmp_path):
    offers = JSONLD_PRODUCT["offers"] | {
        "priceSpecification": {"priceType": "ListPrice", "price": "20"}
    }
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": offers}], path)
    (row,) = read_csv(path)
    assert (row["price"], row["compare_at_price"]) == ("20.00", "")


def test_csv_jsonld_product_group_rows(tmp_path):
    path = tmp_path / "out.csv"
    write_items([JSONLD_GROUP], path)
    rows = read_csv(path)
    assert [(r["variant_id"], r["sku"]) for r in rows] == [("TEE-S", "TEE-S"), ("TEE-L", "TEE-L")]
    assert [(r["options"], r["option1"], r["variant_title"]) for r in rows] == [
        ("size", "S", "S"),
        ("size", "L", "L"),
    ]
    assert [(r["price"], r["available"]) for r in rows] == [("10", "false"), ("12", "true")]
    assert rows[0]["variant_image_url"] == "https://j.test/TEE-S.jpg"
    row = rows[0]
    assert (row["id"], row["handle"], row["title"]) == ("TEE", "tee", "Tee")
    assert (row["min_price"], row["max_price"], row["variant_count"]) == ("10", "12", "2")


def test_csv_jsonld_relative_images_resolved_against_the_page(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10") | {"image": "//cdn.j.test/s.jpg"}
    path = tmp_path / "out.csv"
    write_items([JSONLD_GROUP | {"image": "/media/tee.jpg", "hasVariant": [variant]}], path)
    (row,) = read_csv(path)
    assert row["image_url"] == "https://j.test/media/tee.jpg"
    assert row["variant_image_url"] == "https://cdn.j.test/s.jpg"


def test_csv_jsonld_variant_without_size_is_titled_by_name(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10")
    del variant["size"]
    path = tmp_path / "out.csv"
    write_items([JSONLD_GROUP | {"hasVariant": [variant]}], path)
    (row,) = read_csv(path)
    assert (row["option1"], row["variant_title"]) == ("", "Tee S")


def test_csv_jsonld_variant_value_from_a_dict(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10") | {"color": {"@type": "Color", "name": "Red"}}
    path = tmp_path / "out.csv"
    group = JSONLD_GROUP | {
        "variesBy": ["https://schema.org/color", "size"],
        "hasVariant": [variant],
    }
    write_items([group], path)
    (row,) = read_csv(path)
    assert (row["options"], row["option1"], row["option2"]) == ("color, size", "Red", "S")
    assert row["variant_title"] == "Red / S"


def test_csv_jsonld_prefixed_varies_by(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10") | {"color": "Red"}
    path = tmp_path / "out.csv"
    write_items([JSONLD_GROUP | {"variesBy": "schema:color", "hasVariant": [variant]}], path)
    (row,) = read_csv(path)
    assert (row["options"], row["option1"], row["variant_title"]) == ("color", "Red", "Red")


def test_csv_jsonld_variant_without_price_keeps_product_price(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10")
    del variant["offers"]
    path = tmp_path / "out.csv"
    write_items(
        [JSONLD_GROUP | {"offers": JSONLD_PRODUCT["offers"], "hasVariant": [variant]}], path
    )
    (row,) = read_csv(path)
    assert (row["sku"], row["price"], row["compare_at_price"]) == ("TEE-S", "20.00", "30.00")


def test_csv_jsonld_stub_variants_are_not_rows(tmp_path):
    # Allbirds lists the product's other colourways as bare variants
    stubs = [{"@type": "Product", "url": "https://j.test/tee-blue?size=s"}, {"@type": "Product"}]
    group = JSONLD_GROUP | {"hasVariant": [*JSONLD_GROUP["hasVariant"], *stubs]}
    path = tmp_path / "out.csv"
    write_items([group], path)
    rows = read_csv(path)
    assert [r["sku"] for r in rows] == ["TEE-S", "TEE-L"]
    assert rows[0]["variant_count"] == "2"


def test_csv_jsonld_only_stub_variants_give_one_product_row(tmp_path):
    stub = {"@type": "Product", "url": "https://j.test/tee-blue"}
    path = tmp_path / "out.csv"
    write_items([JSONLD_GROUP | {"hasVariant": [stub], "offers": JSONLD_PRODUCT["offers"]}], path)
    (row,) = read_csv(path)
    assert (row["variant_count"], row["variant_id"], row["price"]) == ("0", "", "20.00")


def test_csv_jsonld_offers_list_rows(tmp_path):
    offers = [
        {
            "@type": "Offer",
            "sku": "HAT-S",
            "name": "Small",
            "price": "18",
            "availability": "InStock",
        },
        {
            "@type": "Offer",
            "sku": "HAT-L",
            "name": "Large",
            "price": "22",
            "availability": "SoldOut",
        },
    ]
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": offers}], path)
    rows = read_csv(path)
    assert [(r["variant_id"], r["variant_title"], r["sku"]) for r in rows] == [
        ("HAT-S", "Small", "HAT-S"),
        ("HAT-L", "Large", "HAT-L"),
    ]
    assert [(r["price"], r["available"]) for r in rows] == [("18", "true"), ("22", "false")]
    assert (rows[0]["min_price"], rows[0]["max_price"], rows[0]["variant_count"]) == (
        "18",
        "22",
        "2",
    )


def test_csv_jsonld_single_offer_in_a_list_gives_one_row(tmp_path):
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": [JSONLD_PRODUCT["offers"]]}], path)
    (row,) = read_csv(path)
    assert (row["sku"], row["price"], row["variant_id"]) == ("HAT-1", "20.00", "")


def test_csv_jsonld_aggregate_offer_row(tmp_path):
    offers = {"@type": "AggregateOffer", "lowPrice": "5", "highPrice": "9", "priceCurrency": "GBP"}
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": offers}], path)
    (row,) = read_csv(path)
    assert (row["currency"], row["min_price"], row["max_price"]) == ("GBP", "5", "9")
    assert (row["price"], row["compare_at_price"], row["available"]) == ("5", "", "")


@pytest.mark.parametrize(
    ("brand", "category", "vendor", "categories"),
    [
        ("Acme", ["Hats", "Winter"], "Acme", "Hats, Winter"),
        (["Acme", {"name": "Beta"}], [{"name": "Hats"}, "Winter"], "Acme, Beta", "Hats, Winter"),
        ({"@type": "Brand"}, None, "", ""),
    ],
)
def test_csv_jsonld_brand_and_category_shapes(tmp_path, brand, category, vendor, categories):
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"brand": brand, "category": category}], path)
    (row,) = read_csv(path)
    assert (row["vendor"], row["categories"]) == (vendor, categories)


def test_csv_holds_jsonld_beside_other_platforms(tmp_path):
    path = tmp_path / "out.csv"
    write_items([MAGENTO_SIMPLE, JSONLD_GROUP, WOO_PRODUCT, JSONLD_PRODUCT], path)
    rows = read_csv(path)
    assert [r["platform"] for r in rows] == ["magento"] + ["jsonld"] * 2 + ["woocommerce"] * 2 + [
        "jsonld"
    ]


def test_item_platform_names_jsonld():
    from shopify_spy._flatten import item_platform

    assert item_platform(JSONLD_PRODUCT) == "jsonld"
    assert item_platform(JSONLD_GROUP) == "jsonld"
    assert item_platform({"platform": "jsonld", "name": "x"}) is None


def test_csv_jsonld_text_unescaped_until_stable(tmp_path):
    variant = _jl_variant("TEE-S", "S", "10") | {"name": "Tee &amp;amp; Co\n S"}
    del variant["size"]
    item = JSONLD_GROUP | {
        "name": "Tee &amp;amp;\n  Co",
        "description": "Soft&amp;nbsp;cotton &amp;lt;b&amp;gt;tee&amp;lt;/b&amp;gt;",
        "brand": "Acme &amp;amp; Sons",
        "hasVariant": [variant],
    }
    path = tmp_path / "out.csv"
    write_items([item], path)
    (row,) = read_csv(path)
    assert (row["title"], row["vendor"]) == ("Tee & Co", "Acme & Sons")
    assert row["description"] == "Soft cotton tee"
    assert row["variant_title"] == "Tee & Co S"


def test_csv_jsonld_formatted_prices_and_nested_availability(tmp_path):
    offers = {
        "@type": "AggregateOffer",
        "priceCurrency": "EUR",
        "offers": [
            {"@type": "Offer", "price": "1.234,56", "availability": "schema:OutOfStock"},
            {"@type": "Offer", "price": "19,99 €", "availability": "schema:InStock"},
        ],
    }
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": offers}], path)
    (row,) = read_csv(path)
    assert (row["min_price"], row["max_price"], row["price"]) == ("19.99", "1234.56", "19.99")
    assert (row["currency"], row["available"]) == ("EUR", "true")


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        ({"ratingValue": "4.6", "reviewCount": "89"}, ("4.6", "89")),
        ({"ratingValue": 4, "ratingCount": 7}, ("4", "7")),
        # Magento's Luma gives a percentage, with its scale
        ({"ratingValue": "60", "bestRating": "100", "reviewCount": "3"}, ("3.00", "3")),
        ({"ratingValue": "8", "bestRating": "10"}, ("4.00", "")),
        # Not ratings: 0 with no reviews, a value past the scale, no value
        ({"ratingValue": "0", "reviewCount": "0"}, ("", "0")),
        ({"ratingValue": "60", "reviewCount": "3"}, ("", "3")),
        ({"reviewCount": "x"}, ("", "")),
        ("4.5", ("", "")),
    ],
)
def test_csv_jsonld_rating(tmp_path, rating, expected):
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"aggregateRating": rating}], path)
    (row,) = read_csv(path)
    assert (row["average_rating"], row["review_count"]) == expected


def test_csv_jsonld_gtin_and_mpn(tmp_path):
    variants = [
        _jl_variant("TEE-S", "S", "10") | {"gtin12": "012345678905", "mpn": "M-S"},
        _jl_variant("TEE-M", "M", "10") | {"offers": {"price": "10", "gtin": "4006381333931"}},
        _jl_variant("TEE-L", "L", "12"),
    ]
    group = JSONLD_GROUP | {"gtin14": "10012345678902", "mpn": "M", "hasVariant": variants}
    path = tmp_path / "out.csv"
    write_items([group], path)
    rows = read_csv(path)
    assert [(r["gtin"], r["mpn"]) for r in rows] == [
        ("012345678905", "M-S"),
        ("4006381333931", "M"),
        ("10012345678902", "M"),
    ]


def test_csv_jsonld_gtin_per_offer_and_from_the_offer(tmp_path):
    offers = [
        {"@type": "Offer", "sku": "A", "price": "1", "gtin13": "4006381333931"},
        {"@type": "Offer", "sku": "B", "price": "2"},
    ]
    path = tmp_path / "out.csv"
    write_items([JSONLD_PRODUCT | {"offers": offers}], path)
    # B's row doesn't take A's barcode
    assert [r["gtin"] for r in read_csv(path)] == ["4006381333931", ""]
    single = JSONLD_PRODUCT | {"offers": JSONLD_PRODUCT["offers"] | {"gtin8": 12345670}}
    write_items([single], path)
    (row,) = read_csv(path)
    assert (row["gtin"], row["mpn"]) == ("12345670", "")


def test_csv_other_platforms_have_no_gtin_or_mpn(tmp_path):
    path = tmp_path / "out.csv"
    write_items([SHOPIFY_PRODUCT, WOO_PRODUCT, MAGENTO_SIMPLE], path)
    assert {(r["gtin"], r["mpn"]) for r in read_csv(path)} == {("", "")}
