#!/usr/bin/env python3
"""Backtest: ABCD+DB2 Smart vs Spike Easiest — med riktiga ATG-utdelningar.

Jämför vid 4 budgetnivåer: 300, 1500, 2000, 3000 kr.
Hämtar dividends från GameRound.dividends (ATG API).

Kör: python3 scripts/backtest_smart_payout.py
"""

import asyncio
import os
import sys
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PYTHONUNBUFFERED"] = "1"

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import build_system


GAME_TYPES = ["V64", "GS75", "V85", "V86"]
BUDGETS = [300, 1500, 2000, 3000]
START_DATE = date(2026, 5, 12)
END_DATE = date(2026, 5, 25)


def estimate_system_payout(game_round, system_plan, budget):
    """Beräkna estimerad utdelning för ett system.

    Logik:
    - Kolla vilka lopp systemet träffar (vinnaren bland picks)
    - Räkna ut antal rader per tier (antal exakt-rätt kombinationer)
    - Multiplicera med ATG:s utdelning per rad per tier
    - Summera alla tiers

    ATG-utdelning: varje rad betalar ut baserat på antal rätt.
    En rad med 5/6 rätt betalar 5-tier-utdelning.
    En rad med 6/6 rätt betalar 6-tier-utdelning (inte 5-tier).

    Returns: (max_hits, total_correct_rows, total_payout, top_tier_payout)
    """
    num_races = len(system_plan.legs)
    sorted_legs = sorted(system_plan.legs, key=lambda l: l.race_number)
    leg_hits = []  # True/False per leg

    for leg in sorted_legs:
        race = game_round.get_race(leg.race_number)
        if not race or not race.result_order:
            return (0, 0, 0.0, 0.0)  # Resultat saknas

        winner = race.result_order[0]
        hit = winner in leg.picks
        leg_hits.append(hit)

    n_hits = sum(leg_hits)

    if n_hits == 0:
        return (0, 0, 0.0, 0.0)

    # Beräkna antal rader per tier
    # Tier T = antal rader med exakt T rätt
    #
    # Metod: för varje kombination av träffade/missade lopp,
    # räkna ut multiplikation av picks i missade lopp.
    #
    # Förenkling: vi vet vilka lopp vi träffar (winner in picks).
    # I miss-lopp har vi len(picks) alternativ men ingen av dem vann.
    # VIKTIGT: i miss-lopp bidrar alla picks till "felaktiga" rader.
    #
    # Rader med exakt n_hits rätt = 1 (en kombination: alla hits, alla missar)
    # Men vi har FLER rader totalt (picks_leg1 × picks_leg2 × ...).
    #
    # Alla rader delar sig:
    # - Rader med n_hits rätt: product(1 for hit_legs) × product(picks-1 for miss_legs)
    #   Wait — nej, alla rader har en specifik kombination:
    #   - I ett hit-lopp: 1 av picks är vinnaren, picks-1 är förlorare
    #   - I ett miss-lopp: 0 av picks är vinnare, alla picks-val är förlorare
    #
    # Tier K = sum over all subsets S of hit_legs with |S|=K:
    #   product(1 for leg in S) × product(picks_i - 1 for hit_leg i NOT in S)
    #   × product(picks_i for miss_leg i)
    #
    # Men detta är komplicerat. Förenkling för V64/V85:
    # den viktigaste tier:en är "alla rätt" (max payout).
    # Tiers under det ger typiskt låg utdelning.

    # Snabbare approach: beräkna varje tier
    # hit_legs = de lopp där vi har vinnaren bland picks
    # miss_legs = de lopp där vi inte har vinnaren

    hit_legs = [(i, sorted_legs[i]) for i in range(num_races) if leg_hits[i]]
    miss_legs = [(i, sorted_legs[i]) for i in range(num_races) if not leg_hits[i]]

    total_payout = 0.0
    total_correct_rows = 0

    # Beräkna alla tiers via combinatorics
    from itertools import combinations

    for tier in range(n_hits, 0, -1):
        tier_payout_per_row = game_round.dividends.get(tier, 0.0)
        if tier_payout_per_row <= 0:
            continue

        # Antal rader med exakt 'tier' rätt
        # Vi måste välja exakt 'tier' av våra 'n_hits' hit-lopp
        # och i de "ej valda" hit-loppen, välja en av de (picks-1) felaktiga
        # samt i alla miss-lopp, alla picks är felaktiga

        rows_at_tier = 0

        if tier > n_hits:
            continue  # Kan inte ha fler rätt än antal hits

        # Välj vilka 'tier' av n_hits hit-lopp som ska vara "rätt" på denna rad
        # De övriga hit-loppen tar en av sina (picks-1) icke-vinnare
        for correct_subset in combinations(range(len(hit_legs)), tier):
            correct_set = set(correct_subset)
            row_count = 1
            for j, (orig_i, leg) in enumerate(hit_legs):
                if j in correct_set:
                    row_count *= 1  # Vinnaren
                else:
                    row_count *= (len(leg.picks) - 1)  # Icke-vinnare i hit-lopp
            for orig_i, leg in miss_legs:
                row_count *= len(leg.picks)  # Alla picks i miss-lopp
            rows_at_tier += row_count

        total_payout += rows_at_tier * tier_payout_per_row
        if tier == n_hits:
            total_correct_rows = rows_at_tier

    top_tier_payout = game_round.dividends.get(n_hits, 0.0)
    return (n_hits, total_correct_rows, total_payout, top_tier_payout)


