"""FastAPI wrapper for trav-agent."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import ANTHROPIC_API_KEY, GAME_TYPES, SUPABASE_ENABLED
from trav_agent.data.atg_client import ATGClient
from trav_agent.output.dashboard import generate_dashboard_html, generate_landing_html

logger = logging.getLogger(__name__)

MAIN_GAME_TYPES = {"V75", "V85", "V86", "V64", "GS75"}

# In-memory cache for analyzed rounds (avoid re-fetching on chat)
_round_cache: dict[str, object] = {}
_backlog_cache: dict | None = None
_tips_cache: dict[str, dict] = {}

# Cache for available rounds (avoid 21 API calls per page load)
_available_rounds_cache: list[tuple] = []
_available_rounds_ts: float = 0.0
_ROUNDS_CACHE_TTL = 600  # 10 minutes


def _build_strategies_summary(backlog: dict) -> dict:
    """Build strategies summary from raw entries for _stats_html().

    Returns the backlog dict enriched with a "strategies" key containing
    per-strategy aggregates (rounds, hits, ROI, netto, game_types breakdown).
    """
    entries = backlog.get("entries", [])
    if not entries or "strategies" in backlog:
        return backlog  # already has strategies or empty

    from collections import defaultdict

    strats: dict[str, dict] = {}
    gt_data: dict[str, dict[str, dict]] = {}  # strat -> game_type -> stats

    for e in entries:
        s = e.get("strategy", "unknown")
        gt = e.get("game_type", "?")
        cost = e.get("cost", 0) or 0
        payout = e.get("payout", 0) or 0
        hit = e.get("hit", False)
        num_correct = e.get("num_correct", 0) or 0
        num_races = e.get("num_races", 0) or 0

        if s not in strats:
            strats[s] = {
                "rounds_played": 0, "full_hits": 0, "partial_hits": 0,
                "total_cost": 0, "total_payout": 0, "game_types": {},
            }
        ss = strats[s]
        ss["rounds_played"] += 1
        ss["total_cost"] += cost
        ss["total_payout"] += payout
        if hit:
            ss["full_hits"] += 1
        elif num_correct >= num_races - 1 and num_races > 0:
            ss["partial_hits"] += 1

        # Per game type
        if gt not in ss["game_types"]:
            ss["game_types"][gt] = {
                "rounds": 0, "full": 0, "wins": 0,
                "cost": 0, "payout": 0,
            }
        g = ss["game_types"][gt]
        g["rounds"] += 1
        g["cost"] += cost
        g["payout"] += payout
        if hit:
            g["full"] += 1
        if payout > 0:
            g["wins"] += 1

    # Compute ROI and netto
    for s, ss in strats.items():
        tc = ss["total_cost"]
        tp = ss["total_payout"]
        ss["netto"] = tp - tc
        ss["roi"] = ((tp - tc) / tc * 100) if tc > 0 else 0
        for gt, g in ss["game_types"].items():
            gc = g["cost"]
            gp = g["payout"]
            g["netto"] = gp - gc
            g["roi"] = ((gp - gc) / gc * 100) if gc > 0 else 0

    backlog["strategies"] = strats
    logger.info(f"Built strategies summary: {len(strats)} strategies from {len(entries)} entries")
    return backlog


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
            _backlog_cache = _build_strategies_summary(_backlog_cache)
            return _backlog_cache
        except Exception as e:
            logger.warning(f"Failed to load backlog from Supabase: {e}")

    # Fallback: local file (try .gz first, then plain json)
    try:
        from trav_agent.config import PROJECT_ROOT
        gz_path = PROJECT_ROOT / "backlog.json.gz"
        plain_path = PROJECT_ROOT / "backlog.json"
        if gz_path.exists():
            with gzip.open(gz_path, "rt", encoding="utf-8") as f:
                _backlog_cache = json.load(f)
            n = len(_backlog_cache.get("entries", []))
            logger.info(f"Backlog loaded from gzip: {n} entries")
            _backlog_cache = _build_strategies_summary(_backlog_cache)
            return _backlog_cache
        elif plain_path.exists():
            with open(plain_path) as f:
                _backlog_cache = json.load(f)
            n = len(_backlog_cache.get("entries", []))
            logger.info(f"Backlog loaded from file: {n} entries")
            _backlog_cache = _build_strategies_summary(_backlog_cache)
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
                        _dashboard_html_cache.pop(key, None)  # invalidate HTML cache
                        logger.info(f"Refreshed live round: {key}")
        except Exception as e:
            logger.warning(f"Live refresh error: {e}")


async def _prewarm_caches():
    """Pre-warm available rounds + backlog + active round in background.

    Runs as a background task so it doesn't block startup / health checks.
    The goal: first user page load should hit 100% cached data.
    """
    try:
        client = ATGClient()
        analyzer = CompositeAnalyzer()

        # Load backlog first (fast, no external API calls)
        await _load_backlog()
        n_entries = len((_backlog_cache or {}).get("entries", []))
        logger.info(f"Pre-warm: {n_entries} backlog entries cached")

        # Then fetch available rounds (21 calendar API calls)
        await _get_available_rounds(client)
        n_rounds = len(_available_rounds_cache)
        logger.info(f"Pre-warm: {n_rounds} rounds cached")

        # Pre-fetch the active round (the one root / redirects to)
        # This is the expensive part (~60-90s) but runs in background
        if _available_rounds_cache:
            today_str = str(date.today())
            future = [r for r in _available_rounds_cache if r[2] >= today_str]
            past = [r for r in _available_rounds_cache if r[2] < today_str]
            target = future[0] if future else (past[-1] if past else None)
            if target:
                gt, d_str = target[1], target[2]
                key = f"{gt}/{d_str}"
                if key not in _round_cache:
                    d = date.fromisoformat(d_str)
                    logger.info(f"Pre-warm: fetching active round {key}...")
                    game_round = await client.fetch_full_round(gt, d)
                    if game_round:
                        if not game_round.is_finished and d >= date.today():
                            await client.refresh_results(game_round)
                        analyzer.analyze_round(game_round)
                        _round_cache[key] = game_round
                        logger.info(f"Pre-warm: active round {key} cached ✓")
    except Exception as e:
        logger.warning(f"Pre-warm failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm caches in background (non-blocking so health checks pass)
    logger.info("Startup: launching background cache warming...")
    warmup = asyncio.create_task(_prewarm_caches())
    task = asyncio.create_task(_refresh_live_rounds())
    yield
    warmup.cancel()
    task.cancel()


app = FastAPI(title="Trav Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ETag cache for dashboard HTML (avoid re-generating identical pages)
_dashboard_html_cache: dict[str, tuple[str, str]] = {}  # key -> (etag, html)


async def _get_available_rounds(client: ATGClient, days_back: int = 14, days_forward: int = 7) -> list[tuple]:
    """Fetch available rounds from ATG calendar for the round switcher.

    Cached for 10 minutes. Uses parallel fetches to avoid 21× sequential 1s delays.
    """
    global _available_rounds_cache, _available_rounds_ts

    now = time.time()
    if _available_rounds_cache and (now - _available_rounds_ts) < _ROUNDS_CACHE_TTL:
        return _available_rounds_cache

    today = date.today()
    days = [today + timedelta(days=offset) for offset in range(-days_back, days_forward + 1)]

    # Parallel fetch — ATG cache hits are instant, only uncached ones throttle
    async def _fetch_day(d):
        try:
            return d, await client.get_calendar(d)
        except Exception:
            return d, {}

    results = await asyncio.gather(*[_fetch_day(d) for d in days])

    rounds = []
    for d, cal in results:
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
                    first_track = tracks[0]
                    if isinstance(first_track, dict):
                        track = first_track.get("name", "")
                elif isinstance(tracks, dict):
                    first_val = next(iter(tracks.values()), {})
                    if isinstance(first_val, dict):
                        track = first_val.get("name", "")
            is_past = d < today
            key = f"{gt_upper}/{d}"
            rounds.append((key, gt_upper, str(d), is_past, track))

    _available_rounds_cache = rounds
    _available_rounds_ts = now
    logger.info(f"Refreshed available rounds cache: {len(rounds)} rounds")
    return rounds


@app.get("/")
async def root():
    # Use cached rounds if available (instant redirect)
    if _available_rounds_cache:
        today_str = str(date.today())
        # Find closest round: today or nearest future, fallback to most recent past
        future = [(k, gt, d, past, tr) for k, gt, d, past, tr in _available_rounds_cache if d >= today_str]
        past = [(k, gt, d, past, tr) for k, gt, d, past, tr in _available_rounds_cache if d < today_str]
        target = future[0] if future else (past[-1] if past else None)
        if target:
            return RedirectResponse(f"/dashboard/{target[1]}/{target[2]}")

    # Fallback: quick calendar scan (only if cache empty)
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
async def dashboard(game_type: str, day: str, request: Request):
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

        # Sync vinnarspel candidates to Supabase (fire-and-forget)
        if SUPABASE_ENABLED:
            try:
                from trav_agent.database.bet_sync import sync_bet_results
                sync_bet_results(game_round)
            except Exception as e:
                logger.debug(f"Bet sync skipped: {e}")

    # Fetch all auxiliary data in parallel (4 independent tasks)
    async def _load_tips():
        try:
            from trav_agent.data.tips_scraper import load_tips_cache_raw
            return load_tips_cache_raw(game_type, str(d))
        except Exception:
            return None

    async def _load_proffs():
        try:
            from pathlib import Path
            proffs_dir = Path("proffs_cache")
            if proffs_dir.exists():
                candidates = list(proffs_dir.glob(f"{game_type}*{str(d)}*_pre.json"))
                if candidates:
                    import json as _json
                    with open(candidates[0]) as _pf:
                        return _json.load(_pf)
        except Exception:
            pass
        return None

    available_rounds, backlog_data, tips_raw, proffs_data = await asyncio.gather(
        _get_available_rounds(client), _load_backlog(), _load_tips(), _load_proffs()
    )

    # Check ETag cache — return 304 if unchanged
    cache_entry = _dashboard_html_cache.get(key)
    if cache_entry:
        etag, cached_html = cache_entry
        if_none_match = request.headers.get("if-none-match")
        if if_none_match == etag:
            return HTMLResponse(status_code=304, headers={"ETag": etag})
        html = cached_html
    else:
        html = generate_dashboard_html(
            game_round,
            available_rounds=available_rounds,
            backlog_data=backlog_data,
            tips_raw=tips_raw,
            proffs_data=proffs_data,
        )
        etag = '"' + hashlib.md5(html.encode()).hexdigest()[:16] + '"'
        _dashboard_html_cache[key] = (etag, html)

    return HTMLResponse(
        content=html,
        headers={
            "ETag": etag,
            "Cache-Control": "public, max-age=60, stale-while-revalidate=300",
        },
    )


# ── Available Rounds API (AJAX round switcher) ───────────────────────────

@app.get("/api/rounds")
async def api_rounds():
    """Return available rounds for the round switcher (loaded async via JS)."""
    client = ATGClient()
    rounds = await _get_available_rounds(client)
    return JSONResponse([
        {"key": key, "game_type": gt, "date": d, "is_past": past, "track": tr}
        for key, gt, d, past, tr in rounds
    ])


# ── Bet Result API ─────────────────────────────────────────────────────────

@app.get("/api/bets")
async def api_bets(game_type: str = None, days: int = 90):
    """Return vinnarspel bet history with P&L aggregation."""
    if not SUPABASE_ENABLED:
        return JSONResponse({"error": "Supabase ej konfigurerad"}, status_code=503)

    try:
        from trav_agent.database.bet_sync import load_bet_history
        data = load_bet_history(game_type=game_type, days_back=days)
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Bet history error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── AI Chat (SSE Streaming) ────────────────────────────────────────────────

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

    raw_messages = data.get("messages", [])
    messages = [{"role": m["role"], "content": m["content"]} for m in raw_messages if m.get("role") and m.get("content")]
    round_key = data.get("round_key", "")

    game_round = _round_cache.get(round_key)
    if not game_round and round_key:
        parts = round_key.split("/")
        if len(parts) == 2:
            try:
                from trav_agent.data.atg_client import ATGClient
                from trav_agent.analysis.composite import CompositeAnalyzer
                client = ATGClient()
                gr = await client.fetch_full_round(parts[0], date.fromisoformat(parts[1]))
                if gr:
                    CompositeAnalyzer().analyze_round(gr)
                    _round_cache[round_key] = gr
                    game_round = gr
            except Exception as e:
                logger.warning(f"Failed to fetch round for chat: {e}")
    backlog_data = await _load_backlog()

    try:
        from trav_agent.chat.agent import (
            build_backlog_context, build_consensus_ranking,
            build_model_performance_context, build_round_context,
            chat_stream,
        )
        from trav_agent.chat.memory import (
            build_learning_context, extract_learnings_from_session,
            get_learnings_context, save_session,
        )
        from trav_agent.data.tips_scraper import (
            build_tips_context, load_tips_cache_raw, scrape_tips,
        )

        round_ctx = build_round_context(game_round) if game_round else "Ingen omgångsdata."
        bl_ctx = build_backlog_context(game_round, backlog_data) if game_round and backlog_data else ""

        # Build tips context
        tips_ctx = ""
        consensus_ctx = ""
        if game_round:
            game_type = game_round.game_type
            round_date = str(game_round.round_date)
            cache_key = f"{game_type}:{round_date}"
            if cache_key not in _tips_cache:
                try:
                    _tips_cache[cache_key] = await scrape_tips(game_type, round_date)
                except Exception:
                    _tips_cache[cache_key] = {}
            tips_ctx = build_tips_context(_tips_cache.get(cache_key, {}))

            tips_raw = load_tips_cache_raw(game_type, round_date)
            consensus_ctx = build_consensus_ranking(game_round, tips_raw)

        # Build model performance context
        model_perf_ctx = ""
        if backlog_data:
            model_perf_ctx = build_model_performance_context(backlog_data)

        # Build memory context
        memory_ctx = ""
        if game_round and backlog_data:
            memory_ctx = build_learning_context(backlog_data, game_round)

        # Build persistent learnings context (cross-session memory)
        learnings_ctx = get_learnings_context()

        async def event_generator():
            full_response = ""
            try:
                async for chunk in chat_stream(
                    messages, round_ctx, bl_ctx, ANTHROPIC_API_KEY,
                    tips_context=tips_ctx, memory_context=memory_ctx,
                    consensus_context=consensus_ctx,
                    model_perf_context=model_perf_ctx,
                    learnings_context=learnings_ctx,
                ):
                    full_response += chunk
                    yield f"data: {json.dumps({'delta': chunk})}\n\n"

                yield f"data: {json.dumps({'done': True})}\n\n"

                # Save session and extract learnings after completion
                if round_key:
                    all_msgs = list(messages) + [{"role": "assistant", "content": full_response}]
                    save_session(round_key, all_msgs)
                    # Auto-extract learnings from session
                    extract_learnings_from_session(all_msgs, round_key)

            except Exception as e:
                logger.error(f"Chat stream error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Chat Session Endpoints ──────────────────────────────────────────────────

@app.get("/api/chat/session/{round_key:path}")
async def get_chat_session(round_key: str):
    from trav_agent.chat.memory import load_session
    messages = load_session(round_key)
    return {"messages": messages}


@app.delete("/api/chat/session/{round_key:path}")
async def delete_chat_session(round_key: str):
    from trav_agent.chat.memory import clear_session
    clear_session(round_key)
    return {"status": "cleared"}


@app.get("/api/chat/sessions")
async def list_chat_sessions():
    """List all saved chat sessions with metadata."""
    from trav_agent.chat.memory import list_sessions
    return {"sessions": list_sessions()}


@app.post("/api/chat/learn")
async def add_chat_learning(request: Request):
    """Manually add a learning to the agent's persistent memory."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Ogiltigt JSON"}, status_code=400)

    text = data.get("text", "")
    category = data.get("category", "general")

    if not text:
        return JSONResponse({"error": "'text' krävs"}, status_code=400)

    from trav_agent.chat.memory import add_learning
    add_learning(text, category=category)
    return {"status": "learned", "text": text, "category": category}


