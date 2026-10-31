"""Item writers (JSONL, JSON, CSV, XLSX, XML) and the CLI's status file.

Two schemas: ``native`` writes each item as scraped, and ``unified`` writes a scraped
item's rows (one per variant, the same columns on both platforms; see ``_flatten``).
JSONL, JSON and XML default to native, which matches what Scrapy's feed exporters wrote
(UTF-8, ``ensure_ascii=False``, no indentation). CSV is always unified; other dicts get a
column per key, nested values as JSON. XLSX is always unified too, as typed cells (see
``_xlsx``).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import IO, Any
from xml.sax.saxutils import XMLGenerator

from shopify_spy._flatten import COLUMNS, default_columns, flatten, item_platform
from shopify_spy._xlsx import XlsxWriter, require_xlsxwriter

logger = logging.getLogger("shopify_spy")

FORMATS: dict[str, str] = {
    ".jsonl": "jsonl",
    ".json": "json",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xml": "xml",
}
EXTENSIONS: dict[str, str] = {v: k for k, v in FORMATS.items()}
SCHEMAS = ("native", "unified")
# Formats that hold one fixed table and so only take unified rows
UNIFIED_ONLY_FORMATS = {"csv", "xlsx"}


def format_for(path: str | Path, format: str | None = None) -> str:
    if format is not None:
        if format not in EXTENSIONS:
            raise ValueError(f"unknown format {format!r}; use one of {', '.join(EXTENSIONS)}")
        return format
    suffix = Path(path).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError(
            f"can't tell the format of {path}; pass format='jsonl' (or json, csv, xlsx, xml)"
        )
    return FORMATS[suffix]


def schema_for(format: str, schema: str | None = None) -> str:
    """The schema a format writes: ``schema``, or the format's default when it's None."""
    if schema is None:
        return "unified" if format in UNIFIED_ONLY_FORMATS else "native"
    if schema not in SCHEMAS:
        raise ValueError(f"unknown schema {schema!r}; use native or unified")
    if schema == "native" and format in UNIFIED_ONLY_FORMATS:
        raise ValueError(
            f"{format.upper()} is always unified (a row per variant); "
            "write JSONL, JSON or XML for native items"
        )
    return schema


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_decimal_str)


def _decimal_str(value: Any) -> str:
    # Unified rows have Decimal prices; anything else that isn't JSON still raises
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def unified_records(item: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
    """The item's unified rows as records keyed by ``fields`` (repeats collapse)."""
    return [{f: row.get(f) for f in fields} for row in flatten(item) or [item]]


