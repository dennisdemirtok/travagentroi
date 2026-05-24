#!/usr/bin/env python3
"""Optimera budget — testa alla nivåer från 25kr till 5000kr.

Inte låsa vid fasta budgetar. Testa hela spektret och hitta sweet spot.
Testa ALLA kombinationer: budget × antal_spikar × rest_width.
"""

import asyncio
import json
import glob
import re
import sys
import random
import time
from datetime import date
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, StartMethod
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import predict_difficulty
from trav_agent.config import DEFAULT_CONFIG

random.seed(42)
CACHE_DIR = Path(__file__).parent / "cache"
ROW_PRICE = 0.50


def find_round_ids(start_year=2022):
    cal_re = re.compile(r"^(\d{4})-\d{2}-\d{2}_[a-f0-9]+\.json$")
    rounds = []
    for fp in sorted(glob.glob(str(CACHE_DIR / "*.json"))):
        fname = Path(fp).name
        m = cal_re.match(fname)
        if not m or int(m.group(1)) < start_year:
            continue
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue
        games = data.get("games", {})
        if not isinstance(games, dict):
            continue
        for gt in ("V75", "V85"):
            for g in games.get(gt, []):
                if g.get("status") == "results" and g.get("races"):
                    rounds.append({"game_type": gt, "game_id": g["id"]})
    return rounds


async def load_round(client, info):
    try:
        parts = info["game_id"].split("_")
        day = date.fromisoformat(parts[1])
        gr = await client.fetch_full_round(parts[0], day)
        if gr and gr.is_finished and gr.races:
            return gr
    except Exception:
        pass
    return None


def apply_temporal_filtering(gr):
    for race in gr.races:
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race.race_date]
            if len(filtered) < len(original):
                entry.horse.past_starts = filtered
            entry.horse.recompute_career_from_starts()
            entry.bet_percentage = None
            entry.odds = None


@dataclass
class RoundData:
    gr: object
    num_legs: int
    top_div: float
    n1_div: float
    game_type: str
    # Per-race data (sorted by difficulty, ascending)
    difficulties: list[float] = field(default_factory=list)
    model_rankings: list[list[int]] = field(default_factory=list)  # picks per race
    winner_pps: list[int] = field(default_factory=list)


def build_system_fast(rd: RoundData, n_spikes: int, rest_width: int, budget: float):
    """Fast system check without full SystemPlan objects.

    Returns (hit, n1_correct, cost) tuple.
    """
    n = rd.num_legs
    n_spikes = min(n_spikes, n - 1)

    # Assign picks: spike easiest, rest_width on rest
    picks = []
    for i in range(n):
        if i < n_spikes:
            picks.append(1)
        else:
            picks.append(min(rest_width, len(rd.model_rankings[i])))

    # Budget constraint: reduce widest non-spike
    rows = 1
    for p in picks:
        rows *= p
    cost = rows * ROW_PRICE

    while cost > budget:
        # Find widest non-spike (search from lowest difficulty first)
        widest_idx = -1
        widest_val = 0
        for i in range(n_spikes, n):
            if picks[i] > widest_val:
                widest_val = picks[i]
                widest_idx = i
        if widest_idx < 0 or picks[widest_idx] <= 1:
            break
        picks[widest_idx] -= 1
        rows = 1
        for p in picks:
            rows *= p
        cost = rows * ROW_PRICE

    # Check hits
    correct = 0
    for i in range(n):
        top_picks = rd.model_rankings[i][:picks[i]]
        if rd.winner_pps[i] in top_picks:
            correct += 1

    return correct == n, correct == n - 1, cost


@dataclass
class Result:
    budget: float
    n_spikes: int
    rest_width: int
    tested: int = 0
    hits: int = 0
    n1: int = 0
    total_cost: float = 0.0
    total_payout: float = 0.0
    total_n1_payout: float = 0.0

    @property
    def key(self):
        return f"{self.n_spikes}S+rest{self.rest_width}|{self.budget:.0f}kr"

    @property
    def hitrate(self):
        return self.hits / self.tested if self.tested else 0

    @property
    def roi(self):
        return (self.total_payout - self.total_cost) / self.total_cost if self.total_cost > 0 else 0

    @property
    def roi_incl_n1(self):
        return (self.total_payout + self.total_n1_payout - self.total_cost) / self.total_cost if self.total_cost > 0 else 0

    @property
    def avg_cost(self):
        return self.total_cost / self.tested if self.tested else 0

    @property
    def profit_per_round(self):
        return (self.total_payout - self.total_cost) / self.tested if self.tested else 0


