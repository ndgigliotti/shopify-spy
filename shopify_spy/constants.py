"""Names shared by the CLI and the Scrapy components.

Kept free of imports so the extension and middlewares don't load the config models,
and the CLI doesn't load Scrapy.
"""

# Spider close reason used by the NoItemTimeout extension (the --bail timer)
NO_ITEM_TIMEOUT_REASON = "no_item_timeout"

# Stats recorded by middlewares.StoreResponseStats for the store's own requests.
# Scrapy's downloader stats also count the robots.txt fetch, its retries and redirects.
STORE_RESPONSE_COUNT = "shopify_spy/store_response_count"
STORE_RESPONSE_STATUS_COUNT = "shopify_spy/store_response_status_count/{}"
