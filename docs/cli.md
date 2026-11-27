# Command line

The CLI has two commands: `scrape`, which does the work, and `init`, which writes a config file. `spy --version` (`-V`) prints the installed version, and `spy scrape --help` (or `-h`) lists every option with its default, in the groups below. Piped, as coding agents run it, `--help` is plain unwrapped text; in a terminal it renders in panels.

## `scrape`

```bash
spy scrape [URL...] [OPTIONS]
```

Give one or more store URLs, or a file of them with `--url-file`. Malformed URLs are skipped with a warning and recorded in the [status file](troubleshooting.md#the-status-file). Results go to `./output` as JSONL unless you say otherwise; [Output](output.md) describes the files.

```bash
# Scrape a Shopify store (the default platform)
spy scrape https://www.example.com

# Scrape a WooCommerce store
spy scrape --platform woocommerce https://www.example.com

# Several stores, from arguments or a file
spy scrape https://store1.com https://store2.com
spy scrape --url-file stores.txt

# Include collections and download images
spy scrape https://www.example.com --collections --images

# Sample 10 items into a directory of your choice
spy scrape https://www.example.com --limit 10 --output ./my-data

# Print one item to stdout and write nothing
spy scrape --peek https://www.example.com 2>/dev/null | jq '.product.title'
```

### What to scrape

- `URL...`: one or more store URLs (optional with `--url-file`).
- `--url-file, -f FILE`: a file of URLs, one per line; lines starting with `#` are comments.
- `--platform, -p PLATFORM`: `shopify` (the default) or `woocommerce`.
- `--products / --no-products`: scrape products (default: yes; Shopify only).
- `--collections / --no-collections`: scrape collections (default: no; Shopify only).
- `--images / --no-images`: download product images into `<output>/images/full/` (default: no). Downloads from one host are at least `--image-delay` apart while throttling. See [Images](output.md#images).
- `--limit, -n INT`: stop each store after N items. The limit is per store, which is useful for sampling or testing.

### Output

- `--output, -o PATH`: the output directory (default: `./output`).
- `--format, -F FORMAT`: `jsonl` (the default), `json`, `csv`, `xlsx` or `xml`. `xlsx` needs the `excel` extra; see [Excel output](output.md#excel-output).
- `--schema SCHEMA`: `native` (items as scraped, the default) or `unified` (a row per variant). CSV and XLSX are always unified. See [Schemas](output.md#schemas).
- `--peek`: print one item to stdout as JSONL and exit, creating no files or directories. With `--schema unified`, it prints that item's unified rows. Warnings and errors go to stderr, so stdout stays valid JSONL.

### Network

- `--concurrent INT`: concurrent requests per host (default: 16).
- `--max-stores INT`: stores scraped at once (default: 16).
- `--throttle / --no-throttle`: auto-throttle requests (default: yes). `--no-throttle` drops the adaptive delay, `--min-delay` and `--image-delay`, but a robots.txt `Crawl-delay` and `Retry-After` pauses still apply. See [Rate limiting](troubleshooting.md#rate-limiting).
- `--min-delay SECONDS`: the minimum gap between page and API requests to one host while throttling (default: 1).
- `--image-delay SECONDS`: the minimum gap between image downloads from one host while throttling, the store's own host included (default: 0.25).
- `--user-agent, -A TEXT`: a fixed User-Agent header, never swapped (default: a random browser user agent per store, swapped after a 403 or 429).
- `--proxy URL`: send every request through this proxy; see [Proxies](#proxies).
- `--insecure`: don't verify TLS certificates.
- `--ignore-robots, -i`: ignore robots.txt restrictions.

### Run control

- `--bail INT`: abort a store if it scrapes no items within N seconds (default: 30; 0 turns it off). Time spent waiting out a server's `Retry-After` doesn't count, so a rate-limited store can take much longer to fail; see [Rate limiting](troubleshooting.md#rate-limiting).
- `--config, -c FILE`: a YAML config file; see [Configuration](#configuration).
- `--verbose, -v`: show debug output, logging to both the log file and the terminal.
- `--quiet, -q`: show only warnings and errors, with no live item counter. The log file still gets the details.

When a run ends, the CLI's [exit status](troubleshooting.md#exit-status) says whether any store produced items, and the [status file](troubleshooting.md#the-status-file) says how each store did.

## `init`

```bash
spy init [PATH] [--force]
```

`init` writes a default config file to `PATH` (default: `./shopify-spy.yaml`). It refuses to overwrite an existing file unless you pass `--force` (`-f`).

## Configuration

A YAML config file sets defaults for `scrape`, and options on the command line override it. `--config` names the file, which must exist. Without it, the CLI reads the first of these that exists:

1. `./shopify-spy.yaml`
2. `~/.config/shopify-spy/config.yaml`

A config file looks like this, and `spy init` writes one to start from:

```yaml
# shopify-spy.yaml
scrape:
  platform: shopify   # Platform: shopify, woocommerce
  products: true      # Scrape product data (Shopify only)
  collections: false  # Scrape collection data (Shopify only)
  images: false       # Download product images
  # limit: 100        # Stop each store after N items
  bail: 30            # Abort if no items scraped within N seconds (0 = off)

output:
  dir: ./output       # Output directory for results
  format: jsonl       # Output format: json, jsonl, csv, xlsx, xml
  # schema: unified   # native (as scraped) or unified (row per variant; csv and xlsx always)
  images_subdir: images  # Subdirectory for downloaded images

network:
  concurrent_requests: 16  # Concurrent requests per host
  timeout: 180             # Request timeout (seconds)
  retries: 2               # Retry failed requests
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent
  # proxy: http://proxy.example.com:8080  # Fixed proxy (overrides HTTPS_PROXY)
  respect_robots_txt: true
  verify_tls: true         # Verify TLS certificates (false = --insecure)
  max_concurrent_stores: 16  # Stores scraped at once

throttle:
  enabled: true            # Auto-throttle based on server response
  start_delay: 1           # Initial download delay (seconds)
  max_delay: 60            # Maximum download delay (seconds)
  min_delay: 1             # Minimum seconds between requests to one host
  image_delay: 0.25        # Minimum seconds between image downloads from one host
  target_concurrency: 1.0  # Target concurrent requests (higher = faster)
```

`network.timeout`, `network.retries`, `output.images_subdir` and the `throttle` settings other than `enabled`, `min_delay` and `image_delay` have no command-line option, so the config file is the only way to change them from the CLI. The library reads no config file; it takes the same settings as [options](library.md#options).

## Proxies

Requests honor the `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY` environment variables. To set one fixed proxy instead, use `--proxy URL`, `network.proxy` in the config file, or `proxy=` in the library. A fixed proxy overrides those variables, `NO_PROXY` included. `http://`, `https://` and `socks5://` proxies all work out of the box.

- Credentials in the URL are masked in logs and errors (`http://USER:***@gateway:PORT`).
- `--proxy` takes credentials as typed. In the environment variables, a `/`, `?` or `#` in them must be percent-encoded (`%2F`, `%3F`, `%23`).
- To keep a password out of shell history and process lists, or out of a coding agent's transcript, set it in the environment before starting the shell or agent rather than passing it to `--proxy`.
- Through a proxy, only literal IP addresses and `localhost` names are checked against the [non-public address rule](troubleshooting.md#refused-requests-to-non-public-addresses), since the proxy resolves hostnames itself.
- A proxy is for a fixed egress, such as a company network or another region. It is not a way around a store's rate limits, which in practice key on user agent or endpoint as much as on IP.

A proxy's 407 (refused credentials) counts as [`no_response`](troubleshooting.md#store-statuses). In the library, a store that never answers while a proxy is in use gets a `hint` saying to check the proxy.
