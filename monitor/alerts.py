from __future__ import annotations

import csv
import hashlib
import os
import uuid
from datetime import datetime, timezone

from .models import ArbitrageOpportunity, FiatArbitrageOpportunity


# ============================================================
# CSV LOGGING CONFIG
# ============================================================

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data"
)

OPPORTUNITY_DATA_DIR = os.path.join(DATA_DIR, "opportunities")
RAW_DATA_DIR = os.path.join(DATA_DIR, "raw_detections")


def current_month_file(prefix: str) -> str:
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"{prefix}_{month_key}.csv"


def current_week_file(prefix: str) -> str:
    year, week_number, _ = datetime.now(timezone.utc).isocalendar()
    return f"{prefix}_{year}-W{week_number:02d}.csv"


def opportunity_observations_file() -> str:
    return os.path.join(
        OPPORTUNITY_DATA_DIR,
        current_month_file("arbitrage_opportunities")
    )


def raw_detections_file() -> str:
    return os.path.join(
        RAW_DATA_DIR,
        current_week_file("arbitrage_raw_detections")
    )


OPPORTUNITY_OBSERVATION_FIELDS = [
    "ObservationID",
    "RunID",
    "OpportunityKey",
    "Category",
    "OpportunityType",
    "Market",
    "Matchup",
    "HomeTeam",
    "AwayTeam",
    "GameDate",
    "ObservedAtUTC",
    "AlertRank",
    "WasSentToTelegram",
    "Bookmaker1",
    "Selection1",
    "Odds1",
    "Stake1",
    "Bookmaker2",
    "Selection2",
    "Odds2",
    "Stake2",
    "PolySelection",
    "PolyVWAP",
    "PolyMarginalPrice",
    "PolySpend",
    "PolyFees",
    "TotalOutlay",
    "LockedProfit",
    "ROI_Percent",
]


RAW_DETECTION_FIELDS = [
    "RawDetectionID",
    "RunID",
    "DetectionKey",
    "Category",
    "OpportunityType",
    "Market",
    "Matchup",
    "HomeTeam",
    "AwayTeam",
    "GameDate",
    "ObservedAtUTC",
    "Bookmaker",
    "Selection",
    "OddsDecimal",
    "OppositeSelection",
    "OppositeOddsDecimal",
    "PolySelection",
    "PolyBestAsk",
    "PolyVWAP",
    "PolyMarginalPrice",
    "ImpliedTotal",
    "TotalOutlay",
    "LockedProfit",
    "ROI_Percent",
    "PassedLiquidityFilter",
    "PassedROIFilter",
    "WasProfitable",
    "RejectReason",
    "Notes",
]


# ============================================================
# CSV HELPERS
# ============================================================

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_run_id(category: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"{category.upper()}-{timestamp}-{suffix}"


def safe_value(value) -> str:
    if value is None:
        return ""
    return str(value)


def safe_float(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.6f}"
    except Exception:
        return str(value)


def make_stable_key(*parts) -> str:
    raw = "|".join(safe_value(part).strip().lower() for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20].upper()


def ensure_csv_folder(path: str) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)


