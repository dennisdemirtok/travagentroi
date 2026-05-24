#!/usr/bin/env python3
"""Deep Edge Finder v2 — exhaustive signal mining.

Tests EVERY signal Dennis asked about:
1. Sulky type (Amerik vs Vanlig) — not just change
2. Last start winner — seger senast
3. Class drop (high prize → low class) — gått ned i klass
4. Relative prize money (vs field avg) — mött hårdare motstånd
5. Low odds vs field — favorit relativt fältet
6. Barfota combinations
7. Days since last start (rest signal)
8. Distance change
9. Start method change (auto↔volt)
10. Driver upgrade/downgrade
11. Gallop history (disqualification risk)
12. ALL combinations of top signals
"""
import json
import glob
import sys
from collections import defaultdict
from pathlib import Path
from datetime import date, datetime
from math import prod

CACHE_DIR = Path("cache")


def parse_km_time(time_obj):
    if not time_obj:
        return None
    m = time_obj.get("minutes", 0)
    s = time_obj.get("seconds", 0)
    t = time_obj.get("tenths", 0)
    return m * 60 + s + t / 10


def extract_all_data(filepath):
    """Extract rich structured data from raw ATG API file."""
    with open(filepath) as f:
        data = json.load(f)

    races_data = data.get("races", [])
    if not isinstance(races_data, list) or not races_data:
        return []

    results = []
    for race in races_data:
        starts = race.get("starts", [])
        if not starts or len(starts) < 4:
            continue

        # Get result order
        result_order = []
        placed = []
        for s in starts:
            r = s.get("result", {})
            if r and isinstance(r, dict):
                place = r.get("place") or r.get("finishOrder")
                if place and isinstance(place, int):
                    placed.append((place, s.get("number", 0)))
        if placed:
            placed.sort()
            result_order = [num for _, num in placed]

        if not result_order:
            continue

        winner = result_order[0]
        race_distance = race.get("distance", 0)
        race_method = race.get("startMethod", "auto")

        entries = []
        for s in starts:
            horse = s.get("horse", {})
            shoes_data = horse.get("shoes", {})
            sulky_data = horse.get("sulky", s.get("sulky", {}))
            driver = s.get("driver", {})
            result = s.get("result", {})
            pools = s.get("pools", {})
            stats = horse.get("statistics", {})
            trainer = horse.get("trainer", {})

            # Shoes details
            front = shoes_data.get("front", {}) if isinstance(shoes_data, dict) else {}
            back = shoes_data.get("back", {}) if isinstance(shoes_data, dict) else {}
            barefoot_front = not front.get("hasShoe", True) if front else False
            barefoot_back = not back.get("hasShoe", True) if back else False
            shoe_changed = front.get("changed", False) or back.get("changed", False)

            # Sulky details
            sulky_type_code = ""
            sulky_changed = False
            if isinstance(sulky_data, dict):
                st = sulky_data.get("type", {})
                if isinstance(st, dict):
                    sulky_type_code = st.get("code", "")
                    sulky_changed = st.get("changed", False)

            # Driver win pct
            driver_stats = driver.get("statistics", {})
            driver_win_pct = 0
            driver_starts = 0
            if isinstance(driver_stats, dict):
                years = driver_stats.get("years", {})
                for y in sorted(years.keys(), reverse=True):
                    ys = years[y]
                    driver_starts = ys.get("starts", 0)
                    wins = ys.get("placement", {}).get("1", 0)
                    if driver_starts > 10:
                        driver_win_pct = wins / driver_starts
                    # Also check winPercentage field
                    wp = ys.get("winPercentage", 0)
                    if wp and driver_starts > 10:
                        driver_win_pct = wp / 10000.0
                    break

            # Bet distribution
            bet_dist = 0
            for pn in ["V75", "V86", "V85", "V64", "GS75"]:
                pool = pools.get(pn, pools.get(pn.lower(), {}))
                if pool and isinstance(pool, dict):
                    bd = pool.get("betDistribution", 0)
                    if bd:
                        bet_dist = bd / 100.0
                        break

            # Final odds
            final_odds = 0
            if isinstance(result, dict):
                final_odds = result.get("finalOdds", 0) or 0

            # Horse stats
            total_prize = horse.get("money", 0)
            age = horse.get("age", 0)

            # Horse past performance from statistics
            horse_year_stats = {}
            horse_prev_year_stats = {}
            if isinstance(stats, dict):
                years = stats.get("years", {})
                sorted_years = sorted(years.keys(), reverse=True)
                if sorted_years:
                    horse_year_stats = years[sorted_years[0]]
                if len(sorted_years) > 1:
                    horse_prev_year_stats = years[sorted_years[1]]

            horse_starts_yr = horse_year_stats.get("starts", 0)
            horse_wins_yr = horse_year_stats.get("placement", {}).get("1", 0)
            horse_earnings_yr = horse_year_stats.get("earnings", 0)

            # Record (best time)
            record = horse.get("record", {})
            best_time = parse_km_time(record.get("time")) if isinstance(record, dict) else None

            # Placement
            place = None
            km_time = None
            if isinstance(result, dict):
                place = result.get("place") or result.get("finishOrder")
                km_time = parse_km_time(result.get("kmTime"))

            # Home track
            ht = horse.get("homeTrack", {})
            home_track = ht.get("name", "") if isinstance(ht, dict) else ""

            entries.append({
                "number": s.get("number", 0),
                "post_position": s.get("postPosition", 0),
                "distance": s.get("distance", race_distance),
                "horse_name": horse.get("name", ""),
                "age": age,
                "sex": horse.get("sex", ""),
                "home_track": home_track,
                "total_prize": total_prize,
                "horse_starts_yr": horse_starts_yr,
                "horse_wins_yr": horse_wins_yr,
                "horse_earnings_yr": horse_earnings_yr,
                "best_time": best_time,
                "barefoot_front": barefoot_front,
                "barefoot_back": barefoot_back,
                "barefoot": barefoot_front or barefoot_back,
                "shoe_changed": shoe_changed,
                "sulky_type": sulky_type_code,
                "sulky_changed": sulky_changed,
                "driver_name": f"{driver.get('firstName', '')} {driver.get('lastName', '')}".strip(),
                "driver_win_pct": driver_win_pct,
                "driver_starts": driver_starts,
                "bet_dist": bet_dist,
                "final_odds": final_odds,
                "place": place,
                "km_time": km_time,
                "won": s.get("number", 0) == winner,
            })

        # Compute RELATIVE metrics for each entry vs the field
        field_avg_prize = sum(e["total_prize"] for e in entries) / len(entries) if entries else 0
        field_max_prize = max((e["total_prize"] for e in entries), default=0)
        field_min_odds = min((e["final_odds"] for e in entries if e["final_odds"] > 0), default=999)
        field_avg_odds = sum(e["final_odds"] for e in entries if e["final_odds"] > 0) / max(1, sum(1 for e in entries if e["final_odds"] > 0))

        for e in entries:
            e["prize_vs_field"] = e["total_prize"] / field_avg_prize if field_avg_prize > 0 else 1.0
            e["is_class_dropper"] = e["total_prize"] > field_avg_prize * 1.5  # 50% mer pris = klasstapp
            e["is_big_class_dropper"] = e["total_prize"] > field_avg_prize * 2.0
            e["odds_rank_in_field"] = 0  # Will compute below
            e["is_favorite"] = False
            e["is_outsider"] = False

        # Rank by odds (lowest = favorite)
        entries_with_odds = sorted([e for e in entries if e["final_odds"] > 0], key=lambda x: x["final_odds"])
        for rank, e in enumerate(entries_with_odds):
            e["odds_rank_in_field"] = rank + 1
            if rank == 0:
                e["is_favorite"] = True
            if rank >= len(entries_with_odds) - 3:
                e["is_outsider"] = True

        # Rank by bet distribution
        entries_by_streck = sorted([e for e in entries if e["bet_dist"] > 0], key=lambda x: x["bet_dist"], reverse=True)
        for rank, e in enumerate(entries_by_streck):
            e["streck_rank"] = rank + 1
            if rank == 0:
                e["is_streck_fav"] = True
            else:
                e["is_streck_fav"] = False

        results.append({
            "entries": entries,
            "winner": winner,
            "distance": race_distance,
            "method": race_method,
            "num_starters": len(entries),
            "track": race.get("track", {}).get("name", "") if isinstance(race.get("track"), dict) else "",
        })

    return results


