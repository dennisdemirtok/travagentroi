"""Djupanalys av skrällar — extraherar finalOdds från cachad API-data.

Matchar mot sparade backtest-resultat för att klassificera varje lopp:
- Favorit-vinst (odds < 3.0)
- Normal vinst (odds 3-7)
- Mild skräll (odds 7-15)
- Stor skräll (odds 15-30)
- Extrem skräll (odds > 30)

Analyserar vilka faktorpoäng som skiljer skrällar från favoriter.
"""

import json
import math
import numpy as np
from pathlib import Path
from collections import defaultdict
from rich.console import Console
from rich.table import Table

console = Console()
CACHE_DIR = Path("cache")
BACKTEST_DIR = Path("backtest_results")


def extract_odds_from_cache() -> dict[str, dict[int, float]]:
    """Scanna alla cache-filer och extrahera finalOdds per race+häst.

    Returns: {race_id: {horse_number: finalOdds}}
    """
    race_odds: dict[str, dict[int, float]] = {}

    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except:
            continue

        # Race-data har "starts" med "result" som har "finalOdds"
        starts = data.get("starts", [])
        if not starts or not isinstance(starts, list):
            continue

        # Kolla om detta ser ut som race-data
        race_id = data.get("id", "")
        if not race_id:
            continue

        horse_odds = {}
        for start in starts:
            num = start.get("number", 0)
            result = start.get("result", {})
            if result and isinstance(result, dict):
                final_odds = result.get("finalOdds")
                if final_odds and isinstance(final_odds, (int, float)):
                    horse_odds[num] = float(final_odds)

        if horse_odds:
            race_odds[race_id] = horse_odds

    return race_odds


def load_backtest_races():
    """Ladda alla unika lopp från backtest-filer."""
    seen = set()
    races = []

    for f in sorted(BACKTEST_DIR.glob("backtest_*.json")):
        data = json.loads(f.read_text())
        for rp in data.get("predictions", []):
            for race in rp.get("races", []):
                key = (race.get("date"), race.get("race_number"))
                if key in seen:
                    continue
                seen.add(key)

                actual = race.get("actual_result", [])
                hfs = race.get("horse_factor_scores", {})

                if actual and hfs:
                    races.append({
                        "date": race.get("date"),
                        "race_number": race.get("race_number"),
                        "track": race.get("track", ""),
                        "actual_result": actual,
                        "horse_factor_scores": {int(k): v for k, v in hfs.items()},
                        "horse_scores": {int(k): float(v) for k, v in race.get("horse_scores", {}).items()},
                    })

    return races


def match_odds_to_races(races, race_odds_by_id):
    """Matcha odds-data till backtest-lopp via cache-scanning.

    Vi scannar alla cachade game-filer för att hitta race_id → datum+avd mappning.
    """
    # Bygg race_id → (date, race_number) mappning från game-cache
    race_id_map: dict[str, tuple[str, int]] = {}

    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except:
            continue

        # Game-data har "races" med "id" per lopp
        game_races = data.get("races", [])
        if not game_races or not isinstance(game_races, list):
            continue

        # Kolla om det är game-data (har tracks etc)
        if "tracks" not in data and "@type" not in str(data.get("races", [{}])[0]):
            continue

        game_date = ""
        # Extrahera datum från game_id (t.ex. "V85_2026-02-14_6_5")
        game_id = data.get("id", "")
        parts = game_id.split("_")
        if len(parts) >= 2:
            game_date = parts[1]

        for i, race_data in enumerate(game_races, start=1):
            race_id = race_data.get("id", "")
            if race_id and game_date:
                race_id_map[race_id] = (game_date, i)

    # Nu matcha odds till races
    matched = 0
    for race in races:
        race_key = (race["date"], race["race_number"])

        # Hitta matchande race_id
        for race_id, (r_date, r_num) in race_id_map.items():
            if r_date == race["date"] and r_num == race["race_number"]:
                if race_id in race_odds_by_id:
                    race["odds"] = race_odds_by_id[race_id]
                    matched += 1
                break

    return matched


def classify_upset(winner_odds: float) -> str:
    """Klassificera resultat baserat på vinnarodds."""
    if winner_odds < 3.0:
        return "Favorit"
    elif winner_odds < 7.0:
        return "Normal"
    elif winner_odds < 15.0:
        return "Mild skräll"
    elif winner_odds < 30.0:
        return "Stor skräll"
    else:
        return "Extrem skräll"


