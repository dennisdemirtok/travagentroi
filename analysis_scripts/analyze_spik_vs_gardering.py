"""Analysera spik vs gardering — vad ger bäst ROI?

Testar varianter:
- allow_spik_0: Nuvarande (minimum 2 picks per lopp, ingen spik)
- allow_spik_1: Tillåt max 1 spiklopp (1 pick) per system
- allow_spik_2: Tillåt max 2 spiklopp per system
- allow_spik_all: Tillåt obegränsat antal spikar (1 pick i alla höga conf-lopp)

Spiklogik: Om conf >= 75 OCH super_score-gap >= 12 → 1 pick (spik)

Använder faktiska ATG-utdelningar.
"""

import asyncio
import json
import re
import sys
from copy import deepcopy
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator, RacePick, BettingSystem
from trav_agent.data.models import GameRound, Race
from roi_backtest_integrated import count_correct, _count_winning_rows_in_system


class FlexibleSystemGenerator(SystemGenerator):
    """SystemGenerator som tillåter spikar (1 pick)."""

    def __init__(self, max_spikes: int = 0, spike_conf_threshold: float = 75,
                 spike_score_gap: float = 12, **kwargs):
        super().__init__(**kwargs)
        self.max_spikes = max_spikes
        self.spike_conf_threshold = spike_conf_threshold
        self.spike_score_gap = spike_score_gap

    def _allocate_picks(self, race_confs):
        """Modifierad pick-allokering med spik-stöd."""
        result = []
        spikes_used = 0

        for race, conf in race_confs:
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )

            # Kolla om detta lopp kvalificerar för spik
            can_spike = False
            if (self.max_spikes > spikes_used and
                len(sorted_entries) >= 2):
                score_gap = sorted_entries[0].super_score - sorted_entries[1].super_score
                if conf >= self.spike_conf_threshold and score_gap >= self.spike_score_gap:
                    can_spike = True

            # Bestäm antal picks
            if can_spike:
                n_picks = 1
                spikes_used += 1
            elif conf >= 70:
                n_picks = 2
            elif conf >= 50:
                n_picks = 3
            elif conf >= 30:
                n_picks = 4
            elif conf >= 15:
                n_picks = min(6, race.num_starters)
            else:
                n_picks = min(8, race.num_starters)

            # Extra gardering vid hög skrällrisk (men inte om spik)
            if not can_spike:
                if race.upset_risk >= 50:
                    n_picks = max(n_picks, min(5, race.num_starters))
                elif race.upset_risk >= 30:
                    n_picks = max(n_picks, 3)

            selected = sorted_entries[:n_picks]

            pick = RacePick(
                race_number=race.race_number,
                track_name=race.track_name,
                distance=race.distance,
                start_method=race.start_method.value,
                num_starters=race.num_starters,
                confidence=conf,
                confidence_formula=self.strategy,
                picks=[e.post_position for e in selected],
                pick_names=[e.horse.name for e in selected],
                pick_scores=[e.super_score for e in selected],
                pick_streck=[e.bet_percentage or 0.05 for e in selected],
                upset_risk=race.upset_risk,
            )
            result.append(pick)

        return result

    def _scale_to_budget(self, system):
        """Skala ner men behåll spikar (1 pick)."""
        max_iterations = 50
        iteration = 0

        while system.total_cost > self.budget and iteration < max_iterations:
            iteration += 1

            # Hitta lopp med flest picks, minst 2 (spik = 1 kan inte skalas)
            candidates = [
                rp for rp in system.race_picks if rp.num_picks > 2
            ]
            if not candidates:
                # Prova att ta från 2-picks-lopp (men aldrig under 1 om spik)
                candidates = [
                    rp for rp in system.race_picks if rp.num_picks > 1
                ]
                if not candidates:
                    break

            candidates.sort(
                key=lambda rp: rp.confidence - rp.upset_risk * 0.3,
                reverse=True,
            )
            target = candidates[0]

            target.picks.pop()
            target.pick_names.pop()
            target.pick_scores.pop()
            target.pick_streck.pop()
            target.num_picks -= 1
            target.combined_streck = sum(target.pick_streck)

            system.calculate_cost()


