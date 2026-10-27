import logging
import re
import urllib.parse
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: the CLI imports this module and defers Scrapy to keep startup fast
    import scrapy

logger = logging.getLogger(__name__)

# A leading "name:" is a scheme unless a port number follows (store.com:8080)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")


def normalize_url(url: str) -> urllib.parse.ParseResult:
    """Parse a URL, assuming https if no scheme is provided."""
    # Don't rely on urlparse's scheme: it reads "store.com:8080" as scheme "store.com"
    if not _SCHEME_RE.match(url):
        url = f"https://{url.lstrip('/')}"
    return urllib.parse.urlparse(url)


def is_valid_url(url: str) -> bool:
    """Return True if the URL is an http(s) URL with a host and a usable port."""
    try:
        parsed = normalize_url(url)
        port = parsed.port  # Raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and port != 0
        and not any(c.isspace() for c in url)
    )


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


def uri_params(params: dict[str, Any], spider: "scrapy.Spider") -> dict[str, Any]:
    """Returns URI parameters as dict."""
    return {**params, "spider_name": spider.name}


def load_store_urls(url: str | None, url_file: str | None) -> list[str]:
    """Load store URLs from a single URL or a file of URLs (one per line).

    Invalid URLs are skipped with a warning, since one that fails in a spider's
    __init__ stops the whole crawl.
    """
    if url:
        urls = [url.strip()]
    elif url_file:
        with open(url_file) as f:
            urls = [line.strip() for line in f if line.strip()]
    else:
        return []
    valid = []
    for u in urls:
        if is_valid_url(u):
            valid.append(u)
        else:
            logger.warning("Skipping invalid URL: %s", u)
    return valid


def as_bool(value: Any) -> bool:
    """Interpret a string representation of a truth value as True or False.

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', '0', 'none', 'nan', 'na', and 'null'.
    Case insensitive. Raises ValueError if `value` is anything else. Expects
    a string or something castable as a string.

    Inspired by the former distutils.util.strtobool (removed in Python 3.12).
    """
    str_value = str(value).lower()
    if str_value in {"y", "yes", "t", "true", "on", "1"}:
        return True
    elif str_value in {"n", "no", "f", "false", "off", "0", "none", "nan", "na", "null"}:
        return False
    else:
        raise ValueError(f"Could not interpret '{value}' as bool.")
