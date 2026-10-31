"""Per-store and per-run outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shopify_spy._session import StoreStats


class StoreStatus(StrEnum):
    """Why a store ended the way it did. New values may be added in future versions."""

    ok = "ok"  # finished with items
    partial = "partial"  # items, then ended by an error
    empty = "empty"  # finished cleanly with no items
    timed_out = "timed_out"  # bail fired before the first item
    forbidden = "forbidden"  # HTTP 403 after retries
    not_found = "not_found"  # HTTP 404 on the platform endpoint and fallback
    robots_blocked = "robots_blocked"  # robots.txt disallowed the needed URLs
    rate_limited = "rate_limited"  # HTTP 429 after retries
    unavailable = "unavailable"  # HTTP 5xx after retries (outage, maintenance)
    tls_error = "tls_error"  # certificate verification failed
    no_response = "no_response"  # DNS or connection failures only
    error = "error"  # unexpected exception while scraping this store
    stopped = "stopped"  # closed or shut down before it produced anything

    @property
    def is_failure(self) -> bool:
        """True for statuses that mean the store didn't produce its data.

        ``partial`` isn't a failure by default; ``strict=True`` counts it.
        """
        return self not in (
            StoreStatus.ok,
            StoreStatus.partial,
            StoreStatus.empty,
            StoreStatus.stopped,
        )

    @property
    def label(self) -> str:
        """The status string used in CLI summaries and status files."""
        return _LABELS[self]


_PLATFORM_NAMES = {"shopify": "Shopify", "woocommerce": "WooCommerce"}

_LABELS = {
    StoreStatus.ok: "ok",
    StoreStatus.partial: "ok",
    StoreStatus.empty: "0 items",
    StoreStatus.timed_out: "timed out",
    StoreStatus.forbidden: "403 Forbidden",
    StoreStatus.not_found: "404 Not Found",
    StoreStatus.robots_blocked: "blocked by robots.txt",
    StoreStatus.rate_limited: "rate limited (429)",
    StoreStatus.unavailable: "unavailable (5xx)",
    StoreStatus.tls_error: "TLS error",
    StoreStatus.no_response: "no response",
    StoreStatus.error: "failed",
    StoreStatus.stopped: "stopped",
}


@dataclass(frozen=True)
class StoreResult:
    """What happened to one store.

    ``complete`` is False when the store stopped early: item limit, bail, shutdown,
    close, or an error. ``finish_reason`` says which.
    """

    input_url: str  # as given
    url: str  # normalized store URL
    store: str  # host
    platform: str
    items: int
    status: StoreStatus
    complete: bool
    finish_reason: str  # finished, item_limit, bail, shutdown, closed, error
    suggested_platform: str | None = None  # set when the store looks like another platform
    responses_by_status: dict[int, int] = field(default_factory=dict)
    robots_disallowed: int = 0
    rate_limited_requests: int = 0  # requests that ended on a 429, robots.txt's included
    images_failed: int = 0
    errors: list[str] = field(default_factory=list)
    duration: float = 0.0

    @property
    def failed(self) -> bool:
        return self.status.is_failure

    @property
    def hint(self) -> str | None:
        """Readable advice, e.g. "looks like WooCommerce; pass platform='woocommerce'"."""
        if self.suggested_platform is None:
            return None
        name = _PLATFORM_NAMES.get(self.suggested_platform, self.suggested_platform)
        return f"looks like {name}; pass platform='{self.suggested_platform}'"


@dataclass(frozen=True)
class RunResult:
    """Outcome of a whole run. ``items`` is filled only by ``collect()``/``await``."""

    stores: list[StoreResult]
    finish_reason: str
    items: list[dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0

    @property
    def ok(self) -> list[StoreResult]:
        """Stores that didn't fail (including empty and partial ones)."""
        return [s for s in self.stores if not s.failed]

    @property
    def failed(self) -> list[StoreResult]:
        """Stores whose status is a failure (see ``StoreStatus.is_failure``)."""
        return [s for s in self.stores if s.failed]

    @property
    def total_items(self) -> int:
        """Items produced by all stores."""
        return sum(s.items for s in self.stores)

    def failures(self, strict: bool = False) -> list[StoreResult]:
        """Failed stores; with ``strict=True`` partial stores count too."""
        return [s for s in self.stores if s.failed or (strict and s.status == StoreStatus.partial)]

    def raise_for_status(self, strict: bool = False) -> None:
        """Raise ScrapeFailed if every store failed (with strict=True: if any did)."""
        failed = self.failures(strict)
        if failed and (strict or len(failed) == len(self.stores)):
            raise ScrapeFailed(self, failed)