async def main():
    client = ATGClient()
    cache_dir = Path("cache")

    # Hitta unika omgångar
    v75_dates = set()
    v85_dates = set()
    for f in cache_dir.iterdir():
        m = re.match(r"(V75)_(\d{4}-\d{2}-\d{2})_", f.name)
        if m:
            v75_dates.add(m.group(2))
        m = re.match(r"(V85)_(\d{4}-\d{2}-\d{2})_", f.name)
        if m:
            v85_dates.add(m.group(2))

    dates_to_load = [(t, d) for t in ["V75"] for d in sorted(v75_dates)] + \
                    [(t, d) for t in ["V85"] for d in sorted(v85_dates)]

    # Ladda omgångar
    rounds = []
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races:
                rounds.append(gr)
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar\n")

    # Analysera
    analyzer = CompositeAnalyzer()
    for gr in rounds:
        analyzer.analyze_round(gr)

    # ── Testa varianter ──────────────────────────────────────────────────────
    # Baseline: I_streck_1st (bästa strategin)
    strategy = "I_streck_1st"
    filt = "avg_upset_lt_40"
    budget = 2500

    variants = [
        ("Min-2 (nuvarande)", 0, 75, 12),
        ("Max 1 spik (conf≥75, gap≥12)", 1, 75, 12),
        ("Max 2 spikar (conf≥75, gap≥12)", 2, 75, 12),
        ("Alla spikar (conf≥75, gap≥12)", 99, 75, 12),
        ("Max 1 spik (conf≥70, gap≥10)", 1, 70, 10),
        ("Max 2 spikar (conf≥70, gap≥10)", 2, 70, 10),
        ("Alla spikar (conf≥70, gap≥10)", 99, 70, 10),
        ("Max 1 spik (conf≥80, gap≥15)", 1, 80, 15),
    ]

    print(f"{'Variant':<40} {'Spelat':>6} {'Full':>5} {'Part':>5} "
          f"{'Kostnad':>10} {'Utdeln':>12} {'ROI':>8} {'Netto':>12} "
          f"{'Spikar/omg':>10}")
    print("=" * 120)

    all_results = []

    for name, max_spikes, conf_thr, gap_thr in variants:
        if max_spikes == 0:
            gen = SystemGenerator(budget=budget, strategy=strategy, selective_filter=filt)
        else:
            gen = FlexibleSystemGenerator(
                max_spikes=max_spikes,
                spike_conf_threshold=conf_thr,
                spike_score_gap=gap_thr,
                budget=budget,
                strategy=strategy,
                selective_filter=filt,
            )

        total_cost = 0
        total_payout = 0
        rounds_played = 0
        hits_full = 0
        hits_partial = 0
        total_spikes = 0
        spike_correct = 0
        spike_total = 0
        tier_counts = {}

        for gr in rounds:
            system = gen.generate(gr)
            if system.skip_round:
                continue

            rounds_played += 1
            total_cost += system.total_cost

            # Räkna spikar
            spikes_this = sum(1 for rp in system.race_picks if rp.num_picks == 1)
            total_spikes += spikes_this

            # Kolla spik-träffar
            race_results = [race.result_order for race in gr.races]
            for rp, result in zip(system.race_picks, race_results):
                if rp.num_picks == 1:
                    spike_total += 1
                    if result and result[0] in rp.picks:
                        spike_correct += 1

            num_races = len(gr.races)
            num_correct = count_correct(system.race_picks, race_results)

            payout_per_row = gr.dividends.get(num_correct, 0.0)
            if payout_per_row > 0:
                winning_rows = _count_winning_rows_in_system(
                    system.race_picks, race_results, num_correct
                )
                total_payout_this = payout_per_row * winning_rows

                if num_correct == num_races:
                    hits_full += 1
                else:
                    hits_partial += 1
                total_payout += total_payout_this

                tier_key = f"{num_correct}_ratt"
                tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        netto = total_payout - total_cost
        avg_spikes = total_spikes / rounds_played if rounds_played > 0 else 0
        spike_hit_pct = spike_correct / spike_total * 100 if spike_total > 0 else 0

        print(f"{name:<40} {rounds_played:>6} {hits_full:>5} {hits_partial:>5} "
              f"{total_cost:>10,.0f} {total_payout:>12,.0f} {roi:>+7.1%} {netto:>+12,.0f} "
              f"{avg_spikes:>7.1f}/omg")

        all_results.append({
            "name": name,
            "max_spikes": max_spikes,
            "conf_threshold": conf_thr,
            "gap_threshold": gap_thr,
            "rounds_played": rounds_played,
            "hits_full": hits_full,
            "hits_partial": hits_partial,
            "total_cost": round(total_cost),
            "total_payout": round(total_payout),
            "roi": round(roi, 4),
            "netto": round(netto),
            "tier_counts": tier_counts,
            "avg_spikes_per_round": round(avg_spikes, 2),
            "spike_total": spike_total,
            "spike_correct": spike_correct,
            "spike_hit_rate": round(spike_hit_pct, 1),
        })

    # Spikstatistik
    print(f"\n{'='*60}")
    print("SPIKSTATISTIK:")
    for r in all_results:
        if r["spike_total"] > 0:
            print(f"  {r['name']}: {r['spike_correct']}/{r['spike_total']} "
                  f"spikar rätt ({r['spike_hit_rate']:.1f}%)")
    print(f"{'='*60}")

    # Spara
    with open("spik_analysis_results.json", "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSparat till spik_analysis_results.json")


if __name__ == "__main__":
    asyncio.run(main())
