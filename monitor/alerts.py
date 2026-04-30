from __future__ import annotations
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity

# ==========================================
# NBA ALERT BUILDERS
# ==========================================
def build_global_alerts(poly_opps: list[ArbitrageOpportunity], fiat_opps: list[FiatArbitrageOpportunity], limit: int = 3) -> list[str]:
    if limit <= 0:
        return []

    # Combine and sort all opportunities globally by profit
    all_opps = []
    for o in poly_opps: 
        all_opps.append({'profit': o.expected_profit_percent, 'msg': format_opportunity_alert(o)})
    for o in fiat_opps: 
        all_opps.append({'profit': o.expected_profit_percent, 'msg': format_fiat_opportunity_alert(o)})
    
    # Sort from highest profit to lowest
    sorted_opps = sorted(all_opps, key=lambda x: x['profit'], reverse=True)
    
    # Remove duplicates
    unique_messages: dict[str, str] = {}
    for item in sorted_opps:
        if item['msg'] not in unique_messages:
            unique_messages[item['msg']] = item['msg']

    return list(unique_messages.values())[:limit]


def format_opportunity_alert(op: ArbitrageOpportunity) -> str:
    # We combine the spend and the fee so you only have to type one number into Polymarket
    poly_total = op.poly_spend + op.poly_fees
    
    return (
        f"🚨 POLYMARKET ARB ALERT 🚨\n\n"
        f"🏀 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ EXECUTION CALCULATOR (${op.total_outlay:.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Bet ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Enter ${poly_total:.2f} on Poly for '{op.selection_name}'\n\n"
        f"✅ GUARANTEED NET PROFIT: ${op.locked_profit:.2f}"
    )


def format_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)
    return (
        f"🚨 TRADITIONAL FIAT ARB ALERT 🚨\n\n"
        f"🏀 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ HEDGE CALCULATOR (${(op.stake_1 + op.stake_2):.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Bet ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Bet ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ GUARANTEED NET PROFIT: ${net_profit:.2f}"
    )


def build_no_opportunities_message() -> str:
    return "⚖️ Markets efficient. No arbitrage gaps found."


# ==========================================
# MMA / UFC ALERT BUILDERS
# ==========================================
def build_mma_global_alerts(poly_opps: list[ArbitrageOpportunity], fiat_opps: list[FiatArbitrageOpportunity], limit: int = 3) -> list[str]:
    if limit <= 0: return []
    all_opps = []
    for o in poly_opps: all_opps.append({'profit': o.expected_profit_percent, 'msg': format_mma_opportunity_alert(o)})
    for o in fiat_opps: all_opps.append({'profit': o.expected_profit_percent, 'msg': format_mma_fiat_opportunity_alert(o)})
    sorted_opps = sorted(all_opps, key=lambda x: x['profit'], reverse=True)
    
    unique_messages: dict[str, str] = {}
    for item in sorted_opps:
        if item['msg'] not in unique_messages:
            unique_messages[item['msg']] = item['msg']
    return list(unique_messages.values())[:limit]


def format_mma_opportunity_alert(op: ArbitrageOpportunity) -> str:
    poly_total = op.poly_spend + op.poly_fees
    return (
        f"🥊 UFC ARB ALERT 🥊\n\n"
        f"🥋 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ EXECUTION CALCULATOR (${op.total_outlay:.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Bet ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Enter ${poly_total:.2f} on Poly for '{op.selection_name}'\n\n"
        f"✅ GUARANTEED NET PROFIT: ${op.locked_profit:.2f}\n"
        f"⚠️ WARNING: DRAW/NC RISK. If fight is a Draw/No Contest, fiat books refund but Poly resolves NO."
    )


def format_mma_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)
    return (
        f"🥊 UFC TRADITIONAL FIAT ARB 🥊\n\n"
        f"🥋 MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ HEDGE CALCULATOR (${(op.stake_1 + op.stake_2):.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Bet ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Bet ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ GUARANTEED NET PROFIT: ${net_profit:.2f}"
    )

# ==========================================
# SOCCER / FOOTBALL ALERT BUILDERS
# ==========================================
def build_soccer_global_alerts(poly_opps: list[ArbitrageOpportunity], fiat_opps: list[FiatArbitrageOpportunity], limit: int = 3) -> list[str]:
    if limit <= 0: return []
    all_opps = []
    for o in poly_opps: all_opps.append({'profit': o.expected_profit_percent, 'msg': format_soccer_opportunity_alert(o)})
    for o in fiat_opps: all_opps.append({'profit': o.expected_profit_percent, 'msg': format_soccer_fiat_opportunity_alert(o)})
    sorted_opps = sorted(all_opps, key=lambda x: x['profit'], reverse=True)
    
    unique_messages: dict[str, str] = {}
    for item in sorted_opps:
        if item['msg'] not in unique_messages:
            unique_messages[item['msg']] = item['msg']
    return list(unique_messages.values())[:limit]

def format_soccer_opportunity_alert(op: ArbitrageOpportunity) -> str:
    poly_total = op.poly_spend + op.poly_fees
    return (
        f"⚽ SOCCER ARB ALERT ⚽\n\n"
        f"🏟️ MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ EXECUTION CALCULATOR (${op.total_outlay:.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.shares:.2f}\n"
        f"▪️ Bet ${op.sportsbook_stake:.2f} on '{op.fiat_selection}' at {op.bookmaker} ({op.odds_decimal:.2f})\n"
        f"▪️ Enter ${poly_total:.2f} on Poly for '{op.selection_name}'\n\n"
        f"✅ GUARANTEED NET PROFIT: ${op.locked_profit:.2f}"
    )

def format_soccer_fiat_opportunity_alert(op: FiatArbitrageOpportunity) -> str:
    net_profit = op.payout - (op.stake_1 + op.stake_2)
    return (
        f"⚽ SOCCER TRADITIONAL FIAT ARB ⚽\n\n"
        f"🏟️ MATCHUP: {op.home_team} vs {op.away_team}\n"
        f"📅 DATE: {op.commence_time}\n"
        f"🎯 MARKET: {op.market_title}\n"
        f"💵 NET PROFIT MARGIN: {op.expected_profit_percent:.2f}%\n\n"
        f"🛠️ HEDGE CALCULATOR (${(op.stake_1 + op.stake_2):.2f} Bankroll):\n"
        f"💰 TARGET PAYOUT ON BOTH SIDES: ${op.payout:.2f}\n"
        f"▪️ Bet ${op.stake_1:.2f} on '{op.selection_1}' at {op.bookmaker_1} ({op.odds_1:.2f})\n"
        f"▪️ Bet ${op.stake_2:.2f} on '{op.selection_2}' at {op.bookmaker_2} ({op.odds_2:.2f})\n\n"
        f"✅ GUARANTEED NET PROFIT: ${net_profit:.2f}"
    )
