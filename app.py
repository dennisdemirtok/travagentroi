"""FastAPI wrapper for trav-agent."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import GAME_TYPES
from trav_agent.data.atg_client import ATGClient

app = FastAPI(title="Trav Agent API", version="0.1.0")


@app.get("/")
async def root():
    return {"status": "ok", "service": "trav-agent"}


@app.get("/analyze/{game_type}/{day}")
async def analyze(game_type: str, day: str):
    game_type = game_type.upper()
    if game_type not in GAME_TYPES:
        raise HTTPException(400, f"Ogiltig spelform: {game_type}")

    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"Ogiltigt datum: {day}")

    client = ATGClient()
    game_round = await client.fetch_full_round(game_type, d)
    if not game_round:
        raise HTTPException(404, f"Ingen {game_type} hittad för {day}")

    analyzer = CompositeAnalyzer()
    analyzer.analyze_round(game_round)

    races = []
    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        entries = []
        for e in sorted_entries:
            entries.append({
                "rank": e.rank,
                "post_position": e.post_position,
                "horse": e.horse.name,
                "super_score": round(e.super_score, 1),
                "bet_percentage": round(e.bet_percentage * 100, 1) if e.bet_percentage else None,
                "recommendation": e.recommendation,
            })
        races.append({
            "race_number": race.race_number,
            "distance": race.distance,
            "start_method": race.start_method.value,
            "num_starters": race.num_starters,
            "entries": entries,
        })

    return {"game_type": game_type, "date": day, "races": races}


@app.get("/upcoming")
async def upcoming(days: int = 7):
    client = ATGClient()
    today = date.today()
    results = []

    for offset in range(days):
        d = today + timedelta(days=offset)
        try:
            calendar = await client.get_calendar(d, skip_cache=True)
            cal_games = calendar.get("games", {})
            relevant = {
                gt: gl for gt, gl in cal_games.items()
                if gt.upper() in GAME_TYPES
            }
            if relevant:
                results.append({"date": str(d), "games": list(relevant.keys())})
        except Exception:
            continue

    return {"upcoming": results}
