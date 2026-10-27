"""Scrapy settings for parity runs: the project settings plus record/replay.

Selected with ``SCRAPY_SETTINGS_MODULE=shopify_spy._parity.settings``, which also
makes the legacy CLI load the project settings from any working directory.
``SHOPIFY_SPY_SEED`` seeds the user-agent choice so runs are repeatable.
"""

import os
import random

from shopify_spy.settings import *  # noqa: F403
from shopify_spy.settings import DOWNLOADER_MIDDLEWARES as _BASE

if os.environ.get("SHOPIFY_SPY_SEED"):
    random.seed(int(os.environ["SHOPIFY_SPY_SEED"]))

DOWNLOADER_MIDDLEWARES = dict(_BASE)
if os.environ.get("SHOPIFY_SPY_RECORD"):
    DOWNLOADER_MIDDLEWARES["shopify_spy._parity.middlewares.RecordMiddleware"] = 990
if os.environ.get("SHOPIFY_SPY_REPLAY"):
    DOWNLOADER_MIDDLEWARES["shopify_spy._parity.middlewares.ReplayMiddleware"] = 990
