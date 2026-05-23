"""ROI-backtest med integrerad super_score + systemgenerator.

Kör alla omgångar genom den fullständiga pipelinen:
CompositeAnalyzer → SystemGenerator → Payout-beräkning

Version 3: Med FAKTISKA utdelningar från ATG API (inte estimerade)
+ delvinster (V75: 7/6/5 rätt, V85: 8/7/6/5 rätt)
"""

import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from trav_agent.backtest.runner import BacktestRunner


def count_correct(system_picks, race_results):
    """Räkna antal rätt i ett system."""
    correct = 0
    for rp, result in zip(system_picks, race_results):
        if not result:
            continue
        winner = result[0]
        if winner in rp.picks:
            correct += 1
    return correct


def get_payout(game_round, num_correct: int) -> float:
    """Hämta faktisk utdelning per rad för givet antal rätt.

    Använder ATG:s faktiska utdelningsdata (dividends).
    Returnerar utdelning i kr per rad, eller 0 om ingen utdelning.
    """
    if not game_round.dividends:
        return 0.0

    payout = game_round.dividends.get(num_correct, 0.0)
    return payout


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

    print(f"Hittade {len(v75_dates)} V75 + {len(v85_dates)} V85 = {len(dates_to_load)} omgångar")

    strategies = [
        ("D_market_gap", "avg_upset_lt_40", 2500),
        ("I_streck_1st", "avg_upset_lt_40", 2500),
        ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500),
        ("D_market_gap", "V75_only", 2500),
        ("D_market_gap", "no_big_field_volt", 2500),
        ("D_market_gap", "avg_upset_lt_40", 3000),
        ("D_market_gap", "avg_upset_lt_40", 3500),
    ]

    # Ladda alla omgångar
    rounds = []
    no_dividends = 0
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races:
                rounds.append(gr)
                if not gr.dividends:
                    no_dividends += 1
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar ({no_dividends} utan utdelningsdata)")

    # ── Temporal filtrering: ta bort framtida starter + omberäkna karriär ──
    for gr in rounds:
        BacktestRunner._filter_future_starts(gr)
        for race in gr.races:
            for entry in race.entries:
                entry.horse.recompute_career_from_starts()

    # Analysera alla med CompositeAnalyzer
    analyzer = CompositeAnalyzer()
    for gr in rounds:
        analyzer.analyze_round(gr)
    print("Alla omgångar analyserade (temporal filtrering aktiv)\n")

    results_all = []

    for strategy, filter_name, budget in strategies:
        gen = SystemGenerator(budget=budget, strategy=strategy, selective_filter=filter_name)

        total_cost = 0.0
        total_payout = 0.0
        rounds_played = 0
        rounds_skipped = 0
        hits_full = 0
        hits_partial = 0
        hit_details = []
        tier_counts = {}

        for gr in rounds:
            system = gen.generate(gr)

            if system.skip_round:
                rounds_skipped += 1
                continue

            rounds_played += 1
            total_cost += system.total_cost

            # Resultat
            race_results = [race.result_order for race in gr.races]
            num_races = len(gr.races)
            num_correct = count_correct(system.race_picks, race_results)

            # Hämta FAKTISK utdelning
            payout_per_row = get_payout(gr, num_correct)

            if payout_per_row > 0:
                # Vi har num_correct rätt, och utdelningen är per rad
                # Vår totala utdelning = payout_per_row (vi spelar 1 system = 1 rad per kombination)
                # Men vårt system har total_rows rader, och exakt EN av dem
                # har alla N rätt (om vi har alla rätt) eller fler har N-1 rätt.
                # Faktiskt: antal rader i VÅRT system med exakt num_correct rätt
                #
                # Förenkling: om alla rätt → 1 rad vinner.
                # Om N-1 rätt → antal rader med N-1 rätt = num_picks[miss_race] - 1
                # (alla rader utom de med rätt häst i missat lopp)
                # Mer exakt: rader med exakt K rätt beror på vilka lopp vi missade
                # och hur många picks vi hade där.

                # Beräkna antal vinnande rader i VÅRT system
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

                hit_details.append({
                    "date": str(gr.round_date),
                    "type": gr.game_type,
                    "cost": system.total_cost,
                    "payout": round(total_payout_this, 0),
                    "payout_per_row": round(payout_per_row, 0),
                    "winning_rows": winning_rows,
                    "rows": system.total_rows,
                    "correct": num_correct,
                    "num_races": num_races,
                })

        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        netto = total_payout - total_cost
        total_hits = hits_full + hits_partial
        hit_rate_full = hits_full / rounds_played if rounds_played > 0 else 0

        result = {
            "strategy": strategy,
            "filter": filter_name,
            "budget": budget,
            "rounds_played": rounds_played,
            "rounds_skipped": rounds_skipped,
            "hits_full": hits_full,
            "hits_partial": hits_partial,
            "hits_total": total_hits,
            "hit_rate_full": round(hit_rate_full, 4),
            "total_cost": round(total_cost, 0),
            "total_payout": round(total_payout, 0),
            "roi": round(roi, 4),
            "netto": round(netto, 0),
            "tier_counts": tier_counts,
            "hits_detail": hit_details,
        }
        results_all.append(result)

        roi_str = f"{roi:+.1%}" if total_cost > 0 else "N/A"
        print(f"{strategy:20} + {filter_name:20} @ {budget:5}kr: "
              f"Spelat {rounds_played:3}/{len(rounds)} | "
              f"Full {hits_full:2} Part {hits_partial:3} | "
              f"ROI {roi_str:>8} | "
              f"Netto {netto:>12,.0f} kr | "
              f"Tiers: {tier_counts}")

    # Spara
    with open("roi_integrated_results.json", "w") as f:
        json.dump(results_all, f, indent=2, default=str)

    print(f"\nSparat till roi_integrated_results.json")

    # Visa bästa hit-detaljer
    best = max(results_all, key=lambda r: r["roi"])
    print(f"\n{'='*60}")
    print(f"  BÄSTA STRATEGI: {best['strategy']} + {best['filter']} @ {best['budget']}kr")
    print(f"  ROI: {best['roi']:+.1%} | Netto: {best['netto']:,.0f} kr")
    print(f"  Full hits: {best['hits_full']} | Delvinster: {best['hits_partial']}")
    print(f"  Tiers: {best['tier_counts']}")
    print(f"{'='*60}")

    if best["hits_detail"]:
        print("\nTop hits:")
        sorted_hits = sorted(best["hits_detail"], key=lambda h: h["payout"], reverse=True)
        for h in sorted_hits[:20]:
            print(f"  {h['date']} {h['type']}: {h['correct']}/{h['num_races']} rätt "
                  f"→ {h['payout']:,.0f} kr ({h['payout_per_row']:,.0f} kr/rad × {h['winning_rows']} rader)")


