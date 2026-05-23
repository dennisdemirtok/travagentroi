"""Enrich GameRound med scrapead data från ATG:s webbsida.

Scraped JSON har formatet:
{
  "1": { "raceNumber": 1, "horses": [ { "num": 1, "name": "...", "driver": "...",
         "v85pct": 0.11, "starter2025": "10 3-1-0", "starterLivs": "27 6-4-4",
         "poang": 1423, "krStart": 13933, "snittodds": 19.91, "pengar": 376200,
         "trend": "+1.81", "vagn": "Va.", "vodds": 3.74,
         "starts": [ { "date": "2026-02-03", "track": "Östersund-8", "plac": "1",
                        "distance": 1640, "spaar": 1, "kmTid": "13,6a", "odds": 1.63, ... } ]
  } ] }, ...
}
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .models import (
    GameRound,
    Horse,
    PastStart,
    Race,
    RaceEntry,
    StartMethod,
)


def _parse_km_time(raw: str) -> float | None:
    """Parsa km-tid från ATG-format, t.ex. '13,6a' -> 73.6, '14,1a' -> 74.1, '15,4' -> 75.4."""
    if not raw or raw == '-':
        return None
    # Remove 'a' (autostart) or other suffixes
    cleaned = re.sub(r'[a-zA-Z]', '', raw).strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace(',', '.')
    try:
        val = float(cleaned)
    except ValueError:
        return None
    # ATG visar km-tid som t.ex. 13,6 = 1:13.6 = 73.6 sekunder
    # Om värdet < 60 tolkas det som "sekunder-del" (t.ex. 13.6 -> 73.6)
    if val < 60:
        return 60.0 + val
    return val


def _parse_placement(raw: str) -> int | None:
    """Parsa placering: '1' -> 1, 'd' -> None (disk), 'str' -> None, '0' -> None."""
    if not raw:
        return None
    raw = raw.strip()
    if raw in ('d', 'str', 'u', 'ag', '0', '-'):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_starts_string(raw: str) -> tuple[int, int, int, int]:
    """Parsa '10 3-1-0' -> (10, 3, 1, 0) = (starts, wins, seconds, thirds).

    Hanterar även ihopklistrat format från scrape där mellanslag saknas:
    '103-1-0' -> '10 3-1-0' -> (10, 3, 1, 0)
    '276-4-4' -> '27 6-4-4' -> (27, 6, 4, 4)
    """
    if not raw:
        return (0, 0, 0, 0)
    raw = raw.strip()

    # Format med mellanslag: "10 3-1-0"
    parts = re.split(r'\s+', raw)
    if len(parts) == 2:
        try:
            total = int(parts[0])
        except ValueError:
            total = 0
        wst = parts[1].split('-')
        try:
            wins = int(wst[0]) if len(wst) > 0 else 0
            seconds = int(wst[1]) if len(wst) > 1 else 0
            thirds = int(wst[2]) if len(wst) > 2 else 0
        except ValueError:
            wins, seconds, thirds = 0, 0, 0
        return (total, wins, seconds, thirds)

    # Format utan mellanslag: "103-1-0" eller "276-4-4"
    # Scrapen missade mellanslaget mellan totala starter och vinster
    if len(parts) == 1 and '-' in raw:
        dash_parts = raw.split('-')
        if len(dash_parts) >= 3:
            first_chunk = dash_parts[0]  # t.ex. "103" eller "276"
            try:
                seconds = int(dash_parts[1])
                thirds = int(dash_parts[2]) if len(dash_parts) > 2 else 0
            except ValueError:
                return (0, 0, 0, 0)

            # Hitta splitpunkt i första chunken: "103" -> "10" + "3"
            # Prova alla splitpunkter, välj den där total > wins och rimliga värden
            best = None
            for i in range(1, len(first_chunk)):
                try:
                    total = int(first_chunk[:i])
                    wins = int(first_chunk[i:])
                except ValueError:
                    continue
                if wins <= total and seconds <= total and thirds <= total and total < 500:
                    best = (total, wins, seconds, thirds)
                    break  # Första rimliga splitten (minsta total)

            if best:
                return best

            # Fallback: tolka hela första chunken som vinster (inga totala starter)
            try:
                wins = int(first_chunk)
            except ValueError:
                wins = 0
            total = wins + seconds + thirds
            return (total, wins, seconds, thirds)

    return (0, 0, 0, 0)


def _parse_prize(raw: str) -> int:
    """Parsa pris: "35'" -> 35000, "135'" -> 135000, "1'" -> 1000."""
    if not raw:
        return 0
    cleaned = raw.strip().rstrip("'").replace(' ', '')
    try:
        return int(cleaned) * 1000
    except ValueError:
        return 0


def _track_name_clean(raw: str) -> str:
    """'Östersund-8' -> 'Östersund'."""
    return re.sub(r'-\d+$', '', raw)


def enrich_game_round(game_round: GameRound, scraped_path: str | Path) -> GameRound:
    """Berika en GameRound med scrapead data från JSON-fil."""
    with open(scraped_path) as f:
        scraped = json.load(f)

    for race in game_round.races:
        race_key = str(race.race_number)
        if race_key not in scraped:
            continue

        # Detektera spårtrappa från loppvillkor eller loppnamn
        cond = (race.conditions or "").lower()
        name = (race.race_name or "").lower()
        if any("spårtrappa" in s or "spartrappa" in s for s in [cond, name]):
            race.is_stair_draw = True

        scraped_race = scraped[race_key]
        scraped_horses = {h["num"]: h for h in scraped_race["horses"]}

        for entry in race.entries:
            sh = scraped_horses.get(entry.post_position)
            if not sh:
                continue

            # Streckprocent (bet_percentage)
            entry.bet_percentage = sh.get("v85pct", 0) or None

            # V-odds
            vodds = sh.get("vodds", 0)
            if vodds and vodds > 0:
                entry.odds = vodds

            # Kusknamn
            if sh.get("driver"):
                entry.driver_name = sh["driver"]

            # Hästnamn (om saknas)
            if sh.get("nm") or sh.get("name"):
                name = sh.get("nm") or sh.get("name")
                if name:
                    entry.horse.name = name

            # Karriärstatistik från livs-strängen
            livs = _parse_starts_string(sh.get("starterLivs", "") or sh.get("sl", ""))
            if livs[0] > 0:
                entry.horse.career.total_starts = livs[0]
                entry.horse.career.wins = livs[1]
                entry.horse.career.seconds = livs[2]
                entry.horse.career.thirds = livs[3]
                entry.horse.career.win_rate = livs[1] / livs[0] if livs[0] > 0 else 0
                entry.horse.career.top3_rate = (livs[1] + livs[2] + livs[3]) / livs[0] if livs[0] > 0 else 0

            # Prispengar
            pengar = sh.get("pengar") or sh.get("pe", 0)
            if pengar:
                entry.horse.career.total_prize_money = pengar

            # Trend (+1.81, -2.73)
            trend_raw = sh.get("trend", "")
            if trend_raw:
                try:
                    entry.trend = float(str(trend_raw).replace(",", "."))
                except (ValueError, TypeError):
                    pass

            # Poäng (ATG:s heuristiska score)
            poang = sh.get("poang", 0)
            if poang:
                try:
                    entry.points = int(poang)
                except (ValueError, TypeError):
                    pass

            # Kr/start (snitt prispengar per start)
            kr_start = sh.get("krStart", 0)
            if kr_start:
                try:
                    entry.horse.career.prize_per_start = int(kr_start)
                except (ValueError, TypeError):
                    pass

            # Snittodds
            snittodds = sh.get("snittodds", 0)
            if snittodds and float(snittodds) > 0:
                try:
                    entry.horse.career.avg_odds = float(snittodds)
                except (ValueError, TypeError):
                    pass

            # Senaste starter — bara om API:t inte redan gett oss historik
            scraped_starts = sh.get("starts", [])
            if scraped_starts and len(entry.horse.past_starts) < len(scraped_starts):
                past_starts = []
                for s in scraped_starts:
                    track_raw = s.get("track", "")
                    km_time = _parse_km_time(s.get("kmTid", ""))
                    plac = _parse_placement(s.get("plac", ""))

                    dist = s.get("distance", 0)
                    if not dist:
                        # Try to parse from distSpaar
                        ds = s.get("distSpaar", "")
                        dm = re.match(r'(\d+)', ds)
                        if dm:
                            dist = int(dm.group(1))

                    start_date = date.today()
                    try:
                        start_date = date.fromisoformat(s["date"])
                    except (KeyError, ValueError):
                        pass

                    km_tid_raw = s.get("kmTid", "")
                    is_galopp = km_tid_raw.endswith("g") or "ag" in km_tid_raw
                    # Startmetod: suffix 'a' = auto, annars volt
                    is_auto = km_tid_raw.rstrip('0123456789,. ').endswith('a')
                    start_method = StartMethod.AUTO if is_auto else StartMethod.VOLT

                    # Pris: "35'" = 35000, "135'" = 135000
                    pris_raw = s.get("pris", "")
                    prize_money = _parse_prize(pris_raw)

                    past_starts.append(PastStart(
                        start_date=start_date,
                        track_name=_track_name_clean(track_raw),
                        distance=dist,
                        start_method=start_method,
                        post_position=s.get("spaar", 0),
                        placement=plac,
                        disqualified=plac is None and s.get("plac", "") == "d",
                        galloped=is_galopp,
                        km_time=km_time,
                        prize_money=prize_money,
                        race_purse=prize_money,
                        driver_name=s.get("driver", ""),
                        odds=s.get("odds"),
                    ))

                entry.horse.past_starts = past_starts

    return game_round
