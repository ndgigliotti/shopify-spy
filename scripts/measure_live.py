"""Live smoke test and measurements for plan milestone 0: block rate and TLS failures.

    uv run python scripts/measure_live.py

Fetches one item per store with the new engine (robots.txt respected) and checks
whether each store's TLS certificate verifies. Hits the network.
"""

import asyncio
import collections
import ssl
import urllib.parse

import httpx

import shopify_spy as spy

SHOPIFY = [
    "https://www.snowdevil.ca", "https://ugmonk.com", "https://www.beardbrand.com",
    "https://deathwishcoffee.com", "https://shop.lonelyplanet.com", "https://www.leesa.com",
    "https://bombas.com", "https://www.allbirds.com", "https://www.gymshark.com",
    "https://www.kith.com", "https://www.brooklinen.com", "https://www.chubbiesshorts.com",
]  # fmt: skip
WOOCOMMERCE = [
    "https://porterandyork.com", "https://www.nalgene.com", "https://www.storefrontblocks.com",
    "https://woocommerce.com", "https://www.woothemes.com",
]  # fmt: skip


async def first_item(url: str, platform: str) -> spy.StoreResult:
    run = spy.scrape_async(url, platform=platform, max_items_per_store=1, bail=30,
                           on_failure="ignore")  # fmt: skip
    result = await run
    return result.stores[0]


async def tls_check(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.get(f"https://{host}/robots.txt")
        return "ok"
    except httpx.ConnectError as exc:
        cause = exc.__context__ or exc
        if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE" in str(exc):
            return f"verify failed: {getattr(cause, 'verify_message', str(exc))[:80]}"
        return f"connect error: {str(exc)[:60]}"
    except httpx.HTTPError as exc:
        return f"error: {type(exc).__name__}"


async def main() -> None:
    stores = [(u, "shopify") for u in SHOPIFY] + [(u, "woocommerce") for u in WOOCOMMERCE]
    results = await asyncio.gather(*(first_item(u, p) for u, p in stores))
    tls = await asyncio.gather(*(tls_check(u) for u, _ in stores))
    statuses = collections.Counter()
    for (url, platform), result, tls_status in zip(stores, results, tls):
        statuses[result.status.value] += 1
        hint = f" ({result.hint})" if result.hint else ""
        print(f"{platform:12} {url:40} {result.status.value:15} tls={tls_status}{hint}")
    print("\nstatus counts:", dict(statuses))
    print("tls failures:", sum(t.startswith("verify failed") for t in tls), "of", len(tls))


if __name__ == "__main__":
    asyncio.run(main())