def main():
    console.print("\n[bold cyan]═══ SKRÄLLANALYS — finalOdds × faktorpoäng ═══[/bold cyan]\n")

    # 1. Extrahera odds från cache
    console.print("Extraherar finalOdds från cache...")
    race_odds = extract_odds_from_cache()
    console.print(f"  Hittade odds för [bold]{len(race_odds)}[/bold] lopp\n")

    # 2. Ladda backtest-data
    races = load_backtest_races()
    console.print(f"Laddade [bold]{len(races)}[/bold] backtest-lopp\n")

    # 3. Matcha
    matched = match_odds_to_races(races, race_odds)
    console.print(f"Matchade odds till [bold]{matched}[/bold] lopp\n")

    # 4. Analysera
    races_with_odds = [r for r in races if "odds" in r]

    if not races_with_odds:
        console.print("[red]Kunde inte matcha odds till backtest-lopp![/red]")
        console.print("Testar direkt odds-extraction istället...\n")

        # Alternativ: extrahera allt direkt från cache
        # Scanna alla race-cache filer och bygg analys direkt
        direct_races = []

        for f in CACHE_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except:
                continue

            starts = data.get("starts", [])
            if not starts or not isinstance(starts, list):
                continue

            race_id = data.get("id", "")
            if not race_id:
                continue

            # Samla odds och resultat
            horse_data = []
            for start in starts:
                num = start.get("number", 0)
                result = start.get("result", {})
                horse_info = start.get("horse", {})

                if result and isinstance(result, dict):
                    place = result.get("place")
                    final_odds = result.get("finalOdds")

                    if place and final_odds:
                        horse_data.append({
                            "number": num,
                            "place": place,
                            "odds": float(final_odds),
                            "name": horse_info.get("name", "?"),
                        })

            if horse_data and any(h["place"] == 1 for h in horse_data):
                winner = [h for h in horse_data if h["place"] == 1][0]
                direct_races.append({
                    "race_id": race_id,
                    "winner_odds": winner["odds"],
                    "winner_name": winner["name"],
                    "winner_number": winner["number"],
                    "all_horses": horse_data,
                    "category": classify_upset(winner["odds"]),
                })

        console.print(f"Extraherade [bold]{len(direct_races)}[/bold] lopp direkt från cache\n")

        if not direct_races:
            return

        # Distributionsanalys
        categories = defaultdict(list)
        for r in direct_races:
            categories[r["category"]].append(r)

        table = Table(title=f"Resultatfördelning ({len(direct_races)} lopp)", show_lines=True)
        table.add_column("Kategori", style="cyan", min_width=15)
        table.add_column("Antal", justify="right")
        table.add_column("Andel", justify="right")
        table.add_column("Snittodds", justify="right")
        table.add_column("Medianodds", justify="right")

        order = ["Favorit", "Normal", "Mild skräll", "Stor skräll", "Extrem skräll"]
        for cat in order:
            rs = categories.get(cat, [])
            if not rs:
                continue
            odds_list = [r["winner_odds"] for r in rs]
            table.add_row(
                cat,
                str(len(rs)),
                f"{len(rs)/len(direct_races):.1%}",
                f"{np.mean(odds_list):.1f}",
                f"{np.median(odds_list):.1f}",
            )

        console.print(table)

        # Nu matcha mot backtest-data manuellt
        # Vi kan matcha via race_id
        console.print("\n[bold yellow]Matchar odds mot faktorpoäng...[/bold yellow]\n")

        # Bygg race_id → odds mapping
        odds_by_race_id = {r["race_id"]: r for r in direct_races}

        # Scanna game-cache för race_id → (date, race_num) mapping
        race_to_datenum = {}
        for f in CACHE_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except:
                continue

            game_id = data.get("id", "")
            if "_" not in game_id:
                continue
            parts = game_id.split("_")
            if len(parts) < 2:
                continue
            game_date = parts[1]

            for i, rd in enumerate(data.get("races", []), start=1):
                rid = rd.get("id", "")
                if rid:
                    race_to_datenum[rid] = (game_date, i)

        # Nu matcha
        matched_count = 0
        for race in races:
            rkey = (race["date"], race["race_number"])
            for rid, (d, n) in race_to_datenum.items():
                if d == race["date"] and n == race["race_number"]:
                    if rid in odds_by_race_id:
                        odds_info = odds_by_race_id[rid]
                        race["winner_odds"] = odds_info["winner_odds"]
                        race["upset_category"] = odds_info["category"]
                        # Spara alla hästars odds
                        race["all_odds"] = {
                            h["number"]: h["odds"]
                            for h in odds_info["all_horses"]
                        }
                        matched_count += 1
                    break

        console.print(f"Matchade [bold]{matched_count}[/bold] lopp med faktorpoäng\n")

        races_matched = [r for r in races if "winner_odds" in r]

        if not races_matched:
            console.print("[red]Ingen matchning lyckades[/red]")
            return

        analyze_factors_by_category(races_matched)
        analyze_what_predicts_upsets(races_matched)
        analyze_model_vs_upsets(races_matched)