def _count_winning_rows_in_system(system_picks, race_results, num_correct):
    """Räkna antal rader i systemet som har exakt num_correct rätt.

    Varje "rad" i systemet är en kombination av en häst per lopp.
    En rad har N rätt om den har vinnaren i exakt N lopp.

    Om vi har num_correct = alla → bara 1 rad har alla rätt
    (produkten av 1 per lopp, dvs 1 rad).

    Om vi missar K lopp → antal rader med num_correct rätt =
    produkt(picks_i för rätta lopp) × produkt(picks_j - har_vinnare för fel lopp)
    Nej wait... mer exakt:

    Rader med exakt num_correct rätt:
    - I lopp där vi HAR rätt: vi måste välja vinnaren → 1 val per sådant lopp
    - I lopp där vi HAR FEL: vi måste välja EN av de ICKE-vinnande hästarna
      → (picks_j - 1) om vinnaren är i våra picks, annars picks_j
      Men vi vet att vi har FEL i dessa lopp, dvs vinnaren är INTE i våra picks
      → vi har picks_j möjliga val (alla "fel"), products = picks_j

    Antal rader med num_correct rätt (givet vilka lopp som är rätt/fel):
    = produkt av 1 (för rätta lopp) × produkt av (num_picks) för fel lopp
    = produkt av num_picks för missade lopp

    Men vänta — vi vet att vinnaren INTE är i våra picks i de missade loppen.
    Så alla våra picks i de missade loppen är "fel". Alltså bidrar varje missat
    lopp med num_picks_i val, alla felaktiga.

    Totalt: rader med exakt num_correct rätt =
    ∏(num_picks_j) för j i missade lopp

    Exempel: 5/7 rätt, missade lopp har 3 och 4 picks → 3×4 = 12 rader med 5 rätt
    """
    num_races = len(system_picks)

    if num_correct == num_races:
        return 1  # Bara 1 rad har alla rätt

    # Identifiera missade lopp
    missed_product = 1
    for rp, result in zip(system_picks, race_results):
        if not result:
            missed_product *= rp.num_picks
            continue
        winner = result[0]
        if winner not in rp.picks:
            # Missat lopp — alla våra picks är fel
            missed_product *= rp.num_picks

    return missed_product


if __name__ == "__main__":
    asyncio.run(main())
