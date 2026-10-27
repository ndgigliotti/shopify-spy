"""Capture Scrapy's feed-exporter output as fixtures for tests/test_export.py.

Run once while Scrapy is installed (milestone 0 of the httpx rewrite):

    uv run python scripts/capture_exporter_fixtures.py

The CSV fixture records Scrapy's format for reference only; the new writer encodes
nested values as JSON on purpose (deviation D2).
"""

import io
import json
from pathlib import Path

from scrapy.exporters import (
    CsvItemExporter,
    JsonItemExporter,
    JsonLinesItemExporter,
    XmlItemExporter,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "exporters"

ITEMS = [
    {
        "product": {
            "id": 1,
            "title": 'Café ☕ "quoted" <tag> & amp',
            "tags": ["a", "b,c"],
            "variants": [{"price": "9.99", "available": True, "sku": None}],
            "empty": {},
            "none": None,
        },
        "url": "https://a.com/products/x.json",
        "store": "a.com",
        "image_urls": ["https://cdn.example/x.jpg", "https://cdn.example/y.png"],
    },
    {
        "collection": {"id": 2, "title": "Line\nbreak", "n": 1.5, "flag": False},
        "url": "https://a.com/collections/y.json",
        "store": "a.com",
        "image_urls": [],
    },
]

EXPORTERS = {
    "jsonl": JsonLinesItemExporter,
    "json": JsonItemExporter,
    "xml": XmlItemExporter,
    "csv": CsvItemExporter,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "items.json").write_text(json.dumps(ITEMS, ensure_ascii=False, indent=2) + "\n")
    for name, cls in EXPORTERS.items():
        buf = io.BytesIO()
        # Same arguments the CLI's FEEDS produce (FEED_EXPORT_INDENT defaults to 0)
        exporter = cls(buf, encoding="utf8", export_empty_fields=True, indent=0)
        exporter.start_exporting()
        for item in ITEMS:
            exporter.export_item(json.loads(json.dumps(item)))
        exporter.finish_exporting()
        (OUT / f"scrapy.{name}").write_bytes(buf.getvalue())
        print(f"wrote {OUT / f'scrapy.{name}'}")


if __name__ == "__main__":
    main()
