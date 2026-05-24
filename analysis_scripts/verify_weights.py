"""Verifiering: Jämför gamla vs nya vikter mot sparade backtest-data.

Laddar sparade faktorpoäng och beräknar composite_score med:
1. Gamla vikter (handpickade)
2. Nya vikter (empiriskt kalibrerade, blended V75+V85)

Mäter hur precision (top-1 accuracy, top-3 accuracy) förändras.
"""

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

# ── Viktuppsättningar ──────────────────────────────────────────────

OLD_WEIGHTS = {
    "time_analysis": 0.25,
    "prize_index": 0.20,
    "form_curve": 0.20,
    "track_profile": 0.05,
    "category_profile": 0.10,
    "driver_trainer": 0.10,
    "post_position": 0.10,
}

NEW_WEIGHTS = {
    "time_analysis": 0.09,
    "prize_index": 0.16,
    "form_curve": 0.11,
    "track_profile": 0.22,
    "category_profile": 0.17,
    "driver_trainer": 0.12,
    "post_position": 0.13,
}


def _normalize_weights(w: dict[str, float]) -> dict[str, float]:
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def _sigmoid_spread(raw: float, k: float = 0.08) -> float:
    return 100.0 / (1.0 + math.exp(-k * (raw - 50.0)))


@dataclass
class RaceResult:
    race_number: int
    date_str: str
    actual_winner: int
    actual_top3: list[int]
    # Per vikt-set
    predicted_winner_old: int
    predicted_winner_new: int
    predicted_top3_old: list[int]
    predicted_top3_new: list[int]


def recompute_rankings(
    horse_factor_scores: dict[int, dict[str, float]],
    weights: dict[str, float],
) -> list[tuple[int, float]]:
    """Beräkna composite_score med givna vikter, returnera ranking."""
    w = _normalize_weights(weights)
    horse_composites: list[tuple[int, float]] = []

    for horse_num, factors in horse_factor_scores.items():
        raw = sum(factors.get(fn, 50.0) * wt for fn, wt in w.items())
        spread = _sigmoid_spread(raw)
        horse_composites.append((horse_num, spread))

    return sorted(horse_composites, key=lambda x: x[1], reverse=True)


def analyze_file(filepath: Path) -> tuple[str, list[RaceResult]]:
    """Analysera en sparad backtest-fil."""
    data = json.loads(filepath.read_text(encoding="utf-8"))
    results: list[RaceResult] = []

    for rp in data.get("predictions", []):
        for race in rp.get("races", []):
            actual = race.get("actual_result", [])
            if not actual:
                continue

            horse_fs = {
                int(k): v
                for k, v in race.get("horse_factor_scores", {}).items()
            }
            if not horse_fs:
                continue

            winner = actual[0]
            top3 = actual[:3]

            old_ranking = recompute_rankings(horse_fs, OLD_WEIGHTS)
            new_ranking = recompute_rankings(horse_fs, NEW_WEIGHTS)

            results.append(RaceResult(
                race_number=race["race_number"],
                date_str=race.get("date", ""),
                actual_winner=winner,
                actual_top3=top3,
                predicted_winner_old=old_ranking[0][0] if old_ranking else 0,
                predicted_winner_new=new_ranking[0][0] if new_ranking else 0,
                predicted_top3_old=[h for h, _ in old_ranking[:3]],
                predicted_top3_new=[h for h, _ in new_ranking[:3]],
            ))

    label = f"{data.get('game_type', '?')} {data.get('start_date', '?')} → {data.get('end_date', '?')}"
    return label, results


def compute_metrics(results: list[RaceResult], label_prefix: str) -> dict:
    n = len(results)
    if n == 0:
        return {}

    old_top1 = sum(1 for r in results if r.predicted_winner_old == r.actual_winner)
    new_top1 = sum(1 for r in results if r.predicted_winner_new == r.actual_winner)

    old_top3_overlap = sum(
        len(set(r.predicted_top3_old) & set(r.actual_top3))
        for r in results
    )
    new_top3_overlap = sum(
        len(set(r.predicted_top3_new) & set(r.actual_top3))
        for r in results
    )

    # Vinnare i top3
    old_winner_in_top3 = sum(1 for r in results if r.actual_winner in r.predicted_top3_old)
    new_winner_in_top3 = sum(1 for r in results if r.actual_winner in r.predicted_top3_new)

    return {
        "n": n,
        "old_top1": old_top1,
        "new_top1": new_top1,
        "old_top1_pct": old_top1 / n,
        "new_top1_pct": new_top1 / n,
        "old_top3_avg": old_top3_overlap / n,
        "new_top3_avg": new_top3_overlap / n,
        "old_winner_in_top3": old_winner_in_top3,
        "new_winner_in_top3": new_winner_in_top3,
        "old_winner_in_top3_pct": old_winner_in_top3 / n,
        "new_winner_in_top3_pct": new_winner_in_top3 / n,
    }


