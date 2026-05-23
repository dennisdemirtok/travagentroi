"""FastAPI wrapper for trav-agent."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import ANTHROPIC_API_KEY, GAME_TYPES, SUPABASE_ENABLED
from trav_agent.data.atg_client import ATGClient
from trav_agent.output.dashboard import generate_dashboard_html, generate_landing_html

logger = logging.getLogger(__name__)

MAIN_GAME_TYPES = {"V75", "V85", "V86", "V64", "GS75"}

# In-memory cache for analyzed rounds (avoid re-fetching on chat)
_round_cache: dict[str, object] = {}
_backlog_cache: dict | None = None


async def _load_backlog():
    """Load backlog data from Supabase or local file."""
    global _backlog_cache
    if _backlog_cache is not None:
        return _backlog_cache

    if SUPABASE_ENABLED:
        try:
            from trav_agent.database.backlog_sync import load_backlog_from_supabase
            _backlog_cache = load_backlog_from_supabase(limit=3000)
            n = len(_backlog_cache.get("entries", []))
            logger.info(f"Backlog loaded from Supabase: {n} entries")
            return _backlog_cache
        except Exception as e:
            logger.warning(f"Failed to load backlog from Supabase: {e}")

    # Fallback: local file
    try:
        import json
        from trav_agent.config import PROJECT_ROOT
        path = PROJECT_ROOT / "backlog.json"
        if path.exists():
            with open(path) as f:
                _backlog_cache = json.load(f)
            n = len(_backlog_cache.get("entries", []))
            logger.info(f"Backlog loaded from file: {n} entries")
            return _backlog_cache
    except Exception as e:
        logger.warning(f"Failed to load backlog from file: {e}")

    return None


async def _refresh_live_rounds():
    """Background task: refresh live rounds every 10 minutes."""
    while True:
        await asyncio.sleep(600)
        try:
            client = ATGClient()
            analyzer = CompositeAnalyzer()
            today = date.today()

            for offset in range(2):
                d = today + timedelta(days=offset)
                cal = await client.get_calendar(d, skip_cache=True)
                cal_games = cal.get("games", {})
                for gt, game_list in cal_games.items():
                    gt_upper = gt.upper()
                    if gt_upper not in MAIN_GAME_TYPES:
                        continue
                    has_id = game_list and any(g.get("id") for g in game_list)
                    if not has_id:
                        continue
                    key = f"{gt_upper}/{d}"
                    game_round = await client.fetch_full_round(gt_upper, d)
                    if game_round and not game_round.is_finished:
                        await client.refresh_results(game_round)
                        analyzer.analyze_round(game_round)
                        _round_cache[key] = game_round
                        logger.info(f"Refreshed live round: {key}")
        except Exception as e:
            logger.warning(f"Live refresh error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_refresh_live_rounds())
    yield
    task.cancel()


app = FastAPI(title="Trav Agent API", version="0.1.0", lifespan=lifespan)


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
    key = f"{game_type}/{d}"

    # Check cache first
    game_round = _round_cache.get(key)
    if not game_round:
        game_round = await client.fetch_full_round(game_type, d)
        if not game_round:
            raise HTTPException(404, f"Ingen {game_type} hittad för {day}")

        if not game_round.is_finished and d >= date.today():
            await client.refresh_results(game_round)

        analyzer = CompositeAnalyzer()
        analyzer.analyze_round(game_round)
        _round_cache[key] = game_round

    available_rounds = await _get_available_rounds(client)
    backlog_data = await _load_backlog()

    html = generate_dashboard_html(
        game_round,
        available_rounds=available_rounds,
        backlog_data=backlog_data,
    )
    return html


# ── AI Chat ──────────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def api_chat(request: Request):
    if not ANTHROPIC_API_KEY:
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY saknas"},
            status_code=500,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Ogiltigt JSON"}, status_code=400)

    messages = data.get("messages", [])
    round_key = data.get("round_key", "")

    game_round = _round_cache.get(round_key)
    backlog_data = await _load_backlog()

    try:
        from trav_agent.chat.agent import build_backlog_context, build_round_context, chat

        round_ctx = build_round_context(game_round) if game_round else "Ingen omgångsdata."
        bl_ctx = build_backlog_context(game_round, backlog_data) if game_round and backlog_data else ""

        response_text = await chat(messages, round_ctx, bl_ctx, ANTHROPIC_API_KEY)
        return {"response": response_text}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Backlog API ──────────────────────────────────────────────────────────────

@app.get("/api/backlog")
async def api_backlog(strategy: str = None, game_type: str = None, limit: int = 500):
    if SUPABASE_ENABLED:
        try:
            from trav_agent.database.backlog_sync import load_backlog_from_supabase
            data = load_backlog_from_supabase(
                strategy=strategy, game_type=game_type, limit=limit
            )
            return data
        except Exception as e:
            logger.warning(f"Supabase backlog fetch failed: {e}")

    backlog_data = await _load_backlog()
    if not backlog_data:
        return {"entries": [], "strategies": {}}

    entries = backlog_data.get("entries", [])
    if strategy:
        entries = [e for e in entries if e.get("strategy") == strategy]
    if game_type:
        entries = [e for e in entries if e.get("game_type") == game_type]

    return {"entries": entries[:limit], "strategies": backlog_data.get("strategies", {})}


# ── System Generation ────────────────────────────────────────────────────────

@app.get("/api/system/{game_type}/{day}")
async def api_system(game_type: str, day: str, budget: int = 2500):
    game_type = game_type.upper()
    key = f"{game_type}/{day}"

    game_round = _round_cache.get(key)
    if not game_round:
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

    try:
        from trav_agent.betting.system_generator import SystemGenerator

        strategies = [
            ("I_streck_1st", "avg_upset_lt_40", budget, 0, 0, 0),
            ("I_streck_1st", "avg_upset_lt_40", budget, 1, 75, 12),
            ("Q_dom_x_mktgap", "avg_upset_lt_40", budget, 0, 0, 0),
            ("D_market_gap", "avg_upset_lt_40", budget, 0, 0, 0),
        ]

        results = []
        for strat, filt, b, spikes, conf, gap in strategies:
            gen = SystemGenerator(
                budget=b, strategy=strat, selective_filter=filt,
                max_spikes=spikes, spike_conf_threshold=conf, spike_score_gap=gap,
            )
            system = gen.generate(game_round)
            results.append({
                "strategy": strat,
                "skip": system.skip_round,
                "skip_reason": system.skip_reason,
                "total_rows": system.total_rows,
                "total_cost": system.total_cost,
                "avg_confidence": system.avg_confidence,
                "races": [
                    {
                        "race_number": rp.race_number,
                        "picks": rp.picks,
                        "pick_names": rp.pick_names,
                        "num_picks": rp.num_picks,
                        "confidence": rp.confidence,
                        "upset_risk": rp.upset_risk,
                    }
                    for rp in system.race_picks
                ] if not system.skip_round else [],
            })

        return {"systems": results}
    except Exception as e:
        logger.error(f"System generation error: {e}")
        raise HTTPException(500, str(e))


# ── Other API ────────────────────────────────────────────────────────────────

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


@app.get("/api/refresh/{game_type}/{day}")
async def api_refresh(game_type: str, day: str):
    game_type = game_type.upper()
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400)

    client = ATGClient()
    game_round = await client.fetch_full_round(game_type, d)
    if not game_round:
        raise HTTPException(404)

    await client.refresh_results(game_round)
    analyzer = CompositeAnalyzer()
    analyzer.analyze_round(game_round)

    key = f"{game_type}/{d}"
    _round_cache[key] = game_round

    return {"status": "refreshed", "key": key}