def analyze_factors_by_category(races):
    """Analysera faktorpoäng per resultatkategori."""
    console.print("[bold yellow]Faktorpoäng per resultatkategori[/bold yellow]\n")

    factors = ["track_profile", "category_profile", "prize_index",
               "post_position", "form_curve", "time_analysis", "driver_trainer"]

    categories = defaultdict(list)
    for race in races:
        cat = race.get("upset_category", "?")
        winner = race["actual_result"][0]
        winner_fs = race["horse_factor_scores"].get(winner, {})
        if winner_fs:
            categories[cat].append(winner_fs)

    table = Table(title="Vinnarens faktorpoäng per kategori", show_lines=True)
    table.add_column("Faktor", style="cyan")
    for cat in ["Favorit", "Normal", "Mild skräll", "Stor skräll", "Extrem skräll"]:
        if cat in categories:
            table.add_column(f"{cat}\n(n={len(categories[cat])})", justify="right")

    for fn in factors:
        row = [fn]
        for cat in ["Favorit", "Normal", "Mild skräll", "Stor skräll", "Extrem skräll"]:
            if cat not in categories:
                continue
            vals = [fs.get(fn, 50) for fs in categories[cat]]
            avg = np.mean(vals)
            row.append(f"{avg:.1f}")
        table.add_row(*row)

    console.print(table)

    # Beräkna skillnad skräll vs favorit
    console.print("\n[bold yellow]Δ Skräll vs Favorit (medelpoäng)[/bold yellow]\n")

    fav_scores = categories.get("Favorit", [])
    upset_scores = categories.get("Mild skräll", []) + categories.get("Stor skräll", []) + categories.get("Extrem skräll", [])

    if fav_scores and upset_scores:
        for fn in factors:
            fav_avg = np.mean([fs.get(fn, 50) for fs in fav_scores])
            ups_avg = np.mean([fs.get(fn, 50) for fs in upset_scores])
            delta = ups_avg - fav_avg
            style = "green" if delta > 5 else ("red" if delta < -5 else "dim")
            console.print(f"  {fn:<20} Favorit={fav_avg:5.1f}  Skräll={ups_avg:5.1f}  [{style}]Δ={delta:+5.1f}[/{style}]")


