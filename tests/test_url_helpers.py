"""URL helpers for the Shopify and WooCommerce adapters."""

from shopify_spy._shopify import (
    get_bulk_products_url,
    get_sitemap_url,
    next_bulk_page_url,
    product_handle,
)
from shopify_spy._woocommerce import get_api_url, next_page_url


def test_get_sitemap_url():
    inputs = [
        "https://www.example.com",
        "https://www.example.com/",
        "https://www.example.com/products/big_fancy_table",
        "https://www.example.com/products/big_fancy_table/",
    ]
    expected = ["https://www.example.com/sitemap.xml"] * 4

    for url, answer in zip(inputs, expected):
        assert get_sitemap_url(url) == answer


def test_get_sitemap_url_no_scheme():
    """URLs without scheme should default to https."""
    assert get_sitemap_url("www.example.com") == "https://www.example.com/sitemap.xml"
    assert get_sitemap_url("example.com") == "https://example.com/sitemap.xml"


def test_get_bulk_products_url():
    assert (
        get_bulk_products_url("https://www.example.com")
        == "https://www.example.com/products.json?limit=250&page=1"
    )


def test_get_bulk_products_url_page():
    assert (
        get_bulk_products_url("https://www.example.com", page=3)
        == "https://www.example.com/products.json?limit=250&page=3"
    )


def test_get_bulk_products_url_no_scheme():
    assert (
        get_bulk_products_url("www.example.com")
        == "https://www.example.com/products.json?limit=250&page=1"
    )


def test_next_bulk_page_url():
    url = "https://www.example.com/products.json?limit=250&page=1"
    assert next_bulk_page_url(url) == "https://www.example.com/products.json?limit=250&page=2"


def test_next_bulk_page_url_preserves_limit():
    url = "https://www.example.com/products.json?limit=250&page=5"
    result = next_bulk_page_url(url)
    assert "limit=250" in result
    assert "page=6" in result


def test_get_api_url_basic():
    assert get_api_url("https://store.com") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


def test_get_api_url_page():
    assert get_api_url("https://store.com", page=3) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=3"
    )


def test_get_api_url_strips_path():
    assert get_api_url("https://store.com/some/path/") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


def test_get_api_url_no_scheme():
    assert get_api_url("store.com") == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    )


# --- next_page_url tests ---


def test_next_page_url():
    url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=1"
    assert next_page_url(url) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=2"
    )


def test_next_page_url_increments():
    url = "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=5"
    assert next_page_url(url) == (
        "https://store.com/wp-json/wc/store/v1/products?per_page=100&page=6"
    )


def test_product_handle_ignores_host_locale_and_case():
    assert product_handle("https://www.a.com/en-ca/products/Blue%20Hat?x=1") == "blue hat"
    assert product_handle("https://a.com/products/x.json") == "x"
    assert product_handle("https://a.com/collections/y") is None
