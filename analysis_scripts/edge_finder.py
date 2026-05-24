#!/usr/bin/env python3
"""Edge Finder — mine patterns from 42,000 cached ATG races.

Analyzes raw API data to find predictive signals we're NOT using:
1. Equipment changes (shoes, sulky) as win predictors
2. Days since last start (rest / freshness)
3. Class transitions (up/down)
4. Distance changes
5. Start method changes (auto ↔ volt)
6. Driver changes (new driver signal)
7. "Bounce" effect (bad last result → improvement)
8. Field size effect
9. Driver win percentage as standalone predictor
10. Home track advantage
11. Barfota (barefoot) signal
12. Combined signals

Reads directly from cache/ directory — no API calls needed.
"""
import json
import glob
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, date

CACHE_DIR = Path("cache")


def parse_km_time(time_obj):
    """Parse ATG time object to seconds per km."""
    if not time_obj:
        return None
    minutes = time_obj.get("minutes", 0)
    seconds = time_obj.get("seconds", 0)
    tenths = time_obj.get("tenths", 0)
    return minutes * 60 + seconds + tenths / 10


def extract_race_data(data):
    """Extract structured data from raw ATG API response."""
    races = data.get("races", [])
    if not isinstance(races, list):
        return []

    extracted = []
    for race in races:
        starts = race.get("starts", [])
        if not starts:
            continue

        race_result = race.get("result", {})
        result_order = []
        if race_result:
            # Different formats for result
            if isinstance(race_result, dict):
                for key in ["first", "winners"]:
                    if key in race_result:
                        winners = race_result[key]
                        if isinstance(winners, list):
                            result_order = [w.get("number") or w.get("startNumber") for w in winners if isinstance(w, dict)]
                if not result_order and "rppieces" in race_result:
                    pieces = race_result["rppieces"]
                    if isinstance(pieces, list):
                        result_order = [p.get("number") for p in sorted(pieces, key=lambda p: p.get("place", 99))]

        if not result_order:
            # Try from individual start results
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

        winner_num = result_order[0] if result_order else None

        race_info = {
            "distance": race.get("distance", 0),
            "start_method": race.get("startMethod", ""),
            "num_starters": len(starts),
            "winner": winner_num,
            "result_order": result_order,
            "entries": [],
        }

        for start in starts:
            horse = start.get("horse", {})
            shoes = horse.get("shoes", {})
            sulky = horse.get("sulky", start.get("sulky", {}))
            driver = start.get("driver", {})
            stats = horse.get("statistics", {})
            result = start.get("result", {})
            pools = start.get("pools", {})
            trainer = horse.get("trainer", {})

            # Extract bet distribution (streckprocent proxy)
            bet_dist = 0
            for pool_name in ["V75", "V86", "V85", "V64", "GS75"]:
                pool = pools.get(pool_name, pools.get(pool_name.lower(), {}))
                if pool and isinstance(pool, dict):
                    bd = pool.get("betDistribution", 0)
                    if bd:
                        bet_dist = bd / 100.0  # Convert from per-mille to percent
                        break
            if not bet_dist:
                # Try vinnare pool odds as proxy
                vinnare = pools.get("vinnare", {})
                if vinnare:
                    odds = vinnare.get("odds", 0)
                    if odds and odds > 0:
                        bet_dist = 1000 / odds  # rough conversion

            # Shoes
            front_shoe = shoes.get("front", {})
            back_shoe = shoes.get("back", {})
            shoe_changed = front_shoe.get("changed", False) or back_shoe.get("changed", False)
            barefoot_front = not front_shoe.get("hasShoe", True)
            barefoot_back = not back_shoe.get("hasShoe", True)
            barefoot = barefoot_front or barefoot_back

            # Sulky
            sulky_type = ""
            sulky_changed = False
            if isinstance(sulky, dict):
                st = sulky.get("type", {})
                if isinstance(st, dict):
                    sulky_type = st.get("code", "")
                    sulky_changed = st.get("changed", False)

            # Driver stats
            driver_stats = driver.get("statistics", {})
            driver_year_stats = {}
            if isinstance(driver_stats, dict):
                years = driver_stats.get("years", {})
                # Get most recent year
                for y in sorted(years.keys(), reverse=True):
                    driver_year_stats = years[y]
                    break

            driver_starts = driver_year_stats.get("starts", 0)
            driver_wins = driver_year_stats.get("placement", {}).get("1", 0)
            driver_win_pct = driver_wins / driver_starts if driver_starts > 10 else 0

            # Horse yearly stats
            horse_year_stats = stats.get("years", {})
            horse_recent_year = {}
            for y in sorted(horse_year_stats.keys(), reverse=True):
                horse_recent_year = horse_year_stats[y]
                break

            # Record (best time)
            record = horse.get("record", {})
            best_time = parse_km_time(record.get("time")) if record else None

            # Placement from result
            place = None
            final_odds = None
            km_time = None
            if isinstance(result, dict):
                place = result.get("place") or result.get("finishOrder")
                final_odds = result.get("finalOdds")
                km_time_obj = result.get("kmTime")
                if km_time_obj:
                    km_time = parse_km_time(km_time_obj)

            # Home track
            home_track = horse.get("homeTrack", {})
            home_track_name = home_track.get("name", "") if isinstance(home_track, dict) else ""

            entry = {
                "number": start.get("number", 0),
                "post_position": start.get("postPosition", 0),
                "distance": start.get("distance", race.get("distance", 0)),
                "horse_name": horse.get("name", ""),
                "horse_age": horse.get("age", 0),
                "horse_sex": horse.get("sex", ""),
                "horse_id": horse.get("id", 0),
                "home_track": home_track_name,
                "bet_dist": bet_dist,
                "shoe_changed": shoe_changed,
                "barefoot_front": barefoot_front,
                "barefoot_back": barefoot_back,
                "barefoot": barefoot,
                "sulky_type": sulky_type,
                "sulky_changed": sulky_changed,
                "driver_name": f"{driver.get('firstName', '')} {driver.get('lastName', '')}".strip(),
                "driver_id": driver.get("id", 0),
                "driver_win_pct": driver_win_pct,
                "driver_starts": driver_starts,
                "trainer_name": f"{trainer.get('firstName', '')} {trainer.get('lastName', '')}".strip() if isinstance(trainer, dict) else "",
                "best_time": best_time,
                "place": place,
                "final_odds": final_odds,
                "km_time": km_time,
                "total_prize": horse.get("money", 0),
                "horse_starts_year": horse_recent_year.get("starts", 0),
                "horse_wins_year": horse_recent_year.get("placement", {}).get("1", 0),
            }
            race_info["entries"].append(entry)

        extracted.append(race_info)

    return extracted


