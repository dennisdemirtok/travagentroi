#!/usr/bin/env python3
"""Modellförbättrings-analys: VAR kan modellen tillföra mer?

Hybrid 20/80 matchar marknad (40.3% rank-1).
Men vår modell tillför BARA 0.2% utöver ren marknad.

Denna analys identifierar:
1. Vilka lopp modellen MISSAR som den BORDE fånga
2. Vilka faktorer som är "brus" (sänker modellen)
3. Nya signaler vi saknar
4. Potentiella förbättringar med uppskattad effekt
"""

import json
import glob
import statistics
from collections import defaultdict
from pathlib import Path


def load_data():
    """Ladda backtest + marknad."""
    files = [
        "backtest_results/backtest_V75_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V85_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V86_2024-01-01_2026-04-01.json",
    ]

    game_map = {}
    for gf in glob.glob("cache/V75_*.json") + glob.glob("cache/V85_*.json") + \
              glob.glob("cache/V86_*.json"):
        try:
            with open(gf) as f:
                gdata = json.load(f)
            gid = gdata.get("id", "")
            parts = gid.split("_")
            if len(parts) < 2:
                continue
            gt, gdate = parts[0], parts[1]
            for i, race in enumerate(gdata.get("races", [])):
                bd = {}
                for s in race.get("starts", []):
                    num = s.get("number", 0)
                    vpool = s.get("pools", {}).get(gt, {})
                    bval = vpool.get("betDistribution", 0)
                    if bval > 0:
                        bd[num] = bval / 100.0
                game_map[(gt, gdate, i)] = bd
        except Exception:
            continue

    races = []
    for fp in files:
        p = Path(fp)
        if not p.exists():
            continue
        with open(p) as f:
            data = json.load(f)
        gt = data.get("game_type", "?")
        for rd in data["predictions"]:
            ds = rd.get("date", "?")
            for li, race in enumerate(rd["races"]):
                actual = race.get("actual_result", [])
                ranking = race.get("predicted_ranking", [])
                scores = race.get("horse_scores", {})
                factors = race.get("horse_factor_scores", {})
                if not actual or not ranking:
                    continue
                winner = actual[0]
                bd = game_map.get((gt, ds, li), {})

                # Market ranking
                if bd:
                    market_ranking = sorted(bd.keys(), key=lambda h: bd.get(h, 0), reverse=True)
                else:
                    market_ranking = []

                try:
                    model_pos = ranking.index(winner) + 1
                except ValueError:
                    continue

                market_pos = None
                if market_ranking:
                    try:
                        market_pos = market_ranking.index(winner) + 1
                    except ValueError:
                        pass

                races.append({
                    "date": ds, "game_type": gt, "winner": winner,
                    "model_pos": model_pos, "market_pos": market_pos,
                    "model_ranking": ranking, "market_ranking": market_ranking,
                    "model_scores": {int(k): v for k, v in scores.items()},
                    "factors": factors, "bet_dist": bd,
                    "winner_streck": bd.get(winner, 0),
                    "n_horses": len(ranking),
                })
    return races


