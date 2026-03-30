import urllib.parse
from collections.abc import Iterator
from typing import Any

import scrapy


def normalize_url(url: str) -> urllib.parse.ParseResult:
    """Parse a URL, assuming https if no scheme is provided."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"https://{url}")
    return parsed


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


def uri_params(params: dict[str, Any], spider: scrapy.Spider) -> dict[str, Any]:
    """Returns URI parameters as dict."""
    return {**params, "spider_name": spider.name}


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