@app.get("/api/chat/learnings")
async def get_chat_learnings():
    """Get all stored learnings."""
    from trav_agent.chat.memory import _load_learnings
    return {"learnings": _load_learnings()}


# ── Tips API ────────────────────────────────────────────────────────────────

@app.get("/api/tips/{game_type}/{day}")
async def get_tips(game_type: str, day: str):
    """Fetch tips from all available sources."""
    game_type = game_type.upper()

    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"Ogiltigt datum: {day}")

    cache_key = f"{game_type}:{day}"

    # Check in-memory cache
    if cache_key in _tips_cache:
        return {"tips": _tips_cache[cache_key]}

    try:
        from trav_agent.data.tips_scraper import scrape_tips
        tips = await scrape_tips(game_type, day)
        _tips_cache[cache_key] = tips
        return {"tips": tips}
    except Exception as e:
        logger.warning(f"Tips fetch error: {e}")
        return {"tips": {}, "error": str(e)}


@app.post("/api/tips/manual")
async def post_manual_tips(request: Request):
    """Store manually pasted tips from a named source."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Ogiltigt JSON"}, status_code=400)

    source = data.get("source", "")
    text = data.get("text", "")

    if not source or not text:
        return JSONResponse(
            {"error": "Både 'source' och 'text' krävs"},
            status_code=400,
        )

    from trav_agent.data.tips_scraper import add_manual_tips
    add_manual_tips(source, text)

    # Invalidate relevant tips caches (manual tips apply globally)
    keys_to_remove = [k for k in _tips_cache]
    for k in keys_to_remove:
        del _tips_cache[k]

    return {"status": "stored", "source": source, "length": len(text)}


# ── Image-based tips parsing ──────────────────────────────────────────────

@app.post("/api/tips/parse-image")
async def parse_tips_image(request: Request):
    """Parse a tipster screenshot with Claude vision and return structured data."""
    if not ANTHROPIC_API_KEY:
        return JSONResponse({"error": "ANTHROPIC_API_KEY saknas"}, status_code=500)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Ogiltigt JSON"}, status_code=400)

    image_b64 = data.get("image")
    game_type = data.get("game_type", "V85").upper()
    source_name = data.get("source_name", "")
    media_type = data.get("media_type", "image/png")

    if not image_b64:
        return JSONResponse({"error": "'image' (base64) krävs"}, status_code=400)

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    gt = game_type

    prompt = f"""Analysera denna bild från en travtipstjänst. Extrahera ALL data i strukturerad form.

