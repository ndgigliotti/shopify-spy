from pathlib import Path

# Scrapy settings for shopify_spy project
#
# For more settings see:
# https://docs.scrapy.org/en/latest/topics/settings.html

BOT_NAME = "shopify_spy"

# Use asyncio reactor (default since Scrapy 2.7)
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

SPIDER_MODULES = ["shopify_spy.spiders"]
NEWSPIDER_MODULE = "shopify_spy.spiders"

ROBOTSTXT_OBEY = True
CONCURRENT_REQUESTS_PER_DOMAIN = 16

DOWNLOADER_MIDDLEWARES = {
    # Disable Scrapy's built-in UA middleware; ours replaces it
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
    "shopify_spy.middlewares.UserAgentMiddleware": 560,
    # Next to DownloaderStats (850) so it sees every response, before retry/redirect
    "shopify_spy.middlewares.StoreResponseStats": 860,
}

# Retry on 403 so the UA swap middleware can try a different identity
RETRY_HTTP_CODES = [500, 502, 503, 504, 522, 524, 408, 429, 403]

EXTENSIONS = {
    "shopify_spy.extensions.NoItemTimeout": 500,
}
LOG_LEVEL = "INFO"
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

# Relative to the working directory when the crawl writes, not to the package (which may
# be read-only once installed)
OUTPUT_DIR = Path("output")

# Note: The CLI overrides FEEDS based on the configured output format.
FEEDS = {
    f"{OUTPUT_DIR}/%(name)s_%(time)s.jsonl": {
        "format": "jsonlines",
        "encoding": "utf8",
        "store_empty": False,
        "fields": None,
        "item_export_kwargs": {"export_empty_fields": True},
    },
}


ITEM_PIPELINES = {"scrapy.pipelines.images.ImagesPipeline": 1}
IMAGES_STORE = str(OUTPUT_DIR / "images")
IMAGES_RESULT_FIELD = "scraped_images"

# AutoThrottle - enabled by default for polite scraping
# https://docs.scrapy.org/en/latest/topics/autothrottle.html
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1
AUTOTHROTTLE_MAX_DELAY = 60
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
