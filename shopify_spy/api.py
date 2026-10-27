"""Public scraping API: ``scrape()`` for scripts, ``scrape_async()`` for async code.

Both are thin entry points over the same async core (runner.py). ``scrape()`` drives
a private event loop on the caller's thread, one item at a time; it can't run
inside an already-running loop (Jupyter, async code), where ``scrape_async()``
works instead.
"""

from __future__ import annotations

import asyncio
import sys
import time
import warnings
import weakref
from collections.abc import Callable, Generator, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shopify_spy.policy import ThrottleSettings
from shopify_spy.results import RunResult, ScrapeFailed, StoreResult, run_finish_reason
from shopify_spy.runner import RunConfig, Runner
from shopify_spy.utils import is_valid_url, normalize_url

RUNNING_LOOP_ERROR = (
    "An event loop is already running (e.g. Jupyter or async code). "
    "Use `async for item in scrape_async(...)` instead."
)


class ScrapeWarning(UserWarning):
    """Issued by ``on_failure="warn"`` when stores failed."""


class ScrapeOptions(BaseModel):
    """Options shared by ``scrape()`` and ``scrape_async()``; see their docstrings."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    platform: Literal["shopify", "woocommerce"] = "shopify"
    products: bool = True
    collections: bool = False
    images: bool = False
    images_dir: Path | None = None
    max_items_per_store: int | None = Field(default=None, ge=1)
    bail: float | None = Field(default=30.0, ge=0)
    concurrent_requests: int = Field(default=16, ge=1)
    request_timeout: float = Field(default=180.0, gt=0)
    retries: int = Field(default=2, ge=0)
    user_agent: str | None = None
    respect_robots_txt: bool = True
    verify_tls: bool = True
    max_concurrent_stores: int | None = Field(default=16, ge=1)
    throttle: bool = True
    throttle_start_delay: float = Field(default=1.0, ge=0)
    throttle_max_delay: float = Field(default=60.0, ge=0)
    throttle_target_concurrency: float = Field(default=1.0, ge=0.1)
    on_failure: Literal["raise", "warn", "ignore"] = "raise"
    strict: bool = False
    fetcher_factory: Callable[[], Any] | None = None  # provisional


def _prepare(
    urls: str | Iterable[str], options: dict[str, Any]
) -> tuple[list[tuple[str, str]], ScrapeOptions, bool]:
    backoff = options.pop("_backoff", True)
    if options.get("platform") == "auto":
        raise ValueError("auto-detection isn't supported yet; use 'shopify' or 'woocommerce'")
    try:
        opts = ScrapeOptions(**options)
    except ValidationError as exc:
        raise ValueError(str(exc)) from None
    if opts.images and opts.images_dir is None:
        raise ValueError(
            "images=True needs images_dir, e.g. scrape(url, images=True, images_dir='images')"
        )
    if opts.images_dir is not None:
        opts.images_dir = opts.images_dir.expanduser().resolve()
    return _prepare_urls(urls), opts, backoff


def _prepare_urls(urls: str | Iterable[str]) -> list[tuple[str, str]]:
    """Validate URLs; return unique (input URL, store URL) pairs in order."""
    if isinstance(urls, str):
        text = urls.strip()
        if "," in text or any(c.isspace() for c in text):
            raise ValueError(
                f"{urls!r} looks like several URLs; pass a list, e.g. ['a.com', 'b.com']"
            )
        urls = [text]
    inputs = []
    for u in urls:
        if not isinstance(u, str):
            raise TypeError(f"URLs must be strings, got {type(u).__name__}")
        if u.strip():
            inputs.append(u.strip())
    if not inputs:
        raise ValueError("no URLs given")
    invalid = [u for u in inputs if not is_valid_url(u)]
    if invalid:
        raise ValueError(f"invalid URL(s): {', '.join(invalid)}")
    stores: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in inputs:
        parsed = normalize_url(u)
        store = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if store not in seen:
            seen.add(store)
            stores.append((u, store))
    return stores


def _config(opts: ScrapeOptions, backoff: bool) -> RunConfig:
    return RunConfig(
        platform=opts.platform,
        products=opts.products,
        collections=opts.collections,
        images=opts.images,
        images_dir=opts.images_dir,
        max_items_per_store=opts.max_items_per_store,
        bail=opts.bail or None,
        concurrent_requests=opts.concurrent_requests,
        request_timeout=opts.request_timeout,
        retries=opts.retries,
        user_agent=opts.user_agent,
        respect_robots_txt=opts.respect_robots_txt,
        verify_tls=opts.verify_tls,
        max_concurrent_stores=opts.max_concurrent_stores,
        throttle=ThrottleSettings(
            enabled=opts.throttle,
            start_delay=opts.throttle_start_delay,
            max_delay=opts.throttle_max_delay,
            target_concurrency=opts.throttle_target_concurrency,
        ),
        backoff=backoff,
        fetcher_factory=opts.fetcher_factory,
    )


class _BaseRun:
    def __init__(self, stores: list[tuple[str, str]], opts: ScrapeOptions, backoff: bool):
        self._stores = stores
        self._opts = opts
        self._config = _config(opts, backoff)
        self._runner: Runner | None = None
        self._result: RunResult | None = None
        self._iterated = False
        self._started = time.monotonic()
        self._shutdown_requested = False

    @property
    def stores(self) -> list[StoreResult]:
        """A snapshot of every store's result so far (safe to read mid-run)."""
        if self._result is not None:
            return list(self._result.stores)
        if self._runner is None:
            return []
        return self._runner.results()

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> RunResult:
        """The run's RunResult; available once iteration ended (or after a break)."""
        if self._result is None:
            raise RuntimeError("iterate to completion or call collect()")
        return self._result

    def request_shutdown(self) -> None:
        """Stop gracefully: no new requests; in-flight ones finish and their items still
        arrive; iteration then ends normally with stores marked "shutdown"."""
        self._shutdown_requested = True
        if self._runner is not None:
            self._runner.request_shutdown()

    def _start_runner(self) -> Runner:
        runner = Runner(self._stores, self._config)
        runner.start()
        if self._shutdown_requested:
            runner.request_shutdown()
        self._runner = runner
        return runner

    def _finish(self, items: list[dict[str, Any]] | None = None) -> RunResult:
        if self._result is None:
            stores = self._runner.results() if self._runner else []
            self._result = RunResult(
                stores=stores,
                finish_reason=run_finish_reason(stores),
                duration=time.monotonic() - self._started,
            )
        if items is not None:
            self._result = RunResult(
                stores=self._result.stores,
                finish_reason=self._result.finish_reason,
                items=items,
                duration=self._result.duration,
            )
        return self._result

    def _check_failure(self) -> None:
        result = self.result
        failed = result.failures(self._opts.strict)
        if not failed or not (self._opts.strict or len(failed) == len(result.stores)):
            return
        if self._opts.on_failure == "raise":
            raise ScrapeFailed(result, failed)
        if self._opts.on_failure == "warn":
            warnings.warn(str(ScrapeFailed(result, failed)), ScrapeWarning, stacklevel=4)

    def _claim(self) -> None:
        if self._iterated:
            raise RuntimeError("a run can only be iterated once; call scrape() again")
        self._iterated = True


