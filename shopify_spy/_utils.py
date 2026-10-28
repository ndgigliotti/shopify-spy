"""URL and data helpers shared by the library and the CLI."""

import os
import re
import urllib.parse
from collections.abc import Iterator
from typing import Any

# A leading "name:" is a scheme unless a port number (store.com:8080) or nothing
# (store.com:) follows
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d|$)")


def normalize_url(url: str) -> urllib.parse.ParseResult:
    """Parse a URL, assuming https if no scheme is provided."""
    # Don't rely on urlparse's scheme: it reads "store.com:8080" as scheme "store.com"
    if not _SCHEME_RE.match(url):
        url = f"https://{url.lstrip('/')}"
    return urllib.parse.urlparse(url)


def is_valid_url(url: str) -> bool:
    """Return True if the URL is an http(s) URL with a host and a usable port.

    This rejects what can't be requested; it doesn't try to decide which hostnames
    DNS will resolve.
    """
    if any(c.isspace() or not c.isprintable() or c == "\\" for c in url):
        return False
    try:
        parsed = normalize_url(url)
        port = parsed.port  # Raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or port == 0 or "@" in parsed.netloc:
        return False
    host = parsed.hostname or ""
    # "%" is an IPv6 zone ID, which can't be part of a request URL's host
    return any(c.isalnum() for c in host) and "%" not in host


def read_url_file(path: str | os.PathLike[str]) -> list[str]:
    """Read URLs from a UTF-8 file, one per line, skipping blank lines and # comments."""
    # utf-8-sig also accepts the byte-order mark Windows editors may add
    with open(path, encoding="utf-8-sig") as f:
        return [s for line in f if (s := line.strip()) and not s.startswith("#")]


def split_valid_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    """Split URLs into (valid, invalid) lists, preserving order."""
    valid: list[str] = []
    invalid: list[str] = []
    for u in urls:
        (valid if is_valid_url(u) else invalid).append(u)
    return valid, invalid


def canonicalize_url(url: str) -> str:
    """Return a canonical form of a URL for duplicate detection.

    Lowercases the scheme and host, sorts query parameters and drops the fragment,
    roughly matching Scrapy's request fingerprinting.
    """
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    )
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def next_page_url(url: str) -> str:
    """Increment the ``page`` query parameter (1 if absent), keeping the others."""
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params["page"] = str(int(params.get("page", 1)) + 1)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(params), "")
    )


def origin(url: str) -> str:
    """Return scheme://host[:port] for a URL, the key for per-host state."""
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def find_all_values(key: str, obj: Any) -> Iterator[Any]:
    """Recursively find all values for a key in nested dict/list structures."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_all_values(key, v)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_all_values(key, item)