def main():
    console = Console()
    backtest_dir = Path(__file__).parent / "backtest_results"

    if not backtest_dir.exists():
        console.print("[red]Ingen backtest_results/ mapp hittad[/red]")
        return

    files = sorted(backtest_dir.glob("backtest_*.json"))
    if not files:
        console.print("[red]Inga sparade backtest-filer[/red]")
        return

    console.print("\n[bold cyan]═══ Viktjämförelse: Gamla vs Nya vikter ═══[/bold cyan]\n")

    console.print("[dim]Gamla vikter (handpickade):[/dim]")
    for k, v in OLD_WEIGHTS.items():
        console.print(f"  {k}: {v:.2f}")

    console.print("\n[dim]Nya vikter (empiriskt kalibrerade, blended V75+V85 661 lopp):[/dim]")
    for k, v in NEW_WEIGHTS.items():
        console.print(f"  {k}: {v:.2f}")

    console.print()

    # Resultat-tabell
    table = Table(title="Precision: Gamla vs Nya vikter", show_lines=True)
    table.add_column("Dataset", style="cyan", min_width=25)
    table.add_column("Lopp", justify="right", min_width=6)
    table.add_column("Top-1 (Gamla)", justify="right", min_width=14)
    table.add_column("Top-1 (Nya)", justify="right", min_width=14)
    table.add_column("Δ Top-1", justify="right", min_width=8)
    table.add_column("Vinnare i Top3 (Gamla)", justify="right", min_width=14)
    table.add_column("Vinnare i Top3 (Nya)", justify="right", min_width=14)
    table.add_column("Δ Top3", justify="right", min_width=8)

    all_results: list[RaceResult] = []

    for f in files:
        label, results = analyze_file(f)
        all_results.extend(results)
        m = compute_metrics(results, label)
        if not m:
            continue

        delta_top1 = m["new_top1_pct"] - m["old_top1_pct"]
        delta_top3 = m["new_winner_in_top3_pct"] - m["old_winner_in_top3_pct"]

        d1_style = "bold green" if delta_top1 > 0 else ("bold red" if delta_top1 < 0 else "dim")
        d3_style = "bold green" if delta_top3 > 0 else ("bold red" if delta_top3 < 0 else "dim")

        table.add_row(
            label,
            str(m["n"]),
            f"{m['old_top1_pct']:.1%}",
            f"{m['new_top1_pct']:.1%}",
            f"[{d1_style}]{delta_top1:+.1%}[/{d1_style}]",
            f"{m['old_winner_in_top3_pct']:.1%}",
            f"{m['new_winner_in_top3_pct']:.1%}",
            f"[{d3_style}]{delta_top3:+.1%}[/{d3_style}]",
        )

    # Totalt
    if all_results:
        m = compute_metrics(all_results, "TOTALT")
        delta_top1 = m["new_top1_pct"] - m["old_top1_pct"]
        delta_top3 = m["new_winner_in_top3_pct"] - m["old_winner_in_top3_pct"]
        d1_style = "bold green" if delta_top1 > 0 else ("bold red" if delta_top1 < 0 else "dim")
        d3_style = "bold green" if delta_top3 > 0 else ("bold red" if delta_top3 < 0 else "dim")

        table.add_row(
            "[bold]TOTALT (alla dataset)[/bold]",
            f"[bold]{m['n']}[/bold]",
            f"[bold]{m['old_top1_pct']:.1%}[/bold]",
            f"[bold]{m['new_top1_pct']:.1%}[/bold]",
            f"[bold {d1_style}]{delta_top1:+.1%}[/bold {d1_style}]",
            f"[bold]{m['old_winner_in_top3_pct']:.1%}[/bold]",
            f"[bold]{m['new_winner_in_top3_pct']:.1%}[/bold]",
            f"[bold {d3_style}]{delta_top3:+.1%}[/bold {d3_style}]",
        )

    console.print(table)

    # Extra: Top 3 overlap
    if all_results:
        m = compute_metrics(all_results, "")
        console.print(f"\n[dim]Top-3 overlap (medel antal av våra top3 i verklig top3):[/dim]")
        console.print(f"  Gamla: {m['old_top3_avg']:.2f} av 3")
        console.print(f"  Nya:   {m['new_top3_avg']:.2f} av 3")
        diff = m['new_top3_avg'] - m['old_top3_avg']
        style = "green" if diff > 0 else "red"
        console.print(f"  Δ:     [{style}]{diff:+.2f}[/{style}]")

    console.print()


if __name__ == "__main__":
    main()
