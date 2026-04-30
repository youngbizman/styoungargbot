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

def evaluate_buy_hedge_from_asks(asks, decimal_odds, bankroll="100", fee_rate="0.03", max_avg_impact_rel="0.02"):
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
    if q <= Decimal("0"): return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "No profitable depth")

    vwap = cost / q
    profit = q - total
    impact = (vwap / best.price) - Decimal("1")
    reason = None
    if not full_bankroll_supported: reason = "Insufficient depth for $100 bankroll"
    elif impact > Decimal(max_avg_impact_rel): reason = "Slippage exceeds 2% buffer"
    elif profit <= 0: reason = "Negative profit after fees"

    return HedgeEstimate(best.price, q, (q/odds), cost, fees, total, vwap, marginal, profit, (reason is None), reason)

def clean(text: str) -> str:
    if not text: return ""
    return str(text).lower().replace("trail blazers", "blazers").split()[-1]

def format_to_local(iso: str) -> str:
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except: return iso[:10]

def parse_iso8601_to_epoch(t):
    try: return int(datetime.fromisoformat(str(t).replace(" ", "T").replace("Z", "+00:00")).timestamp())
    except: return 0

def is_target_single_game(f_t, p_s, p_e):
    tf, ts, te = parse_iso8601_to_epoch(f_t), parse_iso8601_to_epoch(p_s), parse_iso8601_to_epoch(p_e)
    if tf == 0: return False
    if ts > 0 and abs(ts - tf) > 14400: return False
    return True

def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try: settings = load_settings()
    except ConfigError as exc: logger.error(f"Config error: {exc}"); return
    clients = ApiClients(settings)
    try:
        logger.info("📡 Initializing NBA Sniper...")
        raw_odds, raw_poly = clients.get_fiat_data(), clients.get_polymarket_events()
        fiat_games = {}
        for game in raw_odds:
            h, a = game.get('home_team'), game.get('away_team')
            if not h or not a: continue
            k = f"{clean(h)}_{clean(a)}"
            if k not in fiat_games: 
                fiat_games[k] = {
                    "home": h, 
                    "away": a, 
                    "time": game.get('commence_time'), 
                    "sport_key": game.get('sport_key', 'nba'), 
                    "bookies": []
                }
            for b in game.get("bookmakers", []):
                b_data = {"name": b.get("title"), "last_update": b.get("last_update"), "h2h": {}, "totals": {}, "spreads": {}}
                for m in b.get("markets", []):
                    mk = m.get('key')
                    for o in m.get('outcomes', []):
                        nm, pr = clean(o.get('name')), o.get('price')
                        if pr is None: continue
                        pr, pt = Decimal(str(pr)), round(float(o.get('point', 0)), 1)
                        if mk == 'h2h': b_data["h2h"][nm] = pr
                        elif mk == 'totals':
                            if pt not in b_data["totals"]: b_data["totals"][pt] = {}
                            b_data["totals"][pt][nm.lower()] = pr
                        elif mk == 'spreads':
                            if pt not in b_data["spreads"]: b_data["spreads"][pt] = {}
                            b_data["spreads"][pt][nm] = pr
                fiat_games[k]["bookies"].append(b_data)

        opportunities, fiat_opportunities = [], []
        for gk, x in fiat_games.items():
            h_nk, a_nk = clean(x["home"]), clean(x["away"])
            logger.info(f"\n🏀 MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)

            # 1. Fiat Scanner
            for i in range(len(x["bookies"])):
                for j in range(i + 1, len(x["bookies"])):
                    b1, b2 = x["bookies"][i], x["bookies"][j]
                    for t_nm, o1 in b1["h2h"].items():
                        opp_nk = h_nk if t_nm == a_nk else a_nk
                        o2 = b2["h2h"].get(opp_nk)
                        if o1 and o2:
                            imp = (Decimal("1")/o1) + (Decimal("1")/o2)
                            if imp < 1:
                                roi = round(((1/float(imp))-1)*100, 2)
                                if 0 < roi < 15.0: fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1, o2, "ML", t_nm, opp_nk, imp, roi))

            # 2. Poly Scanner
            target = next((e for e in raw_poly if h_nk in e.get('title','').lower() and a_nk in e.get('title','').lower()), None)
            if not target or not is_target_single_game(x["time"], target.get("gameStartTime"), target.get("endDate")): continue
            for b in x["bookies"]:
                for m in target.get('markets', []):
                    if not m.get('acceptingOrders'): continue
                    mt = str(m.get('sportsMarketType', '')).lower()
                    try:
                        outs, toks = json.loads(m.get('outcomes')), json.loads(m.get('clobTokenIds'))
                    except: continue
                    if mt == 'moneyline':
                        for idx, t_nm in enumerate(outs):
                            p_nk, f_odds = clean(t_nm), b["h2h"].get(clean(t_nm))
                            if f_odds:
                                book = clients.get_clob_book(toks[idx])
                                opp_nk = h_nk if p_nk == a_nk else a_nk
                                f_opp = b["h2h"].get(opp_nk)
                                if f_opp:
                                    hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                    
                                    if hedge.passes_liquidity_filter:
                                        roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                        logger.info(f"   [ML] {b['name']:<12} | {t_nm[:10]:<10} | {b['name']}: {float(f_opp):<5} | ROI: {roi}% | Status: ✅")
                                        
                                        if 0 < roi < 15.0: 
                                            opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "ML", t_nm, opp_nk, roi, 0.0, 0.0))
                                        else:
                                            logger.info(f"      ↳ ⚠️ Alert Skipped: ROI {roi}% is outside safe bounds (0-15%)")
                                    else:
                                        logger.info(f"   [ML] {b['name']:<12} | {t_nm[:10]:<10} | {b['name']}: {float(f_opp):<5} | Status: ❌ {hedge.reject_reason}")

        logger.info("\n" + "="*80)
        final_alerts = build_global_alerts(opportunities, fiat_opportunities, limit=3)
        for msg in final_alerts: clients.send_telegram_alert(msg)
        logger.info(f"✅ SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("="*80)
    finally: clients.close()

def _build_fiat_opp(x, b1, b2, o1, o2, m, s1, s2, imp, roi):
    payout = 100.0 / float(imp)
    return FiatArbitrageOpportunity(x['sport_key'], x['home'], x['away'], format_to_local(x['time']), m, b1, s1, float(o1), (payout/float(o1)), b2, s2, float(o2), (payout/float(o2)), float(imp), payout, roi)

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("nba", x['home'], x['away'], format_to_local(x['time']), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