Bilden kan innehålla:
1. **Avdelningar/Rankings** — A/B/BC/C/D-ranking per avdelning ({gt}-1, {gt}-2, osv). Siffror representerar hästarnas startnummer.
2. **Hetaste infon** — Insider-tips per avdelning
3. **Förändringar** — Utrustningsändringar, barfota, etc.

Returnera ENBART ett JSON-objekt (inget annat text):
{{
  "source_name": "<tipstjänstens namn, t.ex. 'SpV Gävle'>",
  "author": "<författare om synlig>",
  "rankings": {{
    "{gt}-1": {{"A": "3-11", "B": "8-10-6", "BC": "1-4-14", "C": "9-12-2", "D": ""}},
    "{gt}-2": {{"A": "...", "B": "...", "BC": "...", "C": "...", "D": ""}},
    ...
  }},
  "hot_info": {{
    "{gt}-1": "Kort sammanfattning av het info för avdelning 1",
    "{gt}-2": "...",
    ...
  }},
  "changes": {{
    "{gt}-1": "Sammanfattning av förändringar för avdelning 1",
    "{gt}-2": "...",
    ...
  }}
}}

Viktigt:
- Startnummer ska vara separerade med bindestreck (t.ex. "15-5-11")
- Om B/C visas i bilden, mappa till "BC"
- Om en tier saknas, sätt tom sträng ""
- Om inget hett tips finns för en avdelning, utelämna den från hot_info
- Om inga förändringar finns, utelämna från changes
- Returnera BARA JSON, ingen annan text"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw_text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()

        parsed = json.loads(raw_text)

        # If user didn't provide source_name, use the one from the parsed data
        if not source_name and parsed.get("source_name"):
            source_name = parsed["source_name"]

        return {
            "status": "parsed",
            "source_name": source_name or "custom_tipster",
            "parsed": parsed,
        }

    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "Kunde inte parsa AI-svaret som JSON", "raw": raw_text[:500]},
            status_code=422,
        )
    except Exception as e:
        logger.error(f"Image parse error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/tips/add-source")