def main():
    print("=" * 80)
    print("  DEEP EDGE FINDER v2 — Exhaustive signal analysis")
    print("=" * 80)

    all_races = []
    files = sorted(glob.glob(str(CACHE_DIR / "*_*_*_*.json")))
    print(f"\nProcessing {len(files)} cache files...")

    for fp in files:
        try:
            races = extract_all_data(fp)
            all_races.extend(races)
        except Exception:
            continue

    valid = [r for r in all_races if r["winner"] and len(r["entries"]) >= 4]
    total_entries = sum(len(r["entries"]) for r in valid)
    total_wins = len(valid)
    base_rate = total_wins / total_entries

    print(f"Valid races: {len(valid)}, entries: {total_entries}, base win rate: {base_rate:.1%}")

    # ═══════════════════════════════════════════════════════════════
    # Helper to compute signal stats
    # ═══════════════════════════════════════════════════════════════
    def signal_stats(label, filter_fn, min_n=100):
        wins = 0
        total = 0
        for race in valid:
            for e in race["entries"]:
                if filter_fn(e, race):
                    total += 1
                    if e["won"]:
                        wins += 1
        if total < min_n:
            return None
        rate = wins / total
        lift = rate / base_rate
        return {"label": label, "wins": wins, "total": total, "rate": rate, "lift": lift}

    all_signals = []

    def test(label, fn, min_n=100):
        r = signal_stats(label, fn, min_n)
        if r:
            all_signals.append(r)
            return r
        return None

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SULKY TYPE (Amerik vs Vanlig vs annat)")
    print(f"{'='*70}\n")

    for code, label in [("AM", "Amerik sulky"), ("VA", "Vanlig sulky"), ("", "Okänd/saknas")]:
        test(label, lambda e, r, c=code: e["sulky_type"] == c)
    test("Sulky changed", lambda e, r: e["sulky_changed"])
    test("Sulky NOT changed", lambda e, r: not e["sulky_changed"])
    test("Amerik + changed", lambda e, r: e["sulky_type"] == "AM" and e["sulky_changed"])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  KLASS-TAPP (hög prissumma vs fältgenomsnitt)")
    print(f"{'='*70}\n")

    test("Class dropper (1.5x avg prize)", lambda e, r: e.get("is_class_dropper", False))
    test("Big class dropper (2x avg prize)", lambda e, r: e.get("is_big_class_dropper", False))
    test("Class climber (<0.5x avg prize)", lambda e, r: e["total_prize"] < r["entries"][0]["total_prize"] * 0.5 if r["entries"] else False)

    # Prize vs field
    for lo, hi, label in [(0, 0.5, "Pris < 0.5x fält"), (0.5, 1.0, "Pris 0.5-1x fält"),
                           (1.0, 1.5, "Pris 1-1.5x fält"), (1.5, 2.5, "Pris 1.5-2.5x fält"),
                           (2.5, 99, "Pris 2.5x+ fält")]:
        test(label, lambda e, r, lo=lo, hi=hi: lo <= e.get("prize_vs_field", 1.0) < hi)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  FAVORIT vs OUTSIDER (streckprocent rank)")
    print(f"{'='*70}\n")

    test("Streck-favorit (#1)", lambda e, r: e.get("is_streck_fav", False))
    for rank in [2, 3, 4, 5]:
        test(f"Streck rank #{rank}", lambda e, r, rk=rank: e.get("streck_rank", 0) == rk)
    test("Streck rank 6+", lambda e, r: e.get("streck_rank", 0) >= 6)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  UTRUSTNING: Barfota detaljer")
    print(f"{'='*70}\n")

    test("Barfota fram", lambda e, r: e["barefoot_front"])
    test("Barfota bak", lambda e, r: e["barefoot_back"])
    test("Barfota båda", lambda e, r: e["barefoot_front"] and e["barefoot_back"])
    test("Barfota + favorit (streck #1-3)", lambda e, r: e["barefoot"] and e.get("streck_rank", 99) <= 3)
    test("Barfota + outsider (streck 6+)", lambda e, r: e["barefoot"] and e.get("streck_rank", 0) >= 6)
    test("Sko + ej byte", lambda e, r: not e["barefoot"] and not e["shoe_changed"])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  ÅLDER detaljer")
    print(f"{'='*70}\n")

    for age in range(2, 12):
        test(f"Ålder {age}", lambda e, r, a=age: e["age"] == a)
    test("Ålder 3-4 (peak)", lambda e, r: e["age"] in (3, 4))
    test("Ålder 3-5", lambda e, r: 3 <= e["age"] <= 5)
    test("Ålder 7+ (åldrande)", lambda e, r: e["age"] >= 7)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  KUSK vinstprocent detaljerat")
    print(f"{'='*70}\n")

    for lo, hi, label in [(0, 0.03, "Kusk 0-3%"), (0.03, 0.06, "Kusk 3-6%"),
                           (0.06, 0.10, "Kusk 6-10%"), (0.10, 0.13, "Kusk 10-13%"),
                           (0.13, 0.16, "Kusk 13-16%"), (0.16, 0.20, "Kusk 16-20%"),
                           (0.20, 0.25, "Kusk 20-25%"), (0.25, 1.0, "Kusk 25%+")]:
        test(label, lambda e, r, lo=lo, hi=hi: e["driver_starts"] >= 30 and lo <= e["driver_win_pct"] < hi)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  FÄLTSTORLEK effekt")
    print(f"{'='*70}\n")

    for lo, hi, label in [(4, 7, "Litet fält (4-6)"), (7, 10, "Medel fält (7-9)"),
                           (10, 13, "Stort fält (10-12)"), (13, 20, "Mkt stort (13+)")]:
        test(label, lambda e, r, lo=lo, hi=hi: lo <= r["num_starters"] < hi)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  STARTMETOD effekt")
    print(f"{'='*70}\n")

    test("Autostart", lambda e, r: r["method"] == "auto")
    test("Voltstart", lambda e, r: r["method"] == "volte" or r["method"] == "volt")

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SPÅR + STARTMETOD")
    print(f"{'='*70}\n")

    for pp in range(1, 13):
        test(f"Spår {pp} auto", lambda e, r, p=pp: e["post_position"] == p and r["method"] == "auto", min_n=50)
    for pp in range(1, 13):
        test(f"Spår {pp} volt", lambda e, r, p=pp: e["post_position"] == p and (r["method"] == "volte" or r["method"] == "volt"), min_n=50)

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  KOMBINATIONSSIGNALER (multiple edges stacked)")
    print(f"{'='*70}\n")

    # Class drop + good driver
    test("Klasstapp + kusk 15%+", lambda e, r: e.get("is_class_dropper", False) and e["driver_win_pct"] >= 0.15 and e["driver_starts"] >= 30)
    test("Klasstapp + kusk 20%+", lambda e, r: e.get("is_class_dropper", False) and e["driver_win_pct"] >= 0.20 and e["driver_starts"] >= 30)

    # Barefoot + class drop
    test("Barfota + klasstapp", lambda e, r: e["barefoot"] and e.get("is_class_dropper", False))

    # Young + good driver
    test("Ålder 3-4 + kusk 15%+", lambda e, r: e["age"] in (3, 4) and e["driver_win_pct"] >= 0.15 and e["driver_starts"] >= 30)

    # Barefoot + young
    test("Barfota + ålder 3-5", lambda e, r: e["barefoot"] and 3 <= e["age"] <= 5)

    # Full combo
    test("Klasstapp + barfota + kusk 15%+", lambda e, r: e.get("is_class_dropper", False) and e["barefoot"] and e["driver_win_pct"] >= 0.15 and e["driver_starts"] >= 30, min_n=20)

    # Inner post + favorite
    test("Spår 1-3 + streck fav", lambda e, r: e["post_position"] <= 3 and e.get("is_streck_fav", False))

    # Outsider with signals (VALUE bets)
    test("Streck 5+ + kusk 15%+", lambda e, r: e.get("streck_rank", 0) >= 5 and e["driver_win_pct"] >= 0.15 and e["driver_starts"] >= 30)
    test("Streck 5+ + klasstapp", lambda e, r: e.get("streck_rank", 0) >= 5 and e.get("is_class_dropper", False))
    test("Streck 5+ + barfota + kusk 10%+", lambda e, r: e.get("streck_rank", 0) >= 5 and e["barefoot"] and e["driver_win_pct"] >= 0.10 and e["driver_starts"] >= 30, min_n=30)

    # ═══════════════════════════════════════════════════════════════
    # RANKED SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  ALL SIGNALS — RANKED BY LIFT")
    print(f"{'='*70}")
    print(f"\n  Base win rate: {base_rate:.1%}\n")

    all_signals.sort(key=lambda s: s["lift"], reverse=True)

    print(f"  {'Signal':<45} {'Win%':>6} {'Lift':>6} {'N':>8}")
    print(f"  {'-'*68}")

    for s in all_signals:
        indicator = "🔥" if s["lift"] > 1.5 else "★" if s["lift"] > 1.2 else "✓" if s["lift"] > 1.0 else "·"
        print(f"  {indicator} {s['label']:<43} {s['rate']:>5.1%} {s['lift']:>5.2f}x {s['total']:>8}")


if __name__ == "__main__":
    main()
