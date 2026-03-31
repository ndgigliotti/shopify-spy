"""Custom Scrapy item exporters for SQLite and Parquet formats."""

import json
import sqlite3

from scrapy.exporters import BaseItemExporter


class SqliteItemExporter(BaseItemExporter):
    """Exports items to a SQLite database.

    Creates an ``items`` table whose columns are derived from the first item's keys.
    Dict and list values are JSON-serialized to text.
    """

    def __init__(self, file, **kwargs):
        super().__init__(dont_fail=True, **kwargs)
        self._db_path = file.name
        # Close the empty file Scrapy created; sqlite3 manages its own file.
        file.close()
        self._conn = None
        self._columns = None

    def start_exporting(self):
        from pathlib import Path

        Path(self._db_path).unlink(missing_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._placeholders = None

    def export_item(self, item):
        dict_item = dict(self._get_serialized_fields(item))
        if self._columns is None:
            self._columns = list(dict_item.keys())
            cols_def = ", ".join(f'"{c}"' for c in self._columns)
            self._conn.execute(f"CREATE TABLE items ({cols_def})")
            self._placeholders = ", ".join("?" for _ in self._columns)

        values = []
        for col in self._columns:
            val = dict_item.get(col)
            if isinstance(val, dict | list):
                val = json.dumps(val)
            values.append(val)

        self._conn.execute(f"INSERT INTO items VALUES ({self._placeholders})", values)
        return item

    def finish_exporting(self):
        if self._conn:
            self._conn.commit()
            self._conn.close()


class ParquetItemExporter(BaseItemExporter):
    """Exports items to Apache Parquet format.

    Requires the ``pyarrow`` package (install via ``pip install shopify-spy[parquet]``).
    Items are buffered in memory and written as a single Parquet file on close.
    Dict and list values are JSON-serialized to string columns.
    """

    def __init__(self, file, **kwargs):
        super().__init__(dont_fail=True, **kwargs)
        self._file = file
        self._items = []

    def start_exporting(self):
        pass

    def export_item(self, item):
        dict_item = dict(self._get_serialized_fields(item))
        for key, val in dict_item.items():
            if isinstance(val, dict | list):
                dict_item[key] = json.dumps(val)
        self._items.append(dict_item)
        return item

    def finish_exporting(self):
        if not self._items:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist(self._items)
        pq.write_table(table, self._file)
