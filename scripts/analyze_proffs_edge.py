#!/usr/bin/env python3
"""Djup proffs edge-analys — jämför proffs vs publik vs modell.

Hämtar HISTORISK data från beta.kungenstrav.se/api/history/{game_id}
(inte /api/data/ som bara har system för pågående omgångar).

Analyserar:
1. Proffs #1 vs Publik #1 — vem prickar vinnaren oftare?
2. Edge (proffs_pct - publik_pct) — predicerar det vinst?
3. ROI om man spelar proffs-favoriten
4. Chansspik-potential: hästar med hög proffs-edge men låg publik-pct
5. Per speltyp breakdown
6. Modell vs proffs head-to-head
"""

import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "https://beta.kungenstrav.se/api"
CACHE_DIR = Path("proffs_history_cache")
CACHE_DIR.mkdir(exist_ok=True)


# ── Weighting (mirrors site's calcWeight) ────────────────────────────

def calc_weight(n_marks: int) -> float:
    """Weight for a system's leg based on number of marks."""
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


def compute_viktat_streck(races: list[dict], systems: list[dict]) -> list[dict]:
    """Compute weighted professional consensus for each horse in each race."""
    submitted = [s for s in systems if s.get("submitted")]
    if not submitted:
        return []

    result = []
    for race_idx, race in enumerate(races):
        leg = str(race_idx + 1)
        horses = race.get("horses", [])
        horse_numbers = [h["number"] for h in horses]

        horse_weights = {n: 0.0 for n in horse_numbers}
        total_weight = 0.0
        system_count_per_horse = {n: 0 for n in horse_numbers}

        for sys_data in submitted:
            leg_data = sys_data.get("legs", {}).get(leg)
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

        horses_data = []
        for h in horses:
            num = h["number"]
            proffs_pct = (horse_weights[num] / total_weight * 100) if total_weight > 0 else 0
            public_pct = h.get("pct", 0) / 100  # pct stored as x100
            odds = h.get("odds", 0) / 100 if h.get("odds", 0) > 100 else h.get("odds", 0)
            final_odds = h.get("finalOdds", 0) or (h.get("odds", 0) / 100 if h.get("odds", 0) > 100 else h.get("odds", 0))
            edge = proffs_pct - public_pct

            horses_data.append({
                "number": num,
                "name": h.get("name", ""),
                "driver": h.get("driver", ""),
                "odds": round(final_odds, 2),
                "public_pct": round(public_pct, 1),
                "proffs_weighted_pct": round(proffs_pct, 1),
                "proffs_count": system_count_per_horse.get(num, 0),
                "edge_pp": round(edge, 1),
                "place": h.get("place", 0),
                "final_odds": round(final_odds, 2),
                "galloped": h.get("galloped", False),
                "disqualified": h.get("disqualified", False),
            })

        result.append({
            "race_number": race_idx + 1,
            "horses": horses_data,
        })

    return result


# ── API ──────────────────────────────────────────────────────────────

async def fetch_history_list() -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE_URL}/history")
        data = r.json()
        return data.get("games", [])


async def fetch_historical_game(game_id: str) -> dict:
    """Fetch from /api/history/{game_id} — includes saved systems."""
    cache_file = CACHE_DIR / f"{game_id}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE_URL}/history/{game_id}")
        data = r.json()

    with open(cache_file, "w") as f:
        json.dump(data, f)

    return data


# ── Main ─────────────────────────────────────────────────────────────

