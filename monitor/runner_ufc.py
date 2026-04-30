import logging
import json
import unicodedata
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo
from thefuzz import fuzz

from .api_clients import ApiClients
from .config import ConfigError, load_settings
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity
from .alerts import build_mma_global_alerts

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

def clean_fighter_name(text: str) -> str:
    if not text: return ""
    text = unicodedata.normalize('NFKD', str(text)).encode('ASCII', 'ignore').decode('utf-8')
    text = re.sub(r'[^a-zA-Z\s]', '', text.lower())
    if text.strip() == 'draw': return 'draw'
    parts = text.split()
    return parts[-1] if parts else ""

def clean_for_matching(text: str) -> str:
    if not text: return ""
    text = unicodedata.normalize('NFKD', str(text)).encode('ASCII', 'ignore').decode('utf-8').lower()
    return re.sub(r'[^a-z0-9\s]', '', text)

def is_fighter_match(fiat_home: str, fiat_away: str, poly_text: str) -> bool:
    if not poly_text: return False
    fiat_str = clean_for_matching(f"{fiat_home} {fiat_away}")
    poly_str = clean_for_matching(poly_text)
    
    # 1. High-confidence fuzzy match
    if fuzz.token_set_ratio(fiat_str, poly_str) > 75:
        return True
        
    # 2. Strict Last Name Fallback (For grouped events)
    h_last = clean_fighter_name(fiat_home)
    a_last = clean_fighter_name(fiat_away)
    if h_last and a_last and h_last in poly_str and a_last in poly_str:
        return True
        
    return False

def format_to_local(iso: str) -> str:
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except: return iso[:10]

