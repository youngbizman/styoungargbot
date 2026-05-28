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
from .alerts import (
    build_global_alerts,
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
            p = Decimal(str(row.get("price", "0")))
            s = Decimal(str(row.get("size", "0")))

            if s > 0:
                levels.append(BookLevel(price=p, size=s))
        except Exception:
            pass

    return sorted(levels, key=lambda lvl: lvl.price)


def fee_per_share(p: Decimal, r: Decimal) -> Decimal:
    return r * p * (Decimal("1") - p)


def evaluate_buy_hedge_from_asks(
    asks,
    decimal_odds,
    bankroll="100",
    fee_rate="0.03",
    max_avg_impact_rel="0.02",
):
    levels = normalize_asks(asks)

    odds = Decimal(str(decimal_odds))
    bankroll_d = Decimal(bankroll)
    fee_r = Decimal(fee_rate)
    inv_odds = Decimal("1") / odds
    eps = Decimal("0.0000000001")

    if not levels:
        return HedgeEstimate(
            None,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            None,
            None,
            Decimal("0"),
            False,
            "Empty Orderbook",
        )

    best = levels[0]
    q = Decimal("0")
    cost = Decimal("0")
    fees = Decimal("0")
    marginal = None
    full_bankroll_supported = False

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
        return HedgeEstimate(
            best.price,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            None,
            None,
            Decimal("0"),
            False,
            "No profitable depth",
        )

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

    return HedgeEstimate(
        best.price,
        q,
        q / odds,
        cost,
        fees,
        total,
        vwap,
        marginal,
        profit,
        reason is None,
        reason,
    )


def clean(text: str) -> str:
    if not text:
        return ""

    return str(text).lower().replace("trail blazers", "blazers").split()[-1]


def format_to_local(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone(ZoneInfo("America/Toronto"))
            .strftime("%Y-%m-%d %I:%M %p")
        )
    except Exception:
        return iso[:10]


def parse_iso8601_to_epoch(t):
    try:
        return int(
            datetime.fromisoformat(
                str(t).replace(" ", "T").replace("Z", "+00:00")
            ).timestamp()
        )
    except Exception:
        return 0


def is_target_single_game(f_t, p_s, p_e):
    tf = parse_iso8601_to_epoch(f_t)
    ts = parse_iso8601_to_epoch(p_s)
    te = parse_iso8601_to_epoch(p_e)

    if tf == 0:
        return False

    if ts > 0 and abs(ts - tf) > 14400:
        return False

    return True


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error(f"Config error: {exc}")
        return

    run_id = create_run_id("NBA")
    clients = ApiClients(settings)

    try:
        logger.info("📡 Initializing NBA Sniper...")
        logger.info(f"🧾 Run ID: {run_id}")

        raw_odds = clients.get_fiat_data()
        raw_poly = clients.get_polymarket_events()

        fiat_games = {}

        for game in raw_odds:
            h = game.get("home_team")
            a = game.get("away_team")

            if not h or not a:
                continue

            k = f"{clean(h)}_{clean(a)}"

            if k not in fiat_games:
                fiat_games[k] = {
                    "home": h,
                    "away": a,
                    "time": game.get("commence_time"),
                    "sport_key": game.get("sport_key", "nba"),
                    "bookies": [],
                }

            for b in game.get("bookmakers", []):
                b_data = {
                    "name": b.get("title"),
                    "last_update": b.get("last_update"),
                    "h2h": {},
                    "totals": {},
                    "spreads": {},
                }

                for m in b.get("markets", []):
                    mk = m.get("key")

                    for o in m.get("outcomes", []):
                        nm = clean(o.get("name"))
                        pr = o.get("price")

                        if pr is None:
                            continue

                        pr = Decimal(str(pr))
                        pt = round(float(o.get("point", 0)), 1)

                        if mk == "h2h":
                            b_data["h2h"][nm] = pr

                        elif mk == "totals":
                            if pt not in b_data["totals"]:
                                b_data["totals"][pt] = {}

                            b_data["totals"][pt][nm.lower()] = pr

                        elif mk == "spreads":
                            if pt not in b_data["spreads"]:
                                b_data["spreads"][pt] = {}

                            b_data["spreads"][pt][nm] = pr

                fiat_games[k]["bookies"].append(b_data)

        opportunities = []
        fiat_opportunities = []

        for gk, x in fiat_games.items():
            h_nk = clean(x["home"])
            a_nk = clean(x["away"])

            logger.info(
                f"\n🏀 MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}"
            )
            logger.info("-" * 80)

            # 1. Fiat Scanner
            for i in range(len(x["bookies"])):
                for j in range(i + 1, len(x["bookies"])):
                    b1 = x["bookies"][i]
                    b2 = x["bookies"][j]

                    for t_nm, o1 in b1["h2h"].items():
                        opp_nk = h_nk if t_nm == a_nk else a_nk
                        o2 = b2["h2h"].get(opp_nk)

                        if o1 and o2:
                            imp = (Decimal("1") / o1) + (Decimal("1") / o2)
                            roi = round(((1 / float(imp)) - 1) * 100, 2)
                            passed_roi_filter = 0 < roi < 15.0
                            was_profitable = imp < 1 and passed_roi_filter

                            log_raw_detection_to_csv(
                                category="NBA",
                                opportunity_type="FIAT_FIAT",
                                market="ML",
                                home_team=x["home"],
                                away_team=x["away"],
                                game_date=format_to_local(x["time"]),
                                bookmaker=b1["name"],
                                selection=t_nm,
                                odds_decimal=float(o1),
                                opposite_selection=opp_nk,
                                opposite_odds_decimal=float(o2),
                                implied_total=float(imp),
                                roi_percent=roi,
                                passed_liquidity_filter="",
                                passed_roi_filter=passed_roi_filter,
                                was_profitable=was_profitable,
                                reject_reason="" if was_profitable else "Not profitable or outside ROI filter",
                                notes=f"Compared {b1['name']} vs {b2['name']}",
                                run_id=run_id,
                            )

                            if imp < 1:
                                if passed_roi_filter:
                                    fiat_opportunities.append(
                                        _build_fiat_opp(
                                            x,
                                            b1["name"],
                                            b2["name"],
                                            o1,
                                            o2,
                                            "ML",
                                            t_nm,
                                            opp_nk,
                                            imp,
                                            roi,
                                        )
                                    )

            # 2. Poly Scanner
            target = next(
                (
                    e
                    for e in raw_poly
                    if h_nk in e.get("title", "").lower()
                    and a_nk in e.get("title", "").lower()
                ),
                None,
            )

            if not target or not is_target_single_game(
                x["time"],
                target.get("gameStartTime"),
                target.get("endDate"),
            ):
                continue

            for b in x["bookies"]:
                for m in target.get("markets", []):
                    if not m.get("acceptingOrders"):
                        continue

                    mt = str(m.get("sportsMarketType", "")).lower()

                    try:
                        outs = json.loads(m.get("outcomes"))
                        toks = json.loads(m.get("clobTokenIds"))
                    except Exception:
                        continue

                    if mt == "moneyline":
                        for idx, t_nm in enumerate(outs):
                            p_nk = clean(t_nm)
                            f_odds = b["h2h"].get(clean(t_nm))

                            if f_odds:
                                book = clients.get_clob_book(toks[idx])
                                opp_nk = h_nk if p_nk == a_nk else a_nk
                                f_opp = b["h2h"].get(opp_nk)

                                if f_opp:
                                    hedge = evaluate_buy_hedge_from_asks(
                                        book.get("asks", []),
                                        f_opp,
                                    )

                                    if hedge.total_outlay > 0:
                                        roi = round(
                                            float(
                                                (hedge.locked_profit / hedge.total_outlay)
                                                * 100
                                            ),
                                            2,
                                        )
                                    else:
                                        roi = 0.0

                                    passed_roi_filter = 0 < roi < 15.0
                                    was_profitable = (
                                        hedge.passes_liquidity_filter
                                        and passed_roi_filter
                                    )

                                    log_raw_detection_to_csv(
                                        category="NBA",
                                        opportunity_type="POLY_FIAT",
                                        market="ML",
                                        home_team=x["home"],
                                        away_team=x["away"],
                                        game_date=format_to_local(x["time"]),
                                        bookmaker=b["name"],
                                        selection=opp_nk,
                                        odds_decimal=float(f_opp),
                                        opposite_selection=t_nm,
                                        opposite_odds_decimal="",
                                        poly_selection=t_nm,
                                        poly_best_ask=float(hedge.best_ask or 0),
                                        poly_vwap=float(hedge.vwap or 0),
                                        poly_marginal_price=float(
                                            hedge.marginal_price or 0
                                        ),
                                        total_outlay=float(hedge.total_outlay),
                                        locked_profit=float(hedge.locked_profit),
                                        roi_percent=roi,
                                        passed_liquidity_filter=hedge.passes_liquidity_filter,
                                        passed_roi_filter=passed_roi_filter,
                                        was_profitable=was_profitable,
                                        reject_reason=""
                                        if was_profitable
                                        else str(
                                            hedge.reject_reason
                                            or "Outside ROI filter"
                                        ),
                                        notes="Polymarket vs fiat moneyline check",
                                        run_id=run_id,
                                    )

                                    if hedge.passes_liquidity_filter:
                                        logger.info(
                                            f"   [ML] {b['name']:<12} | {t_nm[:10]:<10} | {b['name']}: {float(f_opp):<5} | ROI: {roi}% | Status: ✅"
                                        )

                                        if passed_roi_filter:
                                            opportunities.append(
                                                _build_opp(
                                                    x,
                                                    b["name"],
                                                    f_opp,
                                                    hedge,
                                                    "ML",
                                                    t_nm,
                                                    opp_nk,
                                                    roi,
                                                    0.0,
                                                    0.0,
                                                )
                                            )
                                        else:
                                            logger.info(
                                                f"      ↳ ⚠️ Alert Skipped: ROI {roi}% is outside safe bounds (0-15%)"
                                            )
                                    else:
                                        logger.info(
                                            f"   [ML] {b['name']:<12} | {t_nm[:10]:<10} | {b['name']}: {float(f_opp):<5} | Status: ❌ {hedge.reject_reason}"
                                        )

        logger.info("\n" + "=" * 80)

        final_alerts = build_global_alerts(
            opportunities,
            fiat_opportunities,
            limit=3,
        )

        for msg in final_alerts:
            clients.send_telegram_alert(msg)

        logger.info(f"✅ SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("=" * 80)

    finally:
        clients.close()


def _build_fiat_opp(x, b1, b2, o1, o2, m, s1, s2, imp, roi):
    payout = 100.0 / float(imp)

    return FiatArbitrageOpportunity(
        x["sport_key"],
        x["home"],
        x["away"],
        format_to_local(x["time"]),
        m,
        b1,
        s1,
        float(o1),
        payout / float(o1),
        b2,
        s2,
        float(o2),
        payout / float(o2),
        float(imp),
        payout,
        roi,
    )


def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity(
        "nba",
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