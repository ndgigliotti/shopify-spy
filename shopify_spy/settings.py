# -*- coding: utf-8 -*-
from os.path import dirname, abspath, join
from pathlib import PurePath

# Scrapy settings for shopify_spy project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://doc.scrapy.org/en/latest/topics/settings.html
#     https://doc.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://doc.scrapy.org/en/latest/topics/spider-middleware.html

BOT_NAME = "shopify_spy"

SPIDER_MODULES = ["shopify_spy.spiders"]
NEWSPIDER_MODULE = "shopify_spy.spiders"

RESOURCES_DIR = join(dirname(dirname(abspath(__file__))), "resources")
RESOURCES_URI = PurePath(RESOURCES_DIR).as_uri()

# Crawl responsibly by identifying yourself (and your website) on the user-agent
# USER_AGENT = "your_name (+http://www.yourdomain.com)"

# Obey robots.txt rules
ROBOTSTXT_OBEY = True

# Configure maximum concurrent requests performed by Scrapy (default: 16)
# CONCURRENT_REQUESTS = 32

# Configure a delay for requests for the same website (default: 0)
# See https://doc.scrapy.org/en/latest/topics/settings.html#download-delay
# See also autothrottle settings and docs
# DOWNLOAD_DELAY = 3
# The download delay setting will honor only one of:
# CONCURRENT_REQUESTS_PER_DOMAIN = 16
CONCURRENT_REQUESTS_PER_IP = 16

# Disable cookies (enabled by default)
COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
TELNETCONSOLE_ENABLED = False

FEEDS = {
    f"{RESOURCES_URI}/%(name)s/%(time)s.json": {
        "format": "jsonlines",
        "encoding": "utf8",
        "store_empty": False,
        "fields": None,
        "item_export_kwargs": {"export_empty_fields": True},
    },
}


FEED_FORMAT = "jsonlines"
FEED_URI_PARAMS = "shopify_spy.utils.uri_params"

# Configure pipelines
ITEM_PIPELINES = {"scrapy.pipelines.images.ImagesPipeline": 1}
IMAGES_STORE = join(RESOURCES_DIR, "images")
IMAGES_RESULT_FIELD = "scraped_images"

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://doc.scrapy.org/en/latest/topics/autothrottle.html
# AUTOTHROTTLE_ENABLED = True
# The initial download delay
# AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
# AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
# AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
# AUTOTHROTTLE_DEBUG = True

# Enable and configure HTTP caching (disabled by default)
# See https://doc.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
HTTPCACHE_ENABLED = False
HTTPCACHE_EXPIRATION_SECS = 0
HTTPCACHE_DIR = "httpcache"
HTTPCACHE_GZIP = True
