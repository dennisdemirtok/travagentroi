"""Hämta utökad historik: 2015-2020 V75 + GS75/V64/V86 alla år.

Tvåfas-strategi:
  1. Snabb kalender-scan: hämta /calendar/day/{date} för att hitta vilka datum
     som har en viss speltyp (lätt anrop, bara JSON-lookup)
  2. Full hämtning: bara för datum som faktiskt har omgångar

Undviker att hämta redan cachade omgångar.

Användning:
    python fetch_extended_history.py --all                    # Kör alla configs
    python fetch_extended_history.py --type V75               # Bara V75 2015-2020
    python fetch_extended_history.py --type GS75              # Bara GS75
    python fetch_extended_history.py --type V75 --start 2015-01-01 --end 2017-12-31
    python fetch_extended_history.py --scan-only --type GS75  # Bara hitta datum, hämta inte
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.config import CACHE_DIR, REQUEST_DELAY


# ── Hämtnings-konfigurationer ──
# (game_type, start_date, end_date, weekdays_to_scan)
# weekdays: 0=Mån, 1=Tis, 2=Ons, 3=Tor, 4=Fre, 5=Lör, 6=Sön
# None = alla dagar (för speltyper med variabelt schema)
DEFAULT_CONFIGS = [
    # V75 2015-2020: Lördagar (utöka befintlig 2021+ data bakåt)
    ("V75", date(2015, 1, 1), date(2020, 12, 31), [5]),
    # GS75 2021-idag: Söndagar
    ("GS75", date(2021, 1, 1), date.today(), [6]),
    # V64 2021-idag: Måndag, Tisdag, Torsdag, Fredag
    ("V64", date(2021, 1, 1), date.today(), [0, 1, 3, 4]),
    # V86 2021-idag: Onsdag
    ("V86", date(2021, 1, 1), date.today(), [2]),
]

# Kalender-scan cache-fil (sparar vilka datum vi redan scannat)
CALENDAR_SCAN_FILE = CACHE_DIR / "_calendar_scan_results.json"


def _scan_cache(cache_dir: Path) -> dict[str, set[str]]:
    """Hitta redan cachade (game_type, datum)-par."""
    cached = {}
    if not cache_dir.exists():
        return cached

    pattern = re.compile(r"^([A-Z0-9]+)_(\d{4}-\d{2}-\d{2})_")
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            gt = m.group(1)
            d = m.group(2)
            if gt not in cached:
                cached[gt] = set()
            cached[gt].add(d)

    return cached


def _load_calendar_scan() -> dict[str, list[str]]:
    """Ladda sparade kalender-scan-resultat."""
    if CALENDAR_SCAN_FILE.exists():
        try:
            with open(CALENDAR_SCAN_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_calendar_scan(scan_data: dict[str, list[str]]) -> None:
    """Spara kalender-scan-resultat."""
    with open(CALENDAR_SCAN_FILE, "w") as f:
        json.dump(scan_data, f, indent=2, default=str)


def _generate_dates(start: date, end: date, weekdays: list[int] | None) -> list[date]:
    """Generera alla datum i intervallet, filtrerat på veckodagar."""
    dates = []
    d = start
    while d <= end:
        if weekdays is None or d.weekday() in weekdays:
            dates.append(d)
        d += timedelta(days=1)
    return dates


async def calendar_scan(client: ATGClient, game_type: str, start: date, end: date,
                         weekdays: list[int] | None) -> list[date]:
    """Fas 1: Snabb kalender-scan — hitta datum med spelomgångar.

    Scannar /calendar/day/{date} som är ett lätt anrop (bara JSON-lista).
    Sparar resultat till fil så vi inte behöver skanna om.
    """
    scan_data = _load_calendar_scan()
    scan_key = f"{game_type}_{start}_{end}"

    # Kolla om vi redan har scannat detta intervall
    if scan_key in scan_data:
        known_dates = [date.fromisoformat(d) for d in scan_data[scan_key]]
        print(f"  Kalender-scan redan gjord: {len(known_dates)} datum med {game_type}")
        return known_dates

    all_dates = _generate_dates(start, end, weekdays)
    print(f"  Kalender-scan: {len(all_dates)} datum att kontrollera...")

    found_dates = []
    scanned = 0
    consecutive_misses = 0

    for i, d in enumerate(all_dates):
        try:
            calendar = await client.get_calendar(d)
            games = calendar.get("games", {})
            game_list = games.get(game_type.upper(), [])

            if game_list:
                found_dates.append(d)
                consecutive_misses = 0
                if len(found_dates) % 10 == 0 or len(found_dates) <= 5:
                    print(f"    ✓ {d} — {game_type} finns (totalt {len(found_dates)} hittade)")
            else:
                consecutive_misses += 1

            scanned += 1

            # Rate limit (kalender-anrop är lätta, men vi respekterar API:t)
            time.sleep(max(REQUEST_DELAY * 0.5, 0.3))

            # Progress
            if scanned % 200 == 0:
                print(f"    ... skannat {scanned}/{len(all_dates)}, "
                      f"hittat {len(found_dates)} datum med {game_type}")

        except Exception as e:
            err_str = str(e)
            if "404" not in err_str and "Not Found" not in err_str:
                print(f"    ✗ Kalender-scan {d}: {e}")
            consecutive_misses += 1

    # Spara resultat
    scan_data[scan_key] = [str(d) for d in found_dates]
    _save_calendar_scan(scan_data)

    print(f"  Scan klar: {len(found_dates)} datum med {game_type} (av {scanned} kontrollerade)")
    return found_dates


async def fetch_config(client: ATGClient, game_type: str, start: date, end: date,
                       weekdays: list[int] | None, cached_dates: set[str],
                       scan_only: bool = False) -> dict:
    """Hämta omgångar för en konfiguration med tvåfas-strategi."""

    print(f"\n{'─'*60}")
    print(f"  {game_type}: {start} → {end}")
    if weekdays:
        day_names = {0: "Mån", 1: "Tis", 2: "Ons", 3: "Tor", 4: "Fre", 5: "Lör", 6: "Sön"}
        days_str = ", ".join(day_names.get(w, str(w)) for w in weekdays)
        print(f"  Veckodagar: {days_str}")
    else:
        print(f"  Alla dagar (variabelt schema → kalender-scan)")
    print(f"{'─'*60}")

    # ── Fas 1: Kalender-scan ──
    # För speltyper med kända veckodagar, skippa scan (V75 lördag, V86 onsdag etc.)
    if weekdays and len(weekdays) <= 2:
        # Kända veckodagar — generera datum direkt
        dates_with_games = _generate_dates(start, end, weekdays)
        print(f"  {len(dates_with_games)} möjliga datum (kända veckodagar)")
    else:
        # Variabelt schema — kör kalender-scan
        dates_with_games = await calendar_scan(client, game_type, start, end, weekdays)

    if scan_only:
        print(f"\n  --scan-only: hittat {len(dates_with_games)} datum, avslutar")
        return {"fetched": 0, "skipped": 0, "failed": 0, "found": len(dates_with_games)}

    # ── Fas 2: Hämta fulla omgångar ──
    # Filtrera bort redan cachade
    dates_to_fetch = [d for d in dates_with_games if str(d) not in cached_dates]

    print(f"  {len(dates_with_games)} datum med {game_type}, "
          f"{len(dates_to_fetch)} att hämta ({len(dates_with_games) - len(dates_to_fetch)} redan cachade)")

    if not dates_to_fetch:
        print(f"  Allt redan hämtat!")
        return {"fetched": 0, "skipped": 0, "failed": 0}

    fetched = 0
    skipped = 0
    failed = 0

    for i, d in enumerate(dates_to_fetch):
        try:
            gr = await client.fetch_full_round(game_type, d)
            if gr and gr.races:
                fetched += 1
                n_horses = sum(r.num_starters for r in gr.races)
                print(f"  [{i+1}/{len(dates_to_fetch)}] ✓ {game_type} {d}: "
                      f"{gr.num_races} lopp, {n_horses} hästar, "
                      f"bana: {gr.track_name or '?'}")
            else:
                skipped += 1
                if weekdays and len(weekdays) <= 2:
                    print(f"  [{i+1}/{len(dates_to_fetch)}] - {game_type} {d}: ingen omgång")
        except Exception as e:
            failed += 1
            err_str = str(e)
            if "404" not in err_str and "Not Found" not in err_str:
                print(f"  [{i+1}/{len(dates_to_fetch)}] ✗ {game_type} {d}: {e}")

        # Rate limit
        time.sleep(REQUEST_DELAY)

        # Progress varje 50:e
        if (i + 1) % 50 == 0:
            print(f"\n  --- Progress {game_type}: {i+1}/{len(dates_to_fetch)}, "
                  f"hämtade={fetched}, skippade={skipped}, fel={failed} ---\n")

    print(f"\n  {game_type} klart: hämtade={fetched}, skippade={skipped}, fel={failed}")
    return {"fetched": fetched, "skipped": skipped, "failed": failed}


async def main():
    parser = argparse.ArgumentParser(description="Hämta utökad travhistorik från ATG API")
    parser.add_argument("--type", type=str, help="Speltyp att hämta (V75, GS75, V64, V86)")
    parser.add_argument("--start", type=str, help="Startdatum (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="Slutdatum (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="Kör alla konfigurationer")
    parser.add_argument("--scan-only", action="store_true",
                        help="Bara kalender-scan, hämta inte fulla omgångar")
    args = parser.parse_args()

    client = ATGClient()
    cache_dir = CACHE_DIR
    cached = _scan_cache(cache_dir)

    # Visa nuläge
    print(f"{'='*60}")
    print(f"  UTÖKAD HISTORIK — ATG Data Fetch")
    print(f"{'='*60}")
    print(f"\n  Redan cachade omgångar:")
    for gt in sorted(cached.keys()):
        dates = sorted(cached[gt])
        if dates:
            print(f"    {gt}: {len(dates)} omgångar ({dates[0]} → {dates[-1]})")
    print()

    # Bestäm vilka configs att köra
    configs_to_run = []

    if args.all:
        configs_to_run = DEFAULT_CONFIGS
    elif args.type:
        gt = args.type.upper()
        # Hitta matchande default config eller skapa custom
        found = False
        for cfg in DEFAULT_CONFIGS:
            if cfg[0] == gt:
                start = date.fromisoformat(args.start) if args.start else cfg[1]
                end = date.fromisoformat(args.end) if args.end else cfg[2]
                configs_to_run.append((gt, start, end, cfg[3]))
                found = True
                break
        if not found:
            # Custom config — scanna alla dagar
            start = date.fromisoformat(args.start) if args.start else date(2021, 1, 1)
            end = date.fromisoformat(args.end) if args.end else date.today()
            configs_to_run.append((gt, start, end, None))
    else:
        # Default: kör alla
        configs_to_run = DEFAULT_CONFIGS

    # Kör
    total_stats = {"fetched": 0, "skipped": 0, "failed": 0}

    for game_type, start, end, weekdays in configs_to_run:
        cached_dates = cached.get(game_type, set())
        stats = await fetch_config(
            client, game_type, start, end, weekdays, cached_dates,
            scan_only=args.scan_only
        )
        for k in total_stats:
            if k in stats:
                total_stats[k] += stats[k]

    # Räkna om cache
    print(f"\n{'='*60}")
    print(f"  SAMMANFATTNING")
    print(f"{'='*60}")
    print(f"  Totalt hämtade: {total_stats['fetched']}")
    print(f"  Skippade (ingen omgång): {total_stats['skipped']}")
    print(f"  Fel: {total_stats['failed']}")

    # Räkna nya totaler
    new_cached = _scan_cache(cache_dir)
    print(f"\n  Cache-totaler efter körning:")
    for gt in sorted(new_cached.keys()):
        dates = sorted(new_cached[gt])
        if dates:
            print(f"    {gt}: {len(dates)} omgångar ({dates[0]} → {dates[-1]})")

    total = sum(len(v) for v in new_cached.values())
    print(f"    Total: {total} omgångar")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