def append_csv_row(path: str, fieldnames: list[str], row: dict) -> None:
    ensure_csv_folder(path)
    file_exists = os.path.isfile(path)

    with open(path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        cleaned_row = {field: row.get(field, "") for field in fieldnames}
        writer.writerow(cleaned_row)


# ============================================================
# PROFITABLE OPPORTUNITY OBSERVATION LOGGER
# This logs ALL detected profitable opportunities, not only top 3.
# File is stored monthly.
# ============================================================

def log_profitable_opportunities_to_csv(
    category: str,
    all_unique_items: list[dict],
    telegram_limit: int = 3,
    run_id: str | None = None,
) -> None:
    if not all_unique_items:
        return

    if run_id is None:
        run_id = create_run_id(category)

    observed_at = now_utc()

    for index, item in enumerate(all_unique_items, start=1):
        op = item["obj"]
        alert_rank = index
        was_sent_to_telegram = index <= telegram_limit

        if isinstance(op, ArbitrageOpportunity):
            opportunity_type = "POLY_FIAT"
            matchup = f"{op.home_team} vs {op.away_team}"

            opportunity_key = make_stable_key(
                category,
                opportunity_type,
                op.home_team,
                op.away_team,
                op.commence_time,
                op.market_title,
                op.bookmaker,
                op.selection_name,
                op.fiat_selection,
            )

            row = {
                "ObservationID": uuid.uuid4().hex,
                "RunID": run_id,
                "OpportunityKey": opportunity_key,
                "Category": category.upper(),
                "OpportunityType": opportunity_type,
                "Market": op.market_title,
                "Matchup": matchup,
                "HomeTeam": op.home_team,
                "AwayTeam": op.away_team,
                "GameDate": op.commence_time,
                "ObservedAtUTC": observed_at,
                "AlertRank": alert_rank,
                "WasSentToTelegram": was_sent_to_telegram,

                "Bookmaker1": op.bookmaker,
                "Selection1": op.fiat_selection,
                "Odds1": safe_float(op.odds_decimal),
                "Stake1": safe_float(op.sportsbook_stake),

                "Bookmaker2": "Polymarket",
                "Selection2": op.selection_name,
                "Odds2": "",
                "Stake2": safe_float(op.poly_spend + op.poly_fees),

                "PolySelection": op.selection_name,
                "PolyVWAP": safe_float(op.vwap),
                "PolyMarginalPrice": safe_float(op.marginal_price),
                "PolySpend": safe_float(op.poly_spend),
                "PolyFees": safe_float(op.poly_fees),
                "TotalOutlay": safe_float(op.total_outlay),
                "LockedProfit": safe_float(op.locked_profit),
                "ROI_Percent": safe_float(op.expected_profit_percent),
            }

        elif isinstance(op, FiatArbitrageOpportunity):
            opportunity_type = "FIAT_FIAT"
            matchup = f"{op.home_team} vs {op.away_team}"

            opportunity_key = make_stable_key(
                category,
                opportunity_type,
                op.home_team,
                op.away_team,
                op.commence_time,
                op.market_title,
                op.bookmaker_1,
                op.selection_1,
                op.bookmaker_2,
                op.selection_2,
            )

            total_outlay = op.stake_1 + op.stake_2
            locked_profit = op.payout - total_outlay

            row = {
                "ObservationID": uuid.uuid4().hex,
                "RunID": run_id,
                "OpportunityKey": opportunity_key,
                "Category": category.upper(),
                "OpportunityType": opportunity_type,
                "Market": op.market_title,
                "Matchup": matchup,
                "HomeTeam": op.home_team,
                "AwayTeam": op.away_team,
                "GameDate": op.commence_time,
                "ObservedAtUTC": observed_at,
                "AlertRank": alert_rank,
                "WasSentToTelegram": was_sent_to_telegram,

                "Bookmaker1": op.bookmaker_1,
                "Selection1": op.selection_1,
                "Odds1": safe_float(op.odds_1),
                "Stake1": safe_float(op.stake_1),

                "Bookmaker2": op.bookmaker_2,
                "Selection2": op.selection_2,
                "Odds2": safe_float(op.odds_2),
                "Stake2": safe_float(op.stake_2),

                "PolySelection": "",
                "PolyVWAP": "",
                "PolyMarginalPrice": "",
                "PolySpend": "",
                "PolyFees": "",
                "TotalOutlay": safe_float(total_outlay),
                "LockedProfit": safe_float(locked_profit),
                "ROI_Percent": safe_float(op.expected_profit_percent),
            }

        else:
            continue

        append_csv_row(
            opportunity_observations_file(),
            OPPORTUNITY_OBSERVATION_FIELDS,
            row,
        )


# ============================================================
# RAW DETECTION LOGGER
# This is for every checked candidate, profitable or not.
# File is stored weekly.
# Runner files need to call this inside their scan loops.
# ============================================================

def log_raw_detection_to_csv(
    category: str,
    opportunity_type: str,
    market: str,
    home_team: str,
    away_team: str,
    game_date: str,
    bookmaker: str = "",
    selection: str = "",
    odds_decimal: float | str = "",
    opposite_selection: str = "",
    opposite_odds_decimal: float | str = "",
    poly_selection: str = "",
    poly_best_ask: float | str = "",
    poly_vwap: float | str = "",
    poly_marginal_price: float | str = "",
    implied_total: float | str = "",
    total_outlay: float | str = "",
    locked_profit: float | str = "",
    roi_percent: float | str = "",
    passed_liquidity_filter: bool | str = "",
    passed_roi_filter: bool | str = "",
    was_profitable: bool | str = "",
    reject_reason: str = "",
    notes: str = "",
    run_id: str | None = None,
) -> None:
    if run_id is None:
        run_id = create_run_id(category)

    matchup = f"{home_team} vs {away_team}"

    detection_key = make_stable_key(
        category,
        opportunity_type,
        market,
        home_team,
        away_team,
        game_date,
        bookmaker,
        selection,
        opposite_selection,
        poly_selection,
    )

    row = {
        "RawDetectionID": uuid.uuid4().hex,
        "RunID": run_id,
        "DetectionKey": detection_key,
        "Category": category.upper(),
        "OpportunityType": opportunity_type,
        "Market": market,
        "Matchup": matchup,
        "HomeTeam": home_team,
        "AwayTeam": away_team,
        "GameDate": game_date,
        "ObservedAtUTC": now_utc(),

        "Bookmaker": bookmaker,
        "Selection": selection,
        "OddsDecimal": safe_float(odds_decimal),
        "OppositeSelection": opposite_selection,
        "OppositeOddsDecimal": safe_float(opposite_odds_decimal),

        "PolySelection": poly_selection,
        "PolyBestAsk": safe_float(poly_best_ask),
        "PolyVWAP": safe_float(poly_vwap),
        "PolyMarginalPrice": safe_float(poly_marginal_price),

        "ImpliedTotal": safe_float(implied_total),
        "TotalOutlay": safe_float(total_outlay),
        "LockedProfit": safe_float(locked_profit),
        "ROI_Percent": safe_float(roi_percent),

        "PassedLiquidityFilter": passed_liquidity_filter,
        "PassedROIFilter": passed_roi_filter,
        "WasProfitable": was_profitable,
        "RejectReason": reject_reason,
        "Notes": notes,
    }

    append_csv_row(
        raw_detections_file(),
        RAW_DETECTION_FIELDS,
        row,
    )


# ============================================================
# NBA ALERT BUILDERS
# ============================================================

def build_global_alerts(
    poly_opps: list[ArbitrageOpportunity],
    fiat_opps: list[FiatArbitrageOpportunity],
    limit: int = 3,
) -> list[str]:
    if limit <= 0:
        return []

    all_opps = []

    for o in poly_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_opportunity_alert(o),
            "obj": o,
        })

    for o in fiat_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_fiat_opportunity_alert(o),
            "obj": o,
        })

    sorted_opps = sorted(all_opps, key=lambda x: x["profit"], reverse=True)

    unique_messages: dict[str, str] = {}
    unique_objs = []

    for item in sorted_opps:
        if item["msg"] not in unique_messages:
            unique_messages[item["msg"]] = item["msg"]
            unique_objs.append(item)

    top_items = unique_objs[:limit]

    log_profitable_opportunities_to_csv(
        category="NBA",
        all_unique_items=unique_objs,
        telegram_limit=limit,
    )

    return [item["msg"] for item in top_items]


