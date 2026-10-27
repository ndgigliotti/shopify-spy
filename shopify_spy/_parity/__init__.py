"""Parity tooling for the Scrapy → httpx rewrite (removed after switchover).

``record`` captures every downloader outcome of a legacy (Scrapy) run into a corpus;
``replay`` serves a corpus back to the legacy engine. The new engine replays the
same corpus with ``shopify_spy.fetch.ReplayFetcher``. Enable via
``SCRAPY_SETTINGS_MODULE=shopify_spy._parity.settings`` (see scripts/parity.py).
"""