class ScrapeFailed(Exception):
    """Raised when stores failed; ``result`` holds the full RunResult."""

    def __init__(self, result: RunResult, failed: list[StoreResult] | None = None) -> None:
        self.result = result
        self.failed_stores = failed if failed is not None else result.failed
        lines = [f"{len(self.failed_stores)} of {len(result.stores)} store(s) failed:"]
        for s in self.failed_stores:
            line = f"  {s.input_url}: {s.status.value}"
            if s.hint:
                line += f" ({s.hint})"
            lines.append(line)
        if not result.stores:
            lines = ["no stores were scraped"]
        super().__init__("\n".join(lines))


def diagnose(
    *,
    items: int,
    errored: bool,
    truncated: bool,
    finish_reason: str,
    stats: StoreStats,
) -> StoreStatus:
    """Decide a store's status. Keeps the Scrapy-era CLI's diagnosis order.

    Blocking causes only count if the store never got a 2xx: a 403 recovered by a UA
    swap, or the products.json 404 that triggers the sitemap fallback, isn't why a
    store that also got a 2xx came up empty. A refusal (robots.txt, 403) wins over a
    429, and a 429 over a 404, which may only be a fallback's (a rate-limited
    products.json falls back to the sitemap). 5xx errors count last. A store that
    ran out of time with no items, with a request whose 429 retries didn't get past
    (robots.txt's included: its pause holds the store's requests), is rate limited.
    """
    if errored:
        return StoreStatus.error
    if items > 0:
        return StoreStatus.partial if truncated else StoreStatus.ok
    if stats.successes == 0:
        if stats.robots_disallowed > 0:
            return StoreStatus.robots_blocked
        if stats.by_status[403] > 0:
            return StoreStatus.forbidden
        if stats.by_status[429] > 0:
            return StoreStatus.rate_limited
        if stats.by_status[404] > 0:
            return StoreStatus.not_found
        if any(code >= 500 for code in stats.by_status):
            return StoreStatus.unavailable
    if finish_reason == "bail":
        # Retries and host pauses after a 429 use up --bail, even with some 2xx
        return StoreStatus.rate_limited if stats.rate_limits > 0 else StoreStatus.timed_out
    if finish_reason in ("closed", "shutdown"):
        # Stopped by the caller (break, close(), Ctrl-C), possibly before it started
        return StoreStatus.stopped
    if stats.responses == 0:
        return StoreStatus.tls_error if stats.tls_errors > 0 else StoreStatus.no_response
    return StoreStatus.empty


def run_finish_reason(stores: list[StoreResult]) -> str:
    """Overall finish reason, in the CLI's priority order.

    Unlike the Scrapy CLI, "item_limit" means some store actually hit its limit, not
    that the run's total reached the per-store limit.
    """
    if any(s.finish_reason == "error" for s in stores):
        return "crawler_failed"
    reasons = [s.finish_reason for s in stores]
    if "bail" in reasons:
        return "bail"
    if "item_limit" in reasons:
        return "item_limit"
    if all(r == "finished" for r in reasons):
        return "finished"
    return next((r for r in reasons if r != "finished"), reasons[0] if reasons else "finished")
