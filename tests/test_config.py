from pathlib import Path

import pytest

from shopify_spy.config import (
    OUTPUT_FORMATS,
    Config,
    OutputConfig,
    ScrapeConfig,
    create_default_config,
    load_config,
    load_config_from_file,
)

# --- Config loading tests ---


def test_config_defaults():
    config = Config()
    assert config.scrape.platform == "shopify"
    assert config.scrape.products is True
    assert config.scrape.collections is False
    assert config.scrape.images is False
    assert config.output.dir == Path("./output")
    assert config.network.concurrent_requests == 16
    assert config.throttle.enabled is True
    assert config.throttle.start_delay == 1.0


def test_config_images_dir():
    config = Config()
    assert config.output.images_dir == Path("./output/images")


def test_load_config_from_file(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
scrape:
  platform: woocommerce
  products: false
  collections: true
  images: false
output:
  dir: ./custom-output
network:
  concurrent_requests: 8
""")
    config = load_config_from_file(config_file)
    assert config.scrape.platform == "woocommerce"
    assert config.scrape.products is False
    assert config.scrape.collections is True
    assert config.scrape.images is False
    assert config.output.dir == Path("./custom-output")
    assert config.network.concurrent_requests == 8


def test_load_config_empty_file(tmp_path):
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("")
    config = load_config_from_file(config_file)
    assert config.scrape.products is True  # default


def test_load_config_with_explicit_path(tmp_path):
    config_file = tmp_path / "my-config.yaml"
    config_file.write_text("scrape:\n  products: false")
    config = load_config(config_file)
    assert config.scrape.products is False


def test_load_config_no_file():
    config = load_config(Path("/nonexistent/path.yaml"))
    assert config.scrape.products is True  # default


def test_create_default_config(tmp_path):
    config_file = tmp_path / "new-config.yaml"
    created = create_default_config(config_file)
    assert created.exists()
    content = created.read_text()
    assert "scrape:" in content
    assert "output:" in content
    assert "network:" in content


# --- Output format tests ---


def test_output_config_default_format():
    """Default output format is jsonl."""
    config = OutputConfig()
    assert config.format == "jsonl"


def test_output_config_valid_formats():
    """All four format values are accepted."""
    for fmt in ("json", "jsonl", "csv", "xml"):
        config = OutputConfig(format=fmt)
        assert config.format == fmt


def test_output_config_invalid_format():
    """Invalid format values are rejected."""
    with pytest.raises(Exception):
        OutputConfig(format="invalid_format")


def test_load_config_with_format(tmp_path):
    """YAML with explicit format loads correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("output:\n  format: csv\n")
    config = load_config_from_file(config_file)
    assert config.output.format == "csv"


def test_load_config_without_format(tmp_path):
    """Missing format in YAML defaults to jsonl."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("output:\n  dir: ./data\n")
    config = load_config_from_file(config_file)
    assert config.output.format == "jsonl"


def test_output_formats_mapping():
    """OUTPUT_FORMATS maps format names to (scrapy_format, file_ext) tuples."""
    assert OUTPUT_FORMATS["json"] == ("json", ".json")
    assert OUTPUT_FORMATS["jsonl"] == ("jsonlines", ".jsonl")
    assert OUTPUT_FORMATS["csv"] == ("csv", ".csv")
    assert OUTPUT_FORMATS["xml"] == ("xml", ".xml")


# --- Limit config tests ---


def test_scrape_config_limit_default():
    """Default limit is None (no limit)."""
    config = Config()
    assert config.scrape.limit is None


def test_scrape_config_limit_valid():
    """Positive integer limit is accepted."""
    assert ScrapeConfig(limit=1).limit == 1
    assert ScrapeConfig(limit=100).limit == 100


def test_scrape_config_limit_invalid():
    """Zero or negative limit is rejected."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ScrapeConfig(limit=0)
    with pytest.raises(ValidationError):
        ScrapeConfig(limit=-5)


def test_load_config_with_limit(tmp_path):
    """YAML with limit loads correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scrape:\n  limit: 25\n")
    config = load_config_from_file(config_file)
    assert config.scrape.limit == 25
