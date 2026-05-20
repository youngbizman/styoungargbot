import logging
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo

from .api_clients import ApiClients
from .config import ConfigError, load_settings
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity
from .alerts import build_global_alerts

logger = logging.getLogger(__name__)
getcontext().prec = 28

# Keep your existing BookLevel and HedgeEstimate logic exactly as is...
@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

@dataclass
class HedgeEstimate:
    best_ask: Optional[Decimal]
    shares: Decimal
    sportsbook_stake: Decimal
    poly_spend: Decimal
    poly_fees: Decimal
    total_outlay: Decimal
    vwap: Optional[Decimal]
    marginal_price: Optional[Decimal]
    locked_profit: Decimal
    passes_liquidity_filter: bool
    reject_reason: Optional[str]

def normalize_asks(asks: Iterable[Mapping[str, str]]) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for row in asks:
        try:
            p, s = Decimal(str(row.get("price", "0"))), Decimal(str(row.get("size", "0")))
            if s > 0: levels.append(BookLevel(price=p, size=s))
        except: pass
    return sorted(levels, key=lambda lvl: lvl.price)

def fee_per_share(p: Decimal, r: Decimal) -> Decimal:
    return r * p * (Decimal("1") - p)

def evaluate_buy_hedge_from_asks(asks, decimal_odds, bankroll="100", fee_rate="0.00", max_avg_impact_rel="0.02"):
    # Note: fee_rate is 0.00 for SX Bet, 0.03 for Polymarket. We call this dynamically.
    levels = normalize_asks(asks)
    odds, bankroll_d, fee_r = Decimal(str(decimal_odds)), Decimal(bankroll), Decimal(fee_rate)
    inv_odds = Decimal("1") / odds
    eps = Decimal("0.0000000001")

    if not levels: return HedgeEstimate(None, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Empty Orderbook")

    best = levels[0]
    q, cost, fees = Decimal("0"), Decimal("0"), Decimal("0")
    marginal, full_bankroll_supported = None, False

    for lvl in levels:
        lvl_fee_ps = fee_per_share(lvl.price, fee_r)
        lvl_all_in_ps = lvl.price + lvl_fee_ps + inv_odds
        if lvl_all_in_ps >= Decimal("1"): break
        rem = bankroll_d - ((q * inv_odds) + cost + fees)
        if rem <= eps: 
            full_bankroll_supported = True
            break
        affordable = rem / lvl_all_in_ps
        take = min(lvl.size, affordable)
        if take <= 0: break
        q += take
        cost += take * lvl.price
        fees += take * lvl_fee_ps
        marginal = lvl.price
        if take < lvl.size:
            full_bankroll_supported = True
            break

    total = cost + fees + (q * inv_odds)
    if total >= bankroll_d - eps: full_bankroll_supported = True
    if q <= Decimal("0"): return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "No depth")

    vwap = cost / q
    profit = q - total
    impact = (vwap / best.price) - Decimal("1")
    reason = None
    if not full_bankroll_supported: reason = "Insufficient depth"
    elif impact > Decimal(max_avg_impact_rel): reason = "Slippage > 2%"
    elif profit <= 0: reason = "Negative ROI"

    return HedgeEstimate(best.price, q, (q/odds), cost, fees, total, vwap, marginal, profit, (reason is None), reason)

def clean(text: str) -> str:
    if not text: return ""
    return str(text).lower().replace("trail blazers", "blazers").split()[-1]

def format_to_local(iso: str) -> str:
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except: return iso[:10]

def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try: settings = load_settings()
    except ConfigError as exc: logger.error(f"Config error: {exc}"); return
    clients = ApiClients(settings)
    
    try:
        logger.info("🏀 Initializing NBA Sniper (Fiat + Poly + SX Bet)...")
        raw_odds = clients.get_fiat_data()
        raw_poly = clients.get_polymarket_events()
        raw_sx = clients.get_sxbet_events()
        
        fiat_games = {} # ... (Keep your fiat parsing logic same as before) ...
        # [Insert your existing fiat_games parsing logic here for brevity]

        opportunities, fiat_opportunities = [], []
        
        for gk, x in fiat_games.items():
            h_nk, a_nk = clean(x["home"]), clean(x["away"])
            
            # --- 1. POLYMARKET SCAN ---
            # [Keep your existing Polymarket Moneyline loop...]
            
            # --- 2. SX BET SCAN ---
            target_sx = next((e for e in raw_sx if h_nk in clean(e['home_team']) and a_nk in clean(e['away_team'])), None)
            if target_sx:
                book = clients.get_sxbet_book(target_sx['market_hash'])
                # Check against Fiat Odds
                for b in x["bookies"]:
                    for t_nm, f_odds in b["h2h"].items():
                        opp_nk = a_nk if clean(t_nm) == h_nk else h_nk
                        f_opp = b["h2h"].get(opp_nk)
                        if f_opp:
                            # Fee Rate 0.00 for SX Bet as requested
                            hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp, fee_rate="0.00")
                            if hedge.passes_liquidity_filter:
                                roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                logger.info(f"   [SX-ML] {b['name']:<12} | {t_nm[:10]:<10} | ROI: {roi}% | ✅")
                                if 0 < roi < 15.0: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "ML (SX)", t_nm, opp_nk, roi, 0.0, 0.0))

        # Build and send alerts
        final_alerts = build_global_alerts(opportunities, fiat_opportunities, limit=3)
        for msg in final_alerts: clients.send_telegram_alert(msg)
        
    finally: clients.close()

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("nba", x['home'], x['away'], format_to_local(x['time']), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