def analyze_what_predicts_upsets(races):
    """Hitta vad som skiljer skrällar — vad hade de som modellen missar?"""
    console.print("\n[bold yellow]Vad karaktäriserar skrällvinnare?[/bold yellow]\n")

    # Jämför vinnarens rank (enligt vår modell) vs faktiskt resultat
    rank_by_category = defaultdict(list)

    for race in races:
        cat = race.get("upset_category", "?")
        winner = race["actual_result"][0]

        # Vinnarens rank i vår modell
        scores = race["horse_scores"]
        ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winner_rank = next((i+1 for i, (h, _) in enumerate(ranking) if h == winner), 99)

        rank_by_category[cat].append(winner_rank)

    console.print("Vinnarens rank i vår modell:")
    for cat in ["Favorit", "Normal", "Mild skräll", "Stor skräll", "Extrem skräll"]:
        if cat not in rank_by_category:
            continue
        ranks = rank_by_category[cat]
        avg_rank = np.mean(ranks)
        top3_pct = sum(1 for r in ranks if r <= 3) / len(ranks)
        console.print(f"  {cat:<15} Snittrank={avg_rank:.1f}  I vår Top-3={top3_pct:.0%}  (n={len(ranks)})")

    # Vad HAR skrällar som andra inte har?
    console.print("\n[bold]Detaljanalys: Mild + Stor + Extrem skräll[/bold]\n")

    upsets = [r for r in races if r.get("upset_category") in ("Mild skräll", "Stor skräll", "Extrem skräll")]

    if not upsets:
        console.print("  Inga skrällar hittade")
        return

    # Vinnarens STARKASTE faktor i skräll-lopp
    strongest_factor_counts = defaultdict(int)
    for race in upsets:
        winner = race["actual_result"][0]
        fs = race["horse_factor_scores"].get(winner, {})
        if fs:
            best_factor = max(fs, key=fs.get)
            strongest_factor_counts[best_factor] += 1

    console.print("  Starkaste faktor hos skrällvinnare:")
    for fn, count in sorted(strongest_factor_counts.items(), key=lambda x: -x[1]):
        pct = count / len(upsets)
        console.print(f"    {fn:<20} {count:>3} ({pct:.0%})")

    # Vinnarens relativa position per faktor vs fältet
    console.print("\n  Skrällvinnarens ranking per faktor:")

    factors = ["track_profile", "category_profile", "prize_index",
               "post_position", "form_curve", "time_analysis", "driver_trainer"]

    for fn in factors:
        top1 = 0
        top3 = 0
        for race in upsets:
            winner = race["actual_result"][0]
            factor_scores = {}
            for h, fs in race["horse_factor_scores"].items():
                if fn in fs:
                    factor_scores[h] = fs[fn]
            if not factor_scores:
                continue
            ranking = sorted(factor_scores.items(), key=lambda x: x[1], reverse=True)
            winner_rank = next((i+1 for i, (h, _) in enumerate(ranking) if h == winner), 99)
            if winner_rank == 1: top1 += 1
            if winner_rank <= 3: top3 += 1

        n = len(upsets)
        console.print(f"    {fn:<20} Top-1={top1/n:.0%}  Top-3={top3/n:.0%}")


def analyze_model_vs_upsets(races):
    """Hur bra är vår modell på att identifiera skrällar?"""
    console.print("\n[bold yellow]Modellens precision per skräll-kategori[/bold yellow]\n")

    factors = ["track_profile", "category_profile", "prize_index",
               "post_position", "form_curve", "time_analysis", "driver_trainer"]

    NEW_W = {'time_analysis': 0.09, 'prize_index': 0.13, 'form_curve': 0.07,
        'track_profile': 0.33, 'category_profile': 0.15, 'driver_trainer': 0.07, 'post_position': 0.16}
    interactions = [('track_profile', 'post_position', 0.15), ('track_profile', 'category_profile', 0.10)]

    def sig(raw, k=0.08):
        return 100.0 / (1.0 + math.exp(-k * (raw - 50.0)))

    by_cat = defaultdict(lambda: {"t1": 0, "t3": 0, "t5": 0, "n": 0})

    total_w = sum(NEW_W.values())
    wn = {k: v/total_w for k, v in NEW_W.items()}

    for race in races:
        cat = race.get("upset_category", "?")
        winner = race["actual_result"][0]

        comps = {}
        for h, fs in race["horse_factor_scores"].items():
            raw = sum(fs.get(fn, 50.0) * wt for fn, wt in wn.items())
            for f1, f2, iw in interactions:
                v1 = fs.get(f1, 50.0) / 100.0
                v2 = fs.get(f2, 50.0) / 100.0
                raw += v1 * v2 * 100.0 * iw
            comps[h] = sig(raw)

        ranking = sorted(comps.items(), key=lambda x: x[1], reverse=True)
        winner_rank = next((i+1 for i, (h, _) in enumerate(ranking) if h == winner), 99)

        by_cat[cat]["n"] += 1
        if winner_rank <= 1: by_cat[cat]["t1"] += 1
        if winner_rank <= 3: by_cat[cat]["t3"] += 1
        if winner_rank <= 5: by_cat[cat]["t5"] += 1

    table = Table(title="V4-modellens precision per kategori", show_lines=True)
    table.add_column("Kategori", style="cyan")
    table.add_column("N", justify="right")
    table.add_column("Top-1", justify="right")
    table.add_column("Top-3", justify="right")
    table.add_column("Top-5", justify="right")

    for cat in ["Favorit", "Normal", "Mild skräll", "Stor skräll", "Extrem skräll"]:
        d = by_cat.get(cat)
        if not d or d["n"] == 0:
            continue
        n = d["n"]
        table.add_row(
            cat,
            str(n),
            f"{d['t1']/n:.0%}",
            f"{d['t3']/n:.0%}",
            f"{d['t5']/n:.0%}",
        )

    console.print(table)


if __name__ == "__main__":
    main()