async def add_tips_source(request: Request):
    """Add a parsed tipster source to the tips cache and refresh consensus."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Ogiltigt JSON"}, status_code=400)

    game_type = data.get("game_type", "").upper()
    round_date = data.get("round_date", "")
    source_name = data.get("source_name", "")
    source_data = data.get("source_data", {})

    if not game_type or not round_date or not source_name or not source_data:
        return JSONResponse(
            {"error": "game_type, round_date, source_name och source_data krävs"},
            status_code=400,
        )

    from trav_agent.data.tips_scraper import add_source_to_cache
    add_source_to_cache(game_type, round_date, source_name, source_data)

    # Clear round cache to force regeneration with new consensus data
    key = f"{game_type}/{round_date}"
    _round_cache.pop(key, None)

    # Clear tips cache
    tips_key = f"{game_type}:{round_date}"
    _tips_cache.pop(tips_key, None)

    return {"status": "added", "source_name": source_name, "round_key": key}


@app.delete("/api/tips/source/{game_type}/{day}/{source_name}")
async def remove_tips_source(game_type: str, day: str, source_name: str):
    """Remove a tipster source from the cache."""
    game_type = game_type.upper()
    from trav_agent.data.tips_scraper import remove_source_from_cache
    removed = remove_source_from_cache(game_type, day, source_name)

    if not removed:
        raise HTTPException(404, "Källan hittades inte")

    key = f"{game_type}/{day}"
    _round_cache.pop(key, None)
    _tips_cache.pop(f"{game_type}:{day}", None)

    return {"status": "removed", "source_name": source_name}


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


@app.get("/api/chansspik/{game_type}/{day}")
async def api_chansspik(game_type: str, day: str, budget: int = 300):
    """Chansspik-analys — upset-targeting system (mål: 25-100k utdelning)."""
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
        from trav_agent.analysis.upset_system import analyze_upset_round, build_upset_system

        analysis = analyze_upset_round(game_round)
        plan = build_upset_system(game_round, budget=float(budget))

        return {
            "analysis": analysis,
            "system": {
                "strategy": plan.strategy_name,
                "total_rows": plan.total_rows,
                "total_cost": plan.total_cost,
                "budget": budget,
                "legs": [
                    {
                        "race_number": leg.race_number,
                        "leg_type": leg.leg_type,
                        "picks": leg.picks,
                        "num_picks": leg.num_picks,
                        "upset_risk": leg.upset_risk,
                        "difficulty": leg.difficulty,
                        "reasoning": leg.reasoning,
                    }
                    for leg in sorted(plan.legs, key=lambda l: l.race_number)
                ],
            },
        }
    except Exception as e:
        logger.error(f"Chansspik error: {e}")
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
