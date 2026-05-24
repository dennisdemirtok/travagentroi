#!/usr/bin/env python3
"""Modell vs Marknad — slår vår modell streckprocenten?

Hämtar slutodds (finalOdds) från cachade lopp och jämför
modellens ranking med marknadens ranking (implicit streckprocent).

Dennis: "Slår ens min modell vs streckprocenten?"
"""

import json
import glob
import statistics
from collections import defaultdict
from pathlib import Path


def load_backtest_races():
    """Ladda alla backtest-lopp med prediktion + utfall."""
    files = [
        "backtest_results/backtest_V75_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V85_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V86_2024-01-01_2026-04-01.json",
    ]
    races = []
    for fp in files:
        p = Path(fp)
        if not p.exists():
            continue
        with open(p) as f:
            data = json.load(f)
        game_type = data.get("game_type", "?")
        for rd in data["predictions"]:
            date = rd.get("date", "?")
            for race in rd["races"]:
                race["_game_type"] = game_type
                race["_round_date"] = date
                races.append(race)
    return races


def main():
    races = load_backtest_races()
    print(f"Backtest-lopp: {len(races)}")

    matched = []
    no_odds = 0

    for race in races:
        date = race.get("_round_date", "")
        track = race.get("track", "?")
        race_num = race.get("race_number", 0)
        actual = race.get("actual_result", [])
        ranking = race.get("predicted_ranking", [])

        if not actual or not ranking:
            continue

        winner = actual[0]

        # Hitta odds i cache
        odds = None
        for track_pattern in glob.glob(f"cache/{date}_*_{race_num}_*.json"):
            try:
                with open(track_pattern) as f:
                    data = json.load(f)
                if data.get("status") != "results":
                    continue

                race_winner = None
                horse_odds = {}
                for s in data.get("starts", []):
                    result = s.get("result", {})
                    num = s.get("number", 0)
                    if result and "finalOdds" in result:
                        horse_odds[num] = result["finalOdds"]
                        if result.get("place") == 1:
                            race_winner = num

                if race_winner == winner and horse_odds:
                    odds = horse_odds
                    break
            except Exception:
                continue

        if not odds:
            no_odds += 1
            continue

        market_ranking = sorted(
            [h for h, o in odds.items() if o > 0],
            key=lambda h: odds[h]
        )
        model_ranking = ranking

        try:
            model_pos = model_ranking.index(winner) + 1
        except ValueError:
            continue
        try:
            market_pos = market_ranking.index(winner) + 1
        except ValueError:
            continue

        matched.append({
            "date": date,
            "track": track,
            "race_num": race_num,
            "winner": winner,
            "model_pos": model_pos,
            "market_pos": market_pos,
            "model_ranking": model_ranking,
            "market_ranking": market_ranking,
            "winner_odds": odds.get(winner, 0),
            "n_horses": len(model_ranking),
            "game_type": race.get("_game_type", "?"),
        })

    print(f"Matchade med odds: {len(matched)}")
    print(f"Utan odds (ej i cache): {no_odds}")
    print()

    if not matched:
        print("Inga matchade lopp!")
        return

    # === JÄMFÖRELSE ===
    print("=" * 70)
    print("MODELL VS MARKNAD (slutodds)")
    print("=" * 70)

    for k_label, k_val in [("Rank 1", 1), ("Top 2", 2), ("Top 3", 3), ("Top 4", 4), ("Top 5", 5)]:
        model_hits = sum(1 for r in matched if r["model_pos"] <= k_val)
        market_hits = sum(1 for r in matched if r["market_pos"] <= k_val)
        total = len(matched)
        model_pct = model_hits / total * 100
        market_pct = market_hits / total * 100
        diff = model_pct - market_pct
        w = "MODELL" if diff > 0 else "MARKNAD" if diff < 0 else "LIKA"
        print(f"  {k_label:>6}: Modell {model_pct:>5.1f}% ({model_hits:>4})  |  "
              f"Marknad {market_pct:>5.1f}% ({market_hits:>4})  |  "
              f"Diff {diff:>+5.1f}%  {w}")

    # Per spelform
    print()
    print("=" * 70)
    print("PER SPELFORM")
    print("=" * 70)

    for game_type in ["V75", "V85", "V86"]:
        subset = [r for r in matched if r["game_type"] == game_type]
        if not subset:
            continue
        print(f"\n  {game_type} ({len(subset)} lopp):")
        for k_val in [1, 2, 4]:
            m_h = sum(1 for r in subset if r["model_pos"] <= k_val)
            k_h = sum(1 for r in subset if r["market_pos"] <= k_val)
            t = len(subset)
            label = f"Top {k_val}" if k_val > 1 else "Rank 1"
            d = m_h/t*100 - k_h/t*100
            w = "M+" if d > 0 else "m+" if d < 0 else "="
            print(f"    {label:>6}: Modell {m_h/t*100:>5.1f}% | Marknad {k_h/t*100:>5.1f}% | {d:>+5.1f}% {w}")

    # Per-lopp breakdown
    print()
    print("=" * 70)
    print("PER LOPP: MODELL VS MARKNAD")
    print("=" * 70)

    model_better = sum(1 for r in matched if r["model_pos"] < r["market_pos"])
    market_better = sum(1 for r in matched if r["market_pos"] < r["model_pos"])
    equal = sum(1 for r in matched if r["model_pos"] == r["market_pos"])

    print(f"\n  Modell bättre rank: {model_better} ({model_better/len(matched)*100:.1f}%)")
    print(f"  Marknad bättre rank: {market_better} ({market_better/len(matched)*100:.1f}%)")
    print(f"  Samma rank: {equal} ({equal/len(matched)*100:.1f}%)")

    model_avg = statistics.mean(r["model_pos"] for r in matched)
    market_avg = statistics.mean(r["market_pos"] for r in matched)
    print(f"\n  Modellens avg rank för vinnare: {model_avg:.2f}")
    print(f"  Marknadens avg rank för vinnare: {market_avg:.2f}")
    diff_rank = model_avg - market_avg
    print(f"  Diff: {diff_rank:+.2f} ({'Modell bättre' if diff_rank < 0 else 'Marknad bättre'})")

    # Upsets
    print()
    print("=" * 70)
    print("VID UPSETS (odds >= 5.0)")
    print("=" * 70)

    upsets = [r for r in matched if r["winner_odds"] >= 5.0]
    if upsets:
        print(f"\n  Upsets: {len(upsets)} lopp")
        m4 = sum(1 for r in upsets if r["model_pos"] <= 4)
        k4 = sum(1 for r in upsets if r["market_pos"] <= 4)
        print(f"  Modell top 4: {m4} ({m4/len(upsets)*100:.1f}%)")
        print(f"  Marknad top 4: {k4} ({k4/len(upsets)*100:.1f}%)")

        mb = sum(1 for r in upsets if r["model_pos"] < r["market_pos"])
        kb = sum(1 for r in upsets if r["market_pos"] < r["model_pos"])
        print(f"  Modell bättre: {mb} ({mb/len(upsets)*100:.1f}%)")
        print(f"  Marknad bättre: {kb} ({kb/len(upsets)*100:.1f}%)")

    big_upsets = [r for r in matched if r["winner_odds"] >= 15.0]
    if big_upsets:
        print(f"\n  Stora upsets (odds >= 15): {len(big_upsets)} lopp")
        m4 = sum(1 for r in big_upsets if r["model_pos"] <= 4)
        k4 = sum(1 for r in big_upsets if r["market_pos"] <= 4)
        print(f"  Modell top 4: {m4} ({m4/len(big_upsets)*100:.1f}%)")
        print(f"  Marknad top 4: {k4} ({k4/len(big_upsets)*100:.1f}%)")

    # Korrelation
    print()
    print("=" * 70)
    print("KORRELATION MODELL/MARKNAD")
    print("=" * 70)

    mad = statistics.mean(abs(r["model_pos"] - r["market_pos"]) for r in matched)
    agree_top1 = sum(1 for r in matched if r["model_ranking"][0] == r["market_ranking"][0])
    
    overlaps = []
    for r in matched:
        m3 = set(r["model_ranking"][:3])
        k3 = set(r["market_ranking"][:3])
        overlaps.append(len(m3 & k3))

    print(f"\n  Mean absolute rank diff: {mad:.2f}")
    print(f"  Top-1 agreement: {agree_top1}/{len(matched)} ({agree_top1/len(matched)*100:.1f}%)")
    print(f"  Top-3 overlap: {statistics.mean(overlaps):.2f}/3 ({statistics.mean(overlaps)/3*100:.1f}%)")

    # Sammanfattning
    print()
    print("=" * 70)
    print("SLUTSATS")
    print("=" * 70)

    model_r1 = sum(1 for r in matched if r["model_pos"] == 1) / len(matched) * 100
    market_r1 = sum(1 for r in matched if r["market_pos"] == 1) / len(matched) * 100
    model_t4 = sum(1 for r in matched if r["model_pos"] <= 4) / len(matched) * 100
    market_t4 = sum(1 for r in matched if r["market_pos"] <= 4) / len(matched) * 100

    print(f"\n  Rank 1:  Modell {model_r1:.1f}%  vs  Marknad {market_r1:.1f}%  ({model_r1 - market_r1:+.1f}%)")
    print(f"  Top 4:   Modell {model_t4:.1f}%  vs  Marknad {market_t4:.1f}%  ({model_t4 - market_t4:+.1f}%)")
    print(f"  Avg rank: Modell {model_avg:.2f}  vs  Marknad {market_avg:.2f}  ({diff_rank:+.2f})")
    print()

    if model_avg < market_avg and model_r1 > market_r1:
        print("  MODELLEN SLÅR MARKNADEN på BÅDA mått")
    elif model_avg < market_avg:
        print("  Modellen har bättre avg rank men sämre rank-1")
    elif model_r1 > market_r1:
        print("  Modellen har bättre rank-1 men sämre avg rank")
    else:
        print("  MARKNADEN SLÅR MODELLEN — behöver förbättring")


if __name__ == "__main__":
    main()
