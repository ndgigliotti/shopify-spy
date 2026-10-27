"""Browser user agent strings for request spoofing.

They live in user_agents.json, which scripts/update_user_agents.py maintains;
rerun the script instead of editing the JSON by hand.
"""

import json
from importlib.resources import files

_DATA = json.loads(files("shopify_spy").joinpath("user_agents.json").read_text("utf-8"))

USER_AGENTS: list[str] = [agent for group in _DATA["agents"].values() for agent in group]
