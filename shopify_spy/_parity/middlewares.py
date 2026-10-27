"""Scrapy downloader middlewares that record and replay raw downloader outcomes.

Both sit next to the downloader (priority 990), so they see each hop raw: before
decompression, redirects and retries. The corpus format is shared with
``shopify_spy.fetch.ReplayFetcher``:

    {"entries": {"GET <canonical url>": [outcome, ...]}}

An outcome is ``{"status", "headers", "body" (base64)}`` or ``{"error": kind}``.
"""

from __future__ import annotations

import atexit
import base64
import gzip
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from scrapy.exceptions import IgnoreRequest
from scrapy.responsetypes import responsetypes
from twisted.internet import error as twisted_error
from twisted.web._newclient import ResponseFailed

from shopify_spy.utils import canonicalize_url

logger = logging.getLogger(__name__)

RECORD_ENV = "SHOPIFY_SPY_RECORD"
REPLAY_ENV = "SHOPIFY_SPY_REPLAY"

# Shared by every crawler in the process (one crawler per store)
_RECORDED: dict[str, list[dict[str, Any]]] = defaultdict(list)


def _key(request: Any) -> str:
    return f"{request.method} {canonicalize_url(request.url)}"


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, twisted_error.DNSLookupError):
        return "dns"
    if isinstance(exc, (twisted_error.TimeoutError, twisted_error.TCPTimedOutError)):
        return "timeout"
    if isinstance(exc, ResponseFailed):
        return "incomplete"
    if "SSL" in type(exc).__name__ or "certificate" in str(exc).lower():
        return "tls"
    return "connection"


def write_corpus(path: str | Path, entries: dict[str, list[dict[str, Any]]]) -> None:
    data = json.dumps({"version": 1, "entries": entries}, sort_keys=True).encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data) if path.suffix == ".gz" else data)


class RecordMiddleware:
    """Record every response and transport error, keyed by method + canonical URL."""

    def __init__(self, path: str) -> None:
        self.path = path
        atexit.register(lambda: write_corpus(self.path, dict(_RECORDED)))

    @classmethod
    def from_crawler(cls, crawler: Any) -> RecordMiddleware:
        return cls(os.environ[RECORD_ENV])

    def process_response(self, request: Any, response: Any) -> Any:
        headers = {
            k.decode("latin-1"): b", ".join(v).decode("latin-1")
            for k, v in response.headers.items()
        }
        _RECORDED[_key(request)].append(
            {
                "status": response.status,
                "headers": headers,
                "body": base64.b64encode(response.body).decode(),
            }
        )
        return response

    def process_exception(self, request: Any, exception: BaseException) -> None:
        # IgnoreRequest comes from other middlewares (e.g. robots.txt), not the network
        if not isinstance(exception, IgnoreRequest):
            _RECORDED[_key(request)].append({"error": _error_kind(exception)})


_ERRORS = {
    "dns": twisted_error.DNSLookupError,
    "timeout": twisted_error.TimeoutError,
    "connection": twisted_error.ConnectionRefusedError,
    "tls": twisted_error.SSLError,
    "incomplete": lambda: ResponseFailed([Exception("incomplete")]),
}


class ReplayMiddleware:
    """Serve recorded outcomes in order per key (the last one repeats)."""

    def __init__(self, path: str) -> None:
        raw = Path(path).read_bytes()
        if path.endswith(".gz"):
            raw = gzip.decompress(raw)
        self.entries: dict[str, list[dict[str, Any]]] = json.loads(raw)["entries"]
        self.calls: dict[str, int] = defaultdict(int)

    @classmethod
    def from_crawler(cls, crawler: Any) -> ReplayMiddleware:
        return cls(os.environ[REPLAY_ENV])

    def process_request(self, request: Any) -> Any:
        key = _key(request)
        outcomes = self.entries.get(key)
        if not outcomes:
            logger.warning("Replay miss: %s", key)
            return responsetypes.from_args(url=request.url)(
                url=request.url,
                status=404,
                headers={"X-Replay-Miss": "1"},
                body=b"",
                request=request,
            )
        index = self.calls[key]
        self.calls[key] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if "error" in outcome:
            raise _ERRORS.get(outcome["error"], twisted_error.ConnectionRefusedError)()
        body = base64.b64decode(outcome["body"])
        headers = outcome["headers"]
        cls = responsetypes.from_args(headers=headers, url=request.url, body=body)
        return cls(
            url=request.url, status=outcome["status"], headers=headers, body=body, request=request
        )
