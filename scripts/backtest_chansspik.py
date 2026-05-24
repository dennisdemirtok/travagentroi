#!/usr/bin/env python3
"""Chansspik backtest — validera upset-targeting strategi med riktig modelldata.

Kör chansspik + spike_easiest på alla cachade omgångar och jämför:
- Träffprocent (alla rätt)
- Utdelningsstorlek per träff
- Target zone hits (25-100k för V85/V86/GS75, 5-20k för V64)
- ROI per strategi

Usage:
    python scripts/backtest_chansspik.py [--game-type V86] [--limit 50]
"""

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import build_system
from trav_agent.analysis.upset_system import (
    PAYOUT_TARGETS, build_upset_system, analyze_upset_round,
)
from trav_agent.data.atg_client import ATGClient


from trav_agent.config import CACHE_DIR

PAYOUT_TARGETS_EXTENDED = {
    "V64": (5_000, 20_000),
    "V65": (5_000, 20_000),
    "V75": (25_000, 100_000),
    "V85": (25_000, 100_000),
    "V86": (25_000, 100_000),
    "GS75": (25_000, 100_000),
}


def discover_cached_rounds(game_type: str, limit: int = 0) -> list[str]:
    """Find cached game rounds from cache/."""
    import re
    pattern = re.compile(
        rf"^{game_type.upper()}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$"
    )
    dates = set()
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            m = pattern.match(f.name)
            if m:
                dates.add(m.group(1))

    result = sorted(dates)
    if limit > 0:
        result = result[-limit:]  # Take most recent
    return result


def check_system_hit(game_round, system_picks: dict[int, list[int]]) -> dict:
    """Check if a system hit all winners.

    Args:
        game_round: Analyzed game round with results
        system_picks: {race_number: [list of picked post positions]}

    Returns:
        dict with hit info, correct count, etc.
    """
    correct = 0
    total = 0
    race_results = []

    for race in game_round.races:
        rn = race.race_number
        picks = system_picks.get(rn, [])
        total += 1

        # Find winner from result_order (list of horse numbers in finish order)
        # Or from entries with known results
        winner = None
        winner_streck = 0

        if race.result_order:
            winner_number = race.result_order[0]
            # Find this horse's post_position
            for entry in race.active_entries:
                if entry.post_position == winner_number:
                    winner = winner_number
                    winner_streck = (entry.bet_percentage or 0) * 100
                    break
            if winner is None:
                # Try matching by number
                winner = winner_number
        else:
            # Fallback: check horse.recent_starts for this race date
            for entry in race.active_entries:
                recent = entry.horse.recent_starts(1)
                if recent and recent[0].won and str(recent[0].date) == str(race.race_date):
                    winner = entry.post_position
                    winner_streck = (entry.bet_percentage or 0) * 100
                    break

        if winner is None:
            race_results.append({
                "rn": rn, "picks": picks, "winner": None,
                "correct": None, "winner_streck": 0,
            })
            continue

        is_correct = winner in picks
        if is_correct:
            correct += 1

        race_results.append({
            "rn": rn, "picks": picks, "winner": winner,
            "correct": is_correct, "winner_streck": winner_streck,
        })

    # Count races with results
    finished_races = [r for r in race_results if r["winner"] is not None]
    n_finished = len(finished_races)

    hit = correct == n_finished and n_finished == total and n_finished > 0

    return {
        "hit": hit,
        "correct": correct,
        "total": total,
        "n_finished": n_finished,
        "race_results": race_results,
    }


