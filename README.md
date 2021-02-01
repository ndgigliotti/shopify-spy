# Shopify Spy
A Scrapy project for scraping Shopify websites. The project includes two spiders: ShopifySpider and DiscoverySpider. ShopifySpider is a universal Shopify spider, designed to extract detailed data&mdash;including high-value information like **vendor names** and **inventory levels**&mdash;from any Shopify store. DiscoverySpider is a supplementary tool for discovering Shopify sites using Google.

## Usage
The spiders can be used like any standard Scrapy spider. Set your working directory to the project directory and execute one of the following commands. The scrape results will be stored in a JSON lines file in `shopify_spy\resources\SpiderClassName`.

Scrape a single Shopify store:
```cmd
scrapy crawl ShopifySpider -a url=https://www.example.com/
```
Scrape multiple Shopify stores at once using a text file with one URL per line:
```cmd
scrapy crawl ShopifySpider -a url_file=shopify_spy\resources\targets.txt
```
Specify which items to scrape:
```cmd
scrapy crawl ShopifySpider -a url=https://www.example.com/ -a products=False -a collections=True
```
Scrape Google for Shopify stores in a particular niche:
```cmd
scrapy crawl DiscoverySpider -a query="board games"
```
## Technologies Used
* [Scrapy](https://docs.scrapy.org/en/latest/index.html)
* [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)

## Limitations
* Attempting to scrape a large store may result in a temporary ban. This can be mitigated by configuring the autothrottle settings, which are lax by default.

* Google does not like being scraped. Heavy use of DiscoverySpider may result in a temporary ban or worse.

## Contributing
Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License
[MIT](https://choosealicense.com/licenses/mit/)
