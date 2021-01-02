from scrapy.exceptions import DropItem


class DuplicateURLPipeline:
    def __init__(self):
        self.urls_seen = set()

    def process_item(self, item, spider):
        if item["url"] in self.urls_seen:
            raise DropItem(f"Duplicate item found: {item!r}")
        else:
            self.urls_seen.add(item["url"])
            return item
