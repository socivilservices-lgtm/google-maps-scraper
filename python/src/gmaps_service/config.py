"""Runtime configuration from the environment, an optional JSON file, and the CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from gmaps_service.errors import ConfigError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_BACKENDS = {"auto", "playwright", "places"}
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def _as_bool(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"expected a boolean, got {value!r}")


def _as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected a number, got {value!r}") from exc


def _as_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected an integer, got {value!r}") from exc


def playwright_proxy(proxy_url: str) -> dict[str, str]:
    """Convert ``http://user:pass@host:port`` into Playwright's proxy dict."""
    parsed = urlparse(proxy_url)
    if parsed.scheme not in _PROXY_SCHEMES or not parsed.hostname or not parsed.port:
        raise ConfigError(f"unsupported proxy URL: {proxy_url}")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def load_proxy_lines(text: str) -> list[str]:
    proxies: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        playwright_proxy(stripped)
        proxies.append(stripped)
    return proxies


@dataclass
class Config:
    """Knobs for the CLI, the HTTP service, and both backends."""

    backend: str = "auto"
    google_places_api_key: str = ""
    service_api_key: str = ""
    max_results: int = 20
    headless: bool = True
    delay_min: float = 2.0
    delay_max: float = 5.0
    timeout_seconds: float = 30.0
    retries: int = 3
    retry_base_delay: float = 1.0
    proxies: list[str] = field(default_factory=list)
    proxies_file: str = ""
    user_data_dir: str = ""
    output_dir: str = "output"
    extract_emails: bool = True
    manual_captcha_timeout: float = 180.0
    language: str = "en"
    region_hint: str = ""
    max_reviews: int = 5
    host: str = "0.0.0.0"
    port: int = 8000
    chromium_no_sandbox: bool = False

    def __post_init__(self) -> None:
        if self.backend not in _BACKENDS:
            raise ConfigError(f"backend must be one of {sorted(_BACKENDS)}")
        if self.max_results < 1:
            raise ConfigError("max_results must be at least 1")
        if self.max_reviews < 1:
            raise ConfigError("max_reviews must be at least 1")
        if self.delay_min < 0 or self.delay_max < 0:
            raise ConfigError("delays cannot be negative")
        if self.delay_min > self.delay_max:
            self.delay_min, self.delay_max = self.delay_max, self.delay_min
        if self.retries < 0:
            raise ConfigError("retries cannot be negative")

    @property
    def places_configured(self) -> bool:
        return bool(self.google_places_api_key)

    def resolve_backend(self, requested: str | None = None) -> str:
        choice = (requested or self.backend or "auto").strip().lower()
        if choice not in _BACKENDS:
            raise ConfigError(f"unknown backend {choice!r}")
        if choice == "auto":
            return "places" if self.places_configured else "playwright"
        if choice == "places" and not self.places_configured:
            raise ConfigError("backend 'places' requires GOOGLE_PLACES_API_KEY")
        return choice

    @classmethod
    def load(cls, path: str | None = None, **overrides: object) -> "Config":
        """Load JSON config, then environment variables, then explicit overrides.

        Environment variables win over the file so a deployed container can
        change secrets without editing the image. CLI flags win over both.
        """
        data: dict[str, object] = {}
        config_path = path or os.environ.get("GMAPS_CONFIG", "")
        if config_path:
            file_path = Path(config_path)
            if not file_path.is_file():
                raise ConfigError(f"config file not found: {config_path}")
            loaded = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ConfigError("config file must be a JSON object")
            data.update(loaded)
        env_map = {
            "backend": os.environ.get("GMAPS_BACKEND"),
            "google_places_api_key": os.environ.get("GOOGLE_PLACES_API_KEY"),
            "service_api_key": os.environ.get("GMAPS_SERVICE_API_KEY"),
            "max_results": os.environ.get("GMAPS_MAX_RESULTS"),
            "headless": os.environ.get("GMAPS_HEADLESS"),
            "delay_min": os.environ.get("GMAPS_DELAY_MIN"),
            "delay_max": os.environ.get("GMAPS_DELAY_MAX"),
            "timeout_seconds": os.environ.get("GMAPS_TIMEOUT"),
            "retries": os.environ.get("GMAPS_RETRIES"),
            "retry_base_delay": os.environ.get("GMAPS_RETRY_BASE_DELAY"),
            "proxies_file": os.environ.get("GMAPS_PROXIES_FILE"),
            "user_data_dir": os.environ.get("GMAPS_USER_DATA_DIR"),
            "output_dir": os.environ.get("GMAPS_OUTPUT_DIR"),
            "extract_emails": os.environ.get("GMAPS_EXTRACT_EMAILS"),
            "manual_captcha_timeout": os.environ.get("GMAPS_MANUAL_CAPTCHA_TIMEOUT"),
            "language": os.environ.get("GMAPS_LANGUAGE"),
            "region_hint": os.environ.get("GMAPS_REGION"),
            "max_reviews": os.environ.get("GMAPS_MAX_REVIEWS"),
            "host": os.environ.get("GMAPS_HOST"),
            "port": os.environ.get("GMAPS_PORT"),
            "chromium_no_sandbox": os.environ.get("GMAPS_CHROMIUM_NO_SANDBOX"),
        }
        for key, value in env_map.items():
            if value not in (None, ""):
                data[key] = value
        inline_proxies = os.environ.get("GMAPS_PROXIES", "")
        if inline_proxies:
            data["proxies"] = [item.strip() for item in inline_proxies.split(",") if item.strip()]
        for key, value in overrides.items():
            if value is not None:
                data[key] = value
        proxies = data.get("proxies") or []
        if isinstance(proxies, str):
            proxies = load_proxy_lines(proxies)
        elif isinstance(proxies, list):
            proxies = load_proxy_lines("\n".join(str(item) for item in proxies))
        else:
            raise ConfigError("proxies must be a list of URLs")
        proxies_file = str(data.get("proxies_file") or "")
        if proxies_file:
            file_path = Path(proxies_file)
            if file_path.is_file():
                proxies.extend(load_proxy_lines(file_path.read_text(encoding="utf-8")))
            else:
                # A missing file is ignored so a container can mount one later.
                proxies_file = proxies_file
        return cls(
            backend=str(data.get("backend") or "auto").lower(),
            google_places_api_key=str(data.get("google_places_api_key") or ""),
            service_api_key=str(data.get("service_api_key") or ""),
            max_results=_as_int(data.get("max_results"), 20),
            headless=_as_bool(data.get("headless"), True),
            delay_min=_as_float(data.get("delay_min"), 2.0),
            delay_max=_as_float(data.get("delay_max"), 5.0),
            timeout_seconds=_as_float(data.get("timeout_seconds"), 30.0),
            retries=_as_int(data.get("retries"), 3),
            retry_base_delay=_as_float(data.get("retry_base_delay"), 1.0),
            proxies=proxies,
            proxies_file=proxies_file,
            user_data_dir=str(data.get("user_data_dir") or ""),
            output_dir=str(data.get("output_dir") or "output"),
            extract_emails=_as_bool(data.get("extract_emails"), True),
            manual_captcha_timeout=_as_float(data.get("manual_captcha_timeout"), 180.0),
            language=str(data.get("language") or "en"),
            region_hint=str(data.get("region_hint") or ""),
            max_reviews=_as_int(data.get("max_reviews"), 5),
            host=str(data.get("host") or "0.0.0.0"),
            port=_as_int(data.get("port"), 8000),
            chromium_no_sandbox=_as_bool(data.get("chromium_no_sandbox"), False),
        )
