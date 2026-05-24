"""Hämta 5 års V75/V85-historik från ATG API.

Scannar varje lördag (V75) och onsdag/fredag (V85) från 2021-01-01 till nu
och hämtar omgångar som vi inte redan har cachade.
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.config import CACHE_DIR, REQUEST_DELAY

import time


async def main():
    client = ATGClient()
    cache_dir = CACHE_DIR

    # Hitta redan cachade datum
    import re
    cached_dates = {"V75": set(), "V85": set()}
    pattern_v75 = re.compile(r"^V75_(\d{4}-\d{2}-\d{2})_")
    pattern_v85 = re.compile(r"^V85_(\d{4}-\d{2}-\d{2})_")

    if cache_dir.exists():
        for f in cache_dir.iterdir():
            m = pattern_v75.match(f.name)
            if m:
                cached_dates["V75"].add(m.group(1))
            m = pattern_v85.match(f.name)
            if m:
                cached_dates["V85"].add(m.group(1))

    print(f"Redan cachade: V75={len(cached_dates['V75'])}, V85={len(cached_dates['V85'])}")

    # Generera alla lördagar (V75) från 2021-01-01 till nu
    start = date(2021, 1, 1)
    end = date.today()

    dates_to_try = []

    # V75: Lördagar
    d = start
    while d <= end:
        if d.weekday() == 5:  # Lördag
            d_str = str(d)
            if d_str not in cached_dates["V75"]:
                dates_to_try.append(("V75", d))
        d += timedelta(days=1)

    # V85: Onsdag + Fredag (V85 körs olika dagar)
    d = start
    while d <= end:
        if d.weekday() in (2, 4):  # Onsdag, Fredag
            d_str = str(d)
            if d_str not in cached_dates["V85"]:
                dates_to_try.append(("V85", d))
        d += timedelta(days=1)

    print(f"Datum att testa: {len(dates_to_try)}")
    print(f"  V75 att hämta: {sum(1 for g, _ in dates_to_try if g == 'V75')}")
    print(f"  V85 att hämta: {sum(1 for g, _ in dates_to_try if g == 'V85')}")

    fetched = 0
    failed = 0
    skipped = 0

    for i, (game_type, d) in enumerate(dates_to_try):
        try:
            gr = await client.fetch_full_round(game_type, d)
            if gr and gr.races:
                fetched += 1
                status = "✓"
                n_horses = sum(r.num_starters for r in gr.races)
                print(f"  [{i+1}/{len(dates_to_try)}] {status} {game_type} {d}: "
                      f"{gr.num_races} lopp, {n_horses} hästar")
            else:
                skipped += 1
                # Skriv inte ut varje miss — det finns många datum utan V85
                if game_type == "V75":
                    print(f"  [{i+1}/{len(dates_to_try)}] - {game_type} {d}: ingen omgång")
        except Exception as e:
            failed += 1
            if "404" not in str(e) and "Not Found" not in str(e):
                print(f"  [{i+1}/{len(dates_to_try)}] ✗ {game_type} {d}: {e}")

        # Respektera rate limit
        time.sleep(REQUEST_DELAY)

        # Progress varje 100:e
        if (i + 1) % 100 == 0:
            print(f"\n  --- Progress: {i+1}/{len(dates_to_try)}, "
                  f"hämtade={fetched}, skippade={skipped}, fel={failed} ---\n")

    print(f"\n{'='*50}")
    print(f"  KLART!")
    print(f"  Hämtade: {fetched}")
    print(f"  Skippade (ej V75/V85 det datumet): {skipped}")
    print(f"  Fel: {failed}")

    # Ny total
    new_v75 = len(cached_dates["V75"]) + sum(1 for g, _ in dates_to_try if g == "V75" and g)
    new_v85 = len(cached_dates["V85"]) + sum(1 for g, _ in dates_to_try if g == "V85" and g)
    print(f"\n  Total cachade (uppskattning):")

    # Räkna om
    v75_count = 0
    v85_count = 0
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if pattern_v75.match(f.name):
                v75_count += 1
            elif pattern_v85.match(f.name):
                v85_count += 1
    print(f"    V75: {v75_count}")
    print(f"    V85: {v85_count}")
    print(f"    Total: {v75_count + v85_count}")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
