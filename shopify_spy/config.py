"""YAML configuration loading and Pydantic models."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ScrapeConfig(BaseModel):
    """Configuration for what to scrape."""

    products: bool = True
    collections: bool = False
    images: bool = False


class OutputConfig(BaseModel):
    """Configuration for output paths."""

    dir: Path = Field(default=Path("./output"))
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
    user_agent: str | None = None  # None = Scrapy default
    respect_robots_txt: bool = True


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
  products: true      # Scrape product data
  collections: false  # Scrape collection data
  images: false       # Download product images

output:
  dir: ./output       # Output directory for results
  images_subdir: images  # Subdirectory for downloaded images

network:
  concurrent_requests: 16  # Concurrent requests per domain
  timeout: 180             # Download timeout (seconds)
  retries: 2               # Retry failed requests
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent
  respect_robots_txt: true

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