def analyze_factor_noise(races):
    """Identifiera faktorer som SKADAR modellen (brus)."""
    print("=" * 70)
    print("1. FAKTOR-KVALITET: SIGNAL vs BRUS")
    print("=" * 70)

    # För varje faktor: hur ofta rankar den VINNAREN bättre
    # (lägre rank) än modellens composite?
    factor_vs_composite = defaultdict(lambda: {"helps": 0, "hurts": 0, "neutral": 0})

    for r in races:
        winner_str = str(r["winner"])
        if winner_str not in r["factors"]:
            continue

        composite_pos = r["model_pos"]

        for factor_name in r["factors"][winner_str].keys():
            # Ranka alla hästar efter ENBART denna faktor
            f_scores = {}
            for h, fs in r["factors"].items():
                if factor_name in fs:
                    f_scores[int(h)] = fs[factor_name]

            if r["winner"] not in f_scores or len(f_scores) < 3:
                continue

            f_ranking = sorted(f_scores.keys(), key=lambda h: f_scores[h], reverse=True)
            try:
                f_pos = f_ranking.index(r["winner"]) + 1
            except ValueError:
                continue

            if f_pos < composite_pos:
                factor_vs_composite[factor_name]["helps"] += 1
            elif f_pos > composite_pos:
                factor_vs_composite[factor_name]["hurts"] += 1
            else:
                factor_vs_composite[factor_name]["neutral"] += 1

    print(f"\n  {'Faktor':<25} {'Hjälper':>8} {'Skadar':>8} {'Net':>8} {'Signal?':>8}")
    print(f"  {'─'*25} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

    sorted_factors = sorted(
        factor_vs_composite.items(),
        key=lambda x: x[1]["helps"] - x[1]["hurts"],
        reverse=True
    )

    for name, stats in sorted_factors:
        h = stats["helps"]
        hu = stats["hurts"]
        total = h + hu + stats["neutral"]
        net = h - hu
        pct = net / total * 100 if total > 0 else 0
        signal = "✓ SIGNAL" if net > 50 else "~ svag" if net > 0 else "✗ BRUS"
        print(f"  {name:<25} {h:>7} {hu:>7} {net:>+7} {signal:>8}")


def analyze_model_unique_edge(races):
    """Hitta lopp där modellen hittar vinnaren men marknad missar."""
    print()
    print("=" * 70)
    print("2. MODELLENS UNIKA EDGE: NÄR LYCKAS VI MEN MARKNAD MISSAR?")
    print("=" * 70)

    model_wins = []  # Model top-3, market rank 5+
    model_rank1_market_miss = []  # Model rank 1, market rank 3+

    for r in races:
        if r["market_pos"] is None:
            continue
        if r["model_pos"] <= 3 and r["market_pos"] >= 5:
            model_wins.append(r)
        if r["model_pos"] == 1 and r["market_pos"] >= 3:
            model_rank1_market_miss.append(r)

    print(f"\n  Modell top-3, marknad rank 5+: {len(model_wins)} lopp")
    print(f"  Modell rank 1, marknad rank 3+: {len(model_rank1_market_miss)} lopp")

    if model_wins:
        # Vilka faktorer driver dessa edge-lopp?
        edge_factor_profile = defaultdict(list)
        for r in model_wins:
            winner_str = str(r["winner"])
            if winner_str in r["factors"]:
                for fname, fval in r["factors"][winner_str].items():
                    edge_factor_profile[fname].append(fval)

        print(f"\n  Faktorprofil för modellens edge-lopp (medel):")
        for fname, vals in sorted(edge_factor_profile.items(),
                                   key=lambda x: statistics.mean(x[1]), reverse=True):
            avg = statistics.mean(vals)
            print(f"    {fname:<25} {avg:>6.1f}")

    # Omvänt: marknad top-3, modell rank 5+
    market_wins = [r for r in races if r["market_pos"] and r["market_pos"] <= 3 and r["model_pos"] >= 5]
    print(f"\n  Marknad top-3, modell rank 5+: {len(market_wins)} lopp (marknaden hittar, vi missar)")

    if market_wins:
        miss_factor_profile = defaultdict(list)
        for r in market_wins:
            winner_str = str(r["winner"])
            if winner_str in r["factors"]:
                for fname, fval in r["factors"][winner_str].items():
                    miss_factor_profile[fname].append(fval)

        print(f"\n  Faktorprofil för modellens MISSAR (medel):")
        for fname, vals in sorted(miss_factor_profile.items(),
                                   key=lambda x: statistics.mean(x[1]), reverse=True):
            avg = statistics.mean(vals)
            print(f"    {fname:<25} {avg:>6.1f}")


def analyze_weak_factors(races):
    """Vilka faktorer har lägst vikt och kan tas bort/förbättras?"""
    print()
    print("=" * 70)
    print("3. SVAGA FAKTORER: BORT ELLER FÖRBÄTTRA?")
    print("=" * 70)

    # Nuvarande vikter (från config.py)
    weights = {
        "post_position": 0.157,
        "prize_index": 0.156,
        "age": 0.146,
        "time_analysis": 0.130,
        "competition_strength": 0.102,
        "track_profile": 0.074,
        "driver_class": 0.062,
        "category_profile": 0.062,
        "last_win": 0.049,
        "form_curve": 0.049,
        "gallop_risk": 0.035,
        "equipment": 0.005,
        "driver_trainer": 0.005,
        "layoff": 0.003,
    }

    # Beräkna single-factor rank-1 accuracy
    factor_accuracy = {}
    for fname in weights:
        r1 = 0
        total = 0
        for r in races:
            f_scores = {}
            for h, fs in r["factors"].items():
                if fname in fs:
                    f_scores[int(h)] = fs[fname]
            if r["winner"] not in f_scores or len(f_scores) < 3:
                continue
            total += 1
            f_ranking = sorted(f_scores.keys(), key=lambda h: f_scores[h], reverse=True)
            if f_ranking[0] == r["winner"]:
                r1 += 1
        if total > 0:
            factor_accuracy[fname] = r1 / total * 100

    print(f"\n  {'Faktor':<25} {'Vikt':>6} {'Rank-1':>8} {'Signal/kr':>10} {'Åtgärd'}")
    print(f"  {'─'*25} {'─'*6} {'─'*8} {'─'*10} {'─'*20}")

    # Signal/kr = accuracy per vikt-punkt (effektivitet)
    for fname, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
        acc = factor_accuracy.get(fname, 0)
        efficiency = acc / (w * 100) if w > 0 else 0
        if w <= 0.01:
            action = "❌ Ta bort / ersätt"
        elif acc < 18:
            action = "⚠️ Förbättra"
        else:
            action = "✅ Behåll"
        print(f"  {fname:<25} {w:>5.1%} {acc:>7.1f}% {efficiency:>9.1f}   {action}")


def analyze_missing_signals(races):
    """Identifiera signaler som saknas i modellen."""
    print()
    print("=" * 70)
    print("4. SAKNADE SIGNALER (vad missar modellen?)")
    print("=" * 70)

    # Analysera lopp där modellen tappar: vinnaren har LAG modellscore
    # men VÅN ändå. Vad kännetecknar dessa?
    big_misses = [r for r in races if r["model_pos"] >= 6]

    print(f"\n  Lopp där modellen rankar vinnaren 6+: {len(big_misses)}")

    if big_misses:
        # Streck-distribution för dessa missar
        streck_buckets = defaultdict(int)
        for r in big_misses:
            ws = r["winner_streck"]
            if ws >= 20:
                streck_buckets["≥20% favorit"] += 1
            elif ws >= 10:
                streck_buckets["10-20%"] += 1
            elif ws >= 5:
                streck_buckets["5-10%"] += 1
            else:
                streck_buckets["<5% outsider"] += 1

        print(f"\n  Streckfördelning för modellens stora missar:")
        for bucket, count in sorted(streck_buckets.items()):
            bar = "█" * (count // 3)
            print(f"    {bucket:<20} {count:>4} {bar}")

        # Genomsnittlig faktor-score för missade vinnare
        print(f"\n  Faktorprofil för missade vinnare (rank 6+):")
        miss_profiles = defaultdict(list)
        for r in big_misses:
            ws = str(r["winner"])
            if ws in r["factors"]:
                for fn, fv in r["factors"][ws].items():
                    miss_profiles[fn].append(fv)

        for fn, vals in sorted(miss_profiles.items(),
                                key=lambda x: statistics.mean(x[1])):
            avg = statistics.mean(vals)
            label = "← LÅG" if avg < 40 else "← OK" if avg < 55 else "← BRA"
            print(f"    {fn:<25} {avg:>6.1f} {label}")

    # Saknade signaler
    print(f"""
  SAKNADE SIGNALER SOM KUNDE HJÄLPA:

  1. 🏋️ TRÄNARE-EDGE
     Gocciadoro + utländska tränare i sommarmånader
     Uppskattad effekt: +1-3% rank-1 (50-100 lopp/år)

  2. 🏆 PROFFS-KONSENSUS
     Viktat proffsstreck från 301 proffs (kungenstrav.se)
     Uppskattad effekt: +2-5% rank-1 (starkare signal än modell)

  3. 📊 STRECKKURVA (streck-trend)
     Hur streck ÄNDRAS under speldagen (stigande = pengar in)
     Kräver: realtids-streck-snapshots
     Uppskattad effekt: +1-2% rank-1

  4. 💰 UTDELNINGS-PREDIKTION
     Predicera utdelningsstorlek → optimera systemkostnad
     Effekt: ROI-förbättring utan ranking-förbättring

  5. 🐴 HÄST-FORM vs FÄLT-STYRKA
     Normalisera form mot fältets samlade styrka
     (vinna mot svagt fält ≠ vinna mot starkt fält)
     Uppskattad effekt: +1-2% rank-1

  6. 🎯 KLASS-HOPP
     Hästar som hoppar upp/ned i klass — edge vid nerhoppare
     Uppskattad effekt: +0.5-1% rank-1
""")


def summarize_improvement_plan(races):
    """Konkret förbättringsplan med prioritering."""
    print()
    print("=" * 70)
    print("═══ FÖRBÄTTRINGSPLAN (prioriterad) ═══")
    print("=" * 70)

    # Beräkna current state
    model_r1 = sum(1 for r in races if r["model_pos"] == 1) / len(races) * 100
    market_r1 = sum(1 for r in races if r["market_pos"] == 1) / len(races) * 100 if \
        any(r["market_pos"] for r in races) else 0

    total_with_market = sum(1 for r in races if r["market_pos"])
    market_r1 = sum(1 for r in races if r["market_pos"] and r["market_pos"] == 1) / total_with_market * 100

    print(f"""
  NULÄGE:
    Modell rank-1: {model_r1:.1f}%
    Marknad rank-1: {market_r1:.1f}%
    Gap: {model_r1 - market_r1:+.1f}%
    Hybrid 20/80: ~40.5% (matchar marknad)

  MÅL: Hybrid ska SLÅR marknad med 2-5%

  PRIORITERAD ÅTGÄRD                    EFFEKT        EFFORT
  ─────────────────────────────────────  ──────────    ──────
  1. Ta bort brusfaktorer (equip/lay)   +0.5%         Låg
  2. Integrera proffs-konsensus          +2-4%         Medium
  3. Tränare-edge (Gocciadoro etc)       +0.5-1%       Medium
  4. Förbättra tid-analys                +1-2%         Hög
  5. Klass-hopp signal                   +0.5-1%       Medium
  6. Fält-styrka normalisering           +1-2%         Hög

  TOTAL POTENTIELL FÖRBÄTTRING: +5-10% rank-1
  → Hybrid från 40.5% → 43-45% (slår marknad med 3-5%)
""")


if __name__ == "__main__":
    races = load_data()
    print(f"Analyserar {len(races)} lopp\n")

    analyze_factor_noise(races)
    analyze_model_unique_edge(races)
    analyze_weak_factors(races)
    analyze_missing_signals(races)
    summarize_improvement_plan(races)
