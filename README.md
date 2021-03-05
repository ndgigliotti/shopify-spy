# Shopify Spy
Shopify Spy is a simple but powerful Scrapy application for scraping Shopify websites. Its main feature is ShopifySpider, a universal Shopify spider. The spider is designed to extract detailed data from *any* Shopify store, including high-value information like vendor names and inventory levels.

## Usage
ShopifySpider can be used like any Scrapy spider. Set your working directory to the project directory and execute one of the following commands. Arguments must be preceded with the `-a` flag, as is standard for Scrapy. The results will be stored in a JSON lines file in `/resources/ShopifySpider`.

Scrape a single Shopify store:
```shell
scrapy crawl ShopifySpider -a url=https://www.example.com/
```
Scrape multiple Shopify stores at once using a text file with one URL per line:
```shell
scrapy crawl ShopifySpider -a url_file=resources/targets.txt
```
Specify which items to scrape:
```shell
scrapy crawl ShopifySpider -a url=https://www.example.com/ -a products=False -a collections=True
```

## Technologies Used
* [Scrapy](https://docs.scrapy.org/en/latest/index.html)
* [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)

## Limitations
Attempting to scrape a large store may result in a temporary ban. This can be mitigated by configuring the autothrottle settings, which are lax by default.

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License
[MIT](https://choosealicense.com/licenses/mit/)
