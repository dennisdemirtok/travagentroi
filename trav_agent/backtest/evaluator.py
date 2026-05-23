"""Backtesting-utvärderare — mäter precision, faktorkorrelation, viktförslag.

Tar en lista av RoundPredictions och beräknar sammanfattande statistik.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from rich.console import Console
from rich.table import Table

from ..config import DEFAULT_CONFIG
from .runner import RacePrediction, RoundPrediction


@dataclass
class FactorEvaluation:
    """Utvärdering av en enskild faktors prediktiva kraft."""

    factor_name: str
    correlation_with_win: float = 0.0  # Point-biserial korrelation
    top1_accuracy: float = 0.0  # Andel gånger faktorn pekar på vinnaren
    top3_accuracy: float = 0.0  # Andel gånger vinnaren är i faktorns topp 3
    sample_size: int = 0


@dataclass
class BacktestResults:
    """Sammanfattande resultat av backtesting."""

    # Grundläggande
    num_rounds: int = 0
    num_races: int = 0

    # Spik-precision
    spik_attempts: int = 0
    spik_wins: int = 0
    spik_accuracy: float = 0.0

    # Top pick precision
    top_pick_wins: int = 0  # Vinnare bland våra top picks
    top_pick_accuracy: float = 0.0

    # Top 3 overlap
    avg_top3_overlap: float = 0.0  # Medel antal av våra top3 som hamnar i top3

    # ROI (simulerat spel)
    simulated_bets: int = 0
    simulated_cost: float = 0.0
    simulated_return: float = 0.0
    roi_percent: float = 0.0

    # Spelvärde
    value_picks_total: int = 0
    value_picks_wins: int = 0
    value_pick_accuracy: float = 0.0

    # Per faktor
    factor_evaluations: list[FactorEvaluation] = field(default_factory=list)
    suggested_weights: dict[str, float] = field(default_factory=dict)


def _point_biserial(scores: list[float], binary: list[float]) -> float:
    """Point-biserial korrelation mellan kontinuerlig poäng och binärt utfall."""
    if len(scores) < 2:
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


class BacktestEvaluator:
    """Utvärderar backtest-resultat."""

    def evaluate(self, predictions: list[RoundPrediction]) -> BacktestResults:
        """Beräkna sammanfattande statistik från backtest."""
        results = BacktestResults()
        results.num_rounds = len(predictions)

        all_races: list[RacePrediction] = []
        for rp in predictions:
            all_races.extend(rp.races)

        results.num_races = len(all_races)

        if not all_races:
            return results

        # ── Spik-precision ────────────────────────────────────────────────
        spik_races = [r for r in all_races if r.spik is not None]
        results.spik_attempts = len(spik_races)
        results.spik_wins = sum(1 for r in spik_races if r.spik_correct)
        results.spik_accuracy = (
            results.spik_wins / results.spik_attempts
            if results.spik_attempts > 0
            else 0.0
        )

        # ── Top pick precision ────────────────────────────────────────────
        races_with_results = [r for r in all_races if r.actual_result]
        results.top_pick_wins = sum(1 for r in races_with_results if r.top_pick_hit)
        results.top_pick_accuracy = (
            results.top_pick_wins / len(races_with_results)
            if races_with_results
            else 0.0
        )

        # ── Top 3 overlap ────────────────────────────────────────────────
        overlaps = [r.top3_picks_in_top3 for r in races_with_results]
        results.avg_top3_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        # ── Faktorkorrelation ─────────────────────────────────────────────
        results.factor_evaluations = self.evaluate_all_factors(predictions)
        results.suggested_weights = self.suggest_weights(results.factor_evaluations)

        return results

    def evaluate_factor_isolation(
        self,
        predictions: list[RoundPrediction],
        factor_name: str,
    ) -> FactorEvaluation:
        """Utvärdera en enskild faktor isolerat.

        Mäter point-biserial korrelation, top-1 accuracy, och top-3 accuracy.
        """
        evaluation = FactorEvaluation(factor_name=factor_name)

        all_races: list[RacePrediction] = []
        for rp in predictions:
            all_races.extend(rp.races)

        races_with_data = [
            r for r in all_races
            if r.actual_result and r.horse_factor_scores
        ]
        evaluation.sample_size = len(races_with_data)

        if not races_with_data:
            return evaluation

        all_scores: list[float] = []
        all_wins: list[float] = []
        top1_correct = 0
        top3_correct = 0

        for race in races_with_data:
            winner = race.actual_result[0]
            actual_top3 = set(race.actual_result[:3])

            # Faktorpoäng per häst i detta lopp
            factor_by_horse: dict[int, float] = {}
            for horse_num, fs in race.horse_factor_scores.items():
                if factor_name in fs:
                    factor_by_horse[horse_num] = fs[factor_name]

            if not factor_by_horse:
                continue

            # Samla score/vinst-par för korrelation
            for horse_num, score in factor_by_horse.items():
                all_scores.append(score)
                all_wins.append(1.0 if horse_num == winner else 0.0)

            # Faktorns ranking i detta lopp
            ranking = sorted(
                factor_by_horse.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            factor_top1 = ranking[0][0]
            factor_top3 = {num for num, _ in ranking[:3]}

            if factor_top1 == winner:
                top1_correct += 1
            if winner in factor_top3:
                top3_correct += 1

        evaluation.correlation_with_win = _point_biserial(all_scores, all_wins)
        evaluation.top1_accuracy = (
            top1_correct / len(races_with_data) if races_with_data else 0.0
        )
        evaluation.top3_accuracy = (
            top3_correct / len(races_with_data) if races_with_data else 0.0
        )

        return evaluation

    def evaluate_all_factors(
        self, predictions: list[RoundPrediction]
    ) -> list[FactorEvaluation]:
        """Kör faktor-isolering för alla 7 faktorer, sorterade efter korrelation."""
        factor_names = [
            "time_analysis", "prize_index", "form_curve",
            "track_profile", "category_profile", "driver_trainer", "post_position",
        ]
        evaluations = []
        for name in factor_names:
            ev = self.evaluate_factor_isolation(predictions, name)
            evaluations.append(ev)
        return sorted(evaluations, key=lambda e: e.correlation_with_win, reverse=True)

    def suggest_weights(
        self, evaluations: list[FactorEvaluation]
    ) -> dict[str, float]:
        """Föreslå nya vikter baserat på empirisk korrelation.

        Blend: 60% korrelationsstyrka + 40% top3_accuracy.
        Golv 0.03 per faktor (ta aldrig bort en faktor helt).
        """
        raw: dict[str, float] = {}
        for ev in evaluations:
            strength = abs(ev.correlation_with_win) * 0.6 + ev.top3_accuracy * 0.4
            raw[ev.factor_name] = max(0.03, strength)

        total = sum(raw.values())
        if total == 0:
            # Fallback: lika vikter
            n = len(raw)
            return {k: round(1.0 / n, 3) for k in raw}
        return {k: round(v / total, 3) for k, v in raw.items()}

    def print_summary(self, results: BacktestResults) -> str:
        """Formatera en sammanfattande rapport (text)."""
        lines = [
            "=" * 60,
            "BACKTEST-RAPPORT",
            "=" * 60,
            f"Omgångar analyserade: {results.num_rounds}",
            f"Lopp analyserade:     {results.num_races}",
            "",
            "── Spik-precision ──",
            f"Spikar:      {results.spik_wins}/{results.spik_attempts}",
            f"Precision:   {results.spik_accuracy:.1%}",
            "",
            "── Top Pick ──",
            f"Vinst i våra picks: {results.top_pick_wins}/{results.num_races}",
            f"Precision:          {results.top_pick_accuracy:.1%}",
            "",
            "── Top 3 Overlap ──",
            f"Medel overlap:      {results.avg_top3_overlap:.2f} av 3",
            "",
        ]

        if results.roi_percent != 0:
            lines.extend([
                "── ROI (simulerat) ──",
                f"Insats:    {results.simulated_cost:.0f} kr",
                f"Utfall:    {results.simulated_return:.0f} kr",
                f"ROI:       {results.roi_percent:.1%}",
            ])

        lines.append("=" * 60)
        return "\n".join(lines)

    def print_factor_report(self, results: BacktestResults) -> None:
        """Skriv ut faktorkorrelationstabell med Rich."""
        current_weights = DEFAULT_CONFIG.weights.normalized()
        console = Console()

        table = Table(
            title=f"Faktorkorrelation med vinst ({results.num_races} lopp)",
            show_lines=False,
        )
        table.add_column("Faktor", style="cyan", min_width=18)
        table.add_column("Korrelation", justify="right", min_width=12)
        table.add_column("Top-1", justify="right", min_width=7)
        table.add_column("Top-3", justify="right", min_width=7)
        table.add_column("Nuv. vikt", justify="right", style="dim", min_width=9)
        table.add_column("Föreslagen", justify="right", style="bold green", min_width=10)

        for ev in results.factor_evaluations:
            cur = current_weights.get(ev.factor_name, 0)
            sug = results.suggested_weights.get(ev.factor_name, 0)

            # Färgkoda korrelation
            corr = ev.correlation_with_win
            if corr >= 0.20:
                corr_style = "bold green"
            elif corr >= 0.10:
                corr_style = "yellow"
            else:
                corr_style = "dim red"

            table.add_row(
                ev.factor_name,
                f"[{corr_style}]{corr:.3f}[/{corr_style}]",
                f"{ev.top1_accuracy:.1%}",
                f"{ev.top3_accuracy:.1%}",
                f"{cur:.2f}",
                f"{sug:.3f}",
            )

        console.print()
        console.print(table)

        # Sammanfattning
        if results.suggested_weights:
            total = sum(results.suggested_weights.values())
            console.print(
                f"\n  Föreslagna vikter summerar till {total:.3f}",
                style="dim",
            )
