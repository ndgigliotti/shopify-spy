"""Writers match Scrapy's feed exporters byte for byte (JSONL/JSON/XML fixtures were
captured from Scrapy 2.14 before it was removed; CSV differs on purpose, D2)."""

import csv
import json
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
    assert row["variant_id"] == ""


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
    assert (row["price"], row["compare_at_price"], row["sale_price"]) == (
        "19.99",
        "24.99",
        "19.99",
    )
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
        {"url": "a.com", "items": 0, "status": "403 Forbidden", "code": "forbidden"},
        {"url": "bad url", "items": 0, "status": "invalid URL", "code": "invalid_url"},
    ]
    assert data["duration_seconds"] == 1.2


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
    assert (row["price"], row["sale_price"], row["min_price"]) == ("19.90", "", "19.90")


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
