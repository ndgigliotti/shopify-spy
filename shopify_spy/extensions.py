"""Scrapy extensions."""

import logging
import sys
import warnings

from rich.console import Console
from scrapy import signals
from scrapy.exceptions import NotConfigured

logger = logging.getLogger(__name__)


class NoItemTimeout:
    """Close the spider if no items are scraped within a timeout.

    Starts a timer when the spider opens. If no items have been scraped
    after ``NO_ITEM_TIMEOUT`` seconds, the spider is closed. The timer
    is cancelled as soon as the first item is scraped.

    Enable via settings::

        NO_ITEM_TIMEOUT = 20  # seconds; 0 disables
    """

    def __init__(self, timeout, crawler):
        self.timeout = timeout
        self.crawler = crawler
        self._timer = None

    @classmethod
    def from_crawler(cls, crawler):
        timeout = crawler.settings.getint("NO_ITEM_TIMEOUT", 0)
        if not timeout:
            raise NotConfigured
        ext = cls(timeout, crawler)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider):
        from twisted.internet import reactor

        self._timer = reactor.callLater(self.timeout, self._close_spider, spider)
        logger.debug(f"No-item timeout set to {self.timeout}s")

    def item_scraped(self, item, spider):
        if self._timer and self._timer.active():
            self._timer.cancel()
            self._timer = None

    def spider_closed(self, spider):
        if self._timer and self._timer.active():
            self._timer.cancel()
            self._timer = None

    def _close_spider(self, spider):
        logger.warning(f"No items scraped in {self.timeout}s, aborting")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*close_spider.*deprecated.*")
            self.crawler.engine.close_spider(spider, reason="no_item_timeout")


class LiveItemCounter:
    """Display a live item counter on the terminal during scraping.

    Uses a shared mutable list ``_ITEM_COUNTER`` (passed via settings) so
    that all crawler instances contribute to a single total.  The counter
    is printed to stderr with a carriage return so that it overwrites itself
    on each update.

    Enable via settings::

        _ITEM_COUNTER = [0]          # mutable counter; required
        _ITEM_COUNTER_CONSOLE = ...  # optional rich.console.Console
    """

    def __init__(self, counter, console):
        self.counter = counter
        self.console = console

    @classmethod
    def from_crawler(cls, crawler):
        counter = crawler.settings.get("_ITEM_COUNTER")
        if counter is None:
            raise NotConfigured
        console = crawler.settings.get("_ITEM_COUNTER_CONSOLE") or Console(file=sys.stderr)
        ext = cls(counter, console)
        crawler.signals.connect(ext.item_scraped, signal=signals.item_scraped)
        return ext

    def item_scraped(self, item, spider):
        self.counter[0] += 1
        self.console.print(f"  Scraped {self.counter[0]} items...", end="\r")
