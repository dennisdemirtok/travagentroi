"""Backlog sync — synkronisera betting backlog till/från Supabase."""

from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _get_client():
    from .client import get_client
    return get_client()


def sync_backlog_entry(entry: dict, client=None) -> bool:
    """Upsert one backlog entry + its race picks to Supabase."""
    if client is None:
        client = _get_client()

    try:
        # Build round data
        round_data = {
            "round_date": entry.get("date", ""),
            "game_type": entry.get("game_type", ""),
            "track": entry.get("track", ""),
            "strategy": entry.get("strategy", ""),
            "cost": entry.get("cost", 0) or 0,
            "rows_count": entry.get("rows", 0) or 0,
            "num_correct": entry.get("num_correct", 0) or 0,
            "num_races": entry.get("num_races", 0) or 0,
            "hit": entry.get("hit", False),
            "payout": entry.get("payout", 0) or 0,
            "avg_confidence": entry.get("avg_confidence", 0) or 0,
            "avg_upset": entry.get("avg_upset", 0) or 0,
            "is_live": entry.get("live", False),
            "races_finished": entry.get("races_finished", 0) or 0,
            "partial_correct": entry.get("partial_correct", 0) or 0,
        }

        # Upsert round
        result = client.table("backlog_rounds").upsert(
            round_data, on_conflict="round_date,game_type,strategy"
        ).execute()

        if not result.data:
            return False

        round_id = result.data[0]["id"]

        # Upsert race picks
        races = entry.get("races", [])
        if races:
            picks_batch = []
            for race in races:
                pick_list = race.get("pick_list", [])
                picks_batch.append({
                    "backlog_round_id": round_id,
                    "race_number": race.get("avd", 0),
                    "distance": race.get("dist", 0),
                    "start_method": race.get("metod", ""),
                    "num_starters": race.get("startande", 0),
                    "confidence": race.get("conf", 0),
                    "upset_risk": race.get("upset", 0),
                    "num_picks": race.get("picks", 0),
                    "pick_list": pick_list,
                    "coverage": race.get("tackning", 0),
                    "winner": race.get("vinnare"),
                    "winner_name": race.get("vinnare_namn", ""),
                    "winner_streck": race.get("vinnare_streck", 0),
                    "correct": race.get("ratt"),
                    "status": "finished" if race.get("ratt") is not None else "pending",
                })

            if picks_batch:
                for i in range(0, len(picks_batch), 50):
                    batch = picks_batch[i:i+50]
                    client.table("backlog_race_picks").upsert(
                        batch, on_conflict="backlog_round_id,race_number"
                    ).execute()

        return True
    except Exception as e:
        logger.error(f"Backlog sync error: {e}")
        return False


def sync_all_backlog(backlog_data: dict, client=None, progress_callback=None) -> dict:
    """Sync all backlog entries to Supabase. Returns stats."""
    if client is None:
        client = _get_client()

    entries = backlog_data.get("entries", [])
    total = len(entries)
    synced = 0
    errors = 0

    for i, entry in enumerate(entries):
        if sync_backlog_entry(entry, client=client):
            synced += 1
        else:
            errors += 1

        if progress_callback and (i + 1) % 100 == 0:
            progress_callback(i + 1, total)

    return {"total": total, "synced": synced, "errors": errors}


def load_backlog_from_supabase(
    client=None,
    strategy: str = None,
    game_type: str = None,
    limit: int = 2000,
) -> dict:
    """Load backlog data from Supabase in the same format as backlog.json."""
    if client is None:
        client = _get_client()

    query = client.table("backlog_rounds").select("*").order("round_date", desc=True).limit(limit)

    if strategy:
        query = query.eq("strategy", strategy)
    if game_type:
        query = query.eq("game_type", game_type)

    result = query.execute()
    rounds = result.data if result.data else []

    # Convert to backlog.json format
    entries = []
    for r in rounds:
        entry = {
            "date": r.get("round_date", ""),
            "game_type": r.get("game_type", ""),
            "track": r.get("track", ""),
            "strategy": r.get("strategy", ""),
            "cost": r.get("cost", 0),
            "rows": r.get("rows_count", 0),
            "num_correct": r.get("num_correct", 0),
            "num_races": r.get("num_races", 0),
            "hit": r.get("hit", False),
            "payout": r.get("payout", 0),
            "avg_confidence": r.get("avg_confidence", 0),
            "avg_upset": r.get("avg_upset", 0),
            "live": r.get("is_live", False),
        }

        # Load race picks for this round
        picks_result = client.table("backlog_race_picks").select("*").eq(
            "backlog_round_id", r["id"]
        ).order("race_number").execute()

        if picks_result.data:
            entry["races"] = [
                {
                    "avd": p.get("race_number", 0),
                    "dist": p.get("distance", 0),
                    "metod": p.get("start_method", ""),
                    "startande": p.get("num_starters", 0),
                    "conf": p.get("confidence", 0),
                    "upset": p.get("upset_risk", 0),
                    "picks": p.get("num_picks", 0),
                    "pick_list": p.get("pick_list", []),
                    "tackning": p.get("coverage", 0),
                    "vinnare": p.get("winner"),
                    "vinnare_namn": p.get("winner_name", ""),
                    "vinnare_streck": p.get("winner_streck", 0),
                    "ratt": p.get("correct"),
                }
                for p in picks_result.data
            ]

        entries.append(entry)

    # Build strategy summaries
    strategies = {}
    for e in entries:
        s = e.get("strategy", "")
        if s not in strategies:
            strategies[s] = {"rounds": 0, "cost": 0, "payout": 0, "hits": 0}
        strategies[s]["rounds"] += 1
        strategies[s]["cost"] += e.get("cost", 0) or 0
        strategies[s]["payout"] += e.get("payout", 0) or 0
        if e.get("hit"):
            strategies[s]["hits"] += 1

    return {"entries": entries, "strategies": strategies}
