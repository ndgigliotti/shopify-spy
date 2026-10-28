"""Item writers (JSONL, JSON, CSV, XML) and the CLI's status file.

JSONL, JSON and XML output matches what Scrapy's feed exporters wrote (UTF-8,
``ensure_ascii=False``, no indentation). CSV encodes nested values as JSON.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any
from xml.sax.saxutils import XMLGenerator

logger = logging.getLogger("shopify_spy")

FORMATS: dict[str, str] = {".jsonl": "jsonl", ".json": "json", ".csv": "csv", ".xml": "xml"}
EXTENSIONS: dict[str, str] = {v: k for k, v in FORMATS.items()}


def format_for(path: str | Path, format: str | None = None) -> str:
    if format is not None:
        if format not in EXTENSIONS:
            raise ValueError(f"unknown format {format!r}; use one of {', '.join(EXTENSIONS)}")
        return format
    suffix = Path(path).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError(
            f"can't tell the format of {path}; pass format='jsonl' (or json, csv, xml)"
        )
    return FORMATS[suffix]


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class ItemWriter:
    """Streams items to a file. Provisional API.

    Use as a context manager; the file is only created when the first item arrives,
    so a run without items leaves no file (like the CLI always did). ``fields``
    fixes CSV columns; otherwise they come from the first item.
    """

    def __init__(
        self,
        path: str | Path,
        format: str | None = None,
        *,
        fields: list[str] | None = None,
        append: bool = False,
    ) -> None:
        self.path = Path(path)
        self.format = format_for(self.path, format)
        if append and self.format not in ("jsonl", "csv"):
            raise ValueError("append is only supported for jsonl and csv")
        self.fields = list(fields) if fields else None
        self.append = append
        self.count = 0
        self._file: IO[bytes] | None = None
        self._text: io.TextIOWrapper | None = None
        self._csv: Any = None
        self._xml: XMLGenerator | None = None
        self._dropped: set[str] = set()
        self._field_set: set[str] = set()

    def __enter__(self) -> ItemWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open(self, item: dict[str, Any]) -> None:
        existing = self.append and self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "ab" if self.append else "wb")  # noqa: SIM115
        if self.format == "json":
            self._file.write(b"[\n")
        elif self.format == "xml":
            self._xml = XMLGenerator(self._file, encoding="utf8")
            self._xml.startDocument()
            self._xml.startElement("items", {})
            self._xml.characters("\n")
        elif self.format == "csv":
            self._text = io.TextIOWrapper(
                self._file, encoding="utf-8", newline="", write_through=True
            )
            self._csv = csv.writer(self._text)
            if self.fields is None and existing:
                with open(self.path, newline="", encoding="utf-8") as f:
                    self.fields = next(csv.reader(f), None)
            if self.fields is None:
                self.fields = list(item.keys())
            self._field_set = set(self.fields)
            if not existing:
                self._csv.writerow(self.fields)

    def write(self, item: dict[str, Any]) -> None:
        if self._file is None:
            self._open(item)
        assert self._file is not None
        if self.format == "jsonl":
            self._file.write((_dumps(item) + "\n").encode("utf-8"))
        elif self.format == "json":
            if self.count:
                self._file.write(b",\n")
            self._file.write(_dumps(item).encode("utf-8"))
        elif self.format == "csv":
            assert self.fields is not None
            extra = item.keys() - self._field_set - self._dropped
            if extra:
                self._dropped |= extra
                logger.warning("CSV columns are fixed; dropping field(s): %s", ", ".join(extra))
            self._csv.writerow([_csv_value(item.get(f)) for f in self.fields])
        else:
            assert self._xml is not None
            self._xml.startElement("item", {})
            for name, value in item.items():
                _xml_field(self._xml, name, value)
            self._xml.endElement("item")
            self._xml.characters("\n")
        self.count += 1

    def close(self) -> None:
        if self._file is None:
            return
        try:
            if self.format == "json":
                self._file.write(b"\n]")
            elif self.format == "xml" and self._xml is not None:
                self._xml.endElement("items")
                self._xml.endDocument()
            if self._text is not None:
                self._text.flush()
                self._text.detach()
        finally:
            self._file.close()
            self._file = None


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    # Lists of strings are comma-joined, as Scrapy's CSV exporter did (image_urls);
    # other nested values are JSON, where Scrapy wrote unparseable Python reprs
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return ",".join(value)
    if isinstance(value, (dict, list, tuple)):
        return _dumps(value)
    return value


def _xml_field(xml: XMLGenerator, name: str, value: Any) -> None:
    xml.startElement(name, {})
    if isinstance(value, dict):
        for sub, v in value.items():
            _xml_field(xml, str(sub), v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _xml_field(xml, "value", v)
    else:
        xml.characters(value if isinstance(value, str) else str(value))
    xml.endElement(name)


def write_items(
    items: Iterable[dict[str, Any]],
    path: str | Path,
    *,
    format: str | None = None,
    fields: list[str] | None = None,
    append: bool = False,
) -> int:
    """Write items to a file, streaming; return how many were written.

    ``items`` can be any iterable, including a run from ``scrape()``. The format
    comes from the extension (.jsonl, .json, .csv, .xml) unless ``format`` is given.
    ``append=True`` adds to an existing JSONL or CSV file (CSV keeps its header).
    ``fields`` fixes CSV columns; by default they come from the first item. No file
    is created when there are no items.
    """
    with ItemWriter(path, format, fields=fields, append=append) as writer:
        for item in items:
            writer.write(item)
        return writer.count


def write_status(
    path: str | Path,
    stores: list[Any],
    *,
    finish_reason: str,
    duration: float,
    log_file: str | None,
    skipped_urls: list[str] | None = None,
) -> None:
    """Write the CLI's ``_status.json`` (provisional API).

    Per-URL ``status`` strings are the CLI's labels; ``code`` is the StoreStatus.
    """
    entries = [
        {
            "url": s.input_url,
            "items": s.items,
            "status": s.status.label,
            "code": s.status.value,
        }
        for s in stores
    ]
    entries += [
        {"url": u, "items": 0, "status": "invalid URL", "code": "invalid_url"}
        for u in skipped_urls or []
    ]
    data = {
        "items_scraped": sum(s.items for s in stores),
        "urls": entries,
        "finish_reason": finish_reason,
        "duration_seconds": round(duration, 1),
        "log_file": log_file,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
