#!/usr/bin/env python3
"""Finjustera vikter kring bästa kombinationer från grid search.

Testar fler varianter runt de 3 bästa kombinationerna:
1. prize_index (33%) + category_profile (26%) + post_position (20%)
2. category_profile dominant (35%) + lika resten
3. category (33%) + driver_trainer (26%) + post_position (20%)

Körs efter optimize_weights.py.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig, FactorWeights
from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

FACTOR_NAMES = [
    "time_analysis", "prize_index", "form_curve",
    "track_profile", "category_profile", "driver_trainer", "post_position",
]


def filter_future_starts(game_round: GameRound) -> None:
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


def evaluate_combo(all_races, weights, itp=0.15, itc=0.10):
    """Snabb utvärdering av en viktkombination på faktorpoäng."""
    total = sum(weights.values())
    if total == 0:
        return 0, 0, 99
    norm_w = {k: v / total for k, v in weights.items()}

    top1 = 0
    top3 = 0
    rank_sum = 0
    n = 0

    for race in all_races:
        winner = race["winner"]
        actual_t3 = set(race["top3"])
        scores = {}
        for h, fs in race["factors"].items():
            c = sum(fs.get(f, 50.0) * norm_w.get(f, 0) for f in FACTOR_NAMES)
            if itp > 0:
                c += (fs.get("track_profile", 50) / 100) * (fs.get("post_position", 50) / 100) * 100 * itp
            if itc > 0:
                c += (fs.get("track_profile", 50) / 100) * (fs.get("category_profile", 50) / 100) * 100 * itc
            c = 100.0 / (1.0 + math.exp(-0.08 * (c - 50.0)))
            scores[h] = c

        ranking = sorted(scores, key=scores.get, reverse=True)
        if ranking[0] == winner:
            top1 += 1
        if winner in set(ranking[:3]):
            top3 += 1
        if winner in ranking:
            rank_sum += ranking.index(winner) + 1
        else:
            rank_sum += len(ranking)
        n += 1

    if n == 0:
        return 0, 0, 99
    return top1 / n, top3 / n, rank_sum / n


async def main():
    start_time = time.time()

    # Hämta data
    client = ATGClient()
    all_rounds: list[GameRound] = []
    end = date(2026, 2, 21)

    for gt, start in [("V75", date(2024, 1, 1)), ("V85", date(2024, 3, 1))]:
        logger.info(f"Hämtar {gt}...")
        async for day, gr in client.fetch_historical_rounds_iter(gt, start, end):
            if gr and gr.is_finished:
                all_rounds.append(gr)

    logger.info(f"Total: {len(all_rounds)} omgångar")

    # Analysera med standardvikter och extrahera faktorpoäng
    logger.info("Analyserar alla omgångar (en gång)...")
    all_races = []
    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)
        analyzer = CompositeAnalyzer(AnalysisConfig())
        analyzer.analyze_round(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue
            factors = {}
            for e in race.active_entries:
                factors[e.post_position] = dict(e.factor_scores)
            all_races.append({
                "winner": race.result_order[0],
                "top3": race.result_order[:3],
                "factors": factors,
                "track": race.track_name,
                "method": race.start_method.value,
                "game_type": gr.game_type,
                "date": race.race_date.isoformat(),
            })

    logger.info(f"Totalt {len(all_races)} lopp med resultat")

    # ── Finjusterad grid search ──────────────────────────────────────────
    logger.info("\nFinjusterad grid search (0.02 steg)...")

    # Grundvikter att perturbera kring
    bases = [
        # Bästa från förra körningen
        {"time_analysis": 0.05, "prize_index": 0.33, "form_curve": 0.05,
         "track_profile": 0.05, "category_profile": 0.26, "driver_trainer": 0.05, "post_position": 0.20},
        # Runner-up: category dominant
        {"time_analysis": 0.11, "prize_index": 0.11, "form_curve": 0.11,
         "track_profile": 0.11, "category_profile": 0.35, "driver_trainer": 0.11, "post_position": 0.11},
        # Prize + category combo
        {"time_analysis": 0.06, "prize_index": 0.30, "form_curve": 0.06,
         "track_profile": 0.06, "category_profile": 0.30, "driver_trainer": 0.06, "post_position": 0.16},
        # Post position boosted
        {"time_analysis": 0.06, "prize_index": 0.25, "form_curve": 0.06,
         "track_profile": 0.06, "category_profile": 0.25, "driver_trainer": 0.06, "post_position": 0.26},
    ]

    candidates = set()
    for base in bases:
        # Perturbera varje faktor ±0.02, ±0.04
        for f in FACTOR_NAMES:
            for delta in [-0.06, -0.04, -0.02, 0.0, 0.02, 0.04, 0.06]:
                w = base.copy()
                w[f] = max(0.03, w[f] + delta)
                total = sum(w.values())
                key = tuple(round(w[f_] / total, 4) for f_ in FACTOR_NAMES)
                candidates.add(key)

        # Perturbera par
        for f1, f2 in itertools.combinations(FACTOR_NAMES, 2):
            for d in [0.03, 0.05, -0.03, -0.05]:
                w = base.copy()
                w[f1] = max(0.03, w[f1] + d)
                w[f2] = max(0.03, w[f2] - d)
                total = sum(w.values())
                key = tuple(round(w[f_] / total, 4) for f_ in FACTOR_NAMES)
                candidates.add(key)

    # Extra: prova alla kombinationer av prize_index/category_profile/post_position
    for pi in np.arange(0.15, 0.45, 0.03):
        for cp in np.arange(0.10, 0.40, 0.03):
            for pp in np.arange(0.08, 0.30, 0.03):
                remaining = 1.0 - pi - cp - pp
                if remaining < 0.12:  # minst 0.03 per övrig faktor
                    continue
                each = remaining / 4  # fördela lika
                w = {
                    "time_analysis": each, "prize_index": pi, "form_curve": each,
                    "track_profile": each, "category_profile": cp,
                    "driver_trainer": each, "post_position": pp,
                }
                total = sum(w.values())
                key = tuple(round(w[f] / total, 4) for f in FACTOR_NAMES)
                candidates.add(key)

    # Konvertera tillbaka till dicts
    candidate_dicts = []
    for key in candidates:
        w = {f: v for f, v in zip(FACTOR_NAMES, key)}
        candidate_dicts.append(w)

    logger.info(f"Testar {len(candidate_dicts)} unika kombinationer...")

    # Testa alla
    results = []
    for i, w in enumerate(candidate_dicts):
        t1, t3, rank = evaluate_combo(all_races, w)
        results.append((t1, t3, rank, w))
        if (i + 1) % 500 == 0:
            best_so_far = max(results, key=lambda x: x[0])
            logger.info(f"  {i+1}/{len(candidate_dicts)} — bäst: Top-1={best_so_far[0]:.1%}")

    # Sortera
    results.sort(key=lambda x: (-x[0], -x[1], x[2]))

    logger.info("\n" + "=" * 80)
    logger.info("TOPP 20 VIKTKOMBINATIONER")
    logger.info("=" * 80)
    logger.info(f"{'#':>3}  {'Top-1':>6}  {'Top-3':>6}  {'Rank':>5}  Vikter")
    for i, (t1, t3, rank, w) in enumerate(results[:20]):
        w_str = "  ".join(f"{f[:5]}={w[f]:.3f}" for f in FACTOR_NAMES)
        logger.info(f"{i+1:>3}  {t1:>5.1%}  {t3:>5.1%}  {rank:>5.2f}  {w_str}")

    # ── Testa interaktioner för bästa vikter ──
    logger.info("\nOptimerar interaktioner för bästa vikter...")
    best_w = results[0][3]
    best_combo = None
    for itp in [0.0, 0.05, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]:
        for itc in [0.0, 0.05, 0.10, 0.12, 0.15, 0.18, 0.20]:
            t1, t3, rank = evaluate_combo(all_races, best_w, itp, itc)
            if best_combo is None or t1 > best_combo[0] or (t1 == best_combo[0] and t3 > best_combo[1]):
                best_combo = (t1, t3, rank, itp, itc)
    logger.info(f"Bästa interaktioner: track×post={best_combo[3]:.2f}  track×cat={best_combo[4]:.2f}")
    logger.info(f"  Top-1={best_combo[0]:.1%}  Top-3={best_combo[1]:.1%}  Rank={best_combo[2]:.2f}")

    # ── Korsvalidering ──
    logger.info("\n5-fold korsvalidering...")
    sorted_races = sorted(all_races, key=lambda r: r["date"])
    fold_size = len(sorted_races) // 5
    cv_t1 = []
    cv_t3 = []

    for fold in range(5):
        start_idx = fold * fold_size
        end_idx = start_idx + fold_size if fold < 4 else len(sorted_races)
        test = sorted_races[start_idx:end_idx]
        train = sorted_races[:start_idx] + sorted_races[end_idx:]

        # Hitta bästa vikter på train
        best_train = None
        for _, _, _, w in results[:30]:
            t1, t3, rank = evaluate_combo(train, w, best_combo[3], best_combo[4])
            if best_train is None or t1 > best_train[0]:
                best_train = (t1, t3, rank, w)

        # Test
        test_t1, test_t3, test_rank = evaluate_combo(test, best_train[3], best_combo[3], best_combo[4])
        cv_t1.append(test_t1)
        cv_t3.append(test_t3)
        logger.info(f"  Fold {fold+1}: Train={best_train[0]:.1%} → Test Top-1={test_t1:.1%}  Top-3={test_t3:.1%}")

    logger.info(f"\nCV: Top-1={np.mean(cv_t1):.1%} ± {np.std(cv_t1):.1%}  Top-3={np.mean(cv_t3):.1%} ± {np.std(cv_t3):.1%}")

    # ── V75 vs V85 separat ──
    logger.info("\nV75 vs V85 separat:")
    v75_races = [r for r in all_races if r["game_type"] == "V75"]
    v85_races = [r for r in all_races if r["game_type"] == "V85"]

    if v75_races:
        t1, t3, rank = evaluate_combo(v75_races, best_w, best_combo[3], best_combo[4])
        logger.info(f"  V75 ({len(v75_races)} lopp): Top-1={t1:.1%}  Top-3={t3:.1%}  Rank={rank:.2f}")
    if v85_races:
        t1, t3, rank = evaluate_combo(v85_races, best_w, best_combo[3], best_combo[4])
        logger.info(f"  V85 ({len(v85_races)} lopp): Top-1={t1:.1%}  Top-3={t3:.1%}  Rank={rank:.2f}")

    # ── Jämförelse med nuvarande vikter ──
    logger.info("\n" + "=" * 80)
    logger.info("JÄMFÖRELSE")
    logger.info("=" * 80)

    old_w = {"time_analysis": 0.09, "prize_index": 0.13, "form_curve": 0.07,
             "track_profile": 0.33, "category_profile": 0.15, "driver_trainer": 0.07, "post_position": 0.16}
    old_t1, old_t3, old_rank = evaluate_combo(all_races, old_w, 0.15, 0.10)
    new_t1, new_t3, new_rank = evaluate_combo(all_races, best_w, best_combo[3], best_combo[4])

    logger.info(f"GAMLA vikter: Top-1={old_t1:.1%}  Top-3={old_t3:.1%}  Rank={old_rank:.2f}")
    logger.info(f"NYA vikter:   Top-1={new_t1:.1%}  Top-3={new_t3:.1%}  Rank={new_rank:.2f}")
    logger.info(f"Förbättring:  Top-1={new_t1 - old_t1:+.1%}  Top-3={new_t3 - old_t3:+.1%}  Rank={new_rank - old_rank:+.2f}")

    # ── Slutliga vikter ──
    logger.info("\n" + "=" * 80)
    logger.info("SLUTLIGA REKOMMENDERADE VIKTER")
    logger.info("=" * 80)
    for f in FACTOR_NAMES:
        old = old_w[f]
        new = best_w[f]
        arrow = "↑" if new > old + 0.01 else ("↓" if new < old - 0.01 else "=")
        logger.info(f"  {f:20s}: {new:.4f}  (var {old:.2f}) {arrow}")
    logger.info(f"  interaction_track_post:     {best_combo[3]:.2f}")
    logger.info(f"  interaction_track_category: {best_combo[4]:.2f}")

    # Spara
    output = {
        "best_weights": best_w,
        "interaction_track_post": best_combo[3],
        "interaction_track_category": best_combo[4],
        "top1": round(new_t1, 4),
        "top3": round(new_t3, 4),
        "avg_rank": round(new_rank, 3),
        "cv_top1_mean": round(float(np.mean(cv_t1)), 4),
        "cv_top1_std": round(float(np.std(cv_t1)), 4),
        "cv_top3_mean": round(float(np.mean(cv_t3)), 4),
        "cv_top3_std": round(float(np.std(cv_t3)), 4),
        "num_races": len(all_races),
        "num_candidates_tested": len(candidate_dicts),
        "top20": [
            {"weights": w, "top1": round(t1, 4), "top3": round(t3, 4), "rank": round(rank, 3)}
            for t1, t3, rank, w in results[:20]
        ],
    }
    Path("fine_tune_results.json").write_text(json.dumps(output, indent=2))
    logger.info(f"\nTid: {time.time() - start_time:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
