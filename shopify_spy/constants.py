"""Names shared by the CLI and the Scrapy components.

Kept free of imports so the extension and middlewares don't load the config models,
and the CLI doesn't load Scrapy.
"""

# Spider close reason used by the NoItemTimeout extension (the --bail timer)
NO_ITEM_TIMEOUT_REASON = "no_item_timeout"

# Stats recorded by middlewares.StoreResponseStats for the spider's own requests.
# Scrapy's downloader stats also count robots.txt and image downloads, with their
# retries and redirect hops.
STORE_RESPONSE_COUNT = "shopify_spy/store_response_count"
STORE_SUCCESS_COUNT = "shopify_spy/store_success_count"  # 2xx responses
STORE_RESPONSE_STATUS_COUNT = "shopify_spy/store_response_status_count/{}"