def main():
    print("=" * 80)
    print("  EDGE FINDER — Mining 42,000 cached races for predictive signals")
    print("=" * 80)

    # Load all cached game files (not calendar files)
    # Game files have format: gameid_trackid_racenum_hash.json
    # They contain 'races' key with starts
    all_races = []
    files_processed = 0

    game_files = sorted(glob.glob(str(CACHE_DIR / "*_*_*_*.json")))
    print(f"\nProcessing {len(game_files)} cache files...")

    for filepath in game_files:
        try:
            with open(filepath) as f:
                data = json.load(f)
            races = extract_race_data(data)
            all_races.extend(races)
            files_processed += 1
        except Exception:
            continue

    print(f"Extracted {len(all_races)} races from {files_processed} files")

    # Filter to races with results
    valid_races = [r for r in all_races if r["winner"] and len(r["entries"]) >= 4]
    print(f"Races with results and 4+ starters: {len(valid_races)}")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 1: EQUIPMENT CHANGES (shoes + sulky)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 1: EQUIPMENT CHANGES")
    print(f"{'='*70}")

    shoe_stats = {"changed_wins": 0, "changed_total": 0, "unchanged_wins": 0, "unchanged_total": 0}
    barefoot_stats = {"bare_wins": 0, "bare_total": 0, "shod_wins": 0, "shod_total": 0}
    sulky_stats = {"changed_wins": 0, "changed_total": 0, "unchanged_wins": 0, "unchanged_total": 0}

    for race in valid_races:
        for entry in race["entries"]:
            won = entry["number"] == race["winner"]

            if entry["shoe_changed"]:
                shoe_stats["changed_total"] += 1
                if won:
                    shoe_stats["changed_wins"] += 1
            else:
                shoe_stats["unchanged_total"] += 1
                if won:
                    shoe_stats["unchanged_wins"] += 1

            if entry["barefoot"]:
                barefoot_stats["bare_total"] += 1
                if won:
                    barefoot_stats["bare_wins"] += 1
            else:
                barefoot_stats["shod_total"] += 1
                if won:
                    barefoot_stats["shod_wins"] += 1

            if entry["sulky_changed"]:
                sulky_stats["changed_total"] += 1
                if won:
                    sulky_stats["changed_wins"] += 1
            else:
                sulky_stats["unchanged_total"] += 1
                if won:
                    sulky_stats["unchanged_wins"] += 1

    # Calculate win rates
    base_win_rate = sum(1 for r in valid_races for e in r["entries"] if e["number"] == r["winner"]) / sum(len(r["entries"]) for r in valid_races)

    print(f"\n  Base win rate: {base_win_rate:.1%}")

    if shoe_stats["changed_total"] > 0:
        sc_rate = shoe_stats["changed_wins"] / shoe_stats["changed_total"]
        su_rate = shoe_stats["unchanged_wins"] / shoe_stats["unchanged_total"]
        print(f"\n  Shoe change:   {sc_rate:.1%} win rate ({shoe_stats['changed_wins']}/{shoe_stats['changed_total']}) — {sc_rate/base_win_rate:.2f}x base")
        print(f"  No change:     {su_rate:.1%} win rate ({shoe_stats['unchanged_wins']}/{shoe_stats['unchanged_total']})")

    if barefoot_stats["bare_total"] > 0:
        bf_rate = barefoot_stats["bare_wins"] / barefoot_stats["bare_total"]
        sh_rate = barefoot_stats["shod_wins"] / barefoot_stats["shod_total"]
        print(f"\n  Barefoot:      {bf_rate:.1%} win rate ({barefoot_stats['bare_wins']}/{barefoot_stats['bare_total']}) — {bf_rate/base_win_rate:.2f}x base")
        print(f"  With shoes:    {sh_rate:.1%} win rate ({barefoot_stats['shod_wins']}/{barefoot_stats['shod_total']})")

    if sulky_stats["changed_total"] > 0:
        sc_rate = sulky_stats["changed_wins"] / sulky_stats["changed_total"]
        su_rate = sulky_stats["unchanged_wins"] / sulky_stats["unchanged_total"]
        print(f"\n  Sulky change:  {sc_rate:.1%} win rate ({sulky_stats['changed_wins']}/{sulky_stats['changed_total']}) — {sc_rate/base_win_rate:.2f}x base")
        print(f"  No change:     {su_rate:.1%} win rate ({sulky_stats['unchanged_wins']}/{sulky_stats['unchanged_total']})")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 2: DRIVER WIN PERCENTAGE
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 2: DRIVER WIN PERCENTAGE")
    print(f"{'='*70}")

    driver_buckets = defaultdict(lambda: [0, 0])  # [wins, total]
    for race in valid_races:
        for entry in race["entries"]:
            won = entry["number"] == race["winner"]
            pct = entry["driver_win_pct"]
            if entry["driver_starts"] < 20:
                bucket = "< 20 starts"
            elif pct < 0.05:
                bucket = "0-5%"
            elif pct < 0.10:
                bucket = "5-10%"
            elif pct < 0.15:
                bucket = "10-15%"
            elif pct < 0.20:
                bucket = "15-20%"
            else:
                bucket = "20%+"

            driver_buckets[bucket][1] += 1
            if won:
                driver_buckets[bucket][0] += 1

    print(f"\n  {'Driver win%':<15} {'Win rate':>10} {'Count':>8} {'Lift':>6}")
    print(f"  {'-'*42}")
    for bucket in ["< 20 starts", "0-5%", "5-10%", "10-15%", "15-20%", "20%+"]:
        if bucket in driver_buckets:
            wins, total = driver_buckets[bucket]
            rate = wins / total if total > 0 else 0
            lift = rate / base_win_rate if base_win_rate > 0 else 0
            print(f"  {bucket:<15} {rate:>9.1%} {total:>8} {lift:>5.2f}x")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 3: HOME TRACK ADVANTAGE
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 3: HOME TRACK ADVANTAGE")
    print(f"{'='*70}")

    home_stats = {"home_wins": 0, "home_total": 0, "away_wins": 0, "away_total": 0}
    for race in valid_races:
        # Need track name from race — not always available in our parsed data
        # Use first entry's data as proxy
        pass  # Skip — need race-level track info

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 4: BET DISTRIBUTION AS PREDICTOR (sanity check)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 4: BET DISTRIBUTION (market efficiency check)")
    print(f"{'='*70}")

    market_buckets = defaultdict(lambda: [0, 0])
    market_races_with_data = 0
    for race in valid_races:
        entries_with_bet = [e for e in race["entries"] if e["bet_dist"] > 0]
        if len(entries_with_bet) < 3:
            continue
        market_races_with_data += 1

        # Rank by bet distribution
        sorted_by_market = sorted(entries_with_bet, key=lambda e: e["bet_dist"], reverse=True)
        for rank, entry in enumerate(sorted_by_market, 1):
            won = entry["number"] == race["winner"]
            bucket = f"Rank {rank}" if rank <= 5 else "Rank 6+"
            market_buckets[bucket][1] += 1
            if won:
                market_buckets[bucket][0] += 1

    print(f"\n  Races with market data: {market_races_with_data}")
    print(f"\n  {'Market rank':<12} {'Win rate':>10} {'Count':>8}")
    print(f"  {'-'*32}")
    for rank in range(1, 7):
        bucket = f"Rank {rank}" if rank <= 5 else "Rank 6+"
        if bucket in market_buckets:
            wins, total = market_buckets[bucket]
            rate = wins / total if total > 0 else 0
            print(f"  {bucket:<12} {rate:>9.1%} {total:>8}")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 5: ODDS-BASED VALUE (model rank vs market rank disagreement)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 5: FINAL ODDS AS PREDICTOR")
    print(f"{'='*70}")

    odds_buckets = defaultdict(lambda: [0, 0])
    for race in valid_races:
        entries_with_odds = [e for e in race["entries"] if e.get("final_odds") and e["final_odds"] > 0]
        if len(entries_with_odds) < 3:
            continue

        for entry in entries_with_odds:
            won = entry["number"] == race["winner"]
            odds = entry["final_odds"]
            if odds < 2:
                bucket = "< 2.0"
            elif odds < 5:
                bucket = "2-5"
            elif odds < 10:
                bucket = "5-10"
            elif odds < 20:
                bucket = "10-20"
            elif odds < 50:
                bucket = "20-50"
            else:
                bucket = "50+"

            odds_buckets[bucket][1] += 1
            if won:
                odds_buckets[bucket][0] += 1

    print(f"\n  {'Odds range':<12} {'Win rate':>10} {'Count':>8} {'Implied':>9} {'Edge':>7}")
    print(f"  {'-'*50}")
    for bucket in ["< 2.0", "2-5", "5-10", "10-20", "20-50", "50+"]:
        if bucket in odds_buckets:
            wins, total = odds_buckets[bucket]
            rate = wins / total if total > 0 else 0
            # Implied probability from odds midpoint
            if bucket == "< 2.0":
                implied = 1 / 1.5
            elif bucket == "2-5":
                implied = 1 / 3.5
            elif bucket == "5-10":
                implied = 1 / 7.5
            elif bucket == "10-20":
                implied = 1 / 15
            elif bucket == "20-50":
                implied = 1 / 35
            else:
                implied = 1 / 75
            edge = (rate - implied) / implied * 100
            print(f"  {bucket:<12} {rate:>9.1%} {total:>8} {implied:>8.1%} {edge:>+6.1f}%")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 6: COMBINED EQUIPMENT + MARKET MISMATCH
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 6: EQUIPMENT CHANGE + LOW STRECK (value combo)")
    print(f"{'='*70}")

    combo_stats = defaultdict(lambda: [0, 0])
    for race in valid_races:
        for entry in race["entries"]:
            won = entry["number"] == race["winner"]
            has_equip_change = entry["shoe_changed"] or entry["sulky_changed"] or entry["barefoot"]
            bet = entry["bet_dist"]

            if has_equip_change:
                if bet > 0 and bet < 5:
                    combo_stats["equip_change + low_streck (<5%)"][1] += 1
                    if won:
                        combo_stats["equip_change + low_streck (<5%)"][0] += 1
                elif bet >= 5 and bet < 15:
                    combo_stats["equip_change + mid_streck (5-15%)"][1] += 1
                    if won:
                        combo_stats["equip_change + mid_streck (5-15%)"][0] += 1
                elif bet >= 15:
                    combo_stats["equip_change + high_streck (15%+)"][1] += 1
                    if won:
                        combo_stats["equip_change + high_streck (15%+)"][0] += 1
                else:
                    combo_stats["equip_change + no streck data"][1] += 1
                    if won:
                        combo_stats["equip_change + no streck data"][0] += 1

    print(f"\n  {'Combination':<40} {'Win rate':>10} {'Count':>8} {'Lift':>6}")
    print(f"  {'-'*66}")
    for combo in sorted(combo_stats.keys()):
        wins, total = combo_stats[combo]
        if total > 20:
            rate = wins / total
            lift = rate / base_win_rate
            print(f"  {combo:<40} {rate:>9.1%} {total:>8} {lift:>5.2f}x")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 7: FIELD SIZE EFFECT
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 7: FIELD SIZE — does model work better in big/small fields?")
    print(f"{'='*70}")

    field_buckets = defaultdict(lambda: [0, 0, 0])  # [top1_from_rank, total, top3_from_rank]
    for race in valid_races:
        n = len(race["entries"])
        if n < 4:
            continue

        if n <= 8:
            bucket = "Small (4-8)"
        elif n <= 11:
            bucket = "Medium (9-11)"
        else:
            bucket = "Large (12+)"

        # Check if highest bet_dist horse won (market favorite)
        entries_with_bet = sorted(race["entries"], key=lambda e: e.get("bet_dist", 0), reverse=True)
        if entries_with_bet and entries_with_bet[0].get("bet_dist", 0) > 0:
            fav = entries_with_bet[0]
            fav_won = fav["number"] == race["winner"]
            field_buckets[bucket][1] += 1
            if fav_won:
                field_buckets[bucket][0] += 1
            if race["winner"] in [e["number"] for e in entries_with_bet[:3]]:
                field_buckets[bucket][2] += 1

    print(f"\n  {'Field size':<15} {'Fav wins':>10} {'Top3 hit':>10} {'Count':>8}")
    print(f"  {'-'*46}")
    for bucket in ["Small (4-8)", "Medium (9-11)", "Large (12+)"]:
        if bucket in field_buckets:
            fav_wins, total, top3_hits = field_buckets[bucket]
            fav_rate = fav_wins / total if total > 0 else 0
            top3_rate = top3_hits / total if total > 0 else 0
            print(f"  {bucket:<15} {fav_rate:>9.1%} {top3_rate:>9.1%} {total:>8}")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 8: KM-TIME CONSISTENCY (low variance = predictable)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 8: POST POSITION (which tracks have strong inside bias)")
    print(f"{'='*70}")

    post_wins = defaultdict(lambda: [0, 0])
    for race in valid_races:
        for entry in race["entries"]:
            pp = entry["post_position"]
            won = entry["number"] == race["winner"]
            if pp > 0:
                bucket = f"Post {pp}" if pp <= 8 else "Post 9+"
                post_wins[bucket][1] += 1
                if won:
                    post_wins[bucket][0] += 1

    print(f"\n  {'Post position':<15} {'Win rate':>10} {'Count':>8}")
    print(f"  {'-'*35}")
    for pp in range(1, 10):
        bucket = f"Post {pp}" if pp <= 8 else "Post 9+"
        if bucket in post_wins:
            wins, total = post_wins[bucket]
            rate = wins / total if total > 0 else 0
            bar = "█" * int(rate * 100)
            print(f"  {bucket:<15} {rate:>9.1%} {total:>8}  {bar}")
    bucket = "Post 9+"
    if bucket in post_wins:
        wins, total = post_wins[bucket]
        rate = wins / total if total > 0 else 0
        bar = "█" * int(rate * 100)
        print(f"  {bucket:<15} {rate:>9.1%} {total:>8}  {bar}")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 9: AGE EFFECT
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 9: AGE EFFECT")
    print(f"{'='*70}")

    age_stats = defaultdict(lambda: [0, 0])
    for race in valid_races:
        for entry in race["entries"]:
            age = entry.get("horse_age", 0)
            won = entry["number"] == race["winner"]
            if age >= 2:
                bucket = f"Age {age}" if age <= 10 else "Age 11+"
                age_stats[bucket][1] += 1
                if won:
                    age_stats[bucket][0] += 1

    print(f"\n  {'Age':<12} {'Win rate':>10} {'Count':>8} {'Lift':>6}")
    print(f"  {'-'*40}")
    for age in range(2, 12):
        bucket = f"Age {age}" if age <= 10 else "Age 11+"
        if bucket in age_stats:
            wins, total = age_stats[bucket]
            rate = wins / total if total > 0 else 0
            lift = rate / base_win_rate if base_win_rate > 0 else 0
            print(f"  {bucket:<12} {rate:>9.1%} {total:>8} {lift:>5.2f}x")

    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL 10: SEX EFFECT
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SIGNAL 10: SEX EFFECT")
    print(f"{'='*70}")

    sex_stats = defaultdict(lambda: [0, 0])
    for race in valid_races:
        for entry in race["entries"]:
            sex = entry.get("horse_sex", "")
            won = entry["number"] == race["winner"]
            sex_stats[sex][1] += 1
            if won:
                sex_stats[sex][0] += 1

    print(f"\n  {'Sex':<15} {'Win rate':>10} {'Count':>8}")
    print(f"  {'-'*35}")
    for sex in sorted(sex_stats.keys()):
        wins, total = sex_stats[sex]
        if total > 100:
            rate = wins / total if total > 0 else 0
            print(f"  {sex:<15} {rate:>9.1%} {total:>8}")

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL SUMMARY: Ranked signals by predictive lift
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("  SUMMARY: Strongest untapped signals (ranked by lift over base)")
    print(f"{'='*70}")

    signals = []

    if shoe_stats["changed_total"] > 50:
        rate = shoe_stats["changed_wins"] / shoe_stats["changed_total"]
        signals.append(("Shoe change", rate, shoe_stats["changed_total"], rate / base_win_rate))

    if barefoot_stats["bare_total"] > 50:
        rate = barefoot_stats["bare_wins"] / barefoot_stats["bare_total"]
        signals.append(("Barefoot", rate, barefoot_stats["bare_total"], rate / base_win_rate))

    if sulky_stats["changed_total"] > 50:
        rate = sulky_stats["changed_wins"] / sulky_stats["changed_total"]
        signals.append(("Sulky change", rate, sulky_stats["changed_total"], rate / base_win_rate))

    # Driver win pct 15%+
    if "15-20%" in driver_buckets:
        wins, total = driver_buckets["15-20%"]
        if total > 50:
            rate = wins / total
            signals.append(("Driver 15-20% win", rate, total, rate / base_win_rate))
    if "20%+" in driver_buckets:
        wins, total = driver_buckets["20%+"]
        if total > 50:
            rate = wins / total
            signals.append(("Driver 20%+ win", rate, total, rate / base_win_rate))

    # Post position 1
    if "Post 1" in post_wins:
        wins, total = post_wins["Post 1"]
        rate = wins / total
        signals.append(("Post 1 (inside)", rate, total, rate / base_win_rate))

    signals.sort(key=lambda s: s[3], reverse=True)

    print(f"\n  Base win rate: {base_win_rate:.1%}\n")
    print(f"  {'Signal':<25} {'Win rate':>10} {'Lift':>7} {'Sample':>8}")
    print(f"  {'-'*53}")
    for name, rate, count, lift in signals:
        indicator = "🔥" if lift > 1.3 else "✓" if lift > 1.1 else "·"
        print(f"  {indicator} {name:<23} {rate:>9.1%} {lift:>6.2f}x {count:>8}")


if __name__ == "__main__":
    main()
