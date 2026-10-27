"""Tests for sitemap parsing and traversal (untrusted input)."""

import asyncio
import gzip

import pytest

from shopify_spy import sitemap
from shopify_spy.fetch import FakeFetcher, FetchTimeout
from shopify_spy.policy import IdentityPool, RetryPolicy, ThrottleSettings
from shopify_spy.session import HostRegistry, StoreSession
from shopify_spy.sitemap import iter_locs, parse

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset(*locs: str) -> str:
    entries = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset {NS}>{entries}</urlset>'


def index(*locs: str) -> str:
    entries = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex {NS}>{entries}</sitemapindex>'


# --- parse ----------------------------------------------------------------------------


def test_parse_urlset():
    body = urlset("https://a.test/products/x", " https://a.test/products/y ").encode()
    assert parse(body) == ("urlset", ["https://a.test/products/x", "https://a.test/products/y"])


def test_parse_sitemapindex():
    body = index("https://a.test/sitemap_products_1.xml").encode()
    assert parse(body) == ("sitemapindex", ["https://a.test/sitemap_products_1.xml"])


def test_parse_gzip_detected_by_magic_bytes():
    body = gzip.compress(urlset("https://a.test/products/x").encode())
    assert parse(body) == ("urlset", ["https://a.test/products/x"])


def test_parse_corrupt_gzip():
    assert parse(b"\x1f\x8b" + b"garbage" * 10) == (None, [])


@pytest.mark.parametrize("body", [b"", b"not xml at all", b"<urlset><url><loc>a</loc>"])
def test_parse_malformed_does_not_raise(body):
    root, locs = parse(body)
    assert isinstance(locs, list)


def test_parse_non_sitemap_root():
    assert parse(b"<html><body><loc>x</loc></body></html>")[0] is None


def test_parse_rejects_entity_expansion():
    bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        b"<urlset><url><loc>&lol2;</loc></url></urlset>"
    )
    assert parse(bomb) == (None, [])


def test_parse_rejects_external_entity():
    xxe = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<urlset><url><loc>&xxe;</loc></url></urlset>"
    )
    assert parse(xxe) == (None, [])


def test_parse_oversize_body_ignored(monkeypatch):
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_SIZE", 100)
    body = urlset(*(f"https://a.test/products/{i}" for i in range(10))).encode()
    assert parse(body) == (None, [])


def test_parse_oversize_gzip_ignored(monkeypatch):
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_SIZE", 100)
    body = gzip.compress(urlset(*(f"https://a.test/products/{i}" for i in range(10))).encode())
    assert parse(body) == (None, [])


# --- iter_locs ------------------------------------------------------------------------


def make_session(routes) -> tuple[StoreSession, FakeFetcher]:
    fetcher = FakeFetcher(routes)
    registry = HostRegistry(
        concurrent_requests=16,
        throttle=ThrottleSettings(enabled=False),
        respect_robots_txt=False,
    )
    session = StoreSession(
        fetcher=fetcher,
        registry=registry,
        retry=RetryPolicy(retries=0, backoff=False),
        identity=IdentityPool(),
        request_timeout=5,
    )
    return session, fetcher


async def collect(session, url) -> list[str]:
    async with asyncio.timeout(2):
        return [loc async for loc in iter_locs(session, url)]


async def test_iter_locs_urlset():
    session, _ = make_session({"https://a.test/sitemap.xml": (200, urlset("https://a.test/p/1"))})
    assert await collect(session, "https://a.test/sitemap.xml") == ["https://a.test/p/1"]


async def test_iter_locs_follows_same_host_children_and_skips_off_host():
    session, fetcher = make_session(
        {
            "https://a.test/sitemap.xml": (
                200,
                index("https://a.test/s1.xml", "https://evil.test/s.xml", "https://a.test/s2.xml"),
            ),
            "https://a.test/s1.xml": (200, urlset("https://a.test/p/1")),
            "https://a.test/s2.xml": (200, urlset("https://a.test/p/2")),
            "https://evil.test/s.xml": (200, urlset("https://evil.test/p/x")),
        }
    )
    locs = await collect(session, "https://a.test/sitemap.xml")
    assert locs == ["https://a.test/p/1", "https://a.test/p/2"]
    assert fetcher.count("https://evil.test/s.xml") == 0


async def test_iter_locs_accepts_children_on_redirected_host():
    session, _ = make_session(
        {
            "https://a.test/sitemap.xml": (301, "", {"Location": "https://www.a.test/sitemap.xml"}),
            "https://www.a.test/sitemap.xml": (
                200,
                index("https://www.a.test/s1.xml", "https://a.test/s2.xml"),
            ),
            "https://www.a.test/s1.xml": (200, urlset("https://www.a.test/p/1")),
            "https://a.test/s2.xml": (200, urlset("https://a.test/p/2")),
        }
    )
    locs = await collect(session, "https://a.test/sitemap.xml")
    assert locs == ["https://www.a.test/p/1", "https://a.test/p/2"]


async def test_iter_locs_depth_limit():
    def chain(last_body):
        return {
            "https://a.test/s0.xml": (200, index("https://a.test/s1.xml")),
            "https://a.test/s1.xml": (200, index("https://a.test/s2.xml")),
            "https://a.test/s2.xml": (200, index("https://a.test/s3.xml")),
            "https://a.test/s3.xml": (200, last_body),
            "https://a.test/s4.xml": (200, urlset("https://a.test/p/deep")),
        }

    # A urlset at depth 3 is read
    session, _ = make_session(chain(urlset("https://a.test/p/3")))
    assert await collect(session, "https://a.test/s0.xml") == ["https://a.test/p/3"]

    # An index at depth 3 pointing to depth 4 is not followed
    session, fetcher = make_session(chain(index("https://a.test/s4.xml")))
    assert await collect(session, "https://a.test/s0.xml") == []
    assert fetcher.count("https://a.test/s4.xml") == 0


async def test_iter_locs_cycle_protection():
    session, fetcher = make_session(
        {
            "https://a.test/a.xml": (200, index("https://a.test/b.xml")),
            "https://a.test/b.xml": (200, index("https://a.test/a.xml", "https://a.test/c.xml")),
            "https://a.test/c.xml": (200, urlset("https://a.test/p/1")),
        }
    )
    assert await collect(session, "https://a.test/a.xml") == ["https://a.test/p/1"]
    assert fetcher.count("https://a.test/a.xml") == 1
    assert session.stats.duplicates == 0  # stopped by the visited set, not session dedupe


@pytest.mark.parametrize("status", [404, 500])
async def test_iter_locs_non_2xx_yields_nothing(status):
    session, _ = make_session({"https://a.test/sitemap.xml": (status, urlset("https://a.test/p"))})
    assert await collect(session, "https://a.test/sitemap.xml") == []


async def test_iter_locs_request_failure_yields_nothing():
    session, _ = make_session({"https://a.test/sitemap.xml": FetchTimeout("slow")})
    assert await collect(session, "https://a.test/sitemap.xml") == []