async def main():
    print("═══ PROFFS EDGE-ANALYS — beta.kungenstrav.se (historik-API) ═══")
    print()

    # Fetch all available game IDs
    game_ids = await fetch_history_list()
    print(f"Tillgängliga omgångar: {len(game_ids)}")

    # Fetch all game data via /api/history/{game_id}
    all_races = []
    rounds_data = []
    skipped = 0
    no_systems = 0

    for i, gid in enumerate(game_ids):
        try:
            data = await fetch_historical_game(gid)
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  [{i+1}] {gid}: FETCH ERROR {e}")
            skipped += 1
            continue

        races_raw = data.get("races", [])
        systems = data.get("systems", [])
        meta = data.get("meta", {})

        if not races_raw:
            skipped += 1
            continue

        if not meta.get("hasResults"):
            skipped += 1
            continue

        submitted = [s for s in systems if s.get("submitted")]
        if not submitted:
            no_systems += 1
            continue

        gt = meta.get("gameType", gid.split("_")[0])

        # Compute viktat proffsstreck
        race_analysis = compute_viktat_streck(races_raw, systems)

        for race_idx, (raw_race, analyzed) in enumerate(zip(races_raw, race_analysis)):
            horses = analyzed["horses"]
            if not horses:
                continue

            # Find winner
            winner = None
            for h in horses:
                if h.get("place") == 1 and not h.get("disqualified") and not h.get("galloped"):
                    winner = h
                    break

            if winner is None:
                continue

            all_races.append({
                "game_type": gt,
                "game_id": gid,
                "race_number": analyzed["race_number"],
                "winner_number": winner["number"],
                "winner_name": winner["name"],
                "winner_public_pct": winner["public_pct"],
                "winner_proffs_pct": winner["proffs_weighted_pct"],
                "winner_edge": winner["edge_pp"],
                "winner_proffs_count": winner["proffs_count"],
                "winner_odds": winner["final_odds"],
                "n_horses": len(horses),
                "n_systems": len(submitted),
                "horses": horses,
            })

        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(game_ids)}] {len(all_races)} lopp, {no_systems} utan system...")

    print(f"\nTotalt: {len(all_races)} lopp med resultat + proffsdata")
    print(f"Skippad: {skipped}, Utan system: {no_systems}")
    print()

    if len(all_races) < 10:
        print("För lite data!")
        return

    total = len(all_races)

    # ═══ 1. PROFFS vs PUBLIK — vem rankar vinnaren bättre? ═══
    print("═" * 70)
    print("1. PROFFS vs PUBLIK — Vem rankar vinnaren högst?")
    print("═" * 70)
    print()

    proffs_top = defaultdict(int)
    public_top = defaultdict(int)

    for race in all_races:
        horses = race["horses"]
        winner_num = race["winner_number"]

        by_proffs = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
        by_public = sorted(horses, key=lambda h: h["public_pct"], reverse=True)

        for i, h in enumerate(by_proffs):
            if h["number"] == winner_num:
                proffs_top[i + 1] += 1
                break
        for i, h in enumerate(by_public):
            if h["number"] == winner_num:
                public_top[i + 1] += 1
                break

    print(f"{'Rank':>6} {'Proffs':>12} {'Publik':>12} {'Diff':>8}")
    print("─" * 45)
    for r in range(1, 13):
        p = proffs_top.get(r, 0)
        pub = public_top.get(r, 0)
        pp = p / total * 100
        pubp = pub / total * 100
        diff = pp - pubp
        marker = " ★" if diff > 1.5 else " ↓" if diff < -1.5 else ""
        print(f"  {r:>4} {p:>5} ({pp:>5.1f}%) {pub:>5} ({pubp:>5.1f}%) {diff:>+6.1f}%{marker}")

    print()
    for n in [1, 2, 3, 5]:
        p_cum = sum(proffs_top.get(r, 0) for r in range(1, n + 1))
        pub_cum = sum(public_top.get(r, 0) for r in range(1, n + 1))
        pp = p_cum / total * 100
        pubp = pub_cum / total * 100
        print(f"  Topp-{n}: Proffs {pp:.1f}% vs Publik {pubp:.1f}% ({pp - pubp:+.1f}%)")

    # ═══ 2. EDGE — predicerar skillnaden vinst? ═══
    print()
    print("═" * 70)
    print("2. PROFFS EDGE — Predicerar skillnaden vinst?")
    print("═" * 70)
    print()

    edge_buckets = defaultdict(lambda: {"wins": 0, "total": 0, "odds_sum": 0})

    for race in all_races:
        for h in race["horses"]:
            edge = h["edge_pp"]
            is_winner = h["number"] == race["winner_number"]
            odds = h.get("final_odds") or h.get("odds", 0) or 0

            if edge >= 30:
                bucket = "30+ pp"
            elif edge >= 20:
                bucket = "20-30 pp"
            elif edge >= 10:
                bucket = "10-20 pp"
            elif edge >= 5:
                bucket = "5-10 pp"
            elif edge >= 0:
                bucket = "0-5 pp"
            elif edge >= -5:
                bucket = "-5-0 pp"
            elif edge >= -10:
                bucket = "-10--5 pp"
            else:
                bucket = "<-10 pp"

            edge_buckets[bucket]["total"] += 1
            if is_winner:
                edge_buckets[bucket]["wins"] += 1
                edge_buckets[bucket]["odds_sum"] += odds

    print(f"{'Edge':>12} {'Vinst%':>8} {'Vinner':>8} {'Tot':>8} {'Snitt odds':>11} {'ROI (flat)':>10}")
    print("─" * 65)
    bucket_order = ["30+ pp", "20-30 pp", "10-20 pp", "5-10 pp", "0-5 pp", "-5-0 pp", "-10--5 pp", "<-10 pp"]
    for bucket in bucket_order:
        d = edge_buckets.get(bucket, {"wins": 0, "total": 0, "odds_sum": 0})
        if d["total"] == 0:
            continue
        pct = d["wins"] / d["total"] * 100
        avg_odds = d["odds_sum"] / d["wins"] if d["wins"] > 0 else 0
        roi = (d["odds_sum"] - d["total"]) / d["total"] * 100 if d["total"] > 0 else 0
        star = " ★" if roi > 0 else ""
        print(f"  {bucket:>10} {pct:>7.1f}% {d['wins']:>8} {d['total']:>8} {avg_odds:>10.2f} {roi:>9.1f}%{star}")

    # ═══ 3. ROI — proffs-favorit vs publik-favorit ═══
    print()
    print("═" * 70)
    print("3. ROI — Proffs-favorit vs Publik-favorit")
    print("═" * 70)
    print()

    proffs_fav_wins = 0
    proffs_fav_odds = 0
    public_fav_wins = 0
    public_fav_odds = 0

    for race in all_races:
        horses = race["horses"]
        by_proffs = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
        by_public = sorted(horses, key=lambda h: h["public_pct"], reverse=True)

        p_fav = by_proffs[0]
        pub_fav = by_public[0]

        if p_fav["number"] == race["winner_number"]:
            proffs_fav_wins += 1
            proffs_fav_odds += p_fav.get("final_odds") or p_fav.get("odds", 0) or 0

        if pub_fav["number"] == race["winner_number"]:
            public_fav_wins += 1
            public_fav_odds += pub_fav.get("final_odds") or pub_fav.get("odds", 0) or 0

    proffs_roi = (proffs_fav_odds - total) / total * 100
    public_roi = (public_fav_odds - total) / total * 100

    print(f"  Proffs #1: {proffs_fav_wins}/{total} vinster ({proffs_fav_wins/total*100:.1f}%), "
          f"snitt odds {proffs_fav_odds/max(1,proffs_fav_wins):.2f}, "
          f"ROI {proffs_roi:.1f}%")
    print(f"  Publik #1: {public_fav_wins}/{total} vinster ({public_fav_wins/total*100:.1f}%), "
          f"snitt odds {public_fav_odds/max(1,public_fav_wins):.2f}, "
          f"ROI {public_roi:.1f}%")

    print()
    for top_n in [2, 3, 5]:
        proffs_hits = 0
        public_hits = 0
        for race in all_races:
            horses = race["horses"]
            by_proffs = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
            by_public = sorted(horses, key=lambda h: h["public_pct"], reverse=True)
            if any(h["number"] == race["winner_number"] for h in by_proffs[:top_n]):
                proffs_hits += 1
            if any(h["number"] == race["winner_number"] for h in by_public[:top_n]):
                public_hits += 1
        print(f"  Topp-{top_n} täckning: Proffs {proffs_hits/total*100:.1f}% vs Publik {public_hits/total*100:.1f}%")

    # ═══ 4. CHANSSPIK-EDGE — hästar med hög proffs-edge + låg streck ═══
    print()
    print("═" * 70)
    print("4. CHANSSPIK-EDGE — Hästar med hög proffs-edge + låg streck")
    print("═" * 70)
    print()

    # Various edge thresholds for chansspik
    print("  Chansspik edge-trösklar (5-20% publik streck):")
    print(f"  {'Min edge':>10} {'Antal':>8} {'Vinst':>8} {'Vinst%':>8} {'Snitt odds':>11} {'ROI':>8}")
    print("  " + "─" * 60)
    for min_edge in [5, 10, 15, 20, 25, 30]:
        cnt = 0
        wins = 0
        odds_sum = 0
        for race in all_races:
            for h in race["horses"]:
                pub = h["public_pct"]
                edge = h["edge_pp"]
                if 5 <= pub <= 20 and edge >= min_edge:
                    cnt += 1
                    if h["number"] == race["winner_number"]:
                        wins += 1
                        odds_sum += h.get("final_odds") or h.get("odds", 0)
        if cnt > 0:
            pct = wins / cnt * 100
            avg_odds = odds_sum / wins if wins > 0 else 0
            roi = (odds_sum - cnt) / cnt * 100
            star = " ★" if roi > 0 else ""
            print(f"  {f'≥{min_edge}pp':>10} {cnt:>8} {wins:>8} {pct:>7.1f}% {avg_odds:>10.1f} {roi:>7.1f}%{star}")

    # Wider streck range
    print()
    print("  Chansspik edge-trösklar (3-25% publik streck):")
    print(f"  {'Min edge':>10} {'Antal':>8} {'Vinst':>8} {'Vinst%':>8} {'Snitt odds':>11} {'ROI':>8}")
    print("  " + "─" * 60)
    for min_edge in [5, 10, 15, 20]:
        cnt = 0
        wins = 0
        odds_sum = 0
        for race in all_races:
            for h in race["horses"]:
                pub = h["public_pct"]
                edge = h["edge_pp"]
                if 3 <= pub <= 25 and edge >= min_edge:
                    cnt += 1
                    if h["number"] == race["winner_number"]:
                        wins += 1
                        odds_sum += h.get("final_odds") or h.get("odds", 0)
        if cnt > 0:
            pct = wins / cnt * 100
            avg_odds = odds_sum / wins if wins > 0 else 0
            roi = (odds_sum - cnt) / cnt * 100
            star = " ★" if roi > 0 else ""
            print(f"  {f'≥{min_edge}pp':>10} {cnt:>8} {wins:>8} {pct:>7.1f}% {avg_odds:>10.1f} {roi:>7.1f}%{star}")

    # Show winning chansspik horses
    print()
    print("  Vinnande chansspik-hästar (5-20% streck, edge ≥10pp):")
    chansspik_winners = []
    for race in all_races:
        for h in race["horses"]:
            if 5 <= h["public_pct"] <= 20 and h["edge_pp"] >= 10 and h["number"] == race["winner_number"]:
                chansspik_winners.append({
                    "game": race["game_id"],
                    "race": race["race_number"],
                    "horse": h["name"],
                    "number": h["number"],
                    "public_pct": h["public_pct"],
                    "proffs_pct": h["proffs_weighted_pct"],
                    "edge": h["edge_pp"],
                    "odds": h.get("final_odds") or h.get("odds", 0),
                })

    chansspik_winners.sort(key=lambda x: x["odds"], reverse=True)
    for c in chansspik_winners[:20]:
        print(f"    {c['game']:30s} avd {c['race']}: #{c['number']:2d} {c['horse']:25s} "
              f"pub={c['public_pct']:5.1f}% proffs={c['proffs_pct']:5.1f}% "
              f"edge=+{c['edge']:.0f}pp odds={c['odds']:.1f}")

    # ═══ 5. PER SPELTYP ═══
    print()
    print("═" * 70)
    print("5. PER SPELTYP — Proffs edge")
    print("═" * 70)
    print()

    by_gt = defaultdict(list)
    for r in all_races:
        by_gt[r["game_type"]].append(r)

    for gt in sorted(by_gt.keys()):
        races = by_gt[gt]
        n = len(races)

        p1_wins = 0
        pub1_wins = 0
        p1_odds = 0
        pub1_odds = 0

        for race in races:
            horses = race["horses"]
            by_p = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
            by_pub = sorted(horses, key=lambda h: h["public_pct"], reverse=True)

            if by_p[0]["number"] == race["winner_number"]:
                p1_wins += 1
                p1_odds += by_p[0].get("final_odds") or by_p[0].get("odds", 0)
            if by_pub[0]["number"] == race["winner_number"]:
                pub1_wins += 1
                pub1_odds += by_pub[0].get("final_odds") or by_pub[0].get("odds", 0)

        p1_roi = (p1_odds - n) / n * 100
        pub1_roi = (pub1_odds - n) / n * 100

        # Chansspik in this game type
        cs_cnt = 0
        cs_wins = 0
        cs_odds = 0
        for race in races:
            for h in race["horses"]:
                if 5 <= h["public_pct"] <= 20 and h["edge_pp"] >= 10:
                    cs_cnt += 1
                    if h["number"] == race["winner_number"]:
                        cs_wins += 1
                        cs_odds += h.get("final_odds") or h.get("odds", 0)
        cs_roi = (cs_odds - cs_cnt) / cs_cnt * 100 if cs_cnt > 0 else 0

        print(f"  {gt} ({n} lopp, {races[0].get('n_systems', '?')}+ system):")
        print(f"    Proffs #1: {p1_wins/n*100:.0f}% vinst, ROI {p1_roi:+.1f}%")
        print(f"    Publik #1: {pub1_wins/n*100:.0f}% vinst, ROI {pub1_roi:+.1f}%")
        print(f"    Diff: {(p1_wins-pub1_wins)/n*100:+.1f}% (proffs bättre)")
        if cs_cnt > 0:
            print(f"    Chansspik (5-20%, edge≥10pp): {cs_wins}/{cs_cnt} = {cs_wins/cs_cnt*100:.1f}%, ROI={cs_roi:+.1f}%")
        print()

    # ═══ 6. MODELL vs PROFFS — Head-to-head ═══
    print("═" * 70)
    print("6. MODELL vs PROFFS vs PUBLIK — Head-to-head")
    print("═" * 70)
    print()

    try:
        from trav_agent.analysis.composite import CompositeAnalyzer
        from trav_agent.data.atg_client import ATGClient
        from trav_agent.config import CACHE_DIR as MODEL_CACHE
        import re
        from datetime import date

        client = ATGClient()
        analyzer = CompositeAnalyzer()

        model_top1 = 0
        model_top3 = 0
        proffs_top1_h2h = 0
        proffs_top3_h2h = 0
        public_top1_h2h = 0
        public_top3_h2h = 0
        compared_total = 0

        # Group proffs races by game_type + date for efficient model loading
        rounds_map = defaultdict(list)
        for race in all_races:
            gt = race["game_type"]
            gid = race["game_id"]
            date_str = gid.split("_")[1]
            rounds_map[(gt, date_str)].append(race)

        compared_rounds = 0
        for (gt, date_str), proffs_races in sorted(rounds_map.items()):
            # Check if we have cached model data
            pattern = re.compile(rf"^{gt}_{date_str}_\d+_\d+_[a-f0-9]+\.json$")
            found = False
            for f in MODEL_CACHE.iterdir():
                if pattern.match(f.name):
                    found = True
                    break

            if not found:
                continue

            try:
                d = date.fromisoformat(date_str)
                game_round = await client.fetch_full_round(gt, d)
                if not game_round:
                    continue
                analyzer.analyze_round(game_round)
                compared_rounds += 1

                for proffs_race in proffs_races:
                    for model_race in game_round.races:
                        if model_race.race_number != proffs_race["race_number"]:
                            continue
                        if not model_race.result_order:
                            continue

                        winner_num = proffs_race["winner_number"]
                        entries = sorted(
                            model_race.active_entries,
                            key=lambda e: e.super_score,
                            reverse=True,
                        )

                        for rank, entry in enumerate(entries, 1):
                            if entry.post_position == winner_num:
                                if rank == 1:
                                    model_top1 += 1
                                if rank <= 3:
                                    model_top3 += 1
                                compared_total += 1
                                break

                        # Also count proffs/public for same race
                        horses = proffs_race["horses"]
                        by_p = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
                        by_pub = sorted(horses, key=lambda h: h["public_pct"], reverse=True)

                        if by_p[0]["number"] == winner_num:
                            proffs_top1_h2h += 1
                        if by_pub[0]["number"] == winner_num:
                            public_top1_h2h += 1
                        if any(h["number"] == winner_num for h in by_p[:3]):
                            proffs_top3_h2h += 1
                        if any(h["number"] == winner_num for h in by_pub[:3]):
                            public_top3_h2h += 1

                        break
            except Exception as e:
                continue

            if compared_rounds % 10 == 0:
                print(f"  ... jämfört {compared_rounds} omgångar, {compared_total} lopp")

        if compared_total > 0:
            print(f"\n  Jämförda: {compared_total} lopp från {compared_rounds} omgångar")
            print()
            print(f"  {'':>15} {'#1 vinst':>12} {'Top-3 vinst':>14}")
            print(f"  {'─'*45}")
            print(f"  {'Modell':>15} {model_top1/compared_total*100:>10.1f}% {model_top3/compared_total*100:>12.1f}%")
            print(f"  {'Proffs':>15} {proffs_top1_h2h/compared_total*100:>10.1f}% {proffs_top3_h2h/compared_total*100:>12.1f}%")
            print(f"  {'Publik':>15} {public_top1_h2h/compared_total*100:>10.1f}% {public_top3_h2h/compared_total*100:>12.1f}%")
            print()

            # Combined: proffs + modell agree
            both_top3 = 0
            proffs_only = 0
            model_only = 0
            neither = 0

            # Re-run to count overlaps
            for (gt, date_str), proffs_races in sorted(rounds_map.items()):
                pattern = re.compile(rf"^{gt}_{date_str}_\d+_\d+_[a-f0-9]+\.json$")
                found = False
                for f in MODEL_CACHE.iterdir():
                    if pattern.match(f.name):
                        found = True
                        break
                if not found:
                    continue

                try:
                    d = date.fromisoformat(date_str)
                    game_round = await client.fetch_full_round(gt, d)
                    if not game_round:
                        continue
                    analyzer.analyze_round(game_round)

                    for proffs_race in proffs_races:
                        for model_race in game_round.races:
                            if model_race.race_number != proffs_race["race_number"]:
                                continue
                            if not model_race.result_order:
                                continue

                            winner_num = proffs_race["winner_number"]
                            entries = sorted(
                                model_race.active_entries,
                                key=lambda e: e.super_score,
                                reverse=True,
                            )

                            model_in_top3 = any(
                                e.post_position == winner_num
                                for e in entries[:3]
                            )
                            horses = proffs_race["horses"]
                            by_p = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
                            proffs_in_top3 = any(h["number"] == winner_num for h in by_p[:3])

                            if model_in_top3 and proffs_in_top3:
                                both_top3 += 1
                            elif proffs_in_top3:
                                proffs_only += 1
                            elif model_in_top3:
                                model_only += 1
                            else:
                                neither += 1

                            break
                except Exception:
                    continue

            overlap_total = both_top3 + proffs_only + model_only + neither
            if overlap_total > 0:
                print(f"  Överlappsanalys (topp-3):")
                print(f"    Båda top-3: {both_top3}/{overlap_total} ({both_top3/overlap_total*100:.1f}%)")
                print(f"    Bara proffs top-3: {proffs_only}/{overlap_total} ({proffs_only/overlap_total*100:.1f}%)")
                print(f"    Bara modell top-3: {model_only}/{overlap_total} ({model_only/overlap_total*100:.1f}%)")
                print(f"    Ingen top-3: {neither}/{overlap_total} ({neither/overlap_total*100:.1f}%)")
                combined = both_top3 + proffs_only + model_only
                print(f"    Kombinerad täckning: {combined}/{overlap_total} ({combined/overlap_total*100:.1f}%)")

        else:
            print("  (Kunde inte jämföra — ingen modelldata i cachen för dessa omgångar)")

    except Exception as e:
        print(f"  Modell-jämförelse misslyckades: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
