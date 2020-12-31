# Shopify Spy
A simple Scrapy project for scraping Shopify websites. The project includes two spiders: ShopifySpider and GoogleSpider. ShopifySpider is a universal Shopify spider, designed to extract data from any Shopify store. GoogleSpider, on the other hand, is designed for extracting Shopify URLs from a Google search, e.g. "board games."

## Usage
The spiders can be used like any standard Scrapy spider. Set your working directory to the project directory and execute one of the following commands. The scrape results will be stored in a JSON lines file in `shopify_spy\resources\SpiderClassName`.

Scrape a single Shopify store:
```bash
scrapy crawl ShopifySpider -a url=https://www.example.com/
```
Scrape multiple Shopify stores at once using a text file with one URL per line:
```bash
scrapy crawl ShopifySpider -a url_file=shopify_spy\resources\targets.txt
```
Scrape Google for Shopify stores in a particular niche:
```bash
scrapy crawl GoogleSpider -a query="board games"
```
## Technologies Used
* [Scrapy](https://docs.scrapy.org/en/latest/index.html)
* [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)

## Limitations
* Attempting to scrape a large store may result in a temporary ban. This can be mitigated by configuring the autothrottle settings, which are lax by default.

* Google does not like being scraped, so heavy use of GoogleSpider will result in a temporary ban.

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)
