"""Bet result sync — spara och hämta vinnarspel-resultat från Supabase.

Tracks chansspik flat bet candidates (model rank≤2, streck 5-20%)
and their results for the Bet tab P&L tracking.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _get_client():
    from .client import get_client
    return get_client()


def _classify_profile(model_rank: int, streck: float, driver_starts: int, score: float) -> str:
    """Classify a bet candidate into a vinnarspel profile.

    Profiles (from no-leakage backtest, 169 rounds):
      Sniper (+79.5% ROI): rank≤2, streck 3-10%, kusk≥100
      Pro    (+16.3% ROI): rank≤2, streck 5-20%, kusk≥100
      Sharp  (+8.1% ROI):  rank≤2, streck 5-20%, kusk≥100, score≥45
      Bas    (+8.7% ROI):  rank≤2, streck 5-20% (no kusk filter)
    """
    if model_rank > 2:
        return "none"
    if driver_starts >= 100:
        if 0.03 <= streck <= 0.10:
            return "sniper"
        if 0.05 <= streck <= 0.20:
            if score >= 45:
                return "sharp"
            return "pro"
    if 0.05 <= streck <= 0.20:
        return "bas"
    if 0.03 <= streck < 0.05:
        return "bas"  # low-streck without kusk filter
    return "none"


def extract_vinnarspel_candidates(game_round) -> list[dict]:
    """Extract vinnarspel candidates from an analyzed game round.

    Criteria: model rank ≤ 2, streck 3-20%.
    Each candidate is classified into a profile (sniper/pro/sharp/bas).
    Returns list of candidate dicts ready for Supabase upsert.
    """
    candidates = []
    is_finished = game_round.is_finished
    rd = str(game_round.round_date) if game_round.round_date else ""
    gt = game_round.game_type or ""

    # Get track name from first race
    track_name = ""
    if game_round.races:
        track_name = getattr(game_round.races[0], "track_name", "") or ""

    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        for rank_idx, e in enumerate(sorted_entries):
            model_rank = rank_idx + 1
            if model_rank > 2:
                break
            streck = e.bet_percentage or 0
            if streck < 0.03 or streck > 0.20:
                continue

            odds_est = round(1 / streck, 1) if streck > 0.01 else 0
            driver_starts = getattr(e, "driver_starts_year", 0) or 0
            score = e.super_score or 0

            # Classify into profile
            profile = _classify_profile(model_rank, streck, driver_starts, score)

            # Check result if finished
            won = False
            placement = None
            actual_odds = 0.0
            if is_finished and race.result_order:
                for pos, num in enumerate(race.result_order, 1):
                    if num == e.post_position:
                        placement = pos
                        break
                won = placement == 1
                if won:
                    # Use actual win odds if available, else estimate from streck
                    actual_odds = getattr(race, "win_odds", 0) or odds_est

            candidates.append({
                "round_date": rd,
                "game_type": gt,
                "track_name": track_name,
                "race_number": race.race_number,
                "horse_name": e.horse.name,
                "post_position": e.post_position,
                "model_rank": model_rank,
                "streck_pct": round(streck, 4),
                "composite_score": round(score, 1),
                "odds_estimated": odds_est,
                "driver_name": e.driver_name or "",
                "driver_starts_year": driver_starts,
                "profile": profile,
                "bet_amount": 500,
                "won": won,
                "placement": placement,
                "actual_odds": actual_odds if won else 0,
                "payout": round(500 * actual_odds, 0) if won else 0,
                "round_finished": is_finished,
            })

    return candidates


def sync_bet_results(game_round, client=None) -> dict:
    """Sync vinnarspel candidates from a game round to Supabase.

    Returns: {"synced": int, "errors": int}
    """
    if client is None:
        client = _get_client()

    candidates = extract_vinnarspel_candidates(game_round)
    if not candidates:
        return {"synced": 0, "errors": 0}

    synced = 0
    errors = 0
    for c in candidates:
        try:
            client.table("bet_results").upsert(
                c,
                on_conflict="round_date,game_type,race_number,post_position",
            ).execute()
            synced += 1
        except Exception as e:
            logger.error(f"Bet sync error for {c['horse_name']}: {e}")
            errors += 1

    logger.info(f"Bet sync: {synced} synced, {errors} errors for {game_round.game_type}/{game_round.round_date}")
    return {"synced": synced, "errors": errors}


def load_bet_history(
    client=None,
    game_type: Optional[str] = None,
    profile: Optional[str] = None,
    days_back: int = 365,
    limit: int = 2000,
) -> dict:
    """Load bet results from Supabase for P&L tracking.

    Args:
        profile: Filter by profile (sniper/pro/sharp/bas). None = all.
                 "profitable" = pro + sniper + sharp (kuskfilter-profiler).
    Returns:
        {
            "bets": [...],
            "summary": {...},
            "by_period": {...},
            "by_profile": {...},
        }
    """
    if client is None:
        client = _get_client()

    cutoff = date.today() - timedelta(days=days_back)
    query = (
        client.table("bet_results")
        .select("*")
        .gte("round_date", str(cutoff))
        .order("round_date", desc=True)
        .limit(limit)
    )
    if game_type:
        query = query.eq("game_type", game_type.upper())

    result = query.execute()
    all_bets = result.data if result.data else []

    # Filter by profile
    PROFITABLE_PROFILES = {"sniper", "pro", "sharp"}
    if profile == "profitable":
        bets = [b for b in all_bets if b.get("profile") in PROFITABLE_PROFILES]
    elif profile:
        bets = [b for b in all_bets if b.get("profile") == profile]
    else:
        bets = all_bets

    # Summary
    total_bets = len(bets)
    total_staked = sum(b.get("bet_amount", 500) for b in bets)
    total_returned = sum(b.get("payout", 0) for b in bets)
    total_pnl = total_returned - total_staked
    wins = sum(1 for b in bets if b.get("won"))
    finished_bets = [b for b in bets if b.get("round_finished")]
    finished_count = len(finished_bets)

    summary = {
        "total_bets": total_bets,
        "total_staked": total_staked,
        "total_returned": total_returned,
        "total_pnl": total_pnl,
        "roi_pct": (total_pnl / total_staked * 100) if total_staked > 0 else 0,
        "wins": wins,
        "win_rate": (wins / finished_count * 100) if finished_count > 0 else 0,
        "finished_count": finished_count,
        "pending_count": total_bets - finished_count,
    }

    # Aggregate by period
    from collections import defaultdict

    daily = defaultdict(lambda: {"bets": 0, "staked": 0, "returned": 0, "wins": 0})
    weekly = defaultdict(lambda: {"bets": 0, "staked": 0, "returned": 0, "wins": 0})
    monthly = defaultdict(lambda: {"bets": 0, "staked": 0, "returned": 0, "wins": 0})

    for b in bets:
        rd = b.get("round_date", "")
        if not rd:
            continue
        amt = b.get("bet_amount", 500)
        pay = b.get("payout", 0)
        w = 1 if b.get("won") else 0

        # Daily (last 14 days)
        daily[rd]["bets"] += 1
        daily[rd]["staked"] += amt
        daily[rd]["returned"] += pay
        daily[rd]["wins"] += w

        # Weekly (ISO week)
        try:
            d = date.fromisoformat(rd)
            week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
            weekly[week_key]["bets"] += 1
            weekly[week_key]["staked"] += amt
            weekly[week_key]["returned"] += pay
            weekly[week_key]["wins"] += w
        except ValueError:
            pass

        # Monthly
        month_key = rd[:7]  # YYYY-MM
        monthly[month_key]["bets"] += 1
        monthly[month_key]["staked"] += amt
        monthly[month_key]["returned"] += pay
        monthly[month_key]["wins"] += w

    # Add ROI to each period
    for period_data in [daily, weekly, monthly]:
        for k, v in period_data.items():
            pnl = v["returned"] - v["staked"]
            v["pnl"] = pnl
            v["roi_pct"] = (pnl / v["staked"] * 100) if v["staked"] > 0 else 0

    by_period = {
        "day": dict(sorted(daily.items(), reverse=True)),
        "week": dict(sorted(weekly.items(), reverse=True)),
        "month": dict(sorted(monthly.items(), reverse=True)),
    }

    # Per-profile breakdown (always from ALL bets, ignoring profile filter)
    by_profile = {}
    for p_name in ["sniper", "pro", "sharp", "bas"]:
        p_bets = [b for b in all_bets if b.get("profile") == p_name and b.get("round_finished")]
        if not p_bets:
            continue
        p_staked = sum(b.get("bet_amount", 500) for b in p_bets)
        p_returned = sum(b.get("payout", 0) for b in p_bets)
        p_pnl = p_returned - p_staked
        p_wins = sum(1 for b in p_bets if b.get("won"))
        by_profile[p_name] = {
            "bets": len(p_bets),
            "wins": p_wins,
            "win_rate": (p_wins / len(p_bets) * 100) if p_bets else 0,
            "staked": p_staked,
            "returned": p_returned,
            "pnl": p_pnl,
            "roi_pct": (p_pnl / p_staked * 100) if p_staked > 0 else 0,
        }

    return {
        "bets": bets,
        "summary": summary,
        "by_period": by_period,
        "by_profile": by_profile,
    }


def sync_from_backlog(backlog_path: str = "backlog.json", client=None) -> dict:
    """Import historical vinnarspel results from backlog data.

    This reads the backlog entries that have finished rounds and
    extracts the vinnarspel candidates + results for Supabase.
    Useful for bootstrapping bet_results from existing backlog.
    """
    import json
    from pathlib import Path

    p = Path(backlog_path)
    if not p.exists():
        return {"error": f"Backlog not found: {backlog_path}"}

    if client is None:
        client = _get_client()

    with open(p) as f:
        data = json.load(f)

    entries = data.get("entries", [])
    total_synced = 0
    total_errors = 0

    logger.info(f"Syncing bet results from {len(entries)} backlog entries...")

    # We can't re-extract candidates from backlog (no model scores).
    # This function is a placeholder — real sync happens when rounds
    # are analyzed through the dashboard.
    return {
        "note": "Bet results are synced automatically when rounds are viewed in the dashboard. "
                "Historical data needs to be backfilled by re-analyzing rounds.",
        "entries_checked": len(entries),
    }