class _PausedClockLoop(
    asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop  # type: ignore[misc]
):
    """An event loop whose clock stops while the loop isn't running.

    The sync API pauses the loop between items while the caller works; without this,
    timers (request timeouts, bail, throttle) would be overdue when it resumes.
    """

    def __init__(self) -> None:
        super().__init__()
        self._paused_total = 0.0
        self._paused_since: float | None = None

    def time(self) -> float:
        return super().time() - self._paused_total

    def run_until_complete(self, future: Any) -> Any:
        if self._paused_since is not None:
            self._paused_total += time.monotonic() - self._paused_since
            self._paused_since = None
        try:
            return super().run_until_complete(future)
        finally:
            self._paused_since = time.monotonic()


def _ensure_no_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(RUNNING_LOOP_ERROR)


class ScrapeRun(_BaseRun):
    """A scrape in progress (sync). Iterate it, or call ``collect()`` or ``first()``.

    Use as a context manager (or let iteration finish) so an early exit cleans up.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        # Weak, so a bare `for ...: break` drops the last reference and the
        # generator's cleanup runs right away
        self._gen: weakref.ref[Generator[dict[str, Any]]] | None = None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        self._claim()
        gen = self._iterate()
        self._gen = weakref.ref(gen)
        return gen

    def _iterate(self) -> Generator[dict[str, Any]]:
        _ensure_no_running_loop()

        async def start() -> Runner:
            return self._start_runner()

        with asyncio.Runner(loop_factory=_PausedClockLoop) as loop_runner:
            runner = loop_runner.run(start())
            try:
                while True:
                    try:
                        item = loop_runner.run(runner.next())
                    except StopAsyncIteration:
                        break
                    yield item
            finally:
                loop_runner.run(runner.close())
                self._finish()
        self._check_failure()

    def close(self) -> None:
        """Stop the run now if it is still going; unfinished stores end as "closed"."""
        gen = self._gen() if self._gen is not None else None
        if gen is not None:
            gen.close()

    def __enter__(self) -> ScrapeRun:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def collect(self) -> RunResult:
        """Run to completion and return a RunResult including all items.

        Items are held in memory; for large catalogs iterate instead, or pass the run
        to ``write_items()``.
        """
        items = list(self)
        return self._finish(items)

    def first(self) -> dict[str, Any]:
        """Return the first item and stop the run.

        Raises ScrapeFailed (with per-store diagnostics) if no store yields an item.
        Note that a product URL is reduced to its store, so this is the store's first
        item, not that product.
        """
        it = iter(self)
        try:
            return next(it)
        except StopIteration:
            raise ScrapeFailed(self.result, self.result.stores) from None
        finally:
            self.close()


class AsyncScrapeRun(_BaseRun):
    """A scrape in progress (async). ``async for``, ``async with``, or ``await`` it."""

    def __aiter__(self) -> AsyncScrapeRun:
        self._claim()
        return self

    async def __anext__(self) -> dict[str, Any]:
        runner = self._runner or self._start_runner()
        try:
            return await runner.next()
        except StopAsyncIteration:
            self._finish()
            self._check_failure()
            raise

    async def aclose(self) -> None:
        """Stop the run now if it is still going; unfinished stores end as "closed"."""
        if self._runner is not None:
            try:
                await self._runner.close()
            finally:
                self._finish()

    async def __aenter__(self) -> AsyncScrapeRun:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def collect(self) -> RunResult:
        """Run to completion and return a RunResult including all items."""
        items = [item async for item in self]
        return self._finish(items)

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.collect().__await__()

    async def first(self) -> dict[str, Any]:
        """Return the first item and stop the run; raise ScrapeFailed if there is none."""
        try:
            async for item in self:
                return item
        finally:
            await self.aclose()
        raise ScrapeFailed(self.result, self.result.stores)


_SCRAPE_DOC = """

    Args:
        urls: A store URL or an iterable of them. Only the host matters; paths are
            ignored. One platform per call.
        platform: "shopify" (default) or "woocommerce".
        products: Scrape products (Shopify only; WooCommerce always does).
        collections: Also scrape collections (Shopify only).
        images: Download product images into ``images_dir/full/``. Requires
            ``images_dir``. Each item gains ``scraped_images`` records whose
            ``path`` is relative to ``images_dir``.
        max_items_per_store: Stop each store after N items. This is per store; use
            ``itertools.islice`` for a total cap.
        bail: Give up on a store that yields no item within this many seconds
            (None or 0 disables).
        concurrent_requests: Max concurrent requests per host across the run.
        request_timeout: Seconds allowed for one request (not the whole run).
        retries: Retries for 403/429/5xx responses and network errors.
        user_agent: Fixed User-Agent; by default a random browser UA is used and
            swapped after a 403.
        respect_robots_txt: Obey robots.txt (default True).
        verify_tls: Verify TLS certificates (default True).
        max_concurrent_stores: Stores scraped at once (None = no limit).
        throttle, throttle_start_delay, throttle_max_delay,
        throttle_target_concurrency: AutoThrottle settings (on by default).
        on_failure: What to do when stores fail, checked once iteration ends:
            "raise" (default) raises ScrapeFailed if every store failed; "warn"
            issues a ScrapeWarning; "ignore" leaves it to ``run.result``.
        strict: Count any failed (or partial) store, not only all of them.
        fetcher_factory: Provisional. A callable returning a custom transport.

    Failures: a store fails when it is blocked (403, robots.txt), not found, times
    out, can't be reached, or errors; ``run.result.failed`` lists them with a
    status and, sometimes, a ``hint`` (e.g. a likely wrong ``platform``). An empty
    store is not a failure. Invalid URLs and options raise ValueError at call time.

    Nothing is written to disk unless ``images=True``, and no config file is read.
"""


def scrape(urls: str | Iterable[str], **options: Any) -> ScrapeRun:
    """Scrape stores; iterate the returned run to get items (dicts) as they arrive.

    Example::

        import shopify_spy as spy
        for item in spy.scrape("example.com", max_items_per_store=100):
            print(item["product"]["title"])

    Iterating it inside a running event loop (Jupyter, async code) raises
    RuntimeError; use ``scrape_async`` there.
    """
    stores, opts, backoff = _prepare(urls, options)
    return ScrapeRun(stores, opts, backoff)


def scrape_async(urls: str | Iterable[str], **options: Any) -> AsyncScrapeRun:
    """Async version of ``scrape()``: ``async for item in scrape_async(...)``.

    ``await scrape_async(...)`` returns a RunResult with all items. Takes the same
    options as ``scrape()``.
    """
    stores, opts, backoff = _prepare(urls, options)
    return AsyncScrapeRun(stores, opts, backoff)


scrape.__doc__ = (scrape.__doc__ or "") + _SCRAPE_DOC
scrape_async.__doc__ = (scrape_async.__doc__ or "") + _SCRAPE_DOC