async def main():
    client = ATGClient()
    analyzer = CompositeAnalyzer()

    # Resultat per strategi × budget
    results = defaultdict(lambda: {
        "rounds": 0,
        "races": 0,
        "hits_per_race": 0,
        "total_cost": 0.0,
        "total_payout": 0.0,
        "full_hits": 0,  # Alla rätt
        "tier_hits": defaultdict(int),  # hits per tier
        "round_details": [],
    })

    print(f"=== Backtest Smart vs Spike — {START_DATE} till {END_DATE} ===")
    print(f"    Budgets: {BUDGETS}")
    print(f"    Speltyper: {GAME_TYPES}")
    print()

    for game_type in GAME_TYPES:
        current = START_DATE
        while current <= END_DATE:
            try:
                game_round = await client.fetch_full_round(game_type, current)
            except Exception:
                game_round = None

            if not game_round:
                current += timedelta(days=1)
                continue

            # Kolla att alla lopp har resultat
            has_results = all(
                race.result_order
                for race in game_round.races
            )
            if not has_results:
                print(f"  {game_type} {current}: SKIP (saknar resultat)", flush=True)
                current += timedelta(days=1)
                continue

            # Analysera omgången (fyller i recommendation, effective_form etc)
            analyzer.analyze_round(game_round)

            num_races = len(game_round.races)
            dividends_str = ""
            if game_round.dividends:
                top_tier = max(game_round.dividends.keys())
                top_payout = game_round.dividends[top_tier]
                dividends_str = f" | {top_tier}/{num_races} rätt: {top_payout:,.0f}kr"

            print(f"  {game_type} {current}{dividends_str}", flush=True)

            for budget in BUDGETS:
                for strategy, label in [("smart", "Smart"), ("optimal", "Spike")]:
                    key = f"{label}_{budget}"

                    try:
                        plan = build_system(
                            game_round, budget=budget, strategy=strategy
                        )
                    except Exception as e:
                        print(f"    {key}: FEL - {e}", flush=True)
                        continue

                    n_hits, n_correct, est_payout, tier_pay = estimate_system_payout(
                        game_round, plan, budget
                    )

                    cost = plan.total_cost

                    results[key]["rounds"] += 1
                    results[key]["races"] += num_races
                    results[key]["hits_per_race"] += n_hits
                    results[key]["total_cost"] += cost
                    results[key]["total_payout"] += est_payout
                    results[key]["tier_hits"][n_hits] += 1
                    if n_hits == num_races:
                        results[key]["full_hits"] += 1

                    results[key]["round_details"].append({
                        "game": f"{game_type} {current}",
                        "hits": n_hits,
                        "total": num_races,
                        "cost": cost,
                        "payout": est_payout,
                        "tier_pay": tier_pay,
                        "rows": plan.total_rows,
                    })

                    # Jämför picks per avdelning
                    if budget == 1500:
                        smart_key = f"Smart_{budget}"
                        spike_key = f"Spike_{budget}"
                        # Visa detaljerade diff för 1500kr
                        if strategy == "smart":
                            diff_parts = []
                            spike_plan = build_system(
                                game_round, budget=budget, strategy="optimal"
                            )
                            for s_leg, sp_leg in zip(
                                sorted(plan.legs, key=lambda l: l.race_number),
                                sorted(spike_plan.legs, key=lambda l: l.race_number),
                            ):
                                race = game_round.get_race(s_leg.race_number)
                                winner = race.result_order[0] if race and race.result_order else -1
                                s_hit = winner in s_leg.picks
                                sp_hit = winner in sp_leg.picks

                                if s_hit != sp_hit:
                                    if s_hit:
                                        diff_parts.append(f"Avd{s_leg.race_number}:+S")
                                    else:
                                        diff_parts.append(f"Avd{s_leg.race_number}:+E")
                                elif set(s_leg.picks) != set(sp_leg.picks):
                                    diff_parts.append(f"Avd{s_leg.race_number}:=")

                            if diff_parts:
                                print(f"    1500kr diff: [{' | '.join(diff_parts)}]", flush=True)

            # Hoppa framåt (undvik dubbelsökning)
            if game_type in ("V85", "GS75", "V86"):
                current += timedelta(days=6)
            current += timedelta(days=1)

    # ATGClient uses httpx sessions that auto-close
    if hasattr(client, 'close'):
        await client.close()

    # ── Resultatsammanfattning ──
    print()
    print("=" * 80)
    print("=== RESULTAT: Smart vs Spike vid 4 budgetnivåer ===")
    print("=" * 80)

    for budget in BUDGETS:
        print(f"\n{'─' * 60}")
        print(f"  BUDGET: {budget} kr")
        print(f"{'─' * 60}")

        for label in ["Smart", "Spike"]:
            key = f"{label}_{budget}"
            r = results[key]

            if r["rounds"] == 0:
                continue

            hit_rate = r["hits_per_race"] / r["races"] * 100 if r["races"] > 0 else 0
            roi = (r["total_payout"] - r["total_cost"]) / r["total_cost"] * 100 if r["total_cost"] > 0 else 0
            profit = r["total_payout"] - r["total_cost"]

            # Tier breakdown
            tier_str = ", ".join(
                f"{tier}rätt:{count}"
                for tier, count in sorted(r["tier_hits"].items(), reverse=True)
            )

            print(f"\n  {label:6s}: {r['hits_per_race']}/{r['races']} lopp ({hit_rate:.1f}%)")
            print(f"           Kostnad: {r['total_cost']:,.0f} kr | Utbetalt: {r['total_payout']:,.0f} kr")
            print(f"           ROI: {roi:+.1f}% | Vinst: {profit:+,.0f} kr")
            print(f"           Alla rätt: {r['full_hits']}/{r['rounds']}")
            print(f"           Tiers: {tier_str}")

    # ── Detaljerad jämförelse vid 1500kr ──
    print(f"\n{'=' * 80}")
    print("=== DETALJERAD JÄMFÖRELSE: 1500kr ===")
    print(f"{'=' * 80}")

    smart_details = results["Smart_1500"]["round_details"]
    spike_details = results["Spike_1500"]["round_details"]

    print(f"\n  {'Omgång':<20} {'Smart':>12} {'Spike':>12} {'Smart kr':>12} {'Spike kr':>12}")
    print(f"  {'─' * 68}")

    for sd, spd in zip(smart_details, spike_details):
        s_str = f"{sd['hits']}/{sd['total']}"
        sp_str = f"{spd['hits']}/{spd['total']}"
        s_pay = f"{sd['payout']:,.0f}" if sd['payout'] > 0 else "—"
        sp_pay = f"{spd['payout']:,.0f}" if spd['payout'] > 0 else "—"

        print(f"  {sd['game']:<20} {s_str:>12} {sp_str:>12} {s_pay:>12} {sp_pay:>12}")

    # ── ATG utdelningar per omgång ──
    print(f"\n{'=' * 80}")
    print("=== ATG UTDELNINGAR (alla rätt) ===")
    print(f"{'=' * 80}")

    for sd in smart_details:
        game = sd['game']
        # Hämta från spike details om tier_pay finns
        spd = next((x for x in spike_details if x['game'] == game), None)
        total = sd['total']
        print(f"  {game:<20} {total}/{total} rätt: {sd.get('tier_pay', 0):>10,.0f} kr/rad")


if __name__ == "__main__":
    asyncio.run(main())
