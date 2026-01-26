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
LOG_LEVEL = "INFO"
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
OUTPUT_URI = OUTPUT_DIR.as_uri()

FEEDS = {
    f"{OUTPUT_URI}/%(name)s_%(time)s.jsonl": {
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
