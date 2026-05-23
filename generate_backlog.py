"""Generera backlog — kör alla historiska omgångar genom SystemGenerator.

Sparar resultat till backlog.json som dashboarden kan visa.

Version 5: Alla speltyper (V75/V85/GS75/V86/V64) + korrekta radpriser + per-speltyp breakdown

Användning:
    python generate_backlog.py              # Alla speltyper
    python generate_backlog.py --type V64   # Bara V64
    python generate_backlog.py --type GS75 --type V86   # GS75 + V86
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from trav_agent.backtest.runner import BacktestRunner
from roi_backtest_integrated import count_correct, _count_winning_rows_in_system

# Alla speltyper som ska ingå i backlogen
BACKLOG_GAME_TYPES = ["V75", "V85", "GS75", "V86", "V64"]


def _get_track_name(cache_dir: Path, game_type: str, game_date: str) -> str:
    """Hämta bannamn från cache-fil."""
    for f in cache_dir.glob(f"{game_type}_{game_date}_*.json"):
        try:
            data = json.load(open(f))
            races = data.get("races", [])
            if races:
                return races[0].get("track", {}).get("name", "?")
        except Exception:
            pass
    return "?"


def _process_round(gr, system, cache_dir):
    """Processa en omgång med ett system och returnera entry dict."""
    game_type = gr.game_type
    game_date = str(gr.round_date)
    track_name = _get_track_name(cache_dir, game_type, game_date)

    race_results = [race.result_order for race in gr.races]
    num_races = len(gr.races)
    num_correct = count_correct(system.race_picks, race_results)

    payout_per_row = gr.dividends.get(num_correct, 0.0)
    winning_rows = 0
    total_payout = 0.0

    if payout_per_row > 0:
        winning_rows = _count_winning_rows_in_system(
            system.race_picks, race_results, num_correct
        )
        total_payout = payout_per_row * winning_rows

    # Detaljerade picks per lopp
    race_details = []
    for race, rp in zip(gr.races, system.race_picks):
        winner = race.result_order[0] if race.result_order else None
        winner_name = ""
        winner_streck = 0.0
        if winner:
            for e in race.active_entries:
                if e.post_position == winner:
                    winner_name = e.horse.name
                    winner_streck = e.bet_percentage or 0
                    break

        race_details.append({
            "avd": rp.race_number,
            "dist": rp.distance,
            "metod": rp.start_method,
            "startande": rp.num_starters,
            "conf": round(rp.confidence, 0),
            "upset": round(rp.upset_risk, 0),
            "picks": rp.num_picks,
            "pick_list": [
                {"nr": nr, "namn": namn[:16], "score": round(sc, 1), "streck": round(st * 100, 1)}
                for nr, namn, sc, st in zip(rp.picks, rp.pick_names, rp.pick_scores, rp.pick_streck)
            ],
            "tackning": round(rp.combined_streck * 100, 0),
            "vinnare": winner,
            "vinnare_namn": winner_name[:16] if winner_name else "",
            "vinnare_streck": round(winner_streck * 100, 1),
            "ratt": winner is not None and winner in rp.picks,
        })

    return {
        "date": game_date,
        "game_type": game_type,
        "track": track_name,
        "strategy": system.strategy,
        "cost": round(system.total_cost, 0),
        "rows": system.total_rows,
        "num_correct": num_correct,
        "num_races": num_races,
        "hit": num_correct == num_races,
        "payout": round(total_payout, 0),
        "payout_per_row": round(payout_per_row, 0),
        "winning_rows": winning_rows,
        "avg_confidence": round(system.avg_confidence, 1),
        "avg_upset": round(system.avg_upset_risk, 1),
        "races": race_details,
    }


async def main():
    parser = argparse.ArgumentParser(description="Generera backlog.json")
    parser.add_argument("--type", dest="types", action="append", default=None,
                        help="Speltyp att inkludera (kan anges flera gånger)")
    args = parser.parse_args()

    game_types_to_process = args.types if args.types else BACKLOG_GAME_TYPES

    client = ATGClient()
    analyzer = CompositeAnalyzer()
    cache_dir = Path("cache")

    # ── Hitta unika omgångar för alla speltyper ──
    all_dates: dict[str, set[str]] = {gt: set() for gt in game_types_to_process}
    pattern = re.compile(
        r"^(" + "|".join(game_types_to_process) + r")_(\d{4}-\d{2}-\d{2})_"
    )
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            all_dates[m.group(1)].add(m.group(2))

    dates_to_load = []
    for gt in game_types_to_process:
        for d in sorted(all_dates[gt]):
            dates_to_load.append((gt, d))

    print(f"Hittade omgångar i cache:")
    for gt in game_types_to_process:
        if all_dates[gt]:
            print(f"  {gt}: {len(all_dates[gt])}")
    print(f"  Totalt: {len(dates_to_load)} omgångar att processa")

    rounds = []
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races:
                rounds.append(gr)
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar")

    # ── Temporal filtrering: ta bort framtida starter + omberäkna karriär ──
    # Förhindrar data leakage — modellen ska bara se data som fanns
    # tillgänglig vid loppets tidpunkt.
    for gr in rounds:
        BacktestRunner._filter_future_starts(gr)
        for race in gr.races:
            for entry in race.entries:
                entry.horse.recompute_career_from_starts()

    for gr in rounds:
        analyzer.analyze_round(gr)
    print("Alla omgångar analyserade (temporal filtrering aktiv)\n")

    # ── Strategier v5: Union + Marknad + Bred ──
    # (strategy, filter, budget, max_spikes, spike_conf, spike_gap, label)
    strategies = [
        # 1000 kr
        ("A_union",   "none", 1000, 0, 0, 0, "A_union_1000"),
        ("B_marknad", "none", 1000, 0, 0, 0, "B_marknad_1000"),
        ("C_bred",    "none", 1000, 0, 0, 0, "C_bred_1000"),
        # 500 kr
        ("A_union",   "none", 500, 0, 0, 0, "A_union_500"),
        ("B_marknad", "none", 500, 0, 0, 0, "B_marknad_500"),
        # 2000 kr
        ("B_marknad", "none", 2000, 0, 0, 0, "B_marknad_2000"),
        ("C_bred",    "none", 2000, 0, 0, 0, "C_bred_2000"),
    ]

    all_entries = []
    strategy_summaries = {}

    for strategy, filt, budget, max_spikes, spike_conf, spike_gap, label in strategies:
        gen = SystemGenerator(
            budget=budget, strategy=strategy, selective_filter=filt,
            max_spikes=max_spikes, spike_conf_threshold=spike_conf, spike_score_gap=spike_gap,
        )
        strat_entries = []

        for gr in rounds:
            system = gen.generate(gr)
            if system.skip_round:
                continue

            entry = _process_round(gr, system, cache_dir)
            strat_entries.append(entry)
            all_entries.append(entry)

            # Sätt strategi-label explicit (inte det genererade strateginamnet)
            entry["strategy"] = label

            # Post-filter för specialstrategier
            if label == "I_streck_3x2":
                # Bara omgångar med exakt 3 lopp som har 2 picks
                two_pick_races = sum(1 for rp in system.race_picks if rp.num_picks == 2)
                if two_pick_races != 3:
                    strat_entries.pop()  # Ta bort entry som just lades till
                    all_entries.pop()
                    continue

            if label == "Q_dom_spread40":
                # Bara omgångar med confidence-spread 40-80
                confs = [rp.confidence for rp in system.race_picks]
                spread = max(confs) - min(confs) if confs else 0
                if not (40 <= spread <= 80):
                    strat_entries.pop()  # Ta bort entry
                    all_entries.pop()
                    continue

            if entry["payout"] > 0:
                r = entry["num_correct"]
                n = entry["num_races"]
                status = f"{'✓✓✓' if r == n else '✓'} {r}/{n}"
                print(f"  [{label[:10]}] {entry['date']} {entry['game_type']} {entry['track'][:10]} "
                      f"{status} → {entry['payout']:,.0f} kr")

        # Per-strategi summary
        full_hits = sum(1 for e in strat_entries if e["hit"])
        partial_hits = sum(1 for e in strat_entries if e["payout"] > 0 and not e["hit"])
        total_cost = sum(e["cost"] for e in strat_entries)
        total_payout = sum(e["payout"] for e in strat_entries)
        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0

        tier_counts = {}
        for e in strat_entries:
            if e["payout"] > 0:
                key = f"{e['num_correct']}_ratt"
                tier_counts[key] = tier_counts.get(key, 0) + 1

        # ROI per år
        yearly = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0})
        for e in strat_entries:
            y = yearly[e["date"][:4]]
            y["cost"] += e["cost"]
            y["payout"] += e["payout"]
            y["rounds"] += 1
            if e["payout"] > 0:
                y["wins"] += 1
            if e["hit"]:
                y["full"] += 1

        yearly_data = {}
        for year in sorted(yearly.keys()):
            y = yearly[year]
            y_roi = (y["payout"] - y["cost"]) / y["cost"] * 100 if y["cost"] > 0 else 0
            yearly_data[year] = {
                "rounds": y["rounds"], "wins": y["wins"], "full": y["full"],
                "cost": y["cost"], "payout": y["payout"],
                "roi": round(y_roi, 1), "netto": y["payout"] - y["cost"],
            }

        # ROI per månad (Jan-Dec, aggregerat över alla år)
        MONTH_NAMES = {
            '01': 'Januari', '02': 'Februari', '03': 'Mars', '04': 'April',
            '05': 'Maj', '06': 'Juni', '07': 'Juli', '08': 'Augusti',
            '09': 'September', '10': 'Oktober', '11': 'November', '12': 'December',
        }
        monthly = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0})
        for e in strat_entries:
            month = e["date"][5:7]  # "02" från "2026-02-23"
            m = monthly[month]
            m["cost"] += e["cost"]
            m["payout"] += e["payout"]
            m["rounds"] += 1
            if e["payout"] > 0:
                m["wins"] += 1
            if e["hit"]:
                m["full"] += 1

        monthly_data = {}
        for month in sorted(monthly.keys()):
            m = monthly[month]
            m_roi = (m["payout"] - m["cost"]) / m["cost"] * 100 if m["cost"] > 0 else 0
            monthly_data[month] = {
                "name": MONTH_NAMES.get(month, month),
                "rounds": m["rounds"], "wins": m["wins"], "full": m["full"],
                "cost": m["cost"], "payout": m["payout"],
                "roi": round(m_roi, 1), "netto": m["payout"] - m["cost"],
            }

        # ROI per bana
        track_data = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0})
        for e in strat_entries:
            t = track_data[e.get("track", "?")]
            t["cost"] += e["cost"]
            t["payout"] += e["payout"]
            t["rounds"] += 1
            if e["payout"] > 0:
                t["wins"] += 1
            if e["hit"]:
                t["full"] += 1

        tracks_data = {}
        for track in sorted(track_data.keys(), key=lambda t: track_data[t]["rounds"], reverse=True):
            t = track_data[track]
            if t["rounds"] < 2:
                continue
            t_roi = (t["payout"] - t["cost"]) / t["cost"] * 100 if t["cost"] > 0 else 0
            tracks_data[track] = {
                "rounds": t["rounds"], "wins": t["wins"], "full": t["full"],
                "cost": t["cost"], "payout": t["payout"],
                "roi": round(t_roi, 1), "netto": t["payout"] - t["cost"],
            }

        # ROI per speltyp
        by_game_type = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0})
        for e in strat_entries:
            gt = e.get("game_type", "V75")
            g = by_game_type[gt]
            g["cost"] += e["cost"]
            g["payout"] += e["payout"]
            g["rounds"] += 1
            if e["payout"] > 0:
                g["wins"] += 1
            if e["hit"]:
                g["full"] += 1

        game_type_data = {}
        for gt in sorted(by_game_type.keys()):
            g = by_game_type[gt]
            g_roi = (g["payout"] - g["cost"]) / g["cost"] * 100 if g["cost"] > 0 else 0
            game_type_data[gt] = {
                "rounds": g["rounds"], "wins": g["wins"], "full": g["full"],
                "cost": g["cost"], "payout": g["payout"],
                "roi": round(g_roi, 1), "netto": g["payout"] - g["cost"],
            }

        # ROI per speltyp per år (korsad dimension)
        yearly_by_gt = defaultdict(lambda: defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0}))
        for e in strat_entries:
            gt = e.get("game_type", "V75")
            y = e["date"][:4]
            g = yearly_by_gt[gt][y]
            g["cost"] += e["cost"]
            g["payout"] += e["payout"]
            g["rounds"] += 1
            if e["payout"] > 0:
                g["wins"] += 1
            if e["hit"]:
                g["full"] += 1

        yearly_gt_data = {}
        for gt in sorted(yearly_by_gt.keys()):
            yearly_gt_data[gt] = {}
            for year in sorted(yearly_by_gt[gt].keys()):
                y = yearly_by_gt[gt][year]
                y_roi = (y["payout"] - y["cost"]) / y["cost"] * 100 if y["cost"] > 0 else 0
                yearly_gt_data[gt][year] = {
                    "rounds": y["rounds"], "wins": y["wins"], "full": y["full"],
                    "cost": y["cost"], "payout": y["payout"],
                    "roi": round(y_roi, 1), "netto": y["payout"] - y["cost"],
                }

        strategy_summaries[label] = {
            "strategy": label,
            "filter": filt,
            "budget": budget,
            "rounds_played": len(strat_entries),
            "full_hits": full_hits,
            "partial_hits": partial_hits,
            "total_hits": full_hits + partial_hits,
            "tier_counts": tier_counts,
            "total_cost": total_cost,
            "total_payout": total_payout,
            "roi": round(roi, 1),
            "netto": total_payout - total_cost,
            "yearly": yearly_data,
            "tracks": tracks_data,
            "game_types": game_type_data,
            "yearly_by_game_type": yearly_gt_data,
            "monthly": monthly_data,
        }

        # Print per-speltyp breakdown
        gt_parts = []
        for gt in sorted(game_type_data.keys()):
            g = game_type_data[gt]
            gt_parts.append(f"{gt}={g['rounds']}omg/{g['roi']:+.0f}%")
        gt_str = " | ".join(gt_parts)

        print(f"\n  {label}: {len(strat_entries)} omg | "
              f"Full: {full_hits} | Del: {partial_hits} | "
              f"ROI: {roi:+.1f}% | Netto: {total_payout - total_cost:+,.0f} kr")
        print(f"    Per speltyp: {gt_str}\n")

    # Sortera nyast först
    all_entries.sort(key=lambda e: (e["date"], e["strategy"]), reverse=True)

    output = {
        "generated": str(date.today()),
        "strategies": strategy_summaries,
        "entries": all_entries,
    }

    with open("backlog.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Sparat till backlog.json ({len(all_entries)} entries)")


if __name__ == "__main__":
    asyncio.run(main())
