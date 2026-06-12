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
from .alerts import (
    build_soccer_global_alerts,
    create_run_id,
    log_raw_detection_to_csv,
)

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
            if s > 0:
                levels.append(BookLevel(price=p, size=s))
        except Exception:
            pass
    return sorted(levels, key=lambda lvl: lvl.price)


def fee_per_share(p: Decimal, r: Decimal) -> Decimal:
    return r * p * (Decimal("1") - p)


def evaluate_buy_hedge_from_asks(asks, decimal_odds, bankroll="100", fee_rate="0.03", max_avg_impact_rel="0.02"):
    levels = normalize_asks(asks)
    odds, bankroll_d, fee_r = Decimal(str(decimal_odds)), Decimal(bankroll), Decimal(fee_rate)
    inv_odds = Decimal("1") / odds
    eps = Decimal("0.0000000001")

    if not levels:
        return HedgeEstimate(None, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Empty Orderbook")

    best = levels[0]
    if best.price <= 0:
        return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Invalid Price")

    q, cost, fees = Decimal("0"), Decimal("0"), Decimal("0")
    marginal, full_bankroll_supported = None, False

    for lvl in levels:
        lvl_fee_ps = fee_per_share(lvl.price, fee_r)
        lvl_all_in_ps = lvl.price + lvl_fee_ps + inv_odds
        if lvl_all_in_ps >= Decimal("1"):
            break

        rem = bankroll_d - ((q * inv_odds) + cost + fees)
        if rem <= eps:
            full_bankroll_supported = True
            break

        affordable = rem / lvl_all_in_ps
        take = min(lvl.size, affordable)
        if take <= 0:
            break

        q += take
        cost += take * lvl.price
        fees += take * lvl_fee_ps
        marginal = lvl.price

        if take < lvl.size:
            full_bankroll_supported = True
            break

    total = cost + fees + (q * inv_odds)
    if total >= bankroll_d - eps:
        full_bankroll_supported = True

    if q <= Decimal("0"):
        return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "No profitable depth")

    vwap = cost / q
    profit = q - total
    impact = (vwap / best.price) - Decimal("1")
    reason = None

    if not full_bankroll_supported:
        reason = "Insufficient depth for $100 bankroll"
    elif impact > Decimal(max_avg_impact_rel):
        reason = "Slippage exceeds 2% buffer"
    elif profit <= 0:
        reason = "Negative profit after fees"

    return HedgeEstimate(best.price, q, (q / odds), cost, fees, total, vwap, marginal, profit, (reason is None), reason)


