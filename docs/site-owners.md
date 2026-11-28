# For site owners

This page is for people who run a store and want Shopify Spy to stay away from it, or to read it more slowly. Shopify Spy obeys robots.txt by default, including `Crawl-delay`. It reads the group for its own product token, `shopify-spy`, whatever User-Agent its requests carry, and falls back to the `*` group when there is no `shopify-spy` group. The examples below go in the store's `/robots.txt`.

## Block it entirely

```
User-agent: shopify-spy
Disallow: /
```

The store then ends as `blocked by robots.txt` without any request beyond robots.txt itself.

## Block some paths

```
User-agent: shopify-spy
Disallow: /products.json
```

Any path prefix works the same way, such as `Disallow: /collections/`. Blocking only part of a catalog can mean more requests rather than fewer: with `/products.json` blocked, a Shopify store is read from its sitemap instead, one request per product. To stop catalog reads, block everything.

A `shopify-spy` group replaces the `*` group for this tool, as robots.txt groups do for any crawler. Repeat the `*` rules you want it to keep inside the `shopify-spy` group.

## Slow it down

```
User-agent: shopify-spy
Crawl-delay: 10
```

`Crawl-delay` works in the `shopify-spy` group or in `*`. It is applied per host as the minimum gap, in seconds, between the starts of two requests, capped at the throttle's maximum delay (60 seconds by default). It applies even when the person running the tool turns its own throttling off.

## What it does by default

Without any robots.txt rules, Shopify Spy:

- waits at least 1 second between page and API requests to a host (0.25 seconds between image downloads), and slows down further when the host's responses get slower;
- backs off on 429 (Too Many Requests) and 503 responses, and waits out a `Retry-After` in full, however long it is, before sending that host anything else;
- gives up on a store that produces nothing within 30 seconds (`--bail`);
- reads robots.txt once per host per run, so a change applies from the next run.

## Limits

robots.txt is honored unless the person running the tool passes `--ignore-robots` (`respect_robots_txt=False` in the library). That is their choice and their responsibility, and robots.txt can't prevent it.

By default, requests carry a current browser's User-Agent rather than a fixed tool name, and the person running the tool can set any other User-Agent. A server rule that matches `shopify-spy` in the User-Agent header therefore won't match these requests, so a robots.txt `shopify-spy` group is the way to opt out. When a custom User-Agent is set, that User-Agent's robots.txt group applies as well as the `shopify-spy` group, and the more restrictive of the two wins: a URL disallowed by either is skipped, and the longer `Crawl-delay` is used.