def format_opportunity_alert(op: ArbitrageOpportunity) -> str:
    poly_total = op.poly_spend + op.poly_fees

    return (
        f"🚨 POLYMARKET ARB ALERT 🚨\n\n"
        f"🏀 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${op.total_outlay:.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Fiat side: ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Market side amount: ${poly_total:.2f} for '{op.selection_name}'\n\n"
        f"✅ MODEL NET EDGE: ${op.locked_profit:.2f}"
    )


def format_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)

    return (
        f"🚨 TRADITIONAL FIAT ARB ALERT 🚨\n\n"
        f"🏀 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${(op.stake_1 + op.stake_2):.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Side 1: ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Side 2: ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ MODEL NET EDGE: ${net_profit:.2f}"
    )


def build_no_opportunities_message() -> str:
    return "⚖️ Markets efficient. No arbitrage gaps found."


# ============================================================
# MMA / UFC ALERT BUILDERS
# ============================================================

def build_mma_global_alerts(
    poly_opps: list[ArbitrageOpportunity],
    fiat_opps: list[FiatArbitrageOpportunity],
    limit: int = 3,
) -> list[str]:
    if limit <= 0:
        return []

    all_opps = []

    for o in poly_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_mma_opportunity_alert(o),
            "obj": o,
        })

    for o in fiat_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_mma_fiat_opportunity_alert(o),
            "obj": o,
        })

    sorted_opps = sorted(all_opps, key=lambda x: x["profit"], reverse=True)

    unique_messages: dict[str, str] = {}
    unique_objs = []

    for item in sorted_opps:
        if item["msg"] not in unique_messages:
            unique_messages[item["msg"]] = item["msg"]
            unique_objs.append(item)

    top_items = unique_objs[:limit]

    log_profitable_opportunities_to_csv(
        category="UFC",
        all_unique_items=unique_objs,
        telegram_limit=limit,
    )

    return [item["msg"] for item in top_items]


