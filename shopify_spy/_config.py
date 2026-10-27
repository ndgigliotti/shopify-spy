"""YAML configuration for the CLI: Pydantic models and loading.

The library never reads config files; the CLI maps a Config to ``scrape()`` options
with ``Config.to_kwargs()``.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

OutputFormat = Literal["json", "jsonl", "csv", "xml"]


class Platform(StrEnum):
    shopify = "shopify"
    woocommerce = "woocommerce"


class ScrapeConfig(BaseModel):
    """Configuration for what to scrape."""

    platform: Platform = Platform.shopify
    products: bool = True
    collections: bool = False
    images: bool = False
    limit: int | None = Field(default=None, ge=1)
    bail: int = Field(default=30, ge=0)


class OutputConfig(BaseModel):
    """Configuration for output paths."""

    dir: Path = Field(default=Path("./output"))
    format: OutputFormat = "jsonl"
    images_subdir: str = "images"

    @property
    def images_dir(self) -> Path:
        """Full path to images directory."""
        return self.dir / self.images_subdir


class NetworkConfig(BaseModel):
    """Configuration for network settings."""

    concurrent_requests: int = Field(default=16, ge=1)
    timeout: int = Field(default=180, ge=1)
    retries: int = Field(default=2, ge=0)
    user_agent: str | None = None  # None = random browser UA, swapped after a 403
    respect_robots_txt: bool = True
    verify_tls: bool = True
    max_concurrent_stores: int | None = Field(default=16, ge=1)  # None = no limit


class ThrottleConfig(BaseModel):
    """Configuration for auto-throttling."""

    enabled: bool = True
    start_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=60.0, ge=0)
    target_concurrency: float = Field(default=1.0, ge=0.1)


class Config(BaseModel):
    """Root configuration model."""

    scrape: ScrapeConfig = Field(default_factory=ScrapeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    throttle: ThrottleConfig = Field(default_factory=ThrottleConfig)

    def to_kwargs(self) -> dict[str, Any]:
        """Map this config to ``scrape()``/``scrape_async()`` keyword arguments."""
        return {
            "platform": self.scrape.platform.value,
            "products": self.scrape.products,
            "collections": self.scrape.collections,
            "images": self.scrape.images,
            "images_dir": self.output.images_dir.expanduser() if self.scrape.images else None,
            "max_items_per_store": self.scrape.limit,
            "bail": self.scrape.bail,
            "concurrent_requests": self.network.concurrent_requests,
            "request_timeout": self.network.timeout,
            "retries": self.network.retries,
            "user_agent": self.network.user_agent,
            "respect_robots_txt": self.network.respect_robots_txt,
            "verify_tls": self.network.verify_tls,
            "max_concurrent_stores": self.network.max_concurrent_stores,
            "throttle": self.throttle.enabled,
            "throttle_start_delay": self.throttle.start_delay,
            "throttle_max_delay": self.throttle.max_delay,
            "throttle_target_concurrency": self.throttle.target_concurrency,
        }


# Default config file locations (searched in order)
CONFIG_SEARCH_PATHS = [
    Path("./shopify-spy.yaml"),
    Path("~/.config/shopify-spy/config.yaml").expanduser(),
]

# Default config YAML template
DEFAULT_CONFIG_YAML = """\
# Shopify Spy configuration
# See https://github.com/ndgigliotti/shopify-spy for documentation

scrape:
  platform: shopify   # Platform: shopify, woocommerce
  products: true      # Scrape product data (Shopify only)
  collections: false  # Scrape collection data (Shopify only)
  images: false       # Download product images
  bail: 30 # Abort if no items scraped within N seconds (0 = off)

output:
  dir: ./output       # Output directory for results
  format: jsonl       # Output format: json, jsonl, csv, xml
  images_subdir: images  # Subdirectory for downloaded images

network:
  concurrent_requests: 16  # Concurrent requests per host
  timeout: 180             # Request timeout (seconds)
  retries: 2               # Retry failed requests
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent
  respect_robots_txt: true
  verify_tls: true         # Verify TLS certificates (false = --insecure)
  max_concurrent_stores: 16  # Stores scraped at once

throttle:
  enabled: true            # Auto-throttle based on server response
  start_delay: 1           # Initial download delay (seconds)
  max_delay: 60            # Maximum download delay (seconds)
  target_concurrency: 1.0  # Target concurrent requests (higher = faster)
"""


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from YAML file.

    Search order:
    1. Explicit path (if provided)
    2. ./shopify-spy.yaml
    3. ~/.config/shopify-spy/config.yaml

    Returns default config if no file found.
    """
    paths_to_check: list[Path] = []

    if config_path:
        paths_to_check.append(config_path)
    paths_to_check.extend(CONFIG_SEARCH_PATHS)

    for path in paths_to_check:
        resolved = path.expanduser().resolve()
        if resolved.exists():
            return load_config_from_file(resolved)

    return Config()


def load_config_from_file(path: Path) -> Config:
    """Load and validate configuration from a specific file."""
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        return Config()

    return Config.model_validate(data)


def create_default_config(path: Path | None = None) -> Path:
    """Create a default configuration file.

    Args:
        path: Where to create the file. Defaults to ./shopify-spy.yaml

    Returns:
        Path to the created file.
    """
    if path is None:
        path = Path("./shopify-spy.yaml")

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_YAML)
    return path
