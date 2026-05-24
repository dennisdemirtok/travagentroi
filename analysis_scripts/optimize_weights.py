#!/usr/bin/env python3
"""Viktoptimering utan data leakage.

Kör grid search + korsvalidering på historisk data med korrekt filtrering
av framtida starter. Hämtar data 2+ år bakåt för robust optimering.

Användning:
    python optimize_weights.py
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

# Lägg till projektroten i sökvägen
sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig, FactorWeights
from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, RaceEntry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Konstanter ───────────────────────────────────────────────────────────────

FACTOR_NAMES = [
    "time_analysis",
    "prize_index",
    "form_curve",
    "track_profile",
    "category_profile",
    "driver_trainer",
    "post_position",
]

# Grid search steg — 0.05 steg, total summa = 1.0
WEIGHT_STEPS = [0.03, 0.05, 0.08, 0.10, 0.13, 0.15, 0.18, 0.20, 0.25, 0.30, 0.35]

# Interaktionsvikter att testa
INTERACTION_STEPS = [0.0, 0.05, 0.10, 0.15, 0.20]


@dataclass
class RaceResult:
    """Kompakt resultat för ett lopp efter analys."""
    race_date: date
    track: str
    actual_winner: int  # hästnummer
    actual_top3: list[int]
    predicted_ranking: list[int]  # hästnummer i rank-ordning
    composite_scores: dict[int, float]  # nummer → score
    factor_scores: dict[int, dict[str, float]]  # nummer → {faktor: score}
    num_starters: int
    start_method: str  # "auto" / "volt"


@dataclass
class WeightResult:
    """Resultat av en viktkombination."""
    weights: dict[str, float]
    interaction_track_post: float = 0.0
    interaction_track_category: float = 0.0
    market_signal: bool = True
    top1_pct: float = 0.0
    top3_pct: float = 0.0
    avg_rank: float = 0.0
    num_races: int = 0


def filter_future_starts(game_round: GameRound) -> None:
    """Filtrera bort past_starts som skedde EFTER loppets datum."""
    for race in game_round.races:
        race_date = race.race_date
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race_date]
            entry.horse.past_starts = filtered


def deep_copy_round(game_round: GameRound) -> GameRound:
    """Skapa en djup kopia av en GameRound för att kunna re-analysera."""
    return game_round.model_copy(deep=True)


def extract_race_results(game_round: GameRound) -> list[RaceResult]:
    """Extrahera resultat från en analyserad omgång."""
    results = []
    for race in game_round.races:
        if not race.result_order or not race.active_entries:
            continue

        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.composite_score,
            reverse=True,
        )

        results.append(RaceResult(
            race_date=race.race_date,
            track=race.track_name,
            actual_winner=race.result_order[0],
            actual_top3=race.result_order[:3],
            predicted_ranking=[e.post_position for e in sorted_entries],
            composite_scores={e.post_position: e.composite_score for e in sorted_entries},
            factor_scores={
                e.post_position: dict(e.factor_scores)
                for e in sorted_entries
            },
            num_starters=len(sorted_entries),
            start_method=race.start_method.value,
        ))
    return results


def evaluate_weights_on_races(
    races: list[RaceResult],
    weights: dict[str, float],
    interaction_track_post: float = 0.0,
    interaction_track_category: float = 0.0,
    market_signal: bool = True,
) -> WeightResult:
    """Utvärdera en viktkombination på redan beräknade faktorpoäng.

    Istället för att köra hela analysen om igen (långsamt), tar vi
    de sparade faktorpoängen och beräknar ny composite_score med nya vikter.
    """
    total = sum(weights.values())
    if total == 0:
        return WeightResult(weights=weights, num_races=len(races))
    norm_w = {k: v / total for k, v in weights.items()}

    top1_hits = 0
    top3_hits = 0
    rank_sum = 0
    counted = 0

    for race in races:
        # Beräkna ny composite_score per häst
        new_scores: dict[int, float] = {}
        for horse_num, fs in race.factor_scores.items():
            composite = sum(
                fs.get(factor, 50.0) * norm_w.get(factor, 0)
                for factor in FACTOR_NAMES
            )
            # Interaktioner
            if interaction_track_post > 0:
                v1 = fs.get("track_profile", 50.0) / 100.0
                v2 = fs.get("post_position", 50.0) / 100.0
                composite += v1 * v2 * 100.0 * interaction_track_post

            if interaction_track_category > 0:
                v1 = fs.get("track_profile", 50.0) / 100.0
                v2 = fs.get("category_profile", 50.0) / 100.0
                composite += v1 * v2 * 100.0 * interaction_track_category

            # Sigmoid
            composite = 100.0 / (1.0 + math.exp(-0.08 * (composite - 50.0)))

            new_scores[horse_num] = composite

        # Ranking
        ranking = sorted(new_scores.items(), key=lambda x: x[1], reverse=True)
        predicted_top = [num for num, _ in ranking]

        winner = race.actual_winner
        actual_top3 = set(race.actual_top3)

        # Top-1: vår rank 1 = vinnare?
        if predicted_top and predicted_top[0] == winner:
            top1_hits += 1

        # Top-3: vinnaren i vår top-3?
        our_top3 = set(predicted_top[:3])
        if winner in our_top3:
            top3_hits += 1

        # Snittrank för vinnaren
        if winner in predicted_top:
            rank_sum += predicted_top.index(winner) + 1
        else:
            rank_sum += len(predicted_top)

        counted += 1

    if counted == 0:
        return WeightResult(weights=weights, num_races=0)

    return WeightResult(
        weights=weights,
        interaction_track_post=interaction_track_post,
        interaction_track_category=interaction_track_category,
        market_signal=market_signal,
        top1_pct=top1_hits / counted,
        top3_pct=top3_hits / counted,
        avg_rank=rank_sum / counted,
        num_races=counted,
    )


def generate_weight_candidates(n_factors: int = 7, step: float = 0.05) -> list[dict[str, float]]:
    """Generera viktkombinationer som summerar till ~1.0.

    Använder smart sampling istället för fullständig grid search
    (7 faktorer med 10 steg = 10^7 = 10M kombinationer → för långsamt).
    """
    candidates = []

    # Strategi 1: Baserat på nuvarande vikter ± perturbationer
    base = {
        "time_analysis": 0.09,
        "prize_index": 0.13,
        "form_curve": 0.07,
        "track_profile": 0.33,
        "category_profile": 0.15,
        "driver_trainer": 0.07,
        "post_position": 0.16,
    }

    # Nuvarande vikter
    candidates.append(base.copy())

    # Strategi 2: Perturbera en faktor i taget
    for factor in FACTOR_NAMES:
        for delta in [-0.10, -0.07, -0.05, -0.03, 0.03, 0.05, 0.07, 0.10]:
            w = base.copy()
            new_val = max(0.03, w[factor] + delta)
            w[factor] = new_val
            # Normalisera
            total = sum(w.values())
            w = {k: round(v / total, 4) for k, v in w.items()}
            candidates.append(w)

    # Strategi 3: Perturbera två faktorer
    for f1, f2 in itertools.combinations(FACTOR_NAMES, 2):
        for d1, d2 in [(0.05, -0.05), (-0.05, 0.05), (0.10, -0.10), (-0.10, 0.10)]:
            w = base.copy()
            w[f1] = max(0.03, w[f1] + d1)
            w[f2] = max(0.03, w[f2] + d2)
            total = sum(w.values())
            w = {k: round(v / total, 4) for k, v in w.items()}
            candidates.append(w)

    # Strategi 4: Extrema vikter — testa om en faktor dominerar ännu mer
    for dominant in FACTOR_NAMES:
        for dom_weight in [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
            w = {f: 0.03 for f in FACTOR_NAMES}
            remaining = 1.0 - dom_weight
            others = [f for f in FACTOR_NAMES if f != dominant]
            each = remaining / len(others)
            w[dominant] = dom_weight
            for f in others:
                w[f] = round(each, 4)
            candidates.append(w)

    # Strategi 5: Korrelation-baserade vikter (evaluator-metoden)
    # Dessa läggs till dynamiskt baserat på data

    # Strategi 6: Lika vikter som baseline
    equal = {f: round(1.0 / 7, 4) for f in FACTOR_NAMES}
    candidates.append(equal)

    # Strategi 7: Top-3 faktorer boosted
    for f1, f2, f3 in itertools.combinations(FACTOR_NAMES, 3):
        w = {f: 0.04 for f in FACTOR_NAMES}
        w[f1] = 0.25
        w[f2] = 0.20
        w[f3] = 0.15
        total = sum(w.values())
        w = {k: round(v / total, 4) for k, v in w.items()}
        candidates.append(w)

    # Ta bort dubbletter
    unique = []
    seen = set()
    for w in candidates:
        key = tuple(round(w[f], 3) for f in FACTOR_NAMES)
        if key not in seen:
            seen.add(key)
            unique.append(w)

    logger.info(f"Genererade {len(unique)} unika viktkombinationer")
    return unique


def k_fold_split(races: list[RaceResult], k: int = 5) -> list[tuple[list[RaceResult], list[RaceResult]]]:
    """Dela data i k folds för korsvalidering. Tidsmässig split."""
    # Sortera efter datum
    sorted_races = sorted(races, key=lambda r: r.race_date)
    fold_size = len(sorted_races) // k
    folds = []

    for i in range(k):
        start = i * fold_size
        end = start + fold_size if i < k - 1 else len(sorted_races)
        test = sorted_races[start:end]
        train = sorted_races[:start] + sorted_races[end:]
        folds.append((train, test))

    return folds


async def fetch_all_data(
    client: ATGClient,
    game_types: list[str],
    start_date: date,
    end_date: date,
) -> list[GameRound]:
    """Hämta all historisk data för backtesting."""
    all_rounds = []

    for gt in game_types:
        logger.info(f"Hämtar {gt} från {start_date} till {end_date}...")
        count = 0
        async for day, game_round in client.fetch_historical_rounds_iter(
            gt, start_date, end_date
        ):
            if game_round and game_round.is_finished:
                all_rounds.append(game_round)
                count += 1
                if count % 10 == 0:
                    logger.info(f"  {gt}: {count} omgångar hämtade...")

        logger.info(f"  {gt}: totalt {count} avslutade omgångar")

    logger.info(f"Totalt: {len(all_rounds)} omgångar")
    return all_rounds


def analyze_round_with_weights(
    game_round: GameRound,
    weights: FactorWeights,
    enable_market_signal: bool = True,
) -> list[RaceResult]:
    """Kör analys på en omgång med specifika vikter och returnera resultat."""
    config = AnalysisConfig(weights=weights)
    analyzer = CompositeAnalyzer(config)

    # Kopia så vi inte muterar original
    gr = deep_copy_round(game_round)

    # KRITISKT: Filtrera bort framtida starter
    filter_future_starts(gr)

    # Kör analys
    analyzer.analyze_round(gr)

    return extract_race_results(gr)


async def main():
    """Huvudlogik: hämta data → grid search → korsvalidering → rapport."""
    start_time = time.time()

    # ══════════════════════════════════════════════════════════════════════
    # STEG 1: Hämta data
    # ══════════════════════════════════════════════════════════════════════
    logger.info("=" * 70)
    logger.info("STEG 1: Hämtar historisk data")
    logger.info("=" * 70)

    client = ATGClient()

    # 2 år bakåt för robust optimering
    end = date(2026, 2, 21)
    start_v75 = date(2024, 1, 1)   # ~2 år V75
    start_v85 = date(2024, 3, 1)   # V85 data

    all_rounds: list[GameRound] = []

    # Hämta V75
    v75_rounds = await fetch_all_data(client, ["V75"], start_v75, end)
    all_rounds.extend(v75_rounds)

    # Hämta V85
    v85_rounds = await fetch_all_data(client, ["V85"], start_v85, end)
    all_rounds.extend(v85_rounds)

    logger.info(f"Total data: {len(all_rounds)} omgångar "
                f"({len(v75_rounds)} V75 + {len(v85_rounds)} V85)")

    if not all_rounds:
        logger.error("Ingen data hämtad! Avbryter.")
        return

    # ══════════════════════════════════════════════════════════════════════
    # STEG 2: Första backtest med nuvarande vikter (utan leakage)
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 2: Backtest med nuvarande vikter (UTAN data leakage)")
    logger.info("=" * 70)

    current_weights = FactorWeights()
    all_race_results: list[RaceResult] = []

    for gr in all_rounds:
        results = analyze_round_with_weights(gr, current_weights)
        all_race_results.extend(results)

    logger.info(f"Totalt {len(all_race_results)} lopp analyserade")

    # Beräkna precision
    baseline = evaluate_weights_on_races(
        all_race_results,
        current_weights.as_dict(),
        current_weights.interaction_track_post,
        current_weights.interaction_track_category,
    )
    logger.info(f"NUVARANDE VIKTER (utan leakage):")
    logger.info(f"  Top-1: {baseline.top1_pct:.1%}")
    logger.info(f"  Top-3: {baseline.top3_pct:.1%}")
    logger.info(f"  Snittrank: {baseline.avg_rank:.2f}")

    # ══════════════════════════════════════════════════════════════════════
    # STEG 3: Faktorkorrelation (utan leakage)
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 3: Faktorkorrelation (utan leakage)")
    logger.info("=" * 70)

    factor_correlations: dict[str, float] = {}
    factor_top1: dict[str, float] = {}
    factor_top3: dict[str, float] = {}

    for factor in FACTOR_NAMES:
        all_scores: list[float] = []
        all_wins: list[float] = []
        f_top1 = 0
        f_top3 = 0
        counted = 0

        for race in all_race_results:
            winner = race.actual_winner
            factor_by_horse = {
                h: fs.get(factor, 50.0)
                for h, fs in race.factor_scores.items()
            }
            if not factor_by_horse:
                continue

            for h, score in factor_by_horse.items():
                all_scores.append(score)
                all_wins.append(1.0 if h == winner else 0.0)

            ranking = sorted(factor_by_horse.items(), key=lambda x: x[1], reverse=True)
            if ranking[0][0] == winner:
                f_top1 += 1
            if winner in {num for num, _ in ranking[:3]}:
                f_top3 += 1
            counted += 1

        if len(all_scores) >= 2:
            x = np.array(all_scores)
            y = np.array(all_wins)
            n1 = y.sum()
            n0 = len(y) - n1
            if n0 > 0 and n1 > 0:
                mean1 = x[y == 1].mean()
                mean0 = x[y == 0].mean()
                s = x.std()
                if s > 0:
                    n = len(x)
                    corr = (mean1 - mean0) / s * math.sqrt(n1 * n0 / (n * n))
                    factor_correlations[factor] = corr

        if counted > 0:
            factor_top1[factor] = f_top1 / counted
            factor_top3[factor] = f_top3 / counted

        logger.info(
            f"  {factor:20s}  korr={factor_correlations.get(factor, 0):.3f}  "
            f"Top-1={factor_top1.get(factor, 0):.1%}  "
            f"Top-3={factor_top3.get(factor, 0):.1%}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # STEG 4: Grid Search
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 4: Grid Search — Bästa viktkombination")
    logger.info("=" * 70)

    candidates = generate_weight_candidates()

    # Lägg till korrelation-baserade vikter
    corr_weights = {}
    for f in FACTOR_NAMES:
        strength = abs(factor_correlations.get(f, 0)) * 0.6 + factor_top3.get(f, 0) * 0.4
        corr_weights[f] = max(0.03, strength)
    total = sum(corr_weights.values())
    corr_weights = {k: round(v / total, 4) for k, v in corr_weights.items()}
    candidates.append(corr_weights)

    # Testa alla viktkombinationer
    best_result: Optional[WeightResult] = None
    all_results: list[WeightResult] = []

    for i, weights in enumerate(candidates):
        # Testa med standardinteraktioner
        result = evaluate_weights_on_races(
            all_race_results, weights,
            interaction_track_post=0.15,
            interaction_track_category=0.10,
        )
        all_results.append(result)

        if best_result is None or result.top1_pct > best_result.top1_pct:
            best_result = result

        if (i + 1) % 100 == 0:
            logger.info(
                f"  Testat {i+1}/{len(candidates)} — "
                f"bäst hittills: Top-1={best_result.top1_pct:.1%}"
            )

    # Top 10 resultat
    all_results.sort(key=lambda r: r.top1_pct, reverse=True)

    logger.info(f"\nTopp 10 viktkombinationer (av {len(all_results)}):")
    logger.info(f"{'#':>3}  {'Top-1':>6}  {'Top-3':>6}  {'Rank':>5}  Vikter")
    for i, r in enumerate(all_results[:10]):
        w_str = "  ".join(f"{f[:4]}={r.weights[f]:.2f}" for f in FACTOR_NAMES)
        logger.info(f"{i+1:>3}  {r.top1_pct:>5.1%}  {r.top3_pct:>5.1%}  {r.avg_rank:>5.2f}  {w_str}")

    # ══════════════════════════════════════════════════════════════════════
    # STEG 5: Optimera interaktionsvikter
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 5: Optimera interaktionsvikter")
    logger.info("=" * 70)

    best_weights = all_results[0].weights

    best_interaction_result = None
    for itp in INTERACTION_STEPS:
        for itc in INTERACTION_STEPS:
            result = evaluate_weights_on_races(
                all_race_results, best_weights,
                interaction_track_post=itp,
                interaction_track_category=itc,
            )
            if best_interaction_result is None or result.top1_pct > best_interaction_result.top1_pct:
                best_interaction_result = result
                best_interaction_result.interaction_track_post = itp
                best_interaction_result.interaction_track_category = itc

    logger.info(
        f"Bästa interaktioner: "
        f"track×post={best_interaction_result.interaction_track_post:.2f}  "
        f"track×cat={best_interaction_result.interaction_track_category:.2f}"
    )
    logger.info(
        f"  Top-1: {best_interaction_result.top1_pct:.1%}  "
        f"Top-3: {best_interaction_result.top3_pct:.1%}  "
        f"Rank: {best_interaction_result.avg_rank:.2f}"
    )

    # ══════════════════════════════════════════════════════════════════════
    # STEG 6: Korsvalidering (5-fold temporal)
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 6: 5-fold korsvalidering")
    logger.info("=" * 70)

    folds = k_fold_split(all_race_results, k=5)
    cv_top1 = []
    cv_top3 = []
    cv_rank = []

    for fold_idx, (train, test) in enumerate(folds):
        # Optimera på train
        best_fold = None
        # Testa top-20 vikter från grid search
        for result_template in all_results[:20]:
            result = evaluate_weights_on_races(
                train, result_template.weights,
                best_interaction_result.interaction_track_post,
                best_interaction_result.interaction_track_category,
            )
            if best_fold is None or result.top1_pct > best_fold.top1_pct:
                best_fold = result

        # Utvärdera på test
        test_result = evaluate_weights_on_races(
            test, best_fold.weights,
            best_interaction_result.interaction_track_post,
            best_interaction_result.interaction_track_category,
        )

        cv_top1.append(test_result.top1_pct)
        cv_top3.append(test_result.top3_pct)
        cv_rank.append(test_result.avg_rank)

        logger.info(
            f"  Fold {fold_idx+1}: "
            f"Train Top-1={best_fold.top1_pct:.1%} → "
            f"Test Top-1={test_result.top1_pct:.1%}  "
            f"Test Top-3={test_result.top3_pct:.1%}  "
            f"(train={len(train)}, test={len(test)})"
        )

    mean_top1 = np.mean(cv_top1)
    std_top1 = np.std(cv_top1)
    mean_top3 = np.mean(cv_top3)
    std_top3 = np.std(cv_top3)
    mean_rank = np.mean(cv_rank)

    logger.info(f"\nCV-resultat:")
    logger.info(f"  Top-1: {mean_top1:.1%} ± {std_top1:.1%}")
    logger.info(f"  Top-3: {mean_top3:.1%} ± {std_top3:.1%}")
    logger.info(f"  Rank:  {mean_rank:.2f}")

    # ══════════════════════════════════════════════════════════════════════
    # STEG 7: Marknadssignal A/B-test (utan leakage)
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 7: Marknadssignal A/B-test (utan leakage)")
    logger.info("=" * 70)

    # Marknadssignalen beror på odds och win_rate från past_starts
    # Vi behöver köra fullständig analys med och utan marknadssignal
    # men faktorpoängen påverkas inte — bara den slutliga composite_score
    # Marknadssignalen adderar en bonus i composite.py steg 3b

    # Vi kan simulera detta: för varje häst, kolla om den hade bonus
    # Enklast: kör analyserna helt om med market_signal av/på

    # Kör utan marknadssignal
    no_market_weights = FactorWeights(
        **{k: v for k, v in best_weights.items()},
        interaction_track_post=best_interaction_result.interaction_track_post,
        interaction_track_category=best_interaction_result.interaction_track_category,
    )

    races_no_market: list[RaceResult] = []
    races_with_market: list[RaceResult] = []

    for gr in all_rounds:
        # Utan marknadssignal
        config_no = AnalysisConfig(weights=no_market_weights)
        analyzer_no = CompositeAnalyzer(config_no)
        gr_no = deep_copy_round(gr)
        filter_future_starts(gr_no)

        # Temporärt ta bort odds för att disable market signal
        for race in gr_no.races:
            for entry in race.active_entries:
                entry.odds = None  # Ingen odds → ingen marknadssignal
        analyzer_no.analyze_round(gr_no)
        races_no_market.extend(extract_race_results(gr_no))

        # Med marknadssignal (odds bevarade)
        config_with = AnalysisConfig(weights=no_market_weights)
        analyzer_with = CompositeAnalyzer(config_with)
        gr_with = deep_copy_round(gr)
        filter_future_starts(gr_with)
        analyzer_with.analyze_round(gr_with)
        races_with_market.extend(extract_race_results(gr_with))

    # Utvärdera
    result_no = evaluate_weights_on_races(
        races_no_market, best_weights,
        best_interaction_result.interaction_track_post,
        best_interaction_result.interaction_track_category,
    )
    result_with = evaluate_weights_on_races(
        races_with_market, best_weights,
        best_interaction_result.interaction_track_post,
        best_interaction_result.interaction_track_category,
    )

    logger.info(f"Utan marknadssignal:")
    logger.info(f"  Top-1: {result_no.top1_pct:.1%}  Top-3: {result_no.top3_pct:.1%}  Rank: {result_no.avg_rank:.2f}")
    logger.info(f"Med marknadssignal:")
    logger.info(f"  Top-1: {result_with.top1_pct:.1%}  Top-3: {result_with.top3_pct:.1%}  Rank: {result_with.avg_rank:.2f}")

    diff_top1 = result_with.top1_pct - result_no.top1_pct
    diff_top3 = result_with.top3_pct - result_no.top3_pct
    logger.info(f"Skillnad: Top-1 {diff_top1:+.1%}  Top-3 {diff_top3:+.1%}")
    if diff_top1 > 0:
        logger.info("→ Marknadssignalen HJÄLPER — behåll den!")
    else:
        logger.info("→ Marknadssignalen hjälper INTE utan leakage — överväg att ta bort")

    # ══════════════════════════════════════════════════════════════════════
    # STEG 8: V75 vs V85 separat utvärdering
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("STEG 8: V75 vs V85 separat")
    logger.info("=" * 70)

    # Analysera V75 och V85 separat med bästa vikter
    v75_results: list[RaceResult] = []
    v85_results: list[RaceResult] = []

    for gr in all_rounds:
        results = analyze_round_with_weights(gr, no_market_weights)
        if gr.game_type == "V75":
            v75_results.extend(results)
        elif gr.game_type == "V85":
            v85_results.extend(results)

    if v75_results:
        v75_eval = evaluate_weights_on_races(
            v75_results, best_weights,
            best_interaction_result.interaction_track_post,
            best_interaction_result.interaction_track_category,
        )
        logger.info(f"V75 ({v75_eval.num_races} lopp):")
        logger.info(f"  Top-1: {v75_eval.top1_pct:.1%}  Top-3: {v75_eval.top3_pct:.1%}  Rank: {v75_eval.avg_rank:.2f}")

    if v85_results:
        v85_eval = evaluate_weights_on_races(
            v85_results, best_weights,
            best_interaction_result.interaction_track_post,
            best_interaction_result.interaction_track_category,
        )
        logger.info(f"V85 ({v85_eval.num_races} lopp):")
        logger.info(f"  Top-1: {v85_eval.top1_pct:.1%}  Top-3: {v85_eval.top3_pct:.1%}  Rank: {v85_eval.avg_rank:.2f}")

    # ══════════════════════════════════════════════════════════════════════
    # STEG 9: Sammanfattning & Rekommendation
    # ══════════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("SAMMANFATTNING")
    logger.info("=" * 70)

    elapsed = time.time() - start_time

    logger.info(f"Data: {len(all_rounds)} omgångar, {len(all_race_results)} lopp")
    logger.info(f"Tid: {elapsed:.0f}s")
    logger.info("")

    logger.info("NUVARANDE VIKTER (med leakage-kalibrering):")
    current = FactorWeights()
    for f in FACTOR_NAMES:
        logger.info(f"  {f:20s}: {getattr(current, f):.2f}")
    logger.info(f"  interactions:       track×post={current.interaction_track_post:.2f}  track×cat={current.interaction_track_category:.2f}")
    logger.info(f"  → Top-1={baseline.top1_pct:.1%}  Top-3={baseline.top3_pct:.1%}  Rank={baseline.avg_rank:.2f}")

    logger.info("")
    logger.info("NYA OPTIMERADE VIKTER (utan leakage):")
    for f in FACTOR_NAMES:
        old = getattr(current, f)
        new = best_weights[f]
        delta = new - old
        arrow = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "=")
        logger.info(f"  {f:20s}: {new:.3f}  ({arrow} {delta:+.3f})")
    logger.info(
        f"  interactions:       "
        f"track×post={best_interaction_result.interaction_track_post:.2f}  "
        f"track×cat={best_interaction_result.interaction_track_category:.2f}"
    )
    logger.info(
        f"  → Top-1={best_interaction_result.top1_pct:.1%}  "
        f"Top-3={best_interaction_result.top3_pct:.1%}  "
        f"Rank={best_interaction_result.avg_rank:.2f}"
    )

    logger.info("")
    logger.info(f"KORSVALIDERING: Top-1={mean_top1:.1%} ± {std_top1:.1%}")
    logger.info(f"MARKNADSSIGNAL: {'BEHÅLL' if diff_top1 > 0 else 'TA BORT'} ({diff_top1:+.1%} Top-1)")

    # ── Spara resultat ──────────────────────────────────────────────────
    output = {
        "date": date.today().isoformat(),
        "data_summary": {
            "total_rounds": len(all_rounds),
            "total_races": len(all_race_results),
            "v75_rounds": len(v75_rounds),
            "v85_rounds": len(v85_rounds),
            "date_range": f"{start_v75} → {end}",
        },
        "current_weights": current.as_dict(),
        "current_precision": {
            "top1": round(baseline.top1_pct, 4),
            "top3": round(baseline.top3_pct, 4),
            "avg_rank": round(baseline.avg_rank, 3),
        },
        "optimized_weights": best_weights,
        "optimized_interactions": {
            "track_post": best_interaction_result.interaction_track_post,
            "track_category": best_interaction_result.interaction_track_category,
        },
        "optimized_precision": {
            "top1": round(best_interaction_result.top1_pct, 4),
            "top3": round(best_interaction_result.top3_pct, 4),
            "avg_rank": round(best_interaction_result.avg_rank, 3),
        },
        "cross_validation": {
            "k": 5,
            "top1_mean": round(float(mean_top1), 4),
            "top1_std": round(float(std_top1), 4),
            "top3_mean": round(float(mean_top3), 4),
            "top3_std": round(float(std_top3), 4),
        },
        "market_signal": {
            "keep": diff_top1 > 0,
            "without_top1": round(result_no.top1_pct, 4),
            "with_top1": round(result_with.top1_pct, 4),
            "diff_top1": round(diff_top1, 4),
        },
        "factor_correlations": {
            f: {
                "correlation": round(factor_correlations.get(f, 0), 4),
                "top1": round(factor_top1.get(f, 0), 4),
                "top3": round(factor_top3.get(f, 0), 4),
            }
            for f in FACTOR_NAMES
        },
        "top10_combinations": [
            {
                "weights": r.weights,
                "top1": round(r.top1_pct, 4),
                "top3": round(r.top3_pct, 4),
                "avg_rank": round(r.avg_rank, 3),
            }
            for r in all_results[:10]
        ],
    }

    output_path = Path(__file__).parent / "optimization_results.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    logger.info(f"\nResultat sparat till: {output_path}")

    # ── Skriv ut kod för att uppdatera config.py ────────────────────────
    logger.info("")
    logger.info("=" * 70)
    logger.info("KOPIERA TILL config.py:")
    logger.info("=" * 70)
    logger.info("@dataclass")
    logger.info("class FactorWeights:")
    for f in FACTOR_NAMES:
        logger.info(f"    {f}: float = {best_weights[f]:.3f}")
    logger.info(f"    interaction_track_post: float = {best_interaction_result.interaction_track_post}")
    logger.info(f"    interaction_track_category: float = {best_interaction_result.interaction_track_category}")


if __name__ == "__main__":
    asyncio.run(main())
