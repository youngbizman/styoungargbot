from __future__ import annotations
import logging
import time
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from decimal import Decimal, getcontext, InvalidOperation
from .config import Settings

# Set precision high enough to process EVM math (SX Bet uses base-1e20 integers)
getcontext().prec = 28

logger = logging.getLogger(__name__)

class ApiClients:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        # Retry logic handles 429 (Too Many Requests) automatically
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
        response = self.session.get(url, params=params, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        return response.json()

    # ==========================================
    # --- SX BET (ARBITRUM) METHODS ---
    # ==========================================
    def get_sxbet_events(self) -> list[dict[str, Any]]:
        """Maps SX Bet markets to a structure compatible with your existing Polymarket runners."""
        url = "https://api.sx.bet/markets/active"
        all_events = []
        pagination_key = ""
        
        for _ in range(20): # Safety limit
            params = {"pageSize": 50}
            if pagination_key:
                params["paginationKey"] = pagination_key

            try:
                data = self._get_json(url, params=params)
                if not isinstance(data, dict): break
                
                markets = data.get("data", [])
                
                # Mapping SX Bet Schema to your standard schema
                for m in markets:
                    all_events.append({
                        "home_team": m.get("homeTeam"),
                        "away_team": m.get("awayTeam"),
                        "commence_time": m.get("timestamp"), # Unix timestamp
                        "market_hash": m.get("marketHash"),
                        "sport_id": m.get("sportId")
                    })
                
                pagination_key = data.get("nextKey")
                if not pagination_key: break
                
                time.sleep(0.3) # Rate limit protection
                    
            except Exception as exc:
                logger.error(f"SX Bet active markets failed: {exc}")
                break
        return all_events

    def get_sxbet_book(self, market_hash: str) -> dict[str, Any]:
        """Calculates precise odds using base-1e20 math and normalizes to BookLevel."""
        if not str(market_hash).strip(): return {"asks": [], "bids": [], "timestamp": "0"}
        
        url = "https://api.sx.bet/orders"
        params = {"marketHash": market_hash}
        
        try:
            data = self._get_json(url, params=params)
            if not isinstance(data, dict): return {"asks": [], "bids": [], "timestamp": "0"}
            
            raw_orders = data.get("data", [])
            normalized_asks = []
            
            for order in raw_orders:
                if order.get("status") != "ACTIVE": continue
                    
                try:
                    # Math: Price Conversion (Base-1e20 -> Decimal Odds)
                    # 1. Convert base-1e20 integer to probability
                    maker_implied = Decimal(str(order.get("percentageOdds", "0"))) / Decimal('100000000000000000000')
                    taker_implied = Decimal('1.0') - maker_implied
                    
                    if taker_implied <= Decimal('0.0') or taker_implied >= Decimal('1.0'): continue 
                        
                    decimal_odds = Decimal('1.0') / taker_implied
                    
                    # Math: Size Conversion (Arbitrum USDC MWei -> Executable USD Volume)
                    maker_usdc_risk = Decimal(str(order.get("totalBetSize", "0"))) / Decimal('1000000')
                    odds_multiplier = decimal_odds - Decimal('1.0')
                    
                    if odds_multiplier <= Decimal('0.0'): continue
                        
                    taker_executable_usdc = maker_usdc_risk / odds_multiplier
                    taker_volume = taker_executable_usdc.quantize(Decimal('0.01'))
                    
                    normalized_asks.append({
                        "price": str(decimal_odds.quantize(Decimal('0.001'))),
                        "size": str(taker_volume)
                    })
                except InvalidOperation:
                    continue
            
            return {"asks": normalized_asks, "bids": [], "timestamp": "0"}
            
        except Exception as exc:
            logger.warning(f"SX Bet CLOB failed for {market_hash}: {exc}")
            return {"asks": [], "bids": [], "timestamp": "0"}

    # ==========================================
    # --- EXISTING POLYMARKET & FIAT METHODS ---
    # ==========================================
    def get_fiat_data(self) -> list[dict[str, Any]]:
        url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
        params = {"apiKey": self.settings.odds_api_key, "regions": "eu,us", "markets": "h2h,totals,spreads", "bookmakers": "pinnacle,onexbet"}
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"Odds API failed: {exc}")
            return []

    def get_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        params = {"series_id": 10345, "active": "true", "closed": "false", "limit": 100}
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else data.get("events", [])
        except Exception as exc:
            logger.error(f"Polymarket failed: {exc}")
            return []

    def get_clob_book(self, token_id: str) -> dict[str, Any]:
        if not str(token_id).strip(): return {"asks": [], "bids": [], "timestamp": "0"}
        url = "https://clob.polymarket.com/book"
        params = {"token_id": token_id}
        try:
            data = self._get_json(url, params=params)
            if not isinstance(data, dict): return {"asks": [], "bids": [], "timestamp": "0"}
            return {"asks": data.get("asks", []), "bids": data.get("bids", []), "timestamp": data.get("timestamp", "0")}
        except Exception as exc:
            logger.warning(f"CLOB failed: {exc}")
            return {"asks": [], "bids": [], "timestamp": "0"}

    # ... (Keep all your existing MMA/Soccer/Telegram methods here) ...

    def close(self) -> None:
        self.session.close()
