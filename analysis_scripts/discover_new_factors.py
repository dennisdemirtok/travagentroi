"""Djupanalys: Hitta nya prediktiva signaler i 845+ lopp av backtest-data.

Laddar ALLA sparade backtest-data och undersöker vilka nya mönster
som korrelerar med vinst men inte redan fångas av de 7 befintliga faktorerna.

Nya kandidater att testa:
- Senaste start seger (momentum)
- Galoppfrekvens (pålitlighet)
- Klassdropp/höjning
- Vilodagar (optimalt intervall)
- Distansspecialist
- Antal startande (field size)
- Streckprocent som signal
"""

import json
import math
import numpy as np
from pathlib import Path
from collections import defaultdict
from rich.console import Console
from rich.table import Table

console = Console()

# ── Hjälp-funktioner ──────────────────────────────────────────────

def point_biserial(scores: list[float], binary: list[float]) -> float:
    if len(scores) < 10:
        return 0.0
    x = np.array(scores, dtype=np.float64)
    y = np.array(binary, dtype=np.float64)
    n1 = y.sum()
    n0 = len(y) - n1
    if n0 == 0 or n1 == 0:
        return 0.0
    mean1 = x[y == 1].mean()
    mean0 = x[y == 0].mean()
    s = x.std()
    if s == 0:
        return 0.0
    n = len(x)
    return (mean1 - mean0) / s * math.sqrt(n1 * n0 / (n * n))


def load_all_races():
    """Ladda alla lopp från alla sparade backtest-filer. Deduplicera."""
    backtest_dir = Path("backtest_results")
    files = sorted(backtest_dir.glob("backtest_*.json"))

    seen = set()  # (date, race_number) for dedup
    all_races = []

    for f in files:
        data = json.loads(f.read_text())
        for rp in data.get("predictions", []):
            for race in rp.get("races", []):
                key = (race.get("date"), race.get("race_number"))
                if key in seen:
                    continue
                seen.add(key)

                actual = race.get("actual_result", [])
                hfs = race.get("horse_factor_scores", {})
                hs = race.get("horse_scores", {})
                hbp = race.get("horse_bet_pct", {})

                if actual and hfs:
                    all_races.append({
                        "date": race.get("date"),
                        "race_number": race.get("race_number"),
                        "track": race.get("track", ""),
                        "actual_result": actual,
                        "horse_factor_scores": {int(k): v for k, v in hfs.items()},
                        "horse_scores": {int(k): v for k, v in hs.items()},
                        "horse_bet_pct": {int(k): float(v) for k, v in hbp.items()},
                    })

    return all_races