async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 90)
    print("  BUDGET OPTIMIZER — hitta optimal budget")
    print("  Testar alla budgetar 25kr-5000kr × alla spike/bredd-kombinationer")
    print("=" * 90)

    # Load rounds
    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:250]

    rounds_data: list[RoundData] = []
    loaded = 0

    for info in round_infos:
        gr = await load_round(client, info)
        if not gr:
            continue
        if not all(r.result_order for r in gr.races):
            continue
        num_legs = len(gr.races)
        top_div = gr.dividends.get(num_legs, 0.0)
        n1_div = gr.dividends.get(num_legs - 1, 0.0)
        if top_div <= 0:
            continue

        apply_temporal_filtering(gr)
        analyzer.analyze_round(gr)

        # Sort races by difficulty (ascending = easiest first for spiking)
        race_data = []
        for race in gr.races:
            diff = predict_difficulty(race)
            sorted_entries = sorted(race.active_entries,
                                    key=lambda e: e.super_score, reverse=True)
            model_ranking = [e.post_position for e in sorted_entries]
            winner_pp = race.result_order[0] if race.result_order else -1
            race_data.append((diff, model_ranking, winner_pp))

        race_data.sort(key=lambda x: x[0])  # Sort by difficulty

        rd = RoundData(
            gr=gr,
            num_legs=num_legs,
            top_div=top_div,
            n1_div=n1_div,
            game_type=gr.game_type,
            difficulties=[d for d, _, _ in race_data],
            model_rankings=[mr for _, mr, _ in race_data],
            winner_pps=[wp for _, _, wp in race_data],
        )
        rounds_data.append(rd)
        loaded += 1
        if loaded % 25 == 0:
            print(f"  Loaded {loaded} rounds ({time.time() - t0:.0f}s)...")

    print(f"\nLoaded {loaded} rounds")
    v75 = sum(1 for r in rounds_data if r.game_type == "V75")
    v85 = sum(1 for r in rounds_data if r.game_type == "V85")
    print(f"  V75: {v75}, V85: {v85}")
    divs = [r.top_div for r in rounds_data]
    print(f"  Avg dividend: {sum(divs)/len(divs):,.0f}kr, Median: {sorted(divs)[len(divs)//2]:,.0f}kr")

    # ── Test all combinations ────────────────────────────────────
    budgets = list(range(25, 201, 25)) + list(range(250, 1001, 50)) + \
              list(range(1100, 2001, 100)) + list(range(2500, 5001, 500))
    spikes_range = range(0, 6)
    widths_range = range(2, 9)

    all_results: list[Result] = []
    combos_tested = 0

    for budget in budgets:
        for n_spikes in spikes_range:
            for rest_width in widths_range:
                r = Result(budget=budget, n_spikes=n_spikes, rest_width=rest_width)

                for rd in rounds_data:
                    hit, n1, cost = build_system_fast(rd, n_spikes, rest_width, budget)
                    r.tested += 1
                    r.total_cost += cost
                    if hit:
                        r.hits += 1
                        r.total_payout += rd.top_div
                    elif n1:
                        r.n1 += 1
                        r.total_n1_payout += rd.n1_div

                all_results.append(r)
                combos_tested += 1

    elapsed = time.time() - t0
    print(f"\nTested {combos_tested} combinations in {elapsed:.0f}s")

    # ── Find winners ─────────────────────────────────────────────

    # 1. Best ROI per budget level
    print("\n\n" + "=" * 100)
    print("  BEST STRATEGY PER BUDGET (by ROI)")
    print("=" * 100)

    budget_best = {}
    for r in all_results:
        if r.hits == 0:
            continue
        b = r.budget
        if b not in budget_best or r.roi > budget_best[b].roi:
            budget_best[b] = r

    print(f"\n  {'Budget':>7} {'Strategy':<20} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
          f"{'AvgCost':>8} {'ROI':>8} {'ROI+N1':>8} {'Profit/rnd':>10}")
    print("  " + "-" * 85)

    for b in sorted(budget_best.keys()):
        r = budget_best[b]
        print(f"  {b:>6.0f}kr {r.n_spikes}S+rest{r.rest_width:<13} {r.hits:>4} {r.n1:>4} "
              f"{r.hitrate:>6.1%} {r.avg_cost:>7.0f}kr {r.roi:>+7.0%} "
              f"{r.roi_incl_n1:>+7.0%} {r.profit_per_round:>9.0f}kr")

    # 2. Best hit rate per budget level
    print("\n\n" + "=" * 100)
    print("  BEST STRATEGY PER BUDGET (by Hit Rate)")
    print("=" * 100)

    budget_best_hr = {}
    for r in all_results:
        if r.hits == 0:
            continue
        b = r.budget
        if b not in budget_best_hr or r.hitrate > budget_best_hr[b].hitrate:
            budget_best_hr[b] = r

    print(f"\n  {'Budget':>7} {'Strategy':<20} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
          f"{'AvgCost':>8} {'ROI':>8} {'Profit/rnd':>10}")
    print("  " + "-" * 80)

    for b in sorted(budget_best_hr.keys()):
        r = budget_best_hr[b]
        print(f"  {b:>6.0f}kr {r.n_spikes}S+rest{r.rest_width:<13} {r.hits:>4} {r.n1:>4} "
              f"{r.hitrate:>6.1%} {r.avg_cost:>7.0f}kr {r.roi:>+7.0%} "
              f"{r.profit_per_round:>9.0f}kr")

    # 3. Absolute best strategies overall
    print("\n\n" + "=" * 100)
    print("  TOP 30 STRATEGIES OVERALL (by profit per round)")
    print("=" * 100)

    with_hits = [r for r in all_results if r.hits >= 2]
    with_hits.sort(key=lambda r: r.profit_per_round, reverse=True)

    print(f"\n  {'#':>3} {'Budget':>7} {'Strategy':<18} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
          f"{'AvgCost':>8} {'ROI':>8} {'Profit/rnd':>10} {'TotalProfit':>12}")
    print("  " + "-" * 95)

    for i, r in enumerate(with_hits[:30], 1):
        total_profit = r.total_payout - r.total_cost
        print(f"  {i:>3} {r.budget:>6.0f}kr {r.n_spikes}S+rest{r.rest_width:<12} {r.hits:>4} {r.n1:>4} "
              f"{r.hitrate:>6.1%} {r.avg_cost:>7.0f}kr {r.roi:>+7.0%} "
              f"{r.profit_per_round:>9.0f}kr {total_profit:>11,.0f}kr")

    # 4. Spike count analysis across all budgets
    print("\n\n" + "=" * 100)
    print("  SPIKE COUNT ANALYSIS (aggregated across all budgets)")
    print("=" * 100)

    spike_agg = defaultdict(lambda: {"configs": 0, "with_hits": 0, "total_hits": 0,
                                      "total_profit": 0.0, "total_cost": 0.0, "rois": []})
    for r in all_results:
        if r.tested == 0:
            continue
        sa = spike_agg[r.n_spikes]
        sa["configs"] += 1
        if r.hits > 0:
            sa["with_hits"] += 1
            sa["total_hits"] += r.hits
            sa["rois"].append(r.roi)
        sa["total_profit"] += r.total_payout - r.total_cost
        sa["total_cost"] += r.total_cost

    print(f"\n  {'Spikes':>6} {'Configs':>8} {'WithHits':>8} {'TotalHits':>9} "
          f"{'AvgROI':>8} {'BestROI':>8} {'MedianROI':>9}")
    print("  " + "-" * 60)

    for ns in sorted(spike_agg.keys()):
        sa = spike_agg[ns]
        rois = sa["rois"]
        avg_roi = sum(rois) / len(rois) if rois else -1
        best_roi = max(rois) if rois else -1
        med_roi = sorted(rois)[len(rois) // 2] if rois else -1
        print(f"  {ns:>6} {sa['configs']:>8} {sa['with_hits']:>8} {sa['total_hits']:>9} "
              f"{avg_roi:>+7.0%} {best_roi:>+7.0%} {med_roi:>+8.0%}")

    # 5. Rest width analysis
    print("\n\n" + "=" * 100)
    print("  REST WIDTH ANALYSIS (aggregated across all budgets and spike counts)")
    print("=" * 100)

    width_agg = defaultdict(lambda: {"configs": 0, "with_hits": 0, "total_hits": 0, "rois": []})
    for r in all_results:
        if r.tested == 0:
            continue
        wa = width_agg[r.rest_width]
        wa["configs"] += 1
        if r.hits > 0:
            wa["with_hits"] += 1
            wa["total_hits"] += r.hits
            wa["rois"].append(r.roi)

    print(f"\n  {'Width':>5} {'Configs':>8} {'WithHits':>8} {'TotalHits':>9} "
          f"{'AvgROI':>8} {'BestROI':>8}")
    print("  " + "-" * 50)

    for rw in sorted(width_agg.keys()):
        wa = width_agg[rw]
        rois = wa["rois"]
        avg_roi = sum(rois) / len(rois) if rois else -1
        best_roi = max(rois) if rois else -1
        print(f"  {rw:>5} {wa['configs']:>8} {wa['with_hits']:>8} {wa['total_hits']:>9} "
              f"{avg_roi:>+7.0%} {best_roi:>+7.0%}")

    # 6. Budget sweet spot analysis
    print("\n\n" + "=" * 100)
    print("  BUDGET SWEET SPOT ANALYSIS")
    print("  For each budget: best possible profit/round (any strategy)")
    print("=" * 100)

    print(f"\n  {'Budget':>7} {'BestProfitPerRound':>18} {'BestStrategy':<20} "
          f"{'Hits':>4} {'HitRate':>7} {'ROI':>8} {'AvgCost':>8}")
    print("  " + "-" * 80)

    budget_profits = {}
    for r in all_results:
        if r.hits == 0:
            continue
        b = r.budget
        ppr = r.profit_per_round
        if b not in budget_profits or ppr > budget_profits[b][1]:
            budget_profits[b] = (r, ppr)

    for b in sorted(budget_profits.keys()):
        r, ppr = budget_profits[b]
        print(f"  {b:>6.0f}kr {ppr:>17.0f}kr {r.n_spikes}S+rest{r.rest_width:<13} "
              f"{r.hits:>4} {r.hitrate:>6.1%} {r.roi:>+7.0%} {r.avg_cost:>7.0f}kr")

    # 7. Final recommendation
    print("\n\n" + "=" * 100)
    print("  SLUTSATS: OPTIMAL BUDGET OCH STRATEGI")
    print("=" * 100)

    # Best by profit per round (min 2 hits for reliability)
    reliable = [r for r in all_results if r.hits >= 2]
    if reliable:
        best_profit = max(reliable, key=lambda r: r.profit_per_round)
        best_roi_r = max(reliable, key=lambda r: r.roi)
        best_hr = max(reliable, key=lambda r: r.hitrate)

        print(f"""
  1. BÄST VINST PER OMGÅNG: {best_profit.key}
     Budget: {best_profit.budget:.0f}kr
     Hits: {best_profit.hits}/{best_profit.tested} ({best_profit.hitrate:.1%})
     ROI: {best_profit.roi:+.0%}
     Vinst per omgång: {best_profit.profit_per_round:.0f}kr
     Total vinst: {best_profit.total_payout - best_profit.total_cost:,.0f}kr

  2. BÄST ROI: {best_roi_r.key}
     Budget: {best_roi_r.budget:.0f}kr
     Hits: {best_roi_r.hits}/{best_roi_r.tested} ({best_roi_r.hitrate:.1%})
     ROI: {best_roi_r.roi:+.0%}
     Vinst per omgång: {best_roi_r.profit_per_round:.0f}kr

  3. BÄST HITRATE (bland ≥2 hits): {best_hr.key}
     Budget: {best_hr.budget:.0f}kr
     Hits: {best_hr.hits}/{best_hr.tested} ({best_hr.hitrate:.1%})
     ROI: {best_hr.roi:+.0%}
     Vinst per omgång: {best_hr.profit_per_round:.0f}kr
""")

    # Save results
    output = []
    for r in all_results:
        if r.hits > 0:
            output.append({
                "budget": r.budget,
                "n_spikes": r.n_spikes,
                "rest_width": r.rest_width,
                "hits": r.hits,
                "n1": r.n1,
                "tested": r.tested,
                "hitrate": round(r.hitrate, 4),
                "roi": round(r.roi, 4),
                "avg_cost": round(r.avg_cost, 1),
                "profit_per_round": round(r.profit_per_round, 1),
                "total_profit": round(r.total_payout - r.total_cost, 0),
            })

    with open("budget_optimization_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved {len(output)} results to budget_optimization_results.json")

    elapsed = time.time() - t0
    print(f"\n  Total runtime: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
