"""Browser user agent strings for request spoofing.

They live in user_agents.json, which scripts/update_user_agents.py maintains;
rerun the script instead of editing the JSON by hand.
"""

import json
from pathlib import Path

# A plain file read: importlib.resources would add ~4 ms to `import shopify_spy`
_DATA = json.loads(Path(__file__).with_name("user_agents.json").read_text("utf-8"))

USER_AGENTS: list[str] = [agent for group in _DATA["agents"].values() for agent in group]
