import os
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    odds_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    request_timeout_seconds: int
    scan_interval_seconds: int


def load_settings() -> Settings:
    return Settings(
        odds_api_key=_require_env("ODDS_API_KEY"),
        telegram_bot_token=_require_env("TELEGRAM_TOKEN"),
        telegram_chat_id=_require_env("TELEGRAM_CHAT_ID"),
        request_timeout_seconds=_get_int("REQUEST_TIMEOUT_SECONDS", 15),
        scan_interval_seconds=_get_int("SCAN_INTERVAL_SECONDS", 60),
    )
