"""Scrapy downloader middlewares."""

import logging
import random

from shopify_spy.user_agents import USER_AGENTS

logger = logging.getLogger(__name__)

# Swap user agent only when the server actively rejects us
SWAP_ON_STATUSES = frozenset({403})


class UserAgentMiddleware:
    """Sets a random browser user agent, swapping on 403.

    Picks one UA at spider start and reuses it for all requests. When a
    request is retried after a 403 (Forbidden), a different UA is chosen
    and becomes the new default for subsequent requests. Other retry
    reasons (500, timeout, etc.) keep the current UA.
    """

    def __init__(self):
        self.current_ua = random.choice(USER_AGENTS)

    def process_request(self, request):
        if request.meta.get("retry_reason") in SWAP_ON_STATUSES:
            previous = self.current_ua
            while self.current_ua == previous and len(USER_AGENTS) > 1:
                self.current_ua = random.choice(USER_AGENTS)
            logger.debug(f"Swapped user agent after 403: {self.current_ua}")
        request.headers["User-Agent"] = self.current_ua

    def process_response(self, request, response):
        if response.status in SWAP_ON_STATUSES:
            request.meta["retry_reason"] = response.status
        return response
