import json

import pytest


# --- SQLite exporter tests ---


def test_sqlite_exporter_creates_table(tmp_path):
    """SqliteItemExporter creates an items table with correct columns."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"url": "https://example.com", "store": "example.com"})
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    cursor = conn.execute("SELECT * FROM items")
    cols = [desc[0] for desc in cursor.description]
    conn.close()

    assert cols == ["url", "store"]


def test_sqlite_exporter_inserts_items(tmp_path):
    """SqliteItemExporter inserts multiple rows."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"id": 1, "name": "Widget"})
        exporter.export_item({"id": 2, "name": "Gadget"})
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    rows = conn.execute("SELECT * FROM items").fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0] == (1, "Widget")
    assert rows[1] == (2, "Gadget")


def test_sqlite_exporter_json_serializes_nested(tmp_path):
    """SqliteItemExporter JSON-serializes dict and list values."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.export_item(
            {
                "url": "https://example.com",
                "product": {"title": "Test", "price": 100},
                "tags": ["sale", "new"],
            }
        )
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    rows = conn.execute("SELECT * FROM items").fetchall()
    cols = [desc[0] for desc in conn.execute("SELECT * FROM items").description]
    conn.close()

    assert len(rows) == 1
    product_idx = cols.index("product")
    tags_idx = cols.index("tags")
    assert json.loads(rows[0][product_idx]) == {"title": "Test", "price": 100}
    assert json.loads(rows[0][tags_idx]) == ["sale", "new"]


def test_sqlite_exporter_empty(tmp_path):
    """SqliteItemExporter creates a valid database even with no items."""
    import sqlite3 as _sqlite3

    from shopify_spy.exporters import SqliteItemExporter

    output_file = tmp_path / "test.db"
    with open(output_file, "wb") as f:
        exporter = SqliteItemExporter(f)
        exporter.start_exporting()
        exporter.finish_exporting()

    conn = _sqlite3.connect(str(output_file))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()

    assert tables == []


# --- Parquet exporter tests ---


def test_parquet_exporter_writes_table(tmp_path):
    """ParquetItemExporter writes a readable Parquet file."""
    pq = pytest.importorskip("pyarrow.parquet")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.export_item({"url": "https://example.com", "store": "example.com"})
        exporter.export_item({"url": "https://other.com", "store": "other.com"})
        exporter.finish_exporting()

    table = pq.read_table(str(output_file))
    assert table.num_rows == 2
    assert table.column("url")[0].as_py() == "https://example.com"
    assert table.column("store")[1].as_py() == "other.com"


def test_parquet_exporter_json_serializes_nested(tmp_path):
    """ParquetItemExporter JSON-serializes dict and list values."""
    pq = pytest.importorskip("pyarrow.parquet")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.export_item(
            {
                "url": "https://example.com",
                "product": {"title": "Test"},
                "tags": ["a", "b"],
            }
        )
        exporter.finish_exporting()

    table = pq.read_table(str(output_file))
    assert table.num_rows == 1
    assert json.loads(table.column("product")[0].as_py()) == {"title": "Test"}
    assert json.loads(table.column("tags")[0].as_py()) == ["a", "b"]


def test_parquet_exporter_empty(tmp_path):
    """ParquetItemExporter handles no items gracefully."""
    pytest.importorskip("pyarrow")
    from shopify_spy.exporters import ParquetItemExporter

    output_file = tmp_path / "test.parquet"
    with open(output_file, "wb") as f:
        exporter = ParquetItemExporter(f)
        exporter.start_exporting()
        exporter.finish_exporting()

    # File should be empty (no data written)
    assert output_file.stat().st_size == 0
