"""Excel output: the unified rows and columns (see ``_flatten``) as typed cells.

xlsxwriter (the ``excel`` extra) runs in constant_memory mode, streaming rows to
temporary files next to the output, so memory stays flat; the .xlsx file is only
filled in on close. Text is
always written as a string, never a formula, and capped at Excel's 32,767 UTF-16
units. A sheet that reaches Excel's row limit continues on a new one.
"""

from __future__ import annotations

import logging
import math
import sys
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from shopify_spy._summary import _decimal

logger = logging.getLogger("shopify_spy")

MAX_ROWS = 1_048_576
MAX_TEXT = 32_767
SHEET = "items"

# IDs stay text: Excel shows numbers over 11 digits in scientific notation
_TEXT_COLUMNS = {"id", "variant_id"}
# Numbers that may arrive as strings (Shopify prices, WooCommerce ratings,
# summarize().asdict() prices)
_NUMBER_COLUMNS = {
    "price",
    "compare_at_price",
    "min_price",
    "max_price",
    "average_rating",
    "review_count",
    "image_count",
    "variant_count",
    "products_count",
    "grams",
    "variant_position",
}
_DATE_COLUMNS = {"created_at", "updated_at", "published_at"}
# Beyond 15 digits Excel rounds the number
_MAX_DIGITS = 15


def require_xlsxwriter() -> Any:
    try:
        import xlsxwriter
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "xlsx output needs xlsxwriter: pip install 'shopify-spy[excel]'",
            name="xlsxwriter",
        ) from e
    return xlsxwriter


class XlsxWriter:
    """Writes rows of scalar values under a fixed header, a sheet at a time."""

    def __init__(self, path: Path, fields: list[str]) -> None:
        import xlsxwriter

        self.fields = fields
        # Row data goes to temp files next to the output, not to /tmp (often RAM). The
        # folder goes away even when the close fails, and is visible if a kill leaves it
        self._tmp = tempfile.TemporaryDirectory(
            prefix=f"{path.name}.tmp-", dir=path.parent, ignore_cleanup_errors=True
        )
        try:
            # xlsxwriter only creates the file on close; fail now, not after the whole run
            path.open("wb").close()
        except BaseException:
            self._tmp.cleanup()
            raise
        self._book = xlsxwriter.Workbook(
            str(path),
            # ZIP64 is only used if the file needs it; without it a >4 GB file fails
            {"constant_memory": True, "use_zip64": True, "tmpdir": self._tmp.name},
        )
        self._header = self._book.add_format({"bold": True})
        self._date = self._book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        self._truncated: set[str] = set()
        self._sheet: Any = None
        self._sheets = 0
        self._row = 0

    def write(self, rows: list[list[Any]]) -> None:
        # Keep a product's variant rows on one sheet when they fit
        if self._sheet is None or (self._row > 1 and self._row + len(rows) > MAX_ROWS):
            self._new_sheet()
        for values in rows:
            if self._row == MAX_ROWS:
                self._new_sheet()
            for col, (field, value) in enumerate(zip(self.fields, values, strict=True)):
                self._cell(col, field, value)
            self._row += 1

    def close(self) -> None:
        try:
            self._finish_sheet()
            self._book.close()
        finally:
            self._tmp.cleanup()

    def _new_sheet(self) -> None:
        self._finish_sheet()
        self._sheets += 1
        self._sheet = self._book.add_worksheet(
            SHEET if self._sheets == 1 else f"{SHEET} {self._sheets}"
        )
        self._sheet.freeze_panes(1, 0)
        for col, field in enumerate(self.fields):
            if field in _DATE_COLUMNS:
                # Narrower date columns show ####
                self._sheet.set_column(col, col, 19)
            self._sheet.write_string(0, col, field, self._header)
        self._row = 1

    def _finish_sheet(self) -> None:
        if self._sheet is not None:
            self._sheet.autofilter(0, 0, self._row - 1, len(self.fields) - 1)

    def _cell(self, col: int, field: str, value: Any) -> None:
        sheet, row = self._sheet, self._row
        if value is None or value == "":
            return
        if isinstance(value, bool):
            sheet.write_boolean(row, col, value)
            return
        # Values Excel can't type are written as the text they arrived as
        original = value
        if isinstance(value, str):
            if field in _NUMBER_COLUMNS:
                number = _decimal(value)
                value = value if number is None else number
            elif field in _DATE_COLUMNS:
                value = _parse_datetime(value)
        if isinstance(value, date):
            timestamp = _excel_datetime(value)
            if timestamp is not None:
                sheet.write_datetime(row, col, timestamp, self._date)
                return
        if isinstance(value, (int, float, Decimal)) and field not in _TEXT_COLUMNS:
            number = _number(value)
            if number is not None:
                sheet.write_number(row, col, number)
                return
        sheet.write_string(row, col, self._text(field, str(original)))

    def _text(self, field: str, text: str) -> str:
        if text.isascii() and len(text) <= MAX_TEXT:
            return text
        # Excel counts UTF-16 units: characters outside the BMP take two
        units = text.encode("utf-16-le", "surrogatepass")
        if len(units) > MAX_TEXT * 2:
            units = units[: MAX_TEXT * 2]
            if 0xD800 <= int.from_bytes(units[-2:], "little") <= 0xDBFF:
                units = units[:-2]  # don't split a surrogate pair
            if field not in self._truncated:
                self._truncated.add(field)
                logger.warning(
                    "xlsx: cut %s text to Excel's %d-character cell limit", field, MAX_TEXT
                )
        # A lone surrogate (from a broken JSON escape) would fail the whole file on close
        return units.decode("utf-16-le", "replace")


def _number(value: int | float | Decimal) -> float | int | None:
    """The value as an Excel number, or None when Excel can't hold it exactly."""
    if isinstance(value, int):
        return value if abs(value) < 10**_MAX_DIGITS else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        # normalize() would overflow on exponents like 1E+999999999
        digits = value.as_tuple().digits
        significant = len(digits)
        while significant > 1 and digits[significant - 1] == 0:
            significant -= 1
        if significant > _MAX_DIGITS:
            return None
    number = float(value)
    if not math.isfinite(number) or abs(number) >= 10**_MAX_DIGITS:
        return None
    # Below the smallest normal float (or rounded to 0), Excel reads 0
    return None if value and abs(number) < sys.float_info.min else number


def _parse_datetime(text: str) -> datetime | str:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return text


def _excel_datetime(value: date) -> datetime | date | None:
    """The value in UTC without its offset (Excel has no time zones); None before 1900,
    where Excel's dates start."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        try:
            value = value.astimezone(UTC).replace(tzinfo=None)
        except OverflowError:  # such as 0001-01-01T00:00:00+05:00
            return None
    return value if value.year >= 1900 else None
