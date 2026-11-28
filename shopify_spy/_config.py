"""YAML configuration for the CLI: Pydantic models and loading.

The library never reads config files; the CLI maps a Config to ``scrape()`` options
with ``Config.to_kwargs()``.
"""

import difflib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from shopify_spy._fetch import validate_proxy
from shopify_spy._utils import blank_to_none

OutputFormat = Literal["json", "jsonl", "csv", "xlsx", "xml"]
OutputSchema = Literal["native", "unified"]


class Platform(StrEnum):
    shopify = "shopify"
    woocommerce = "woocommerce"
    magento = "magento"
    bigcommerce = "bigcommerce"
    generic = "generic"
    squarespace = "squarespace"


class _Section(BaseModel):
    # A misspelled key would otherwise be dropped, silently undoing what it set
    model_config = ConfigDict(extra="forbid")


class ScrapeConfig(_Section):
    """Configuration for what to scrape."""

    platform: Platform = Platform.shopify
    products: bool = True
    collections: bool = False
    images: bool = False
    limit: int | None = Field(default=None, ge=1)
    bail: int = Field(default=30, ge=0)


class OutputConfig(_Section):
    """Configuration for output paths."""

    # "schema" would shadow a BaseModel attribute; the YAML key is still "schema"
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dir: Path = Field(default=Path("./output"))
    format: OutputFormat = "jsonl"
    schema_: OutputSchema | None = Field(default=None, alias="schema")  # None = format's
    images_subdir: str = "images"

    @property
    def images_dir(self) -> Path:
        """Full path to images directory."""
        return self.dir / self.images_subdir


class NetworkConfig(_Section):
    """Configuration for network settings."""

    concurrent_requests: int = Field(default=16, ge=1)
    timeout: int = Field(default=180, ge=1)
    retries: int = Field(default=2, ge=0)
    user_agent: str | None = None  # Set: plain requests with exactly this UA, never swapped
    respect_robots_txt: bool = True
    verify_tls: bool = True
    max_concurrent_stores: int | None = Field(default=16, ge=1)  # None = no limit
    proxy: str | None = None  # None = HTTPS_PROXY/HTTP_PROXY/ALL_PROXY from the environment
    # First browser profile (None = chrome), checked when the run starts
    impersonate: str | None = None
    rotate: bool = True  # Switch browser profile after a 403 or 429

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        return None if value is None else validate_proxy(value)

    @field_validator("user_agent")
    @classmethod
    def _blank_user_agent(cls, value: str | None) -> str | None:
        return blank_to_none(value)


class ThrottleConfig(_Section):
    """Configuration for auto-throttling."""

    enabled: bool = True
    start_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=60.0, ge=0)
    min_delay: float = Field(default=1.0, ge=0)
    image_delay: float = Field(default=0.25, ge=0)
    jitter: float = Field(default=0.5, ge=0, le=1)
    target_concurrency: float = Field(default=1.0, ge=0.1)


class Config(_Section):
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
            "proxy": self.network.proxy,
            "impersonate": self.network.impersonate,
            "rotate": self.network.rotate,
            "throttle": self.throttle.enabled,
            "throttle_start_delay": self.throttle.start_delay,
            "throttle_max_delay": self.throttle.max_delay,
            "throttle_min_delay": self.throttle.min_delay,
            "throttle_image_delay": self.throttle.image_delay,
            "throttle_jitter": self.throttle.jitter,
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
  platform: shopify   # Platform: shopify, woocommerce, magento, bigcommerce, generic, squarespace
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
  # user_agent: MyBot/1.0 (+https://example.com)  # Custom user agent (turns impersonation off)
  # proxy: http://proxy.example.com:8080  # Fixed proxy (overrides HTTPS_PROXY)
  # impersonate: safari  # First browser profile (default chrome)
  rotate: true             # Try another browser profile after a 403 or 429 (false = --no-rotate)
  respect_robots_txt: true
  verify_tls: true         # Verify TLS certificates (false = --insecure)
  max_concurrent_stores: 16  # Stores scraped at once

throttle:
  enabled: true            # Auto-throttle based on server response
  start_delay: 1           # Initial download delay (seconds)
  max_delay: 60            # Maximum download delay (seconds)
  min_delay: 1             # Minimum seconds between page and API requests to one host
  image_delay: 0.25        # Minimum seconds between image downloads from one host
  jitter: 0.5              # Random spread of the gaps, 0 (exact) to 1
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


_PROXY_LOC = ("network", "proxy")


def load_config_from_file(path: Path) -> Config:
    """Load and validate configuration from a specific file."""
    import yaml

    # utf-8-sig also accepts the byte-order mark Windows editors may add
    with open(path, encoding="utf-8-sig") as f:
        data = yaml.safe_load(f)

    if data is None:
        return Config()

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        # network.proxy may hold credentials, so hide the input of any error at
        # it or at a level that contains it (the whole file, or all of network)
        line_errors = [
            _unknown_key_error(error)
            if error["type"] == "extra_forbidden"
            else {**error, "input": "<hidden>"}
            if _PROXY_LOC[: len(error["loc"])] == error["loc"]
            else error
            for error in exc.errors(include_url=False)
        ]
        raise ValidationError.from_exception_data(exc.title, line_errors) from None


def _unknown_key_error(error: Any) -> Any:
    """The error for a key no section has, naming its section and a close match."""
    *section, key = error["loc"]
    model: type[BaseModel] = Config
    for name in section:
        annotation = model.model_fields[str(name)].annotation
        assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
        model = annotation
    known = [field.alias or name for name, field in model.model_fields.items()]
    match = difflib.get_close_matches(str(key), known, n=1)
    where = f"in the {'.'.join(map(str, section))} section" if section else "at the top level"
    hint = f"did you mean {match[0]!r}?" if match else f"known keys: {', '.join(known)}"
    return {
        **error,
        # No context, so braces in the key aren't read as placeholders
        "type": PydanticCustomError("extra_forbidden", f"unknown key {key!r} {where}; {hint}"),
        # The value may be a misplaced proxy URL with credentials
        "input": "<hidden>",
    }


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
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path