def run_ufc() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try: settings = load_settings()
    except ConfigError as exc: logger.error(f"Config error: {exc}"); return
    clients = ApiClients(settings)
    
    try:
        logger.info("📡 Initializing UFC/MMA Deep Sniper...")
        raw_odds, raw_poly = clients.get_mma_fiat_data(), clients.get_mma_polymarket_events()
        
        fiat_games = {}
        cutoff_date = datetime.now(timezone.utc) + timedelta(days=14)

        for game in raw_odds:
            commence_time = datetime.fromisoformat(game.get('commence_time').replace("Z", "+00:00"))
            if commence_time > cutoff_date: continue 
            h, a = game.get('home_team'), game.get('away_team')
            if not h or not a: continue
            
            k = f"{clean_fighter_name(h)}_{clean_fighter_name(a)}"
            if k not in fiat_games: 
                fiat_games[k] = {
                    "home": h, "away": a, "time": game.get('commence_time'), 
                    "sport_key": game.get('sport_key', 'mma'), "bookies": []
                }
                
            for b in game.get("bookmakers", []):
                b_data = {"name": b.get("title"), "h2h": {}, "totals": {}}
                for m in b.get("markets", []):
                    mk = m.get('key')
                    for o in m.get('outcomes', []):
                        nm, pr = clean_fighter_name(o.get('name')), o.get('price')
                        pt = o.get('point')
                        
                        if mk == 'h2h':
                            if nm == 'draw': continue
                            if pr is not None: b_data["h2h"][nm] = Decimal(str(pr))
                        elif mk == 'totals':
                            if pr is not None and pt is not None:
                                pt_float = float(pt)
                                if pt_float not in b_data["totals"]: b_data["totals"][pt_float] = {}
                                b_data["totals"][pt_float][nm.lower()] = Decimal(str(pr))
                fiat_games[k]["bookies"].append(b_data)

        opportunities, fiat_opportunities = [], []
        for gk, x in fiat_games.items():
            h_nk, a_nk = clean_fighter_name(x["home"]), clean_fighter_name(x["away"])
            logger.info(f"\n🥊 MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)

            # 1. Fiat Scanner (UFC - H2H & Totals)
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
                                if 0 < roi < 25.0:
                                    fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1, o2, "Moneyline", t_nm, opp_nk, imp, roi))
                                    
                    for pt, t1_odds in b1.get("totals", {}).items():
                        t2_odds = b2.get("totals", {}).get(pt, {})
                        o1_over, o1_under = t1_odds.get('over'), t1_odds.get('under')
                        o2_over, o2_under = t2_odds.get('over'), t2_odds.get('under')

                        if o1_over and o2_under:
                            imp = (Decimal("1")/o1_over) + (Decimal("1")/o2_under)
                            if imp < 1:
                                roi = round(((1/float(imp))-1)*100, 2)
                                if 0 < roi < 25.0: fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1_over, o2_under, f"Total Rounds {pt}", "Over", "Under", imp, roi))
                        if o1_under and o2_over:
                            imp = (Decimal("1")/o1_under) + (Decimal("1")/o2_over)
                            if imp < 1:
                                roi = round(((1/float(imp))-1)*100, 2)
                                if 0 < roi < 25.0: fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1_under, o2_over, f"Total Rounds {pt}", "Under", "Over", imp, roi))

            # 2. Poly Scanner (UFC - Deep Event Mapping)
            target = None
            for e in raw_poly:
                # First check if the overarching Event title matches the fighters
                if is_fighter_match(x["home"], x["away"], e.get('title', '')):
                    target = e
                    break
                # If not, check every single market inside the event (fixes the "UFC 302" grouping problem)
                for m in e.get('markets', []):
                    market_text = f"{m.get('question', '')} {m.get('groupItemTitle', '')}"
                    if is_fighter_match(x["home"], x["away"], market_text):
                        target = e
                        break
                if target: break
                    
            if not target: 
                logger.info(f"   [ML] Polymarket | Status: ❌ No matching market found")
                continue
            
            for b in x["bookies"]:
                for m in target.get('markets', []):
                    if not m.get('acceptingOrders'): continue
                    
                    mt = str(m.get('sportsMarketType', '')).lower()
                    question = str(m.get('question', '')).lower()
                    group_title = str(m.get('groupItemTitle', '')).lower()
                    
                    # Prevent Cross-Contamination in Grouped Events
                    event_context = str(target.get('title', '')).lower()
                    market_context = f"{question} {group_title}"
                    if not is_fighter_match(x["home"], x["away"], event_context) and not is_fighter_match(x["home"], x["away"], market_context):
                        continue
                        
                    try:
                        outs, toks = json.loads(m.get('outcomes')), json.loads(m.get('clobTokenIds'))
                    except: continue
                    
                    if mt == 'moneyline' or mt == 'winner':
                        for idx, t_nm in enumerate(outs):
                            p_nk = clean_fighter_name(t_nm)
                            if p_nk == 'draw': continue
                            f_odds = b["h2h"].get(p_nk)
                            if f_odds:
                                book = clients.get_clob_book(toks[idx])
                                opp_nk = h_nk if p_nk == a_nk else a_nk
                                f_opp = b["h2h"].get(opp_nk)
                                if f_opp:
                                    hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                    poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                    logger.info(f"   [ML] {b['name']:<12} | {t_nm[:10]:<10} | {b['name']} Opp: {float(f_opp):<5} | Poly Ask: {poly_price:<5} | Status: {'✅' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}")
                                    if hedge.passes_liquidity_filter:
                                        roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                        if 0 < roi < 25.0:
                                            opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Moneyline", t_nm, opp_nk, roi, 0.0, 0.0))

                    elif mt == 'round_over_under_match' or 'over/under' in question or 'total' in question:
                        line_match = re.search(r'(\d+\.5)', question)
                        if not line_match: continue
                        line = float(line_match.group(1))

                        if line not in b.get("totals", {}): continue
                        
                        fiat_over = b["totals"][line].get('over')
                        fiat_under = b["totals"][line].get('under')

                        for idx, out_lbl in enumerate(outs):
                            out_lbl = out_lbl.lower()
                            poly_tok = toks[idx]
                            
                            f_opp, poly_side, fiat_side = None, "", ""
                            if out_lbl == 'yes' or out_lbl == 'over':
                                f_opp = fiat_under
                                poly_side = f"Over {line}"
                                fiat_side = "Under"
                            elif out_lbl == 'no' or out_lbl == 'under':
                                f_opp = fiat_over
                                poly_side = f"Under {line}"
                                fiat_side = "Over"
                            
                            if f_opp:
                                book = clients.get_clob_book(poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                logger.info(f"   [TOT] {b['name']:<11} | {poly_side[:10]:<10} | {b['name']} Opp: {float(f_opp):<5} | Poly Ask: {poly_price:<5} | Status: {'✅' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}")
                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                    if 0 < roi < 25.0:
                                        opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Total Rounds {line}", poly_side, fiat_side, roi, 0.0, 0.0))

        logger.info("\n" + "="*80)
        final_alerts = build_mma_global_alerts(opportunities, fiat_opportunities, limit=3)
        for msg in final_alerts: clients.send_telegram_alert(msg)
        logger.info(f"✅ UFC SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("="*80)
        
    finally: clients.close()

def _build_fiat_opp(x, b1, b2, o1, o2, m, s1, s2, imp, roi):
    payout = 100.0 / float(imp)
    return FiatArbitrageOpportunity("mma", x['home'], x['away'], format_to_local(x['time']), m, b1, s1, float(o1), (payout/float(o1)), b2, s2, float(o2), (payout/float(o2)), float(imp), payout, roi)

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("mma", x['home'], x['away'], format_to_local(x['time']), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