def clean_for_matching(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8").lower()
    text = text.replace("-", " ")
    return re.sub(r"[^a-z0-9\s]", "", text)


def is_team_match(fiat_team: str, poly_text: str) -> bool:
    if not poly_text:
        return False

    aliases = {
        "united states": "usa",
        "usmnt": "usa",
        "korea republic": "south korea",
        "cote divoire": "ivory coast",
        "czechia": "czech republic",
        "iran": "ir iran",
        "dr congo": "congo",
        "netherlands": "holland",
        "england": "england",
    }

    f_str = clean_for_matching(fiat_team)
    p_str = clean_for_matching(poly_text)

    for full, short in aliases.items():
        if full in f_str:
            f_str = f_str.replace(full, short)
        if full in p_str:
            p_str = p_str.replace(full, short)

    return fuzz.token_set_ratio(f_str, p_str) > 75


def format_to_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return iso[:10]


def calculate_roi(hedge: HedgeEstimate) -> float:
    if hedge.total_outlay and hedge.total_outlay > 0:
        return round(float((hedge.locked_profit / hedge.total_outlay) * 100), 2)
    return 0.0


def run_soccer() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error(f"Config error: {exc}")
        return

    run_id = create_run_id("SOCCER")
    clients = ApiClients(settings)

    try:
        logger.info("📡 Initializing World Cup Soccer Sniper (YES/NO Double-Chance + BTTS + Totals)...")
        logger.info(f"🧾 Run ID: {run_id}")

        raw_odds = clients.get_soccer_fiat_data()
        raw_poly = clients.get_soccer_polymarket_events()

        fiat_games = {}
        now_utc = datetime.now(timezone.utc)
        cutoff_date = now_utc + timedelta(hours=72)

        logger.info(f"   [INFO] Odds API returned {len(raw_odds)} World Cup events.")
        logger.info(f"   [INFO] Polymarket returned {len(raw_poly)} active events.")

        for game in raw_odds:
            commence_raw = game.get("commence_time")
            if not commence_raw:
                continue

            commence_time = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
            if commence_time < now_utc or commence_time > cutoff_date:
                continue

            h, a = game.get("home_team"), game.get("away_team")
            if not h or not a:
                continue

            k = f"{clean_for_matching(h)}_{clean_for_matching(a)}"

            if k not in fiat_games:
                fiat_games[k] = {
                    "home": h,
                    "away": a,
                    "time": commence_raw,
                    "sport_key": game.get("sport_key", "soccer"),
                    "bookies": [],
                }

            for b in game.get("bookmakers", []):
                b_data = {"name": b.get("title"), "h2h": {}, "totals": {}, "btts": {}}

                for m in b.get("markets", []):
                    mk = m.get("key")

                    for o in m.get("outcomes", []):
                        nm, pr = o.get("name"), o.get("price")
                        pt = o.get("point")

                        if mk == "h2h" and pr is not None:
                            b_data["h2h"][nm] = Decimal(str(pr))

                        elif mk == "totals" and pr is not None and pt is not None:
                            pt_float = float(pt)
                            if pt_float not in b_data["totals"]:
                                b_data["totals"][pt_float] = {}
                            b_data["totals"][pt_float][nm.lower()] = Decimal(str(pr))

                        elif mk == "btts" and pr is not None:
                            b_data["btts"][nm.lower()] = Decimal(str(pr))

                if b_data["h2h"] or b_data["totals"] or b_data["btts"]:
                    fiat_games[k]["bookies"].append(b_data)

        logger.info(f"   [INFO] Built {len(fiat_games)} fiat World Cup games inside next 72 hours.")

        opportunities, fiat_opportunities = [], []

        for gk, x in fiat_games.items():
            if not x["bookies"]:
                continue

            h_nk, a_nk = x["home"], x["away"]

            logger.info(f"\n⚽ MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)

            target = None

            for e in raw_poly:
                if is_team_match(h_nk, e.get("title", "")) and is_team_match(a_nk, e.get("title", "")):
                    target = e
                    break

                for m in e.get("markets", []):
                    market_text = f"{m.get('question', '')} {m.get('groupItemTitle', '')}"

                    if is_team_match(h_nk, market_text) and is_team_match(a_nk, market_text):
                        target = e
                        break

                if target:
                    break

            if not target:
                logger.info("   [INFO] Polymarket | Status: ❌ No matching market found")
                continue

            for b in x["bookies"]:
                for m in target.get("markets", []):
                    if not m.get("acceptingOrders"):
                        continue

                    question = str(m.get("question", "")).lower()

                    try:
                        outs = json.loads(m.get("outcomes"))
                        toks = json.loads(m.get("clobTokenIds"))
                    except Exception:
                        continue

                    if "win" in question and "over" not in question:
                        team_in_q = None

                        if is_team_match(h_nk, question):
                            team_in_q = h_nk
                        elif is_team_match(a_nk, question):
                            team_in_q = a_nk

                        if team_in_q:
                            for idx, out_lbl in enumerate(outs):
                                out_lbl = out_lbl.lower()

                                if out_lbl == "no":
                                    poly_tok = toks[idx]
                                    f_opp = b["h2h"].get(team_in_q)

                                    if f_opp:
                                        book = clients.get_clob_book(poly_tok)
                                        hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                        roi = calculate_roi(hedge)
                                        passed_roi_filter = 0 < roi < 15.0
                                        was_profitable = hedge.passes_liquidity_filter and passed_roi_filter
                                        poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                        log_raw_detection_to_csv(
                                            category="SOCCER",
                                            opportunity_type="POLY_FIAT",
                                            market="Fiat Win vs Poly NO",
                                            home_team=x["home"],
                                            away_team=x["away"],
                                            game_date=format_to_local(x["time"]),
                                            bookmaker=b["name"],
                                            selection=f"{team_in_q} to Win",
                                            odds_decimal=float(f_opp),
                                            opposite_selection=f"NO {team_in_q}",
                                            opposite_odds_decimal="",
                                            poly_selection=f"NO {team_in_q}",
                                            poly_best_ask=float(hedge.best_ask or 0),
                                            poly_vwap=float(hedge.vwap or 0),
                                            poly_marginal_price=float(hedge.marginal_price or 0),
                                            total_outlay=float(hedge.total_outlay),
                                            locked_profit=float(hedge.locked_profit),
                                            roi_percent=roi,
                                            passed_liquidity_filter=hedge.passes_liquidity_filter,
                                            passed_roi_filter=passed_roi_filter,
                                            was_profitable=was_profitable,
                                            reject_reason="" if was_profitable else str(hedge.reject_reason or "Outside ROI filter"),
                                            notes="World Cup winner market raw check",
                                            run_id=run_id,
                                        )

                                        logger.info(
                                            f"   [DC-NO]  {b['name']:<10} | Buy Poly: NO {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: {team_in_q[:7]} Win ({float(f_opp):<4.2f}) | Status: {'✅ ROI ' + str(roi) + '%' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}"
                                        )

                                        if was_profitable:
                                            opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Fiat Win vs Poly NO", f"NO {team_in_q}", f"{team_in_q} to Win", roi, 0.0, 0.0))

                                elif out_lbl == "yes":
                                    poly_tok = toks[idx]
                                    opp_nk = a_nk if team_in_q == h_nk else h_nk
                                    f_opp = b["h2h"].get(opp_nk)
                                    f_draw = b["h2h"].get("Draw")

                                    if f_opp and f_draw:
                                        imp_opp = Decimal("1") / f_opp
                                        imp_draw = Decimal("1") / f_draw
                                        dc_odds = Decimal("1") / (imp_opp + imp_draw)

                                        book = clients.get_clob_book(poly_tok)
                                        hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), dc_odds)
                                        roi = calculate_roi(hedge)
                                        passed_roi_filter = 0 < roi < 15.0
                                        was_profitable = hedge.passes_liquidity_filter and passed_roi_filter
                                        poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                        log_raw_detection_to_csv(
                                            category="SOCCER",
                                            opportunity_type="POLY_FIAT",
                                            market="Fiat Dutched DC vs Poly YES",
                                            home_team=x["home"],
                                            away_team=x["away"],
                                            game_date=format_to_local(x["time"]),
                                            bookmaker=b["name"],
                                            selection=f"Draw or {opp_nk}",
                                            odds_decimal=float(dc_odds),
                                            opposite_selection=f"YES {team_in_q}",
                                            opposite_odds_decimal="",
                                            poly_selection=f"YES {team_in_q}",
                                            poly_best_ask=float(hedge.best_ask or 0),
                                            poly_vwap=float(hedge.vwap or 0),
                                            poly_marginal_price=float(hedge.marginal_price or 0),
                                            implied_total=float(imp_opp + imp_draw),
                                            total_outlay=float(hedge.total_outlay),
                                            locked_profit=float(hedge.locked_profit),
                                            roi_percent=roi,
                                            passed_liquidity_filter=hedge.passes_liquidity_filter,
                                            passed_roi_filter=passed_roi_filter,
                                            was_profitable=was_profitable,
                                            reject_reason="" if was_profitable else str(hedge.reject_reason or "Outside ROI filter"),
                                            notes="World Cup double chance raw check",
                                            run_id=run_id,
                                        )

                                        logger.info(
                                            f"   [DC-YES] {b['name']:<10} | Buy Poly: YES {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: Draw or {opp_nk[:7]} ({float(dc_odds):<4.2f}) | Status: {'✅ ROI ' + str(roi) + '%' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}"
                                        )

                                        if was_profitable:
                                            opportunities.append(_build_opp(x, b["name"], dc_odds, hedge, "Fiat Dutched DC vs Poly YES", f"YES {team_in_q}", f"Draw or {opp_nk}", roi, 0.0, 0.0))

                    elif "both teams" in question and "score" in question:
                        fiat_yes = b["btts"].get("yes")
                        fiat_no = b["btts"].get("no")

                        for idx, out_lbl in enumerate(outs):
                            out_lbl = out_lbl.lower()
                            poly_tok = toks[idx]
                            f_opp, poly_side, fiat_side = None, "", ""

                            if out_lbl == "yes" and fiat_no:
                                f_opp, poly_side, fiat_side = fiat_no, "Yes", "No"
                            elif out_lbl == "no" and fiat_yes:
                                f_opp, poly_side, fiat_side = fiat_yes, "No", "Yes"

                            if f_opp:
                                book = clients.get_clob_book(poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                roi = calculate_roi(hedge)
                                passed_roi_filter = 0 < roi < 15.0
                                was_profitable = hedge.passes_liquidity_filter and passed_roi_filter
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                log_raw_detection_to_csv(
                                    category="SOCCER",
                                    opportunity_type="POLY_FIAT",
                                    market="Both Teams to Score",
                                    home_team=x["home"],
                                    away_team=x["away"],
                                    game_date=format_to_local(x["time"]),
                                    bookmaker=b["name"],
                                    selection=fiat_side,
                                    odds_decimal=float(f_opp),
                                    opposite_selection=poly_side,
                                    opposite_odds_decimal="",
                                    poly_selection=poly_side,
                                    poly_best_ask=float(hedge.best_ask or 0),
                                    poly_vwap=float(hedge.vwap or 0),
                                    poly_marginal_price=float(hedge.marginal_price or 0),
                                    total_outlay=float(hedge.total_outlay),
                                    locked_profit=float(hedge.locked_profit),
                                    roi_percent=roi,
                                    passed_liquidity_filter=hedge.passes_liquidity_filter,
                                    passed_roi_filter=passed_roi_filter,
                                    was_profitable=was_profitable,
                                    reject_reason="" if was_profitable else str(hedge.reject_reason or "Outside ROI filter"),
                                    notes="World Cup BTTS raw check",
                                    run_id=run_id,
                                )

                                logger.info(
                                    f"   [BTTS]   {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: {'✅ ROI ' + str(roi) + '%' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}"
                                )

                                if was_profitable:
                                    opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Both Teams to Score", poly_side, fiat_side, roi, 0.0, 0.0))

                    elif "over" in question or "under" in question or "goals" in question:
                        line_match = re.search(r"(\d+\.5)", question)
                        if not line_match:
                            continue

                        line = float(line_match.group(1))
                        if line not in b.get("totals", {}):
                            continue

                        fiat_over = b["totals"][line].get("over")
                        fiat_under = b["totals"][line].get("under")

                        for idx, out_lbl in enumerate(outs):
                            out_lbl = out_lbl.lower()
                            poly_tok = toks[idx]
                            f_opp, poly_side, fiat_side = None, "", ""

                            if (out_lbl == "yes" or out_lbl == "over") and fiat_under:
                                f_opp, poly_side, fiat_side = fiat_under, f"Over {line}", f"Under {line}"
                            elif (out_lbl == "no" or out_lbl == "under") and fiat_over:
                                f_opp, poly_side, fiat_side = fiat_over, f"Under {line}", f"Over {line}"

                            if f_opp:
                                book = clients.get_clob_book(poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                roi = calculate_roi(hedge)
                                passed_roi_filter = 0 < roi < 15.0
                                was_profitable = hedge.passes_liquidity_filter and passed_roi_filter
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                log_raw_detection_to_csv(
                                    category="SOCCER",
                                    opportunity_type="POLY_FIAT",
                                    market=f"Total Goals {line}",
                                    home_team=x["home"],
                                    away_team=x["away"],
                                    game_date=format_to_local(x["time"]),
                                    bookmaker=b["name"],
                                    selection=fiat_side,
                                    odds_decimal=float(f_opp),
                                    opposite_selection=poly_side,
                                    opposite_odds_decimal="",
                                    poly_selection=poly_side,
                                    poly_best_ask=float(hedge.best_ask or 0),
                                    poly_vwap=float(hedge.vwap or 0),
                                    poly_marginal_price=float(hedge.marginal_price or 0),
                                    total_outlay=float(hedge.total_outlay),
                                    locked_profit=float(hedge.locked_profit),
                                    roi_percent=roi,
                                    passed_liquidity_filter=hedge.passes_liquidity_filter,
                                    passed_roi_filter=passed_roi_filter,
                                    was_profitable=was_profitable,
                                    reject_reason="" if was_profitable else str(hedge.reject_reason or "Outside ROI filter"),
                                    notes="World Cup total goals raw check",
                                    run_id=run_id,
                                )

                                logger.info(
                                    f"   [TOT]    {b['name']:<10} | Buy Poly: {poly_side[:10]:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side[:10]:<10} ({float(f_opp):<4.2f}) | Status: {'✅ ROI ' + str(roi) + '%' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}"
                                )

                                if was_profitable:
                                    opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Total Goals {line}", poly_side, fiat_side, roi, 0.0, 0.0))

        logger.info("\n" + "=" * 80)
        final_alerts = build_soccer_global_alerts(opportunities, fiat_opportunities, limit=3)

        for msg in final_alerts:
            clients.send_telegram_alert(msg)

        logger.info(f"✅ SOCCER SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("=" * 80)

    finally:
        clients.close()


def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity(
        "soccer",
        x["home"],
        x["away"],
        format_to_local(x["time"]),
        m,
        p_s,
        f_s,
        b,
        float(f_o),
        float(hedge.shares),
        float(hedge.vwap or 0),
        float(hedge.marginal_price or 0),
        float(hedge.poly_spend),
        float(hedge.poly_fees),
        float(hedge.sportsbook_stake),
        float(hedge.total_outlay),
        float(hedge.locked_profit),
        roi,
        dt,
        sp,
    )