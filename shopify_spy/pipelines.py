# -*- coding: utf-8 -*-

# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://doc.scrapy.org/en/latest/topics/item-pipeline.html
from scrapy.exceptions import DropItem


class DuplicateURLPipeline:
    """Drops item if URL has already been seen."""

    def __init__(self):
        self.urls_seen = set()

    def process_item(self, item, spider):
        if item["url"] in self.urls_seen:
            raise DropItem(f"Duplicate item found: {item!r}")
        else:
            self.urls_seen.add(item["url"])
            return item