def get_payout(game_round) -> float:
    """Extract payout from game round results (in kr)."""
    gt = game_round.game_type
    rd = str(game_round.round_date)

    # Search for game-level cache file
    prefix = f"{gt}_{rd}"
    for f in CACHE_DIR.glob(f"{prefix}_*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)

            # Navigate to pools -> V86 -> result -> payouts
            pools = data.get("pools", {})
            game_pool = pools.get(gt, {})
            result = game_pool.get("result", {})
            payouts = result.get("payouts", {})

            # Get the main payout (key varies: "7", "8", "6", etc.)
            n_races = len(game_round.races)
            main_key = str(n_races)
            if main_key in payouts:
                payout_ore = payouts[main_key].get("payout", 0)
                return payout_ore / 100  # Convert öre to kr
        except Exception:
            continue

    return 0.0


async def run_backtest(game_type: str, limit: int = 0, budgets: list[int] = None):
    """Run full chansspik backtest."""
    if budgets is None:
        budgets = [300, 500]

    dates = discover_cached_rounds(game_type, limit)
    if not dates:
        print(f"Inga cachade {game_type}-omgångar hittade i {CACHE_DIR}")
        return

    print(f"═══ Chansspik Backtest: {game_type} ═══")
    print(f"Omgångar: {len(dates)} ({dates[0]} → {dates[-1]})")
    print(f"Budgetar: {budgets}")
    print()

    client = ATGClient()
    analyzer = CompositeAnalyzer()

    # Results per strategy
    strategies = {}
    for budget in budgets:
        strategies[f"chansspik_{budget}"] = {
            "budget": budget, "strategy": "chansspik",
            "rounds": [], "hits": 0, "total": 0,
            "target_hits": 0, "total_payout": 0, "total_cost": 0,
        }
        strategies[f"spike_easiest_{budget}"] = {
            "budget": budget, "strategy": "spike_easiest_rest4",
            "rounds": [], "hits": 0, "total": 0,
            "target_hits": 0, "total_payout": 0, "total_cost": 0,
        }

    target_lo, target_hi = PAYOUT_TARGETS_EXTENDED.get(game_type, (25_000, 100_000))

    for i, day_str in enumerate(dates):
        from datetime import date
        d = date.fromisoformat(day_str)

        # Fetch and analyze
        try:
            game_round = await client.fetch_full_round(game_type, d)
            if not game_round:
                continue

            analyzer.analyze_round(game_round)
        except Exception as e:
            print(f"  [{i+1}/{len(dates)}] {day_str} — SKIP (fetch error)")
            continue

        # Check if round is finished (has results)
        has_results = all(
            bool(race.result_order)
            for race in game_round.races
        )
        if not has_results:
            # Try to refresh results
            try:
                await client.refresh_results(game_round)
                has_results = all(
                    bool(race.result_order)
                    for race in game_round.races
                )
            except Exception:
                pass

        if not has_results:
            print(f"  [{i+1}/{len(dates)}] {day_str} — SKIP (inga resultat)")
            continue

        # Get payout
        payout = get_payout(game_round)

        # Count actual upsets in this round
        actual_upsets = 0
        for race in game_round.races:
            if race.result_order:
                winner_num = race.result_order[0]
                for entry in race.active_entries:
                    if entry.post_position == winner_num:
                        if (entry.bet_percentage or 0) < 0.10:
                            actual_upsets += 1
                        break

        # Run both strategies
        for budget in budgets:
            # Chansspik
            try:
                cs_plan = build_upset_system(game_round, budget=float(budget))
                cs_picks = {leg.race_number: leg.picks for leg in cs_plan.legs}
                cs_result = check_system_hit(game_round, cs_picks)

                key = f"chansspik_{budget}"
                strategies[key]["total"] += 1
                strategies[key]["total_cost"] += cs_plan.total_cost

                if cs_result["hit"]:
                    strategies[key]["hits"] += 1
                    strategies[key]["total_payout"] += payout
                    if target_lo <= payout <= target_hi:
                        strategies[key]["target_hits"] += 1

                strategies[key]["rounds"].append({
                    "date": day_str, "hit": cs_result["hit"],
                    "correct": cs_result["correct"], "total": cs_result["total"],
                    "rows": cs_plan.total_rows, "cost": cs_plan.total_cost,
                    "payout": payout if cs_result["hit"] else 0,
                    "actual_upsets": actual_upsets,
                })
            except Exception as e:
                print(f"    chansspik_{budget}: ERROR {e}")

            # Spike easiest
            try:
                se_plan = build_system(
                    game_round, strategy="spike_easiest_rest4",
                    budget=float(budget),
                )
                se_picks = {leg.race_number: leg.picks for leg in se_plan.legs}
                se_result = check_system_hit(game_round, se_picks)

                key = f"spike_easiest_{budget}"
                strategies[key]["total"] += 1
                strategies[key]["total_cost"] += se_plan.total_cost

                if se_result["hit"]:
                    strategies[key]["hits"] += 1
                    strategies[key]["total_payout"] += payout
                    if target_lo <= payout <= target_hi:
                        strategies[key]["target_hits"] += 1

                strategies[key]["rounds"].append({
                    "date": day_str, "hit": se_result["hit"],
                    "correct": se_result["correct"], "total": se_result["total"],
                    "rows": se_plan.total_rows, "cost": se_plan.total_cost,
                    "payout": payout if se_result["hit"] else 0,
                    "actual_upsets": actual_upsets,
                })
            except Exception as e:
                print(f"    spike_easiest_{budget}: ERROR {e}")

        # Progress line
        cs_hit = "✓" if any(
            strategies[f"chansspik_{b}"]["rounds"][-1]["hit"]
            for b in budgets
            if strategies[f"chansspik_{b}"]["rounds"]
        ) else "✗"
        se_hit = "✓" if any(
            strategies[f"spike_easiest_{b}"]["rounds"][-1]["hit"]
            for b in budgets
            if strategies[f"spike_easiest_{b}"]["rounds"]
        ) else "✗"

        payout_str = f"{payout:,.0f}kr" if payout else "?"
        print(f"  [{i+1}/{len(dates)}] {day_str}  upsets={actual_upsets}  "
              f"payout={payout_str}  CS={cs_hit}  SE={se_hit}")

    # ═══ RESULTS ═══
    print()
    print("═" * 70)
    print(f"RESULTAT — {game_type} backtest ({len(dates)} omgångar)")
    print("═" * 70)
    print()

    print(f"{'Strategi':<25} {'Omg':>5} {'Hits':>5} {'Hit%':>7} "
          f"{'Target':>7} {'Kostnad':>10} {'Vinst':>12} {'ROI':>8}")
    print("─" * 80)

    for key, s in sorted(strategies.items()):
        n = s["total"]
        if n == 0:
            continue
        hit_pct = s["hits"] / n * 100
        cost = s["total_cost"]
        payout = s["total_payout"]
        roi = (payout - cost) / cost * 100 if cost > 0 else 0

        print(f"  {key:<23} {n:>5} {s['hits']:>5} {hit_pct:>6.1f}% "
              f"{s['target_hits']:>7} {cost:>9,.0f}kr {payout:>11,.0f}kr {roi:>7.1f}%")

    print()
    print(f"Målzon: {target_lo:,.0f} – {target_hi:,.0f} kr")
    print()

    # Detailed hit analysis
    print("── Träffar per strategi ──")
    for key, s in sorted(strategies.items()):
        hits = [r for r in s["rounds"] if r["hit"]]
        if not hits:
            print(f"\n  {key}: 0 träffar")
            continue

        payouts = [h["payout"] for h in hits]
        print(f"\n  {key}: {len(hits)} träffar")
        for h in hits:
            in_target = "★" if target_lo <= h["payout"] <= target_hi else " "
            print(f"    {h['date']}  {h['payout']:>10,.0f}kr  "
                  f"upsets={h['actual_upsets']}  rows={h['rows']}  {in_target}")

        avg_payout = sum(payouts) / len(payouts)
        med_payout = sorted(payouts)[len(payouts) // 2]
        print(f"    Snitt: {avg_payout:,.0f}kr  Median: {med_payout:,.0f}kr")

    # Upset correlation
    print("\n── Upset-korrelation ──")
    upset_buckets = defaultdict(lambda: {"n": 0, "cs_hits": 0, "se_hits": 0, "payouts": []})

    for b in budgets:
        cs_rounds = strategies[f"chansspik_{b}"]["rounds"]
        se_rounds = strategies[f"spike_easiest_{b}"]["rounds"]

        for r in cs_rounds:
            u = r["actual_upsets"]
            upset_buckets[u]["n"] += 1
            if r["hit"]:
                upset_buckets[u]["cs_hits"] += 1
                upset_buckets[u]["payouts"].append(r["payout"])

        for r in se_rounds:
            u = r["actual_upsets"]
            if r["hit"]:
                upset_buckets[u]["se_hits"] += 1
        break  # Only count once (same round data)

    for upsets in sorted(upset_buckets.keys()):
        b = upset_buckets[upsets]
        avg_p = sum(b["payouts"]) / len(b["payouts"]) if b["payouts"] else 0
        print(f"  {upsets} upsets: {b['n']:>3} omg, "
              f"CS={b['cs_hits']}/{b['n']}, SE={b['se_hits']}/{b['n']}, "
              f"snitt payout={avg_p:,.0f}kr")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-type", "-g", default="V86")
    parser.add_argument("--limit", "-n", type=int, default=0)
    parser.add_argument("--budgets", "-b", default="300,500")
    args = parser.parse_args()

    budgets = [int(b) for b in args.budgets.split(",")]
    await run_backtest(args.game_type, args.limit, budgets)


if __name__ == "__main__":
    asyncio.run(main())