def format_mma_opportunity_alert(op: ArbitrageOpportunity) -> str:
    poly_total = op.poly_spend + op.poly_fees

    return (
        f"🥊 UFC ARB ALERT 🥊\n\n"
        f"🥋 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${op.total_outlay:.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Fiat side: ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Market side amount: ${poly_total:.2f} for '{op.selection_name}'\n\n"
        f"✅ MODEL NET EDGE: ${op.locked_profit:.2f}\n"
        f"⚠️ WARNING: Draw/No Contest rules may differ by market source."
    )


def format_mma_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)

    return (
        f"🥊 UFC TRADITIONAL FIAT ARB 🥊\n\n"
        f"🥋 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${(op.stake_1 + op.stake_2):.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Side 1: ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Side 2: ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ MODEL NET EDGE: ${net_profit:.2f}"
    )


# ============================================================
# SOCCER / FOOTBALL ALERT BUILDERS
# ============================================================

def build_soccer_global_alerts(
    poly_opps: list[ArbitrageOpportunity],
    fiat_opps: list[FiatArbitrageOpportunity],
    limit: int = 3,
) -> list[str]:
    if limit <= 0:
        return []

    all_opps = []

    for o in poly_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_soccer_opportunity_alert(o),
            "obj": o,
        })

    for o in fiat_opps:
        all_opps.append({
            "profit": o.expected_profit_percent,
            "msg": format_soccer_fiat_opportunity_alert(o),
            "obj": o,
        })

    sorted_opps = sorted(all_opps, key=lambda x: x["profit"], reverse=True)

    unique_messages: dict[str, str] = {}
    unique_objs = []

    for item in sorted_opps:
        if item["msg"] not in unique_messages:
            unique_messages[item["msg"]] = item["msg"]
            unique_objs.append(item)

    top_items = unique_objs[:limit]

    log_profitable_opportunities_to_csv(
        category="SOCCER",
        all_unique_items=unique_objs,
        telegram_limit=limit,
    )

    return [item["msg"] for item in top_items]


def format_soccer_opportunity_alert(op: ArbitrageOpportunity) -> str:
    poly_total = op.poly_spend + op.poly_fees

    return (
        f"⚽ SOCCER ARB ALERT ⚽\n\n"
        f"🏟️ MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${op.total_outlay:.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Fiat side: ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Market side amount: ${poly_total:.2f} for '{op.selection_name}'\n\n"
        f"✅ MODEL NET EDGE: ${op.locked_profit:.2f}"
    )


def format_soccer_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)

    return (
        f"⚽ SOCCER TRADITIONAL FIAT ARB ⚽\n\n"
        f"🏟️ MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ ANALYSIS SNAPSHOT (${(op.stake_1 + op.stake_2):.2f} model bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Side 1: ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Side 2: ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ MODEL NET EDGE: ${net_profit:.2f}"
    )