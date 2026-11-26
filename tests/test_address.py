"""Tests for the public/non-public address classifier."""

import asyncio
import ipaddress
import socket

import pytest

from shopify_spy import _address
from shopify_spy._address import (
    is_local_name,
    is_private_host,
    is_public_ip,
    literal_ip,
    non_public_reason,
)
from shopify_spy._utils import hostname

NON_PUBLIC_URLS = [
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://[fd00::1]/",
    "http://169.254.169.254/",
    "http://[fe80::1]/",
    "http://100.64.0.1/",
    "http://0.0.0.0/",
    "http://[::]/",
    "http://224.0.0.1/",
    "http://[ff02::1]/",
    "http://240.0.0.1/",
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:127.0.0.1]/",
    "http://127.1/",
    "http://2130706433/",
    "http://0x7f.0.0.1/",
    "http://localhost/",
    "http://app.localhost/",
    "http://LOCALHOST./",
]
PUBLIC_URLS = ["http://8.8.8.8/", "http://[2606:4700::1]/", "http://[::ffff:8.8.8.8]/"]

PUBLIC = ipaddress.ip_address("8.8.8.8")
PRIVATE = ipaddress.ip_address("10.0.0.5")


@pytest.mark.parametrize("url", NON_PUBLIC_URLS)
def test_non_public_urls_have_a_reason(url):
    host = hostname(url)
    reason = non_public_reason(host, [])
    assert reason == f"{host} is not a public address"
    assert is_private_host(host, [])


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_public_urls_have_no_reason(url):
    host = hostname(url)
    assert non_public_reason(host, []) is None
    assert not is_private_host(host, [])


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "fd00::1",
        "169.254.169.254",
        "fe80::1",
        "100.64.0.1",
        "0.0.0.0",
        "::",
        "224.0.0.1",  # is_global alone calls multicast global
        "ff02::1",
        "ff0e::1",
        "240.0.0.1",
        "::ffff:169.254.169.254",  # 3.11 and 3.13+ disagree without the unwrap
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_is_public_ip_rejects_non_public(address):
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700::1", "::ffff:8.8.8.8"])
def test_is_public_ip_accepts_public(address):
    assert is_public_ip(ipaddress.ip_address(address))


def test_is_public_ip_takes_both_address_types():
    assert is_public_ip(ipaddress.IPv4Address("8.8.8.8"))
    assert not is_public_ip(ipaddress.IPv4Address("127.0.0.1"))
    assert is_public_ip(ipaddress.IPv6Address("2606:4700::1"))
    assert not is_public_ip(ipaddress.IPv6Address("::1"))
    assert is_public_ip(ipaddress.IPv6Address("::ffff:8.8.8.8"))
    assert not is_public_ip(ipaddress.IPv6Address("::ffff:127.0.0.1"))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.1.", "127.0.0.1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("127.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f.0.0.1", "127.0.0.1"),
        ("0X7F.0.0.1", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("127.000.0.1", "127.0.0.1"),
    ],
)
def test_literal_ip_parses_shorthand_and_brackets(host, expected):
    assert literal_ip(host) == ipaddress.ip_address(expected)


@pytest.mark.parametrize(
    "host", ["shop.test", "1e100.net", "123abc.de", "1.2.3.4.5", "localhost", "", "0x"]
)
def test_literal_ip_rejects_names(host):
    assert literal_ip(host) is None


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "localhost.", "app.localhost"])
def test_local_names(host):
    assert is_local_name(host)


@pytest.mark.parametrize("host", ["localhost.test", "notlocalhost", "shop.test", ""])
def test_not_local_names(host):
    assert not is_local_name(host)


def test_resolved_addresses_decide_names():
    assert non_public_reason("shop.test", [PUBLIC]) is None
    assert non_public_reason("shop.test", []) is None
    assert non_public_reason("shop.test", [PRIVATE]) == (
        "shop.test resolves to 10.0.0.5, not a public address"
    )
    # Any non-public answer refuses: the transport may connect to it
    assert non_public_reason("shop.test", [PUBLIC, PRIVATE]) is not None


def test_private_host_needs_every_answer_non_public():
    assert is_private_host("shop.test", [PRIVATE])
    assert is_private_host("shop.test", [PRIVATE, ipaddress.ip_address("192.168.1.10")])
    assert not is_private_host("shop.test", [PUBLIC, PRIVATE])
    assert not is_private_host("shop.test", [PUBLIC])
    assert not is_private_host("shop.test", [])


def test_empty_host_is_not_refused():
    assert non_public_reason("", [PRIVATE]) is None
    assert hostname("http://[::1/") == ""


async def test_resolve_uses_getaddrinfo(monkeypatch):
    calls = []

    async def fake_getaddrinfo(host, port, *, type):
        calls.append((host, port, type))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%2", port, 0, 2)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    assert await _address.resolve("https://internal.test/x") == [
        ipaddress.ip_address("10.0.0.5"),
        ipaddress.ip_address("fe80::1%2"),
    ]
    assert await _address.resolve("http://internal.test:8080/") != []
    assert calls == [
        ("internal.test", 443, socket.SOCK_STREAM),
        ("internal.test", 8080, socket.SOCK_STREAM),
    ]


async def test_resolve_failure_gives_empty(monkeypatch):
    async def failing(host, port, *, type):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", failing)
    assert await _address.resolve("https://nowhere.test/") == []
