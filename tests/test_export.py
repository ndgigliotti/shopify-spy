"""Writers match Scrapy's feed exporters byte for byte (JSONL/JSON/XML fixtures were
captured from Scrapy 2.14 before it was removed; CSV differs on purpose, D2)."""

import csv
import json
from pathlib import Path

import pytest

from shopify_spy._export import ItemWriter, format_for, write_items, write_status
from shopify_spy._results import StoreResult, StoreStatus

FIXTURES = Path(__file__).parent / "fixtures" / "exporters"
ITEMS = json.loads((FIXTURES / "items.json").read_text())


@pytest.mark.parametrize("fmt", ["jsonl", "json", "xml"])
def test_matches_scrapy_exporter(tmp_path, fmt):
    path = tmp_path / f"out.{fmt}"
    assert write_items(ITEMS, path) == len(ITEMS)
    assert path.read_bytes() == (FIXTURES / f"scrapy.{fmt}").read_bytes()


def test_csv_matches_golden_file(tmp_path):
    # D2: CSV deliberately differs from Scrapy's (nested objects are JSON), so the
    # golden file comes from this writer
    path = tmp_path / "out.csv"
    write_items(ITEMS, path)
    assert path.read_bytes() == (FIXTURES / "expected.csv").read_bytes()


def test_csv_encodes_nested_values_as_json(tmp_path):
    path = tmp_path / "out.csv"
    write_items(ITEMS, path)
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    # Columns come from the first item, as with Scrapy
    assert list(rows[0]) == ["product", "url", "store", "image_urls"]
    assert json.loads(rows[0]["product"]) == ITEMS[0]["product"]
    # Lists of strings stay comma-joined, as Scrapy wrote them
    assert rows[0]["image_urls"] == ",".join(ITEMS[0]["image_urls"])
    assert rows[1]["image_urls"] == ""
    assert rows[1]["product"] == ""


def test_csv_fields_keep_every_column(tmp_path):
    path = tmp_path / "out.csv"
    write_items(ITEMS, path, fields=["product", "collection", "url", "store", "image_urls"])
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    assert json.loads(rows[1]["collection"])["id"] == 2


def test_append_keeps_single_csv_header(tmp_path):
    path = tmp_path / "out.csv"
    write_items(ITEMS[:1], path)
    write_items(ITEMS[:1], path, append=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("product,")
    assert sum(line.startswith("product,") for line in lines) == 1
    assert len(list(csv.DictReader(path.open(newline="", encoding="utf-8")))) == 2


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
    assert [json.loads(line) for line in path.read_text().splitlines()] == ITEMS


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
    data = json.loads(path.read_text())
    assert data["urls"] == [
        {"url": "a.com", "items": 0, "status": "403 Forbidden", "code": "forbidden"},
        {"url": "bad url", "items": 0, "status": "invalid URL", "code": "invalid_url"},
    ]
    assert data["duration_seconds"] == 1.2