def analyze_factor_accuracy(races, factor_name):
    """Beräkna top-1, top-3, top-5 accuracy för en faktor."""
    top1 = top3 = top5 = 0
    n = 0
    all_scores = []
    all_wins = []

    for race in races:
        winner = race["actual_result"][0]
        actual_top3 = set(race["actual_result"][:3])
        actual_top5 = set(race["actual_result"][:5])

        factor_by_horse = {}
        for horse, fs in race["horse_factor_scores"].items():
            if factor_name in fs:
                factor_by_horse[horse] = fs[factor_name]

        if not factor_by_horse:
            continue

        n += 1
        ranking = sorted(factor_by_horse.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            top5 += 1

        for horse, score in factor_by_horse.items():
            all_scores.append(score)
            all_wins.append(1.0 if horse == winner else 0.0)

    corr = point_biserial(all_scores, all_wins)

    return {
        "n": n,
        "corr": corr,
        "top1": top1/n if n else 0,
        "top3": top3/n if n else 0,
        "top5": top5/n if n else 0,
    }


def analyze_composite_accuracy(races, weights):
    """Beräkna top-1, top-3, top-5 accuracy för composite med givna vikter."""
    total = sum(weights.values())
    w = {k: v/total for k, v in weights.items()}

    top1 = top3 = top5 = 0
    n = 0

    for race in races:
        winner = race["actual_result"][0]
        actual_top3 = set(race["actual_result"][:3])
        actual_top5 = set(race["actual_result"][:5])

        composites = {}
        for horse, fs in race["horse_factor_scores"].items():
            raw = sum(fs.get(fn, 50.0) * wt for fn, wt in w.items())
            composites[horse] = 100.0 / (1.0 + math.exp(-0.08 * (raw - 50.0)))

        if not composites:
            continue

        n += 1
        ranking = sorted(composites.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            top5 += 1

    return {
        "n": n,
        "top1": top1/n if n else 0,
        "top3": top3/n if n else 0,
        "top5": top5/n if n else 0,
    }


def explore_new_signals(races):
    """Utforska nya signaler bortom de 7 faktorerna."""

    results = {}

    # ── Signal 1: Streckprocent (bet%) som prediktor ────────────
    # "Publiken har ofta rätt" — hög streck% = favorit
    bet_scores = []
    bet_wins = []
    bet_top1 = bet_top3 = bet_top5 = 0
    bet_n = 0

    for race in races:
        winner = race["actual_result"][0]
        bet_pcts = race["horse_bet_pct"]
        if not bet_pcts or max(bet_pcts.values()) == 0:
            continue

        bet_n += 1
        ranking = sorted(bet_pcts.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            bet_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            bet_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            bet_top5 += 1

        for horse, pct in bet_pcts.items():
            bet_scores.append(pct)
            bet_wins.append(1.0 if horse == winner else 0.0)

    results["bet_percentage"] = {
        "n": bet_n,
        "corr": point_biserial(bet_scores, bet_wins),
        "top1": bet_top1/bet_n if bet_n else 0,
        "top3": bet_top3/bet_n if bet_n else 0,
        "top5": bet_top5/bet_n if bet_n else 0,
        "desc": "Streck% (publiken/marknaden)",
    }

    # ── Signal 2: Composite score spread (fältets jämnhet) ────────
    # Tight fält → favoriten vinner mer sällan
    spread_scores = []
    spread_wins = []  # 1 = favoriten vann

    for race in races:
        winner = race["actual_result"][0]
        scores = race["horse_scores"]
        if not scores:
            continue

        vals = sorted(scores.values(), reverse=True)
        if len(vals) < 3:
            continue

        spread = vals[0] - vals[-1]
        gap_1_2 = vals[0] - vals[1]

        # Högt favorit-gap → favoriten borde vinna
        ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        fav_won = 1.0 if ranking[0][0] == winner else 0.0

        spread_scores.append(gap_1_2)
        spread_wins.append(fav_won)

    results["gap_to_second"] = {
        "n": len(spread_wins),
        "corr": point_biserial(spread_scores, spread_wins),
        "top1": 0, "top3": 0, "top5": 0,
        "desc": "Gap rank1→rank2 (stor gap = favoriten vinner)",
    }

    # ── Signal 3: Cross-factor signals ─────────────────────────
    # Hästar som rankas högt av ALLA faktorer vs bara några
    consistency_scores = []
    consistency_wins = []
    cons_top1 = cons_top3 = cons_top5 = 0
    cons_n = 0

    for race in races:
        winner = race["actual_result"][0]

        horse_consistency = {}
        for horse, fs in race["horse_factor_scores"].items():
            if len(fs) < 5:
                continue
            vals = list(fs.values())
            above_median = sum(1 for v in vals if v >= 50)
            horse_consistency[horse] = above_median / len(vals)

        if not horse_consistency:
            continue

        cons_n += 1
        ranking = sorted(horse_consistency.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            cons_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            cons_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            cons_top5 += 1

        for horse, score in horse_consistency.items():
            consistency_scores.append(score)
            consistency_wins.append(1.0 if horse == winner else 0.0)

    results["factor_consistency"] = {
        "n": cons_n,
        "corr": point_biserial(consistency_scores, consistency_wins),
        "top1": cons_top1/cons_n if cons_n else 0,
        "top3": cons_top3/cons_n if cons_n else 0,
        "top5": cons_top5/cons_n if cons_n else 0,
        "desc": "Faktor-konsistens (hög på ALLA faktorer)",
    }

    # ── Signal 4: Form + Track combo ──────────────────────────
    # Backtest visade track_profile stark, form_curve svag
    # Men combo: hög bana + hög form = ännu starkare?
    combo_scores = []
    combo_wins = []
    combo_top1 = combo_top3 = combo_top5 = 0
    combo_n = 0

    for race in races:
        winner = race["actual_result"][0]

        horse_combos = {}
        for horse, fs in race["horse_factor_scores"].items():
            tp = fs.get("track_profile", 50)
            fc = fs.get("form_curve", 50)
            # Bonus för hästar som är BRA på banan OCH har bra form
            horse_combos[horse] = tp * 0.65 + fc * 0.35

        if not horse_combos:
            continue

        combo_n += 1
        ranking = sorted(horse_combos.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            combo_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            combo_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            combo_top5 += 1

        for horse, score in horse_combos.items():
            combo_scores.append(score)
            combo_wins.append(1.0 if horse == winner else 0.0)

    results["track_form_combo"] = {
        "n": combo_n,
        "corr": point_biserial(combo_scores, combo_wins),
        "top1": combo_top1/combo_n if combo_n else 0,
        "top3": combo_top3/combo_n if combo_n else 0,
        "top5": combo_top5/combo_n if combo_n else 0,
        "desc": "Bana×Form combo (65/35)",
    }

    # ── Signal 5: Track + Post combo (skrällprofil inverterad) ────
    track_post_scores = []
    track_post_wins = []
    tp_top1 = tp_top3 = tp_top5 = 0
    tp_n = 0

    for race in races:
        winner = race["actual_result"][0]

        horse_tp = {}
        for horse, fs in race["horse_factor_scores"].items():
            tp = fs.get("track_profile", 50)
            pp = fs.get("post_position", 50)
            horse_tp[horse] = tp * 0.6 + pp * 0.4

        if not horse_tp:
            continue

        tp_n += 1
        ranking = sorted(horse_tp.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            tp_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            tp_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            tp_top5 += 1

        for horse, score in horse_tp.items():
            track_post_scores.append(score)
            track_post_wins.append(1.0 if horse == winner else 0.0)

    results["track_post_combo"] = {
        "n": tp_n,
        "corr": point_biserial(track_post_scores, track_post_wins),
        "top1": tp_top1/tp_n if tp_n else 0,
        "top3": tp_top3/tp_n if tp_n else 0,
        "top5": tp_top5/tp_n if tp_n else 0,
        "desc": "Bana×Spår combo (60/40)",
    }

    # ── Signal 6: Min-faktor (svagaste länken) ──────────────────
    # Hästar utan riktigt svag faktor = mer pålitliga
    minfact_scores = []
    minfact_wins = []
    mf_top1 = mf_top3 = mf_top5 = 0
    mf_n = 0

    for race in races:
        winner = race["actual_result"][0]

        horse_mf = {}
        for horse, fs in race["horse_factor_scores"].items():
            if len(fs) < 5:
                continue
            # Svagaste faktorn avgör golvet
            horse_mf[horse] = min(fs.values())

        if not horse_mf:
            continue

        mf_n += 1
        ranking = sorted(horse_mf.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            mf_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            mf_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            mf_top5 += 1

        for horse, score in horse_mf.items():
            minfact_scores.append(score)
            minfact_wins.append(1.0 if horse == winner else 0.0)

    results["min_factor"] = {
        "n": mf_n,
        "corr": point_biserial(minfact_scores, minfact_wins),
        "top1": mf_top1/mf_n if mf_n else 0,
        "top3": mf_top3/mf_n if mf_n else 0,
        "top5": mf_top5/mf_n if mf_n else 0,
        "desc": "Svagaste faktor (inget svagt ben)",
    }

    # ── Signal 7: Streck vs Score diskrepans (spelvärde) ─────────
    # Hög score + låg streck = undervärde → ofta överpresterare
    value_scores = []
    value_wins = []
    val_top1 = val_top3 = val_top5 = 0
    val_n = 0

    for race in races:
        winner = race["actual_result"][0]
        bet_pcts = race["horse_bet_pct"]
        comp_scores = race["horse_scores"]

        if not bet_pcts or not comp_scores:
            continue
        if max(bet_pcts.values()) == 0:
            continue

        horse_value = {}
        for horse in comp_scores:
            bet = bet_pcts.get(horse, 0)
            score = comp_scores[horse]
            if bet > 0:
                # Score / bet = "value ratio"
                horse_value[horse] = score / (bet * 100 + 1)
            else:
                horse_value[horse] = score / 10

        if not horse_value:
            continue

        val_n += 1
        ranking = sorted(horse_value.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            val_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            val_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            val_top5 += 1

        for horse, score in horse_value.items():
            value_scores.append(score)
            value_wins.append(1.0 if horse == winner else 0.0)

    results["value_ratio"] = {
        "n": val_n,
        "corr": point_biserial(value_scores, value_wins),
        "top1": val_top1/val_n if val_n else 0,
        "top3": val_top3/val_n if val_n else 0,
        "top5": val_top5/val_n if val_n else 0,
        "desc": "Spelvärde (score/streck ratio)",
    }

    # ── Signal 8: Top-2 faktorer (bästa 2 av 7) ────────────────
    top2_scores = []
    top2_wins = []
    t2_top1 = t2_top3 = t2_top5 = 0
    t2_n = 0

    for race in races:
        winner = race["actual_result"][0]

        horse_t2 = {}
        for horse, fs in race["horse_factor_scores"].items():
            if len(fs) < 5:
                continue
            sorted_vals = sorted(fs.values(), reverse=True)
            horse_t2[horse] = sum(sorted_vals[:2]) / 2  # Medel av bästa 2

        if not horse_t2:
            continue

        t2_n += 1
        ranking = sorted(horse_t2.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            t2_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            t2_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            t2_top5 += 1

        for horse, score in horse_t2.items():
            top2_scores.append(score)
            top2_wins.append(1.0 if horse == winner else 0.0)

    results["best_2_factors"] = {
        "n": t2_n,
        "corr": point_biserial(top2_scores, top2_wins),
        "top1": t2_top1/t2_n if t2_n else 0,
        "top3": t2_top3/t2_n if t2_n else 0,
        "top5": t2_top5/t2_n if t2_n else 0,
        "desc": "Bästa 2 faktorer (starkaste sidorna)",
    }

    # ── Signal 9: Residual (hög faktor som modellen missar) ─────
    # Horses with high individual factor scores but low composite
    # (=de modellen UNDERSKATTAR)
    residual_scores = []
    residual_wins = []
    res_top1 = res_top3 = res_top5 = 0
    res_n = 0

    for race in races:
        winner = race["actual_result"][0]
        comp_scores = race["horse_scores"]

        horse_res = {}
        for horse, fs in race["horse_factor_scores"].items():
            if len(fs) < 5:
                continue
            # Starkaste faktor minus composite = "dolt potential"
            max_f = max(fs.values())
            comp = comp_scores.get(horse, 50)
            horse_res[horse] = max_f - comp + 50  # Centrerat kring 50

        if not horse_res:
            continue

        res_n += 1
        ranking = sorted(horse_res.items(), key=lambda x: x[1], reverse=True)

        if ranking[0][0] == winner:
            res_top1 += 1
        if winner in {h for h, _ in ranking[:3]}:
            res_top3 += 1
        if winner in {h for h, _ in ranking[:5]}:
            res_top5 += 1

        for horse, score in horse_res.items():
            residual_scores.append(score)
            residual_wins.append(1.0 if horse == winner else 0.0)

    results["residual_strength"] = {
        "n": res_n,
        "corr": point_biserial(residual_scores, residual_wins),
        "top1": res_top1/res_n if res_n else 0,
        "top3": res_top3/res_n if res_n else 0,
        "top5": res_top5/res_n if res_n else 0,
        "desc": "Dold styrka (max faktor - composite)",
    }

    return results


def main():
    console.print("\n[bold cyan]═══ Djupanalys: Nya prediktiva signaler ═══[/bold cyan]\n")

    # Ladda data
    races = load_all_races()
    console.print(f"Laddade [bold]{len(races)}[/bold] unika lopp\n")

    # ── Del 1: Befintliga faktorer — Top 1/3/5 accuracy ─────────
    console.print("[bold yellow]Del 1: Befintliga 7 faktorer — detaljerad precision[/bold yellow]\n")

    factor_names = [
        "time_analysis", "prize_index", "form_curve",
        "track_profile", "category_profile", "driver_trainer", "post_position",
    ]

    table = Table(title=f"Faktor-precision ({len(races)} lopp)", show_lines=False)
    table.add_column("Faktor", style="cyan", min_width=18)
    table.add_column("Korr", justify="right", min_width=7)
    table.add_column("Top-1 %", justify="right", min_width=8)
    table.add_column("Top-3 %", justify="right", min_width=8)
    table.add_column("Top-5 %", justify="right", min_width=8)

    for fn in factor_names:
        m = analyze_factor_accuracy(races, fn)
        corr_style = "bold green" if m["corr"] >= 0.2 else ("yellow" if m["corr"] >= 0.1 else "dim red")
        table.add_row(
            fn,
            f"[{corr_style}]{m['corr']:.3f}[/{corr_style}]",
            f"{m['top1']:.1%}",
            f"{m['top3']:.1%}",
            f"{m['top5']:.1%}",
        )

    console.print(table)

    # ── Del 2: Composite — Top 1/3/5 med gamla vs nya vikter ─────
    console.print("\n[bold yellow]Del 2: Composite-precision — Gamla vs Nya vikter[/bold yellow]\n")

    OLD_WEIGHTS = {
        "time_analysis": 0.25, "prize_index": 0.20, "form_curve": 0.20,
        "track_profile": 0.05, "category_profile": 0.10,
        "driver_trainer": 0.10, "post_position": 0.10,
    }
    NEW_WEIGHTS = {
        "time_analysis": 0.09, "prize_index": 0.16, "form_curve": 0.11,
        "track_profile": 0.22, "category_profile": 0.17,
        "driver_trainer": 0.12, "post_position": 0.13,
    }

    old = analyze_composite_accuracy(races, OLD_WEIGHTS)
    new = analyze_composite_accuracy(races, NEW_WEIGHTS)

    table2 = Table(title="Composite-precision", show_lines=True)
    table2.add_column("Vikter", style="cyan", min_width=25)
    table2.add_column("Lopp", justify="right")
    table2.add_column("Top-1 %", justify="right")
    table2.add_column("Top-3 %", justify="right")
    table2.add_column("Top-5 %", justify="right")

    table2.add_row(
        "Gamla (handpickade)",
        str(old["n"]),
        f"{old['top1']:.1%}",
        f"{old['top3']:.1%}",
        f"{old['top5']:.1%}",
    )
    table2.add_row(
        "[bold]Nya (empiriska)[/bold]",
        str(new["n"]),
        f"[bold green]{new['top1']:.1%}[/bold green]",
        f"[bold green]{new['top3']:.1%}[/bold green]",
        f"[bold green]{new['top5']:.1%}[/bold green]",
    )
    table2.add_row(
        "[bold]Δ Förbättring[/bold]",
        "",
        f"[bold green]{new['top1']-old['top1']:+.1%}[/bold green]",
        f"[bold green]{new['top3']-old['top3']:+.1%}[/bold green]",
        f"[bold green]{new['top5']-old['top5']:+.1%}[/bold green]",
    )

    console.print(table2)

    # ── Del 3: Nya signaler ──────────────────────────────────────
    console.print("\n[bold yellow]Del 3: Nya signalkandidater[/bold yellow]\n")

    new_signals = explore_new_signals(races)

    table3 = Table(title="Nya prediktiva signaler", show_lines=False)
    table3.add_column("Signal", style="cyan", min_width=30)
    table3.add_column("Korr", justify="right", min_width=7)
    table3.add_column("Top-1 %", justify="right", min_width=8)
    table3.add_column("Top-3 %", justify="right", min_width=8)
    table3.add_column("Top-5 %", justify="right", min_width=8)
    table3.add_column("N", justify="right", min_width=5)

    sorted_signals = sorted(new_signals.items(), key=lambda x: abs(x[1]["corr"]), reverse=True)

    for name, m in sorted_signals:
        corr_style = "bold green" if abs(m["corr"]) >= 0.2 else ("yellow" if abs(m["corr"]) >= 0.1 else "dim red")
        table3.add_row(
            m.get("desc", name),
            f"[{corr_style}]{m['corr']:.3f}[/{corr_style}]",
            f"{m['top1']:.1%}" if m['top1'] else "-",
            f"{m['top3']:.1%}" if m['top3'] else "-",
            f"{m['top5']:.1%}" if m['top5'] else "-",
            str(m['n']),
        )

    console.print(table3)

    # ── Del 4: Jämför bästa combo-vikter ─────────────────────────
    console.print("\n[bold yellow]Del 4: Kan vi göra bättre? Testa varianter[/bold yellow]\n")

    # Testa att boosta track_profile ännu mer
    variants = {
        "Nuvarande nya": NEW_WEIGHTS,
        "Track boosted (28%)": {**NEW_WEIGHTS, "track_profile": 0.28, "time_analysis": 0.06, "form_curve": 0.08},
        "Track+Cat heavy (26/20)": {**NEW_WEIGHTS, "track_profile": 0.26, "category_profile": 0.20, "time_analysis": 0.06, "form_curve": 0.08},
        "Prize boost (20%)": {**NEW_WEIGHTS, "prize_index": 0.20, "form_curve": 0.08},
        "Post boost (18%)": {**NEW_WEIGHTS, "post_position": 0.18, "time_analysis": 0.06},
        "Min-max (top3 → 45%)": {"track_profile": 0.25, "category_profile": 0.20, "prize_index": 0.15, "post_position": 0.15, "driver_trainer": 0.10, "form_curve": 0.10, "time_analysis": 0.05},
        "Ultra-track (30%)": {"track_profile": 0.30, "category_profile": 0.18, "prize_index": 0.15, "post_position": 0.13, "driver_trainer": 0.10, "form_curve": 0.08, "time_analysis": 0.06},
    }

    table4 = Table(title="Viktningsexperiment", show_lines=True)
    table4.add_column("Variant", style="cyan", min_width=28)
    table4.add_column("Top-1 %", justify="right", min_width=8)
    table4.add_column("Top-3 %", justify="right", min_width=8)
    table4.add_column("Top-5 %", justify="right", min_width=8)

    for name, w in variants.items():
        m = analyze_composite_accuracy(races, w)
        table4.add_row(
            name,
            f"{m['top1']:.1%}",
            f"{m['top3']:.1%}",
            f"{m['top5']:.1%}",
        )

    console.print(table4)

    console.print()


if __name__ == "__main__":
    main()
