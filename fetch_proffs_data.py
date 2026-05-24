"""Fetch and store viktat proffsstreck data from beta.kungenstrav.se.

Collects professional tipster picks before race and results after race.
Stores structured JSON for model integration and backtesting.

Usage:
    # Fetch today's games (pre-race)
    python fetch_proffs_data.py

    # Fetch specific game
    python fetch_proffs_data.py --game GS75_2026-05-24_32_4

    # Fetch results for completed games
    python fetch_proffs_data.py --results

    # Fetch all available from history API
    python fetch_proffs_data.py --history

    # Analyze accuracy of collected data
    python fetch_proffs_data.py --analyze
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://beta.kungenstrav.se/api"
SHOPS = "torpatips,vinnatillsammans,succeandelar,direktenmollansspel,icalinkoping,kopandel,gotthornan"
CACHE_DIR = Path(__file__).parent / "proffs_cache"
REQUEST_DELAY = 1.0  # be polite


# ── Weighting (mirrors site's calcWeight) ─────────────────────────────────────

def calc_weight(n_marks: int) -> float:
    """Weight for a system's leg based on number of marks.

    Spik (1 pick)  = 5 points
    2 picks        = 3 points
    3 picks        = 2 points
    4-5 picks      = 1 point
    6+ picks       = 0.5 points
    """
    if n_marks == 1:
        return 5.0
    elif n_marks == 2:
        return 3.0
    elif n_marks == 3:
        return 2.0
    elif n_marks <= 5:
        return 1.0
    else:
        return 0.5


# ── API Client ────────────────────────────────────────────────────────────────

def fetch_calendar() -> list[dict]:
    """Fetch current/upcoming games from calendar API."""
    resp = httpx.get(f"{BASE_URL}/calendar", timeout=15)
    resp.raise_for_status()
    return resp.json().get("games", [])


def fetch_history() -> list[str]:
    """Fetch historical game IDs."""
    resp = httpx.get(f"{BASE_URL}/history", timeout=15)
    resp.raise_for_status()
    return resp.json().get("games", [])


def fetch_game_data(game_id: str) -> dict:
    """Fetch full game data including systems and race info."""
    url = f"{BASE_URL}/data/{game_id}?shops={SHOPS}"
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Data Processing ───────────────────────────────────────────────────────────

def compute_viktat_streck(races: list[dict], systems: list[dict]) -> list[dict]:
    """Compute weighted professional consensus for each horse in each race.

    Returns list of race dicts with proffs data per horse.
    """
    submitted = [s for s in systems if s.get("submitted")]
    if not submitted:
        return []

    result = []
    for race_idx, race in enumerate(races):
        leg = str(race_idx + 1)
        horse_numbers = [h["number"] for h in race.get("horses", [])]

        # Initialize weights
        horse_weights: dict[int, float] = {n: 0.0 for n in horse_numbers}
        total_weight = 0.0
        system_count_per_horse: dict[int, int] = {n: 0 for n in horse_numbers}

        for sys in submitted:
            leg_data = sys.get("legs", {}).get(leg)
            if not leg_data:
                continue
            marks = leg_data.get("marks", [])
            if not marks:
                continue
            w = calc_weight(len(marks))
            for m in marks:
                if m in horse_weights:
                    horse_weights[m] += w
                    system_count_per_horse[m] += 1
            total_weight += w

        # Convert to percentages
        horses_data = []
        for h in race.get("horses", []):
            num = h["number"]
            proffs_pct = (horse_weights[num] / total_weight * 100) if total_weight > 0 else 0
            public_pct = h.get("pct", 0) / 100  # pct is stored as x100
            odds = h.get("odds", 0) / 100  # odds is stored as x100
            edge = proffs_pct - public_pct

            horses_data.append({
                "number": num,
                "name": h.get("name", ""),
                "driver": h.get("driver", ""),
                "odds": round(odds, 2),
                "public_pct": round(public_pct, 1),
                "proffs_weighted_pct": round(proffs_pct, 1),
                "proffs_count": system_count_per_horse.get(num, 0),
                "edge_pp": round(edge, 1),
                "trend": h.get("trend", 0),
                "place": h.get("place", 0),
                "final_odds": h.get("finalOdds", 0),
                "galloped": h.get("galloped", False),
                "disqualified": h.get("disqualified", False),
            })

        # Sort by proffs_weighted_pct descending
        horses_data.sort(key=lambda x: x["proffs_weighted_pct"], reverse=True)

        winner = race.get("winner")
        result.append({
            "race_number": race.get("number", race_idx + 1),
            "race_name": race.get("name", ""),
            "distance": race.get("distance", 0),
            "start_method": race.get("startMethod", ""),
            "track": race.get("track", ""),
            "winner": winner,
            "scratchings": race.get("scratchings", []),
            "horses": horses_data,
        })

    return result


def extract_race_data(races: list[dict]) -> list[dict]:
    """Extract race data without proffs weighting (for historical/no-systems games)."""
    result = []
    for race_idx, race in enumerate(races):
        horses_data = []
        for h in race.get("horses", []):
            public_pct = h.get("pct", 0) / 100
            odds = h.get("odds", 0) / 100
            horses_data.append({
                "number": h["number"],
                "name": h.get("name", ""),
                "driver": h.get("driver", ""),
                "odds": round(odds, 2),
                "public_pct": round(public_pct, 1),
                "proffs_weighted_pct": 0.0,
                "proffs_count": 0,
                "edge_pp": 0.0,
                "trend": h.get("trend", 0),
                "place": h.get("place", 0),
                "final_odds": h.get("finalOdds", 0),
                "galloped": h.get("galloped", False),
                "disqualified": h.get("disqualified", False),
            })
        horses_data.sort(key=lambda x: x["public_pct"], reverse=True)

        winner = race.get("winner")
        result.append({
            "race_number": race.get("number", race_idx + 1),
            "race_name": race.get("name", ""),
            "distance": race.get("distance", 0),
            "start_method": race.get("startMethod", ""),
            "track": race.get("track", ""),
            "winner": winner,
            "scratchings": race.get("scratchings", []),
            "horses": horses_data,
        })
    return result


def process_game(game_id: str, raw_data: dict) -> dict:
    """Process raw API data into structured proffs analysis."""
    races = raw_data.get("races", [])
    systems = raw_data.get("systems", [])
    meta = raw_data.get("meta", {})

    # Compute viktat streck (or just extract race data if no systems)
    submitted = [s for s in systems if s.get("submitted")]
    if submitted:
        race_analysis = compute_viktat_streck(races, systems)
    else:
        race_analysis = extract_race_data(races)

    # Summarize systems
    unique_initiators = set()
    shops_summary: dict[str, int] = {}
    for s in systems:
        if s.get("initiator"):
            unique_initiators.add(s["initiator"])
        shop = s.get("shop", "unknown")
        shops_summary[shop] = shops_summary.get(shop, 0) + 1

    return {
        "game_id": game_id,
        "game_type": meta.get("gameType", game_id.split("_")[0]),
        "has_results": meta.get("hasResults", False),
        "fetched_at": datetime.now().isoformat(),
        "turnover": meta.get("turnover", 0),
        "jackpot": meta.get("jackpot", 0),
        "system_count_total": meta.get("systemCount", 0),
        "systems_fetched": len(systems),
        "systems_submitted": len([s for s in systems if s.get("submitted")]),
        "unique_initiators": len(unique_initiators),
        "shops": shops_summary,
        "payouts": meta.get("payouts", {}),
        "races": race_analysis,
    }


# ── Storage ───────────────────────────────────────────────────────────────────

def save_game(processed: dict, suffix: str = "") -> Path:
    """Save processed game data to JSON file."""
    CACHE_DIR.mkdir(exist_ok=True)
    game_id = processed["game_id"]
    filename = f"{game_id}{suffix}.json"
    filepath = CACHE_DIR / filename
    filepath.write_text(
        json.dumps(processed, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"Saved: {filepath}")
    return filepath


def load_game(game_id: str, suffix: str = "") -> Optional[dict]:
    """Load previously saved game data."""
    filepath = CACHE_DIR / f"{game_id}{suffix}.json"
    if not filepath.exists():
        return None
    return json.loads(filepath.read_text(encoding="utf-8"))


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_fetch_today():
    """Fetch all bettable/ongoing games from calendar."""
    games = fetch_calendar()
    fetchable = [g for g in games if g.get("status") in ("bettable", "ongoing")]

    if not fetchable:
        logger.info("No bettable/ongoing games found.")
        return

    for game in fetchable:
        game_id = game["id"]
        logger.info(f"Fetching {game_id} ({game.get('track', '?')}, {game.get('status', '?')})...")

        try:
            raw = fetch_game_data(game_id)
            systems = raw.get("systems", [])

            if not systems:
                logger.info(f"  No systems available for {game_id}, saving race data only.")
                processed = process_game(game_id, raw)
                save_game(processed, suffix="_races_only")
            else:
                processed = process_game(game_id, raw)
                submitted = processed["systems_submitted"]
                initiators = processed["unique_initiators"]
                logger.info(f"  {submitted} systems from {initiators} initiators")
                save_game(processed, suffix="_pre")

                # Print top picks per race
                for race in processed["races"]:
                    top = race["horses"][0] if race["horses"] else None
                    if top:
                        logger.info(
                            f"  Avd {race['race_number']}: "
                            f"#{top['number']} {top['name']} "
                            f"proffs={top['proffs_weighted_pct']:.1f}% "
                            f"public={top['public_pct']:.1f}% "
                            f"edge={top['edge_pp']:+.1f}pp"
                        )

        except Exception as e:
            logger.error(f"  Failed: {e}")

        time.sleep(REQUEST_DELAY)


def cmd_fetch_results():
    """Re-fetch games that have results to add winner data."""
    # Look for pre-race files without results
    CACHE_DIR.mkdir(exist_ok=True)
    pre_files = sorted(CACHE_DIR.glob("*_pre.json"))

    for pre_file in pre_files:
        data = json.loads(pre_file.read_text(encoding="utf-8"))
        if data.get("has_results"):
            continue  # already has results

        game_id = data["game_id"]
        logger.info(f"Checking results for {game_id}...")

        try:
            raw = fetch_game_data(game_id)
            meta = raw.get("meta", {})

            if meta.get("hasResults"):
                # Merge results into existing data
                for raw_race, saved_race in zip(raw.get("races", []), data.get("races", [])):
                    winner = raw_race.get("winner")
                    saved_race["winner"] = winner
                    for h in saved_race["horses"]:
                        raw_horse = next(
                            (rh for rh in raw_race.get("horses", []) if rh["number"] == h["number"]),
                            None,
                        )
                        if raw_horse:
                            h["place"] = raw_horse.get("place", 0)
                            h["final_odds"] = raw_horse.get("finalOdds", 0)
                            h["galloped"] = raw_horse.get("galloped", False)
                            h["disqualified"] = raw_horse.get("disqualified", False)
                            h["winner"] = (raw_horse["number"] == winner)

                data["has_results"] = True
                data["payouts"] = meta.get("payouts", {})
                data["results_fetched_at"] = datetime.now().isoformat()

                save_game(data, suffix="_pre")  # overwrite pre file with results
                logger.info(f"  Results added for {game_id}")
            else:
                logger.info(f"  No results yet for {game_id}")

        except Exception as e:
            logger.error(f"  Failed: {e}")

        time.sleep(REQUEST_DELAY)


def cmd_fetch_game(game_id: str):
    """Fetch a specific game by ID."""
    logger.info(f"Fetching {game_id}...")
    raw = fetch_game_data(game_id)
    processed = process_game(game_id, raw)
    save_game(processed, suffix="_pre" if not processed["has_results"] else "_results")

    for race in processed["races"]:
        top3 = race["horses"][:3]
        logger.info(f"\nAvd {race['race_number']} — {race.get('race_name', '')} | {race['distance']}m")
        for h in top3:
            winner_mark = " *** WINNER" if h.get("winner") else ""
            logger.info(
                f"  #{h['number']:2d} {h['name']:25s} "
                f"proffs={h['proffs_weighted_pct']:5.1f}% "
                f"public={h['public_pct']:5.1f}% "
                f"edge={h['edge_pp']:+6.1f}pp "
                f"odds={h['odds']:6.2f}"
                f"{winner_mark}"
            )


def cmd_fetch_history():
    """Fetch all historical games (races only, no systems)."""
    game_ids = fetch_history()
    logger.info(f"Found {len(game_ids)} historical games")

    for game_id in game_ids:
        existing = load_game(game_id, "_results") or load_game(game_id, "_races_only")
        if existing:
            continue

        logger.info(f"Fetching {game_id}...")
        try:
            raw = fetch_game_data(game_id)
            processed = process_game(game_id, raw)

            if processed["has_results"]:
                save_game(processed, suffix="_results")
            else:
                save_game(processed, suffix="_races_only")

        except Exception as e:
            logger.error(f"  Failed: {e}")

        time.sleep(REQUEST_DELAY)


def cmd_analyze():
    """Analyze accuracy of collected proffs data where we have both picks and results."""
    CACHE_DIR.mkdir(exist_ok=True)
    pre_files = sorted(CACHE_DIR.glob("*_pre.json"))

    total_races = 0
    proffs_top1_correct = 0
    proffs_top3_correct = 0
    public_top1_correct = 0
    edge_positive_wins = 0
    edge_positive_total = 0
    big_edge_wins = 0
    big_edge_total = 0

    games_with_results = 0

    for f in pre_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not data.get("has_results"):
            continue

        games_with_results += 1

        for race in data.get("races", []):
            winner = race.get("winner")
            if not winner:
                continue

            horses = race["horses"]
            if not horses:
                continue

            total_races += 1

            # Check if proffs top pick won
            proffs_top = horses[0]
            if proffs_top["number"] == winner:
                proffs_top1_correct += 1

            # Check if winner was in proffs top 3
            proffs_top3_nums = {h["number"] for h in horses[:3]}
            if winner in proffs_top3_nums:
                proffs_top3_correct += 1

            # Check if public favorite won
            public_sorted = sorted(horses, key=lambda h: h["public_pct"], reverse=True)
            if public_sorted[0]["number"] == winner:
                public_top1_correct += 1

            # Edge analysis: did positive-edge horses win more?
            winner_horse = next((h for h in horses if h["number"] == winner), None)
            if winner_horse and winner_horse["edge_pp"] > 0:
                edge_positive_wins += 1
            for h in horses:
                if h["edge_pp"] > 0:
                    edge_positive_total += 1
                if h["edge_pp"] > 15:
                    big_edge_total += 1
                    if h["number"] == winner:
                        big_edge_wins += 1

    if total_races == 0:
        logger.info("No games with both proffs data and results found yet.")
        logger.info("Run 'python fetch_proffs_data.py' before races to collect picks,")
        logger.info("then 'python fetch_proffs_data.py --results' after races to add outcomes.")
        return

    print(f"\n{'='*60}")
    print(f"PROFFS ACCURACY ANALYSIS")
    print(f"{'='*60}")
    print(f"Games analyzed:           {games_with_results}")
    print(f"Total races:              {total_races}")
    print(f"")
    print(f"Proffs top-1 accuracy:    {proffs_top1_correct}/{total_races} = {proffs_top1_correct/total_races*100:.1f}%")
    print(f"Proffs top-3 accuracy:    {proffs_top3_correct}/{total_races} = {proffs_top3_correct/total_races*100:.1f}%")
    print(f"Public top-1 accuracy:    {public_top1_correct}/{total_races} = {public_top1_correct/total_races*100:.1f}%")
    print(f"")
    print(f"Edge analysis:")
    print(f"  Winners with +edge:     {edge_positive_wins}/{total_races} = {edge_positive_wins/total_races*100:.1f}%")
    if big_edge_total > 0:
        print(f"  Big edge (>15pp) wins:  {big_edge_wins}/{big_edge_total} = {big_edge_wins/big_edge_total*100:.1f}%")
    print(f"{'='*60}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch viktat proffsstreck data")
    parser.add_argument("--game", type=str, help="Fetch specific game ID")
    parser.add_argument("--results", action="store_true", help="Fetch results for collected games")
    parser.add_argument("--history", action="store_true", help="Fetch all historical games")
    parser.add_argument("--analyze", action="store_true", help="Analyze proffs accuracy")
    parser.add_argument("--calendar", action="store_true", help="Show calendar of upcoming games")

    args = parser.parse_args()

    if args.game:
        cmd_fetch_game(args.game)
    elif args.results:
        cmd_fetch_results()
    elif args.history:
        cmd_fetch_history()
    elif args.analyze:
        cmd_analyze()
    elif args.calendar:
        games = fetch_calendar()
        for g in games:
            print(f"  {g['id']:40s} {g.get('track', ''):20s} {g.get('status', ''):10s} {g.get('startTime', '')}")
    else:
        cmd_fetch_today()


if __name__ == "__main__":
    main()
