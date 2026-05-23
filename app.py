"""FastAPI wrapper for trav-agent."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import GAME_TYPES
from trav_agent.data.atg_client import ATGClient
from trav_agent.output.dashboard import generate_dashboard_html, generate_landing_html

app = FastAPI(title="Trav Agent API", version="0.1.0")

MAIN_GAME_TYPES = {"V75", "V85", "V86", "V64", "GS75"}


async def _get_available_rounds(client: ATGClient, days_back: int = 14, days_forward: int = 7) -> list[tuple]:
    """Fetch available rounds from ATG calendar for the round switcher."""
    today = date.today()
    rounds = []
    for offset in range(-days_back, days_forward + 1):
        d = today + timedelta(days=offset)
        try:
            cal = await client.get_calendar(d)
            cal_games = cal.get("games", {})
            for gt, game_list in cal_games.items():
                gt_upper = gt.upper()
                if gt_upper not in MAIN_GAME_TYPES:
                    continue
                has_id = game_list and any(g.get("id") for g in game_list)
                if not has_id:
                    continue
                track = ""
                if game_list and game_list[0].get("tracks"):
                    tracks = game_list[0]["tracks"]
                    if isinstance(tracks, list) and tracks:
                        track = tracks[0].get("name", "")
                    elif isinstance(tracks, dict):
                        track = next(iter(tracks.values()), {}).get("name", "")
                is_past = d < today
                key = f"{gt_upper}/{d}"
                rounds.append((key, gt_upper, str(d), is_past, track))
        except Exception:
            continue
    return rounds


@app.get("/")
async def root():
    client = ATGClient()
    today = date.today()
    for offset in range(8):
        d = today + timedelta(days=offset)
        try:
            cal = await client.get_calendar(d, skip_cache=(offset <= 1))
            cal_games = cal.get("games", {})
            for gt, game_list in cal_games.items():
                if gt.upper() in MAIN_GAME_TYPES:
                    has_id = game_list and any(g.get("id") for g in game_list)
                    if has_id:
                        return RedirectResponse(f"/dashboard/{gt.upper()}/{d}")
        except Exception:
            continue
    return HTMLResponse(generate_landing_html())


@app.get("/landing", response_class=HTMLResponse)
async def landing():
    return generate_landing_html()


@app.get("/dashboard/{game_type}/{day}", response_class=HTMLResponse)
async def dashboard(game_type: str, day: str):
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

    if game_round.is_finished or d < date.today():
        pass
    else:
        await client.refresh_results(game_round)

    analyzer = CompositeAnalyzer()
    analyzer.analyze_round(game_round)

    available_rounds = await _get_available_rounds(client)

    html = generate_dashboard_html(
        game_round,
        available_rounds=available_rounds,
    )
    return html


@app.get("/api/analyze/{game_type}/{day}")
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


@app.get("/api/upcoming")
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
