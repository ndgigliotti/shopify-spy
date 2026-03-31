from unittest.mock import Mock

import pytest

from shopify_spy.utils import as_bool, find_all_values, uri_params

# --- as_bool tests ---


def test_as_bool():
    pos_inputs = ["y", "yes", "t", "T", "TRue", "on", "ON", "1", True, 1]
    neg_inputs = ["n", "NO", "f", "false", "OfF", "0", "null", "na", "nan", False, 0, None]

    for value in pos_inputs:
        assert as_bool(value) is True

    for value in neg_inputs:
        assert as_bool(value) is False


def test_as_bool_invalid():
    with pytest.raises(ValueError, match="Could not interpret"):
        as_bool("maybe")

    with pytest.raises(ValueError, match="Could not interpret"):
        as_bool("invalid")


# --- uri_params tests ---


def test_uri_params():
    mock_spider = Mock()
    mock_spider.name = "test_spider"

    params = {"key": "value", "time": "2026-01-01"}
    result = uri_params(params, mock_spider)

    assert result == {"key": "value", "time": "2026-01-01", "spider_name": "test_spider"}


# --- find_all_values tests ---


def test_find_all_values():
    nested = {
        "product": {
            "title": "Test",
            "images": [
                {"src": "http://img1.jpg", "alt": "Image 1"},
                {"src": "http://img2.jpg", "alt": "Image 2"},
            ],
            "featured_image": {"src": "http://featured.jpg"},
        }
    }

    result = list(find_all_values("src", nested))
    assert result == ["http://img1.jpg", "http://img2.jpg", "http://featured.jpg"]

    # Empty case
    assert list(find_all_values("nonexistent", nested)) == []

    # Simple dict
    assert list(find_all_values("key", {"key": "value"})) == ["value"]

    # List at root
    assert list(find_all_values("x", [{"x": 1}, {"x": 2}])) == [1, 2]


def test_find_all_values_empty_containers():
    """Test with empty dicts and lists."""
    assert list(find_all_values("key", {})) == []
    assert list(find_all_values("key", [])) == []
    assert list(find_all_values("key", {"a": {}, "b": []})) == []


def test_find_all_values_none_values():
    """Test handling of None values."""
    # None as a value should be returned
    assert list(find_all_values("key", {"key": None})) == [None]
    # None in a list should be skipped (not iterable)
    assert list(find_all_values("key", [None, {"key": "found"}])) == ["found"]


def test_find_all_values_nested_lists():
    """Test deeply nested list structures."""
    data = [[{"x": 1}], [{"x": 2}, {"x": 3}]]
    assert list(find_all_values("x", data)) == [1, 2, 3]


def test_find_all_values_value_is_container():
    """Test when the value itself is a dict or list."""
    data = {"items": [1, 2, 3], "meta": {"nested": "value"}}
    assert list(find_all_values("items", data)) == [[1, 2, 3]]
    assert list(find_all_values("meta", data)) == [{"nested": "value"}]


def test_find_all_values_duplicate_keys():
    """Test finding same key at multiple nesting levels."""
    data = {
        "id": "outer",
        "child": {
            "id": "inner",
            "grandchild": {"id": "deepest"},
        },
    }
    assert list(find_all_values("id", data)) == ["outer", "inner", "deepest"]


def test_find_all_values_primitives_at_root():
    """Test that primitives at root return empty (not iterable)."""
    assert list(find_all_values("key", "string")) == []
    assert list(find_all_values("key", 123)) == []
    assert list(find_all_values("key", None)) == []
    assert list(find_all_values("key", True)) == []


def test_find_all_values_mixed_types():
    """Test with various value types."""
    data = {
        "string": "text",
        "number": 42,
        "float": 3.14,
        "bool": True,
        "null": None,
    }
    assert list(find_all_values("string", data)) == ["text"]
    assert list(find_all_values("number", data)) == [42]
    assert list(find_all_values("float", data)) == [3.14]
    assert list(find_all_values("bool", data)) == [True]
    assert list(find_all_values("null", data)) == [None]
