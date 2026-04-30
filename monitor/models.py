from dataclasses import dataclass

@dataclass(frozen=True)
class Game:
    source: str
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: str
    market_title: str
    selection_name: str
    price: float
    bookmaker: str = ""
    url: str = ""

@dataclass(frozen=True)
class ArbitrageOpportunity:
    sport_key: str
    home_team: str
    away_team: str
    commence_time: str
    market_title: str
    selection_name: str
    fiat_selection: str
    bookmaker: str
    odds_decimal: float
    
    shares: float      
    vwap: float
    marginal_price: float
    poly_spend: float
    poly_fees: float
    sportsbook_stake: float
    total_outlay: float
    locked_profit: float
    expected_profit_percent: float
    
    # Validation metrics (hidden)
    time_delta_seconds: float
    spread_percent: float
    
    odds_url: str = ""
    polymarket_url: str = ""

@dataclass(frozen=True)
class FiatArbitrageOpportunity:
    sport_key: str
    home_team: str
    away_team: str
    commence_time: str
    market_title: str
    bookmaker_1: str
    selection_1: str
    odds_1: float
    stake_1: float
    bookmaker_2: str
    selection_2: str
    odds_2: float
    stake_2: float
    implied_total: float
    payout: float      
    expected_profit_percent: float

@dataclass(frozen=True)
class HealthSummary:
    odds_events_seen: int = 0
    polymarket_events_seen: int = 0
    matched_pairs: int = 0
    opportunities_found: int = 0
    parse_errors: int = 0
    request_errors: int = 0

    def __post_init__(self) -> None:
        for f, v in self.__dict__.items():
            if isinstance(v, int) and v < 0: raise ValueError(f"{f} cannot be negative")