class ItemWriter:
    """Streams items to a file. Provisional API.

    Use as a context manager; the file is only created when the first item arrives,
    so a run without items leaves no file (like the CLI always did). ``schema``
    (see ``write_items``) picks between items as scraped and unified rows, a row per
    variant of a scraped item; ``fields`` picks a unified row's columns, which
    otherwise are ``_flatten.COLUMNS`` for scraped items of either platform (or the
    first item's keys, for other dicts). XLSX needs the ``excel`` extra and is only
    written on close.

    Each item is encoded in full before any of it is written, and a failed write is
    rolled back, so the file holds whole items only (XLSX: rows already handed to
    xlsxwriter stay).
    """

    def __init__(
        self,
        path: str | Path,
        format: str | None = None,
        *,
        fields: list[str] | None = None,
        append: bool = False,
        schema: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.format = format_for(self.path, format)
        self.schema = schema_for(self.format, schema)
        if fields and self.schema == "native":
            logger.warning("fields only applies to the unified schema; writing native items")
        if append and self.format not in ("jsonl", "csv"):
            raise ValueError("append is only supported for jsonl and csv")
        if self.format == "xlsx":
            require_xlsxwriter()
        self.fields = list(fields) if fields else None
        self.append = append
        self.count = 0
        self._records = 0
        self._file: IO[bytes] | None = None
        self._csv_buffer = io.StringIO()
        self._csv = csv.writer(self._csv_buffer)
        self._xlsx: XlsxWriter | None = None
        self._dropped: set[str] = set()
        # Columns the caller picked may leave out fields without a warning
        self._picked = self.fields is not None
        self._field_set: set[str] = set()

    def __enter__(self) -> ItemWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open(self, item: dict[str, Any]) -> None:
        existing = self.append and self.path.exists() and self.path.stat().st_size > 0
        if existing and self.format == "jsonl" and item_platform(item):
            self._check_jsonl_schema()
        if self.schema == "unified":
            self._columns(item, existing)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.format == "xlsx":
            assert self.fields is not None
            self._xlsx = XlsxWriter(self.path, self.fields)
            return
        # Unbuffered: each item is one write, so a failed one can be cut off cleanly
        self._file = open(self.path, "ab" if self.append else "wb", buffering=0)  # noqa: SIM115
        if self.format == "json":
            self._write_all(b"[\n")
        elif self.format == "xml":
            self._write_all(b'<?xml version="1.0" encoding="utf8"?>\n<items>\n')
        elif self.format == "csv" and not existing:
            self._write_all(self._csv_rows([self.fields]))

    def _check_jsonl_schema(self) -> None:
        """Refuse to append scraped items in one schema to a JSONL file in the other."""
        with open(self.path, encoding="utf-8") as f:
            try:
                first = json.loads(f.readline())
            except ValueError:
                return
        if not isinstance(first, dict):
            return
        if item_platform(first):
            found = "native"
        elif {"platform", "kind", "variant_id"} <= first.keys():
            found = "unified"
        else:
            return
        if found != self.schema:
            raise ValueError(
                f"{self.path} holds {found} items; append with schema={found!r} "
                f"or write {self.schema} items to a new file"
            )

    def _columns(self, item: dict[str, Any], existing: bool) -> None:
        """Settle the unified columns before the file is touched."""
        known = COLUMNS if item_platform(item) else None
        if existing and self.format == "csv":
            with open(self.path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), None) or []
            if known and {"product", "collection", "prices"} & set(header):
                raise ValueError(
                    f"{self.path} has CSV columns from shopify-spy 0.3 or earlier "
                    "(one per item field); write the new one-row-per-variant CSV to a new file"
                )
            if self.fields is None:
                self.fields = header or None
        if self.fields is None:
            self.fields = default_columns(item) or list(item.keys())
        elif known and self._picked:
            unknown = [f for f in self.fields if f not in known]
            if unknown:
                logger.warning(
                    "Field(s) not among the unified columns, left empty: %s",
                    ", ".join(unknown),
                )
        self._field_set = set(self.fields)

    def write(self, item: dict[str, Any]) -> None:
        if self._file is None and self._xlsx is None:
            self._open(item)
        fields = self.fields or []
        if self.format == "xlsx":
            assert self._xlsx is not None
            rows = self._rows(item)
            self._xlsx.write([[_xlsx_value(row.get(f)) for f in fields] for row in rows])
            self.count += 1
            return
        if self.format == "csv":
            data = self._csv_rows(
                [_csv_value(row.get(f)) for f in fields] for row in self._rows(item)
            )
            records = 0
        else:
            if self.schema == "unified":
                items = [{f: row.get(f) for f in fields} for row in self._rows(item)]
            else:
                items = [item]
            data = b"".join(self._encode(r, self._records + i) for i, r in enumerate(items))
            records = len(items)
        self._write_item(data)
        self._records += records
        self.count += 1

    def _encode(self, record: dict[str, Any], index: int) -> bytes:
        """A JSONL, JSON or XML record; ``index`` is its place in the file."""
        if self.format == "jsonl":
            return (_dumps(record) + "\n").encode("utf-8")
        if self.format == "json":
            return (",\n" if index else "").encode() + _dumps(record).encode("utf-8")
        out = io.BytesIO()
        xml = XMLGenerator(out, encoding="utf8")
        xml.startElement("item", {})
        for name, value in record.items():
            # Unified rows read like CSV cells: true/false, and empty for None
            _xml_field(xml, name, _csv_value(value) if self.schema == "unified" else value)
        xml.endElement("item")
        xml.characters("\n")
        xml.endDocument()  # flushes
        return out.getvalue()

    def _csv_rows(self, rows: Iterable[Iterable[Any]]) -> bytes:
        self._csv_buffer.seek(0)
        self._csv_buffer.truncate()
        self._csv.writerows(rows)
        return self._csv_buffer.getvalue().encode("utf-8")

    def _write_item(self, data: bytes) -> None:
        assert self._file is not None
        start = self._file.tell()
        try:
            self._write_all(data)
        except BaseException:
            # Cut off the partial item so the file ends on a whole one
            try:
                self._file.truncate(start)
                self._file.seek(start)
            except OSError:
                logger.exception("Could not remove a partly written item from %s", self.path)
            raise

    def _write_all(self, data: bytes) -> None:
        assert self._file is not None
        view = memoryview(data)
        while view:
            view = view[self._file.write(view) :]

    def _rows(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """The item's unified rows, warning once about fields the columns leave out."""
        rows = flatten(item) or [item]
        extra = set() if self._picked else rows[0].keys() - self._field_set - self._dropped
        if extra:
            self._dropped |= extra
            logger.warning("Columns are fixed; dropping field(s): %s", ", ".join(sorted(extra)))
        return rows

    def close(self) -> None:
        if self._xlsx is not None:
            xlsx, self._xlsx = self._xlsx, None
            try:
                xlsx.close()
            except BaseException:
                # Don't leave a half-written zip that Excel can't open
                self.path.unlink(missing_ok=True)
                raise
        if self._file is None:
            return
        try:
            if self.format == "json":
                self._write_all(b"\n]")
            elif self.format == "xml":
                self._write_all(b"</items>")
        finally:
            self._file.close()
            self._file = None


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    # Lists of strings are comma-joined, as Scrapy's CSV exporter did (image_urls);
    # other nested values are JSON, where Scrapy wrote unparseable Python reprs
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return ",".join(value)
    if isinstance(value, (dict, list, tuple)):
        return _dumps(value)
    return value


def _xlsx_value(value: Any) -> Any:
    """Scalars as they are, for typed cells; nested values as in CSV."""
    return _csv_value(value) if isinstance(value, (dict, list, tuple)) else value


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
    schema: str | None = None,
) -> int:
    """Write items to a file, streaming; return how many were written.

    ``items`` can be any iterable, including a run from ``scrape()``. The format
    comes from the extension (.jsonl, .json, .csv, .xlsx, .xml) unless ``format`` is given.
    ``append=True`` adds to an existing JSONL or CSV file (CSV keeps its header, and
    refuses a 0.3.0 CSV of scraped items, which had a column per item field;
    JSONL refuses scraped items in the other schema).

    ``schema="native"`` writes each item as scraped; ``schema="unified"`` writes one
    row per product variant (WooCommerce: per variation; collections and products
    without variants: one row), every value a scalar, with the same columns on both
    platforms and a ``platform`` column. JSONL, JSON and XML default to native; CSV and
    XLSX are always unified (``schema="native"`` raises ValueError). ``fields`` picks a unified
    row's columns. Dicts that aren't scraped items, such as ``summarize().asdict()``,
    get a column per key of the first one. Returns the number of items, not rows. No
    file is created when there are no items.

    XLSX (needs ``pip install 'shopify-spy[excel]'``) has the unified rows as typed
    cells: prices, counts and ratings are numbers, booleans are booleans, timestamps
    are dates in UTC, and IDs and other text are strings (never formulas), cut at
    Excel's 32,767-character cell limit. The header is frozen and filterable; past
    Excel's 1,048,576 rows the items continue on a new sheet. Rows wait in a temporary
    folder next to the file (``<name>.tmp-*``, deleted on close) and the workbook is
    built when the writer closes (a failure there removes the partial file), so a
    killed process leaves no usable file; write JSONL for long runs and convert it
    afterwards.

    A write that fails partway is cut off, so the file ends on the last whole item.
    """
    with ItemWriter(path, format, fields=fields, append=append, schema=schema) as writer:
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
    output_error: str | None = None,
) -> None:
    """Write the CLI's per-run status file, ``status/<name>_status.json`` (provisional API).

    Per-URL ``status`` strings are the CLI's labels; ``code`` is the StoreStatus.
    ``output_error`` says why the data file couldn't be written (None when it was).
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
        "output_error": output_error,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
