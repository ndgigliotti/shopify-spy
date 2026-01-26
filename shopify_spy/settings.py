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

# Set a user agent that identifies your scraper
# USER_AGENT = "MyCompany (+https://example.com)"

ROBOTSTXT_OBEY = True
CONCURRENT_REQUESTS_PER_DOMAIN = 16
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
RESOURCES_URI = RESOURCES_DIR.as_uri()

FEEDS = {
    f"{RESOURCES_URI}/%(name)s/%(time)s.json": {
        "format": "jsonlines",
        "encoding": "utf8",
        "store_empty": False,
        "fields": None,
        "item_export_kwargs": {"export_empty_fields": True},
    },
}

FEED_URI_PARAMS = "shopify_spy.utils.uri_params"

ITEM_PIPELINES = {"scrapy.pipelines.images.ImagesPipeline": 1}
IMAGES_STORE = str(RESOURCES_DIR / "images")
IMAGES_RESULT_FIELD = "scraped_images"

# Enable AutoThrottle to avoid hitting servers too hard
# https://docs.scrapy.org/en/latest/topics/autothrottle.html
# AUTOTHROTTLE_ENABLED = True
# AUTOTHROTTLE_START_DELAY = 5
# AUTOTHROTTLE_MAX_DELAY = 60
# AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
