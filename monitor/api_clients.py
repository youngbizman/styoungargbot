from __future__ import annotations
import logging
import time
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .config import Settings

logger = logging.getLogger(__name__)


class ApiClients:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=5, connect=5, read=5, backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "arb-bot/2.1"})
        return session

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        timeout_val = getattr(self.settings, "request_timeout_seconds", 30)
        response = self.session.get(url, params=params, timeout=timeout_val)
        response.raise_for_status()
        return response.json()

    # --- NBA METHODS ---
    def get_fiat_data(self) -> list[dict[str, Any]]:
        url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": "eu,us",
            "markets": "h2h,totals,spreads",
            "bookmakers": "pinnacle,onexbet",
        }
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"Odds API request failed: {exc}")
            return []

    def get_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        params = {"series_id": 10345, "active": "true", "closed": "false", "limit": 100}
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list): return data
            if isinstance(data, dict): return data.get("events", [])
            return []
        except Exception as exc:
            logger.error(f"Polymarket request failed: {exc}")
            return []

    # --- SHARED POLYMARKET CLOB METHOD ---
    def get_clob_book(self, token_id: str) -> dict[str, Any]:
        if not str(token_id).strip(): return {"asks": [], "bids": [], "timestamp": "0"}
        url = "https://clob.polymarket.com/book"
        params = {"token_id": token_id}
        try:
            data = self._get_json(url, params=params)
            if not isinstance(data, dict): return {"asks": [], "bids": [], "timestamp": "0"}
            return {
                "asks": data.get("asks", []),
                "bids": data.get("bids", []),
                "timestamp": data.get("timestamp", "0"),
            }
        except Exception as exc:
            logger.warning(f"CLOB request failed for token {token_id}: {exc}")
            return {"asks": [], "bids": [], "timestamp": "0"}

    # --- SHARED TELEGRAM SENDER ---
    def send_telegram_alert(self, message: str) -> bool:
        if not message.strip(): return False
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": message}
        try:
            response = self.session.post(url, json=payload, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")
            return False

    def close(self) -> None:
        self.session.close()

    # --- MMA / UFC METHODS ---
    def get_mma_fiat_data(self) -> list[dict[str, Any]]:
        url = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": "eu,us",
            "markets": "h2h,totals",
            "bookmakers": "pinnacle,onexbet",
        }
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"MMA Odds API request failed: {exc}")
            return []

    def get_mma_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []
        for offset in range(0, 5000, 100):
            params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
            try:
                data = self._get_json(url, params=params)
                if isinstance(data, list):
                    all_events.extend(data)
                    if len(data) < 100: break
                elif isinstance(data, dict):
                    events = data.get("events", [])
                    all_events.extend(events)
                    if len(events) < 100: break
                else:
                    break
                time.sleep(0.3)
            except Exception as exc:
                logger.error(f"MMA Polymarket pagination failed at offset {offset}: {exc}")
                break
        return all_events

    # --- SOCCER / FOOTBALL METHODS ---
    def get_soccer_fiat_data(self) -> list[dict[str, Any]]:
        league = "soccer_fifa_world_cup"
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "markets": "h2h,totals",
            "bookmakers": "pinnacle,onexbet",
            "oddsFormat": "decimal",
        }

        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list):
                for event in data:
                    event_id = event.get("id")
                    if not event_id:
                        continue

                    event_odds = self._get_soccer_event_odds(league, str(event_id), "btts")
                    self._merge_event_markets(event, event_odds, {"btts"})
                    time.sleep(0.15)

                return data

        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info(f"   [INFO] ⚽ {league} is currently inactive (404). Skipping safely...")
            else:
                logger.error(f"Soccer Odds API request failed for {league}: {exc}")
        except Exception as exc:
            logger.error(f"Soccer Odds API request failed for {league}: {exc}")

        return []

    def _get_soccer_event_odds(self, league: str, event_id: str, markets: str) -> dict[str, Any]:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/events/{event_id}/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "markets": markets,
            "bookmakers": "pinnacle,onexbet",
            "oddsFormat": "decimal",
        }

        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, dict) else {}
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.info(f"   [INFO] ⚽ Event odds unavailable for {event_id} ({markets}, HTTP {status}). Skipping...")
        except Exception as exc:
            logger.error(f"Soccer event odds request failed for {event_id} ({markets}): {exc}")

        return {}

    def _merge_event_markets(
        self,
        base_event: dict[str, Any],
        event_odds: dict[str, Any],
        market_keys: set[str],
    ) -> None:
        base_bookmakers = base_event.setdefault("bookmakers", [])
        by_key = {b.get("key"): b for b in base_bookmakers if b.get("key")}

        for event_bookmaker in event_odds.get("bookmakers", []):
            markets = [m for m in event_bookmaker.get("markets", []) if m.get("key") in market_keys]
            if not markets:
                continue

            bookmaker_key = event_bookmaker.get("key")
            target = by_key.get(bookmaker_key)

            if target is None:
                target = {
                    "key": bookmaker_key,
                    "title": event_bookmaker.get("title"),
                    "last_update": event_bookmaker.get("last_update"),
                    "markets": [],
                }
                base_bookmakers.append(target)

                if bookmaker_key:
                    by_key[bookmaker_key] = target

            target["markets"] = [
                market for market in target.get("markets", []) if market.get("key") not in market_keys
            ]
            target["markets"].extend(markets)

    def get_soccer_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []

        for offset in range(0, 5000, 100):
            params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}

            try:
                data = self._get_json(url, params=params)

                if isinstance(data, list):
                    all_events.extend(data)
                    if len(data) < 100: break
                elif isinstance(data, dict):
                    events = data.get("events", [])
                    all_events.extend(events)
                    if len(events) < 100: break
                else:
                    break

                time.sleep(0.3)

            except Exception as exc:
                logger.error(f"Soccer Polymarket pagination failed at offset {offset}: {exc}")
                break

        return all_events