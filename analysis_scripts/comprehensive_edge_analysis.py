#!/usr/bin/env python3
"""Comprehensive Betting Edge Analysis — Model vs Market vs Experts.

PART 1: Betting edge analysis (streck vs win rate, spik accuracy, upset risk, calibration)
PART 2: Expert analysis (V85 2026-02-14, consensus vs model vs market)
PART 3: Can streckprocent optimize the model? (blending, anti-correlation)

Loads all V75+V85 historical races the same way as model_vs_market.py.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig
from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, RaceEntry

import logging

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def filter_future_starts(game_round: GameRound) -> None:
    """Remove starts that happened after the race date (prevent data leakage)."""
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


def separator(title: str, char: str = "=", width: int = 80):
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


# ============================================================================
# DATA LOADING (same as model_vs_market.py)
# ============================================================================

async def load_all_data():
    """Load all V75+V85 historical rounds and analyze with CompositeAnalyzer."""
    client = ATGClient()
    all_rounds: list[GameRound] = []
    end = date(2026, 2, 21)

    for gt, start in [("V75", date(2024, 1, 1)), ("V85", date(2024, 3, 1))]:
        logger.info(f"Fetching {gt}...")
        async for day, gr in client.fetch_historical_rounds_iter(gt, start, end):
            if gr and gr.is_finished:
                all_rounds.append(gr)

    logger.info(f"Total: {len(all_rounds)} rounds")

    # Analyze all rounds and collect detailed race data
    logger.info("Analyzing all rounds with CompositeAnalyzer...")

    all_races = []
    skipped_no_result = 0
    skipped_no_market = 0

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)
        analyzer = CompositeAnalyzer(AnalysisConfig())
        analyzer.analyze_round(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                skipped_no_result += 1
                continue

            winner = race.result_order[0]

            # Check if market data is complete
            entries_with_bp = [
                e for e in race.active_entries if e.bet_percentage is not None
            ]
            if len(entries_with_bp) < len(race.active_entries):
                skipped_no_market += 1
                continue

            # Build detailed per-entry data
            entry_data = []
            for e in race.active_entries:
                entry_data.append({
                    "post_position": e.post_position,
                    "horse_name": e.horse.name,
                    "composite_score": e.composite_score,
                    "bet_percentage": e.bet_percentage,
                    "rank": e.rank,
                    "recommendation": e.recommendation,
                    "factor_scores": dict(e.factor_scores),
                    "is_winner": e.post_position == winner,
                })

            # Model ranking: sort by composite_score descending
            model_sorted = sorted(
                race.active_entries, key=lambda e: e.composite_score, reverse=True
            )
            model_ranking = [e.post_position for e in model_sorted]

            # Market ranking: sort by bet_percentage descending
            market_sorted = sorted(
                race.active_entries, key=lambda e: e.bet_percentage, reverse=True
            )
            market_ranking = [e.post_position for e in market_sorted]

            # Winner's data
            winner_entry = None
            for e in race.active_entries:
                if e.post_position == winner:
                    winner_entry = e
                    break

            all_races.append({
                "winner": winner,
                "model_ranking": model_ranking,
                "market_ranking": market_ranking,
                "method": race.start_method.value,
                "field_size": len(race.active_entries),
                "winner_bp": winner_entry.bet_percentage if winner_entry else None,
                "game_type": gr.game_type,
                "date": race.race_date.isoformat(),
                "track": race.track_name,
                "race_number": race.race_number,
                "entries": entry_data,
                "upset_risk": race.upset_risk,
                "upset_candidates": race.upset_candidates,
                "game_id": gr.game_id,
            })

    logger.info(
        f"Collected {len(all_races)} races with complete data "
        f"(skipped {skipped_no_result} no-result, {skipped_no_market} incomplete market)"
    )

    return all_rounds, all_races


# ============================================================================
# PART 1: BETTING EDGE ANALYSIS
# ============================================================================

def part1_betting_edge(all_races: list[dict]):
    separator("PART 1: BETTING EDGE ANALYSIS", "=", 80)

    # ── A) Model top-horse streck vs win rate ──
    part1a_model_rank_vs_streck(all_races)

    # ── B) Spike (spik) analysis ──
    part1b_spike_analysis(all_races)

    # ── C) Upset risk detection accuracy ──
    part1c_upset_risk_accuracy(all_races)

    # ── D) Impossible upsets (<5% streck winners) ──
    part1d_impossible_upsets(all_races)

    # ── E) Calibration: Model rank -> win rate WITH avg streck ──
    part1e_calibration(all_races)


def part1a_model_rank_vs_streck(all_races: list[dict]):
    separator("A) MODEL TOPPHAST STRECK vs VINSTFREKVENS (THE KEY EDGE QUESTION)", "-")

    # For model rank 1, 2, 3: what is their avg streckprocent and actual win rate?
    for target_rank in [1, 2, 3]:
        streck_values = []
        wins = 0
        total = 0

        for race in all_races:
            entries = race["entries"]
            # Sort by composite_score descending to get model ranking
            sorted_entries = sorted(entries, key=lambda e: e["composite_score"], reverse=True)

            if len(sorted_entries) < target_rank:
                continue

            entry = sorted_entries[target_rank - 1]
            bp = entry["bet_percentage"]
            if bp is not None:
                streck_values.append(bp)
                total += 1
                if entry["is_winner"]:
                    wins += 1

        if total == 0:
            continue

        avg_streck = statistics.mean(streck_values)
        win_rate = wins / total
        edge = win_rate - avg_streck

        print(f"\n  Model Rank #{target_rank}:")
        print(f"    Average streckprocent:  {avg_streck:.1%}")
        print(f"    Actual win rate:        {win_rate:.1%}")
        print(f"    EDGE (win% - streck%):  {edge:+.1%}  {'*** PROFITABLE ***' if edge > 0 else '(no edge)'}")
        print(f"    Implied odds:           {1/avg_streck:.1f}x (market) vs {1/win_rate:.1f}x (actual)")
        print(f"    Races:                  {total}")


def part1b_spike_analysis(all_races: list[dict]):
    separator("B) SPIKE (SPIK) / 2-VAL / 3-VAL ANALYSIS", "-")

    # Collect by recommendation
    by_recommendation = defaultdict(lambda: {"total": 0, "wins": 0, "streck": []})

    for race in all_races:
        for entry in race["entries"]:
            rec = entry["recommendation"]
            if rec in ("spik", "2-val", "3-val", "gardering", "strykning"):
                by_recommendation[rec]["total"] += 1
                if entry["is_winner"]:
                    by_recommendation[rec]["wins"] += 1
                if entry["bet_percentage"] is not None:
                    by_recommendation[rec]["streck"].append(entry["bet_percentage"])

    print(f"\n  {'Recommendation':<15} {'Count':>7} {'Wins':>6} {'Win%':>8} {'Avg Streck':>12} {'Edge':>10} {'Profitable?':>14}")
    print(f"  {'-'*15} {'-'*7} {'-'*6} {'-'*8} {'-'*12} {'-'*10} {'-'*14}")

    for rec in ["spik", "2-val", "3-val", "gardering", "strykning"]:
        data = by_recommendation[rec]
        if data["total"] == 0:
            continue
        win_rate = data["wins"] / data["total"]
        avg_streck = statistics.mean(data["streck"]) if data["streck"] else 0
        edge = win_rate - avg_streck
        profitable = "YES +" if edge > 0 else "no"

        print(f"  {rec:<15} {data['total']:>7} {data['wins']:>6} {win_rate:>7.1%} {avg_streck:>11.1%} {edge:>+9.1%} {profitable:>14}")

    # Detailed spik analysis
    spik_data = by_recommendation["spik"]
    if spik_data["total"] > 0:
        print(f"\n  SPIK DEEP DIVE:")
        print(f"    Total spikar:     {spik_data['total']}")
        print(f"    Winners:          {spik_data['wins']}")
        print(f"    Win rate:         {spik_data['wins']/spik_data['total']:.1%}")
        print(f"    Avg streck:       {statistics.mean(spik_data['streck']):.1%}")
        if spik_data['streck']:
            print(f"    Median streck:    {statistics.median(spik_data['streck']):.1%}")
            print(f"    Min/Max streck:   {min(spik_data['streck']):.1%} / {max(spik_data['streck']):.1%}")

        # If you bet 100kr on every spik, what's the ROI?
        # Simplified: each spik has avg_streck -> implied odds = 1/avg_streck
        # Win rate * implied_odds - 1 = ROI
        avg_s = statistics.mean(spik_data['streck'])
        wr = spik_data['wins'] / spik_data['total']
        # ROI if you bet vinnare at implied odds from streck
        if avg_s > 0:
            implied_odds = 1 / avg_s
            roi = wr * implied_odds - 1
            print(f"    Implied V-odds:   {implied_odds:.1f}x")
            print(f"    Theoretical ROI:  {roi:+.1%}")


def part1c_upset_risk_accuracy(all_races: list[dict]):
    separator("C) UPSET RISK DETECTION ACCURACY", "-")

    # Categorize by upset risk level
    # LÅG = 0-30, MEDEL = 30-50, HÖG = 50+
    risk_levels = {
        "LAG (0-30)": lambda r: r["upset_risk"] < 30,
        "MEDEL (30-50)": lambda r: 30 <= r["upset_risk"] < 50,
        "HOG (50+)": lambda r: r["upset_risk"] >= 50,
    }

    print(f"\n  {'Risk Level':<20} {'Races':>7} {'Upsets (<10%)':>15} {'Upset Rate':>12} {'Avg Winner Streck':>18}")
    print(f"  {'-'*20} {'-'*7} {'-'*15} {'-'*12} {'-'*18}")

    for level_name, level_filter in risk_levels.items():
        level_races = [r for r in all_races if level_filter(r)]
        if not level_races:
            continue

        upsets = [r for r in level_races if r["winner_bp"] is not None and r["winner_bp"] < 0.10]
        upset_rate = len(upsets) / len(level_races)

        avg_winner_streck_vals = [r["winner_bp"] for r in level_races if r["winner_bp"] is not None]
        avg_winner_streck = statistics.mean(avg_winner_streck_vals) if avg_winner_streck_vals else 0

        print(f"  {level_name:<20} {len(level_races):>7} {len(upsets):>15} {upset_rate:>11.1%} {avg_winner_streck:>17.1%}")

    # Additional: For HIGH upset risk races, how often did our upset_candidates win?
    hog_races = [r for r in all_races if r["upset_risk"] >= 50]
    if hog_races:
        candidate_wins = 0
        for race in hog_races:
            if race["winner"] in race.get("upset_candidates", []):
                candidate_wins += 1

        print(f"\n  HOG-risk races where an upset_candidate won: {candidate_wins}/{len(hog_races)} ({candidate_wins/len(hog_races):.1%})")

    # Overall upset stats
    all_upsets = [r for r in all_races if r["winner_bp"] is not None and r["winner_bp"] < 0.10]
    if all_upsets:
        flagged_correctly = sum(1 for r in all_upsets if r["upset_risk"] >= 50)
        flagged_medium = sum(1 for r in all_upsets if r["upset_risk"] >= 30)
        print(f"\n  Of all {len(all_upsets)} upsets (<10% streck winner):")
        print(f"    Flagged as HOG risk:   {flagged_correctly} ({flagged_correctly/len(all_upsets):.1%})")
        print(f"    Flagged as MEDEL+HOG:  {flagged_medium} ({flagged_medium/len(all_upsets):.1%})")


def part1d_impossible_upsets(all_races: list[dict]):
    separator("D) 'IMPOSSIBLE' UPSETS - Winners with <5% streck", "-")

    impossible = [r for r in all_races if r["winner_bp"] is not None and r["winner_bp"] < 0.05]

    if not impossible:
        print("\n  No winners with <5% streck found.")
        return

    print(f"\n  Total 'impossible' upsets: {len(impossible)} / {len(all_races)} ({len(impossible)/len(all_races):.1%})")

    model_ranks = []
    market_ranks = []
    flagged_hog = 0

    for race in impossible:
        winner = race["winner"]

        # Model rank
        mr = race["model_ranking"]
        m_rank = mr.index(winner) + 1 if winner in mr else len(mr) + 1
        model_ranks.append(m_rank)

        # Market rank
        mk = race["market_ranking"]
        k_rank = mk.index(winner) + 1 if winner in mk else len(mk) + 1
        market_ranks.append(k_rank)

        if race["upset_risk"] >= 50:
            flagged_hog += 1

    print(f"\n  Model's average rank of impossible winner:  {statistics.mean(model_ranks):.1f}")
    print(f"  Market's average rank of impossible winner: {statistics.mean(market_ranks):.1f}")
    print(f"  Model median rank:                         {statistics.median(model_ranks):.1f}")
    print(f"  Market median rank:                        {statistics.median(market_ranks):.1f}")
    print(f"  Model found in Top-3:                      {sum(1 for r in model_ranks if r <= 3)}/{len(model_ranks)} ({sum(1 for r in model_ranks if r <= 3)/len(model_ranks):.1%})")
    print(f"  Market found in Top-3:                     {sum(1 for r in market_ranks if r <= 3)}/{len(market_ranks)} ({sum(1 for r in market_ranks if r <= 3)/len(market_ranks):.1%})")
    print(f"  Flagged as HOG upset risk:                 {flagged_hog}/{len(impossible)} ({flagged_hog/len(impossible):.1%})")

    # Show individual impossible upsets
    print(f"\n  Individual 'impossible' upsets (top 15 by lowest streck):")
    impossible_sorted = sorted(impossible, key=lambda r: r["winner_bp"])
    for i, race in enumerate(impossible_sorted[:15]):
        winner = race["winner"]
        mr = race["model_ranking"]
        mk = race["market_ranking"]
        m_rank = mr.index(winner) + 1 if winner in mr else "?"
        k_rank = mk.index(winner) + 1 if winner in mk else "?"

        # Find winner name
        winner_name = "?"
        for e in race["entries"]:
            if e["post_position"] == winner:
                winner_name = e["horse_name"]
                break

        print(f"    {race['date']} {race['track']:<12} Avd {race['race_number']}: "
              f"{winner_name:<20} streck={race['winner_bp']:.1%} "
              f"model_rank={m_rank} market_rank={k_rank} "
              f"upset_risk={race['upset_risk']:.0f}")


def part1e_calibration(all_races: list[dict]):
    separator("E) CALIBRATION: Model Rank -> Win Rate WITH Average Streck", "-")

    print(f"\n  {'Model Rank':>12} {'Win Rate':>10} {'Avg Streck':>12} {'Edge (WR-Streck)':>18} {'N Races':>10}")
    print(f"  {'-'*12} {'-'*10} {'-'*12} {'-'*18} {'-'*10}")

    for target_rank in range(1, 9):
        wins = 0
        total = 0
        streck_values = []

        for race in all_races:
            entries = race["entries"]
            sorted_entries = sorted(entries, key=lambda e: e["composite_score"], reverse=True)

            if len(sorted_entries) < target_rank:
                continue

            entry = sorted_entries[target_rank - 1]
            total += 1
            if entry["is_winner"]:
                wins += 1
            if entry["bet_percentage"] is not None:
                streck_values.append(entry["bet_percentage"])

        if total == 0:
            continue

        win_rate = wins / total
        avg_streck = statistics.mean(streck_values) if streck_values else 0
        edge = win_rate - avg_streck

        marker = ""
        if edge > 0.02:
            marker = "  *** VALUE ***"
        elif edge > 0:
            marker = "  + slight edge"
        elif edge < -0.05:
            marker = "  -- market better"

        print(f"  {target_rank:>12} {win_rate:>9.1%} {avg_streck:>11.1%} {edge:>+17.2%}{marker}")

    # Also show MARKET rank calibration for comparison
    print(f"\n  {'Market Rank':>12} {'Win Rate':>10} {'Avg Streck':>12}")
    print(f"  {'-'*12} {'-'*10} {'-'*12}")

    for target_rank in range(1, 9):
        wins = 0
        total = 0
        streck_values = []

        for race in all_races:
            entries = race["entries"]
            sorted_entries = sorted(entries, key=lambda e: e["bet_percentage"] or 0, reverse=True)

            if len(sorted_entries) < target_rank:
                continue

            entry = sorted_entries[target_rank - 1]
            total += 1
            if entry["is_winner"]:
                wins += 1
            if entry["bet_percentage"] is not None:
                streck_values.append(entry["bet_percentage"])

        if total == 0:
            continue

        win_rate = wins / total
        avg_streck = statistics.mean(streck_values) if streck_values else 0
        edge = win_rate - avg_streck
        print(f"  {target_rank:>12} {win_rate:>9.1%} {avg_streck:>11.1%} {'  edge: ' + f'{edge:+.2%}' if edge != 0 else ''}")


# ============================================================================
# PART 2: EXPERT ANALYSIS (V85 2026-02-14)
# ============================================================================

def part2_expert_analysis(all_rounds: list[GameRound], all_races: list[dict]):
    separator("PART 2: EXPERT ANALYSIS (V85 2026-02-14)", "=", 80)

    # Find V85 2026-02-14 in our data
    target_date = "2026-02-14"
    v85_races = [r for r in all_races if r["date"] == target_date and r["game_type"] == "V85"]

    if not v85_races:
        # Try just the date
        v85_races = [r for r in all_races if r["date"] == target_date]

    if not v85_races:
        print(f"\n  WARNING: No V85 races found for {target_date}.")
        print(f"  Available dates (last 10): {sorted(set(r['date'] for r in all_races))[-10:]}")
        print(f"  Skipping Part 2.")
        return

    print(f"\n  Found {len(v85_races)} races for V85 {target_date}")

    # Load expert data from Excel
    expert_data = load_expert_data()
    if not expert_data:
        print("\n  WARNING: Could not load expert data. Skipping expert comparison.")
        print("  Comparing Model vs Market only for V85 2026-02-14.")
        expert_data = None

    # Sort races by race_number
    v85_races.sort(key=lambda r: r["race_number"])

    # Build a name lookup: (avd, pp) -> horse_name from model data
    name_lookup = {}
    for race in v85_races:
        for e in race["entries"]:
            name_lookup[(race["race_number"], e["post_position"])] = e["horse_name"]

    # Enrich expert data with horse names from model (since many are None in Excel)
    if expert_data:
        for avd in expert_data:
            for entry in expert_data[avd]["ranking"]:
                pp = entry["post_position"]
                model_name = name_lookup.get((avd, pp))
                if model_name and not entry["name"]:
                    entry["name"] = model_name

    model_correct_top1 = 0
    model_correct_top3 = 0
    market_correct_top1 = 0
    market_correct_top3 = 0
    expert_correct_top1 = 0
    expert_correct_top3 = 0
    total_races = 0

    print(f"\n  {'Avd':>4} {'Winner':>22} {'Model #1':>22} {'Market #1':>22} {'Expert #1':>22}")
    print(f"  {'-'*4} {'-'*22} {'-'*22} {'-'*22} {'-'*22}")

    for race in v85_races:
        avd = race["race_number"]
        winner = race["winner"]
        total_races += 1

        # Winner name
        winner_name = name_lookup.get((avd, winner), "?")[:20]

        # Model top pick
        model_top = race["model_ranking"][0] if race["model_ranking"] else None
        model_top_name = name_lookup.get((avd, model_top), "?")[:20]

        # Market top pick (highest streck)
        market_top = race["market_ranking"][0] if race["market_ranking"] else None
        market_top_name = name_lookup.get((avd, market_top), "?")[:20]

        # Expert top pick (by consensus%)
        expert_top_name = "-"
        expert_ranking = None
        if expert_data and avd in expert_data:
            ed = expert_data[avd]
            if ed.get("ranking"):
                expert_ranking = ed["ranking"]
                top_entry = expert_ranking[0]
                expert_top_pp = top_entry["post_position"]
                expert_top_name = (top_entry["name"] or name_lookup.get((avd, expert_top_pp), f"#{expert_top_pp}"))[:20]

        # Check accuracy
        model_correct = winner == model_top
        market_correct = winner == market_top

        if model_correct:
            model_correct_top1 += 1
        if winner in race["model_ranking"][:3]:
            model_correct_top3 += 1

        if market_correct:
            market_correct_top1 += 1
        if winner in race["market_ranking"][:3]:
            market_correct_top3 += 1

        # Expert accuracy (by post_position match)
        expert_correct = False
        expert_in_top3 = False
        if expert_ranking:
            expert_top_pp = expert_ranking[0].get("post_position")
            if expert_top_pp == winner:
                expert_correct = True
                expert_correct_top1 += 1

            for er in expert_ranking[:3]:
                if er.get("post_position") == winner:
                    expert_in_top3 = True
                    break

            if expert_in_top3:
                expert_correct_top3 += 1

        m_mark = " *" if model_correct else ""
        k_mark = " *" if market_correct else ""
        e_mark = " *" if expert_correct else ""

        print(f"  {avd:>4} {winner_name:>22} {model_top_name+m_mark:>22} {market_top_name+k_mark:>22} {expert_top_name+e_mark:>22}")

        # Show ranks of winner
        m_rank = race["model_ranking"].index(winner) + 1 if winner in race["model_ranking"] else "?"
        k_rank = race["market_ranking"].index(winner) + 1 if winner in race["market_ranking"] else "?"

        e_rank = "?"
        if expert_ranking:
            for i, er in enumerate(expert_ranking):
                if er.get("post_position") == winner:
                    e_rank = i + 1
                    break

        bp = race["winner_bp"]
        bp_str = f"{bp:.1%}" if bp else "?"

        # Also show expert's consensus% for the winner
        e_cons = "?"
        e_spikar_str = ""
        if expert_ranking:
            for er in expert_ranking:
                if er.get("post_position") == winner:
                    e_cons = f"{er['consensus_pct']:.0f}%"
                    if er['spikar'] > 0:
                        e_spikar_str = f", {er['spikar']} spikar"
                    break

        print(f"       Winner rank: Model={m_rank}, Market={k_rank}, Expert={e_rank}  |  Streck={bp_str}  Expert cons={e_cons}{e_spikar_str}")

    # Detailed expert view per avdelning
    if expert_data:
        separator("EXPERT DETAILED VIEW PER AVDELNING", "-")
        for race in v85_races:
            avd = race["race_number"]
            if avd not in expert_data:
                continue
            ranking = expert_data[avd]["ranking"]
            winner = race["winner"]

            print(f"\n  Avd {avd} — Winner: #{winner} ({name_lookup.get((avd, winner), '?')})")
            print(f"  {'Rank':>5} {'#':>3} {'Horse':>22} {'Cons%':>7} {'Spikar':>7} {'Streck%':>8} {'Rec':>18} {'Won?':>5}")
            print(f"  {'-'*5} {'-'*3} {'-'*22} {'-'*7} {'-'*7} {'-'*8} {'-'*18} {'-'*5}")

            for i, er in enumerate(ranking[:6], start=1):
                pp = er["post_position"]
                horse_name = (er["name"] or name_lookup.get((avd, pp), f"#{pp}"))[:20]
                won = "<<< " if pp == winner else ""
                print(f"  {i:>5} {pp:>3} {horse_name:>22} {er['consensus_pct']:>6.1f}% {er['spikar']:>7} {er['streck_pct']:>7.0f}% {er['recommendation']:>18} {won:>5}")

    # Summary
    if total_races > 0:
        separator("V85 2026-02-14 SUMMARY", "-")
        print(f"\n  {'Metric':<25} {'Model':>12} {'Market':>12} {'Expert':>12}")
        print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")
        print(f"  {'Top-1 Accuracy':<25} {model_correct_top1}/{total_races} ({model_correct_top1/total_races:.0%}){'':>4} {market_correct_top1}/{total_races} ({market_correct_top1/total_races:.0%}){'':>4} {expert_correct_top1}/{total_races} ({expert_correct_top1/total_races:.0%})")
        print(f"  {'Top-3 Accuracy':<25} {model_correct_top3}/{total_races} ({model_correct_top3/total_races:.0%}){'':>4} {market_correct_top3}/{total_races} ({market_correct_top3/total_races:.0%}){'':>4} {expert_correct_top3}/{total_races} ({expert_correct_top3/total_races:.0%})")

        # Where experts saw something model missed
        if expert_data:
            separator("EXPERT vs MODEL: Specific Insights", "-")
            for race in v85_races:
                avd = race["race_number"]
                if avd not in expert_data:
                    continue
                ranking = expert_data[avd]["ranking"]
                winner = race["winner"]

                # Expert rank of winner
                e_rank = None
                for i, er in enumerate(ranking):
                    if er["post_position"] == winner:
                        e_rank = i + 1
                        break

                # Model rank of winner
                m_rank = race["model_ranking"].index(winner) + 1 if winner in race["model_ranking"] else 99

                if e_rank is not None and e_rank < m_rank:
                    horse_name = name_lookup.get((avd, winner), "?")
                    print(f"  Avd {avd}: Experts ranked {horse_name} #{e_rank} (model #{m_rank}) — EXPERTS BETTER")
                elif e_rank is not None and m_rank < e_rank:
                    horse_name = name_lookup.get((avd, winner), "?")
                    print(f"  Avd {avd}: Model ranked {horse_name} #{m_rank} (experts #{e_rank}) — MODEL BETTER")


def load_expert_data() -> dict | None:
    """Load expert consensus data from the Excel file.

    Excel structure (sheet 'Konsensus vs Streck'):
      Col A: Häst (post position number)
      Col B: Namn (horse name, often None)
      Col C: System (number of systems including this horse, out of 40)
      Col D: Konsensus% (consensus percentage)
      Col E: Spikar (number of systems making this horse a spik)
      Col F: Streck% (market bet percentage)
      Col G: Värde (K-S) (value = consensus% - streck%)
      Col H: Värdepoäng
      Col I: Signal
      Col J: Rekommendation

    Rows are grouped by "AVD 1" ... "AVD 8" headers.

    Returns: {avdelning_int: {"ranking": [{"name": ..., "consensus_pct": ..., "spikar": ..., "post_position": ..., "systems": ..., "streck_pct": ..., "recommendation": ...}]}}
    """
    xlsx_path = Path("/Users/dennisdemirtok/Downloads/v85_komplett_40system.xlsx")
    if not xlsx_path.exists():
        print(f"  Excel file not found: {xlsx_path}")
        return None

    try:
        import openpyxl
    except ImportError:
        print("  openpyxl not installed. Run: pip install openpyxl")
        return None

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    target_sheet = wb['Konsensus vs Streck']

    print(f"  Loading expert data from sheet: '{target_sheet.title}'")

    expert_data = {}
    current_avd = None

    for row in target_sheet.iter_rows(min_row=1, values_only=True):
        vals = list(row)

        # Look for avdelning marker (e.g. "AVD 1")
        if vals[0] is not None and isinstance(vals[0], str) and vals[0].strip().upper().startswith("AVD"):
            import re
            m = re.search(r'(\d+)', vals[0])
            if m:
                current_avd = int(m.group(1))
                expert_data[current_avd] = {"ranking": []}
            continue

        # Skip header rows
        if vals[0] == "Häst":
            continue

        if current_avd is None:
            continue

        # Horse data row: [pp, name, systems, consensus%, spikar, streck%, ...]
        pp_val = vals[0]
        if pp_val is not None and isinstance(pp_val, (int, float)):
            pp = int(pp_val)
            if 1 <= pp <= 16:
                name = str(vals[1]).strip() if vals[1] else None
                systems = int(vals[2]) if vals[2] is not None else 0
                consensus_pct = float(vals[3]) if vals[3] is not None else 0
                spikar = int(vals[4]) if vals[4] is not None else 0
                streck_pct = float(vals[5]) if vals[5] is not None else 0
                recommendation = str(vals[9]).strip() if len(vals) > 9 and vals[9] else ""

                expert_data[current_avd]["ranking"].append({
                    "name": name,
                    "post_position": pp,
                    "consensus_pct": consensus_pct,
                    "spikar": spikar,
                    "systems": systems,
                    "streck_pct": streck_pct,
                    "recommendation": recommendation,
                })

    # Sort each avdelning by consensus% descending (primary), spikar descending (secondary)
    for avd in expert_data:
        expert_data[avd]["ranking"].sort(
            key=lambda x: (x["consensus_pct"], x["spikar"]),
            reverse=True
        )

    if expert_data:
        print(f"  Loaded expert data for {len(expert_data)} avdelningar:")
        for avd in sorted(expert_data.keys()):
            ranking = expert_data[avd]["ranking"]
            top = ranking[0] if ranking else None
            if top:
                name_str = top['name'] or f"#{top['post_position']}"
                print(f"    Avd {avd}: {len(ranking)} horses, top = {name_str} (cons={top['consensus_pct']:.0f}%, {top['spikar']} spikar, rec={top['recommendation']})")
    else:
        print("  No expert data could be parsed from the Excel file.")
        return None

    return expert_data


# ============================================================================
# PART 3: CAN STRECKPROCENT OPTIMIZE THE MODEL?
# ============================================================================

def part3_market_blending(all_races: list[dict]):
    separator("PART 3: CAN STRECKPROCENT OPTIMIZE THE MODEL?", "=", 80)

    # For each alpha, compute blended score and evaluate
    alphas = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

    print(f"\n  Testing: final_score = model_score * (1-alpha) + market_score * alpha")
    print(f"  market_score = bet_percentage * 100\n")

    print(f"  {'Alpha':>7} {'Top-1%':>8} {'Top-3%':>8} {'Avg Rank':>10} {'T1 vs pure':>12} {'T3 vs pure':>12}")
    print(f"  {'-'*7} {'-'*8} {'-'*8} {'-'*10} {'-'*12} {'-'*12}")

    baseline_t1 = None
    baseline_t3 = None

    results = []

    for alpha in alphas:
        top1 = 0
        top3 = 0
        ranks = []
        total = 0

        for race in all_races:
            entries = race["entries"]
            winner_pp = race["winner"]

            # Compute blended scores
            blended = []
            for e in entries:
                model_score = e["composite_score"]
                market_score = (e["bet_percentage"] or 0) * 100
                final_score = model_score * (1 - alpha) + market_score * alpha
                blended.append((e["post_position"], final_score, e["is_winner"]))

            blended.sort(key=lambda x: x[1], reverse=True)
            ranking = [pp for pp, _, _ in blended]

            if winner_pp in ranking:
                w_rank = ranking.index(winner_pp) + 1
            else:
                w_rank = len(ranking) + 1

            total += 1
            if w_rank <= 1:
                top1 += 1
            if w_rank <= 3:
                top3 += 1
            ranks.append(w_rank)

        t1_pct = top1 / total
        t3_pct = top3 / total
        avg_rank = statistics.mean(ranks)

        if alpha == 0.0:
            baseline_t1 = t1_pct
            baseline_t3 = t3_pct
            delta_t1 = ""
            delta_t3 = ""
        else:
            delta_t1 = f"{(t1_pct - baseline_t1):>+.1%}"
            delta_t3 = f"{(t3_pct - baseline_t3):>+.1%}"

        results.append((alpha, t1_pct, t3_pct, avg_rank))
        print(f"  {alpha:>7.2f} {t1_pct:>7.1%} {t3_pct:>7.1%} {avg_rank:>10.2f} {delta_t1:>12} {delta_t3:>12}")

    # Find best alpha
    best_t1 = max(results, key=lambda x: x[1])
    best_t3 = max(results, key=lambda x: x[2])
    best_rank = min(results, key=lambda x: x[3])

    print(f"\n  Best alpha for Top-1:    {best_t1[0]:.2f} ({best_t1[1]:.1%})")
    print(f"  Best alpha for Top-3:    {best_t3[0]:.2f} ({best_t3[2]:.1%})")
    print(f"  Best alpha for Avg Rank: {best_rank[0]:.2f} ({best_rank[3]:.2f})")

    # ── Anti-correlation approach ──
    separator("ANTI-CORRELATION: Where model disagrees with market = VALUE", "-")
    print(f"\n  Theory: When the model ranks a horse higher than the market,")
    print(f"  the horse may be undervalued (good bet). When the model")
    print(f"  ranks lower than market, the horse may be overvalued.\n")

    # For each race, find horses where model rank << market rank
    # These are the model's value picks

    # Split into agreement vs disagreement
    agreement_wins = 0
    agreement_total = 0
    disagreement_wins = 0
    disagreement_total = 0

    # Where model rank #1 != market rank #1
    model_top1_disagree_total = 0
    model_top1_disagree_wins = 0
    market_top1_disagree_wins = 0

    # Value detection: model has horse in top 3 but market has it rank 5+
    value_picks_total = 0
    value_picks_wins = 0
    value_picks_streck = []

    for race in all_races:
        entries = race["entries"]
        winner_pp = race["winner"]

        model_rank = race["model_ranking"]
        market_rank = race["market_ranking"]

        # Agreement on top 1?
        if model_rank[0] == market_rank[0]:
            agreement_total += 1
            if model_rank[0] == winner_pp:
                agreement_wins += 1
        else:
            disagreement_total += 1
            model_top1_disagree_total += 1
            if model_rank[0] == winner_pp:
                disagreement_wins += 1
                model_top1_disagree_wins += 1
            if market_rank[0] == winner_pp:
                market_top1_disagree_wins += 1

        # Value picks: model top-3 but market rank 5+
        for e in entries:
            pp = e["post_position"]
            if pp in model_rank[:3]:
                m_rank_idx = model_rank.index(pp) + 1
                k_rank_idx = market_rank.index(pp) + 1 if pp in market_rank else len(market_rank) + 1

                if k_rank_idx >= 5:
                    value_picks_total += 1
                    if e["is_winner"]:
                        value_picks_wins += 1
                    if e["bet_percentage"] is not None:
                        value_picks_streck.append(e["bet_percentage"])

    print(f"  {'Scenario':<45} {'Races':>7} {'Wins':>6} {'Win%':>8}")
    print(f"  {'-'*45} {'-'*7} {'-'*6} {'-'*8}")

    if agreement_total > 0:
        print(f"  {'Model & Market agree on #1':.<45} {agreement_total:>7} {agreement_wins:>6} {agreement_wins/agreement_total:>7.1%}")
    if disagreement_total > 0:
        print(f"  {'Model & Market disagree on #1':.<45} {disagreement_total:>7}")
        print(f"  {'  -> Model #1 wins':.<45} {'':<7} {model_top1_disagree_wins:>6} {model_top1_disagree_wins/model_top1_disagree_total:>7.1%}")
        print(f"  {'  -> Market #1 wins':.<45} {'':<7} {market_top1_disagree_wins:>6} {market_top1_disagree_wins/model_top1_disagree_total:>7.1%}")

    if value_picks_total > 0:
        vp_wr = value_picks_wins / value_picks_total
        vp_avg_streck = statistics.mean(value_picks_streck) if value_picks_streck else 0
        vp_edge = vp_wr - vp_avg_streck

        print(f"\n  VALUE PICKS (Model Top-3, Market Rank 5+):")
        print(f"    Total value picks:    {value_picks_total}")
        print(f"    Winners:              {value_picks_wins}")
        print(f"    Win rate:             {vp_wr:.1%}")
        print(f"    Average streck:       {vp_avg_streck:.1%}")
        print(f"    EDGE:                 {vp_edge:+.1%}  {'*** PROFITABLE ***' if vp_edge > 0 else ''}")

    # Test reverse: market scores high but model scores low -> fade these
    fade_total = 0
    fade_wins = 0
    fade_streck = []

    for race in all_races:
        entries = race["entries"]
        model_rank = race["model_ranking"]
        market_rank = race["market_ranking"]

        for e in entries:
            pp = e["post_position"]
            if pp in market_rank[:3]:
                k_rank_idx = market_rank.index(pp) + 1
                m_rank_idx = model_rank.index(pp) + 1 if pp in model_rank else len(model_rank) + 1

                if m_rank_idx >= 5:
                    fade_total += 1
                    if e["is_winner"]:
                        fade_wins += 1
                    if e["bet_percentage"] is not None:
                        fade_streck.append(e["bet_percentage"])

    if fade_total > 0:
        fade_wr = fade_wins / fade_total
        fade_avg_streck = statistics.mean(fade_streck) if fade_streck else 0
        fade_edge = fade_wr - fade_avg_streck

        print(f"\n  FADES (Market Top-3 but Model Rank 5+) — these should LOSE:")
        print(f"    Total fades:          {fade_total}")
        print(f"    Winners (bad):        {fade_wins}")
        print(f"    Win rate:             {fade_wr:.1%}")
        print(f"    Average streck:       {fade_avg_streck:.1%}")
        print(f"    Market OVERVALUATION: {fade_avg_streck - fade_wr:+.1%}  {'*** MARKET WRONG ***' if fade_avg_streck > fade_wr else ''}")

    # Test: disagreement intensity
    separator("DISAGREEMENT INTENSITY ANALYSIS", "-")
    print(f"\n  When model and market disagree MORE strongly, is there more edge?\n")

    # Bucket by rank difference for top-1 horse
    print(f"  {'Rank Diff':>10} {'Model W%':>10} {'Market W%':>11} {'Model Edge':>12} {'N Races':>10}")
    print(f"  {'-'*10} {'-'*10} {'-'*11} {'-'*12} {'-'*10}")

    disagreements = []
    for race in all_races:
        model_top = race["model_ranking"][0]
        market_top = race["market_ranking"][0]
        winner_pp = race["winner"]

        if model_top == market_top:
            continue

        # How different are they? Use rank of model_top in market and vice versa
        model_top_market_rank = race["market_ranking"].index(model_top) + 1 if model_top in race["market_ranking"] else len(race["market_ranking"]) + 1
        rank_diff = model_top_market_rank - 1  # 0 = same, higher = more disagreement

        disagreements.append({
            "rank_diff": rank_diff,
            "model_won": model_top == winner_pp,
            "market_won": market_top == winner_pp,
        })

    # Bucket: small (1-2), medium (3-4), large (5+)
    for label, low, high in [("Small (1-2)", 1, 2), ("Medium (3-4)", 3, 4), ("Large (5+)", 5, 20)]:
        bucket = [d for d in disagreements if low <= d["rank_diff"] <= high]
        if not bucket:
            continue
        model_w = sum(1 for d in bucket if d["model_won"])
        market_w = sum(1 for d in bucket if d["market_won"])
        n = len(bucket)
        model_edge = model_w/n - market_w/n if n > 0 else 0
        print(f"  {label:>10} {model_w/n:>9.1%} {market_w/n:>10.1%} {model_edge:>+11.1%} {n:>10}")


# ============================================================================
# MAIN
# ============================================================================

async def main():
    start_time = time.time()

    print("=" * 80)
    print("  COMPREHENSIVE BETTING EDGE ANALYSIS")
    print("  Model vs Market vs Experts")
    print("=" * 80)

    # Load data
    all_rounds, all_races = await load_all_data()

    if not all_races:
        print("ERROR: No races loaded!")
        return

    print(f"\n  Total races loaded: {len(all_races)}")
    print(f"  Total rounds:       {len(all_rounds)}")
    print(f"  Date range:         {min(r['date'] for r in all_races)} to {max(r['date'] for r in all_races)}")

    # PART 1
    part1_betting_edge(all_races)

    # PART 2
    part2_expert_analysis(all_rounds, all_races)

    # PART 3
    part3_market_blending(all_races)

    # FINAL SUMMARY
    separator("FINAL SUMMARY & CONCLUSIONS", "=", 80)

    # Key findings
    print("\n  KEY FINDINGS:\n")

    # 1. Model rank 1 edge
    rank1_streck = []
    rank1_wins = 0
    rank1_total = 0
    for race in all_races:
        entries = sorted(race["entries"], key=lambda e: e["composite_score"], reverse=True)
        if entries:
            e = entries[0]
            if e["bet_percentage"] is not None:
                rank1_streck.append(e["bet_percentage"])
                rank1_total += 1
                if e["is_winner"]:
                    rank1_wins += 1

    if rank1_total > 0:
        wr = rank1_wins / rank1_total
        avg_s = statistics.mean(rank1_streck)
        edge = wr - avg_s
        print(f"  1. Model Rank #1: Win rate {wr:.1%} vs avg streck {avg_s:.1%} -> edge {edge:+.1%}")
        if edge > 0:
            print(f"     -> POSITIVE EXPECTED VALUE: Betting the model's #1 pick is profitable!")
        else:
            print(f"     -> No edge at rank 1 level")

    # 2. Spik accuracy
    spik_total = 0
    spik_wins = 0
    for race in all_races:
        for e in race["entries"]:
            if e["recommendation"] == "spik":
                spik_total += 1
                if e["is_winner"]:
                    spik_wins += 1
    if spik_total > 0:
        print(f"  2. Spikar: {spik_wins}/{spik_total} correct ({spik_wins/spik_total:.1%})")

    # 3. Upset detection
    all_upsets = [r for r in all_races if r["winner_bp"] and r["winner_bp"] < 0.10]
    if all_upsets:
        flagged = sum(1 for r in all_upsets if r["upset_risk"] >= 50)
        print(f"  3. Upset detection: {flagged}/{len(all_upsets)} upsets flagged as HOG risk ({flagged/len(all_upsets):.1%})")

    # 4. Impossible upsets
    impossible = [r for r in all_races if r["winner_bp"] and r["winner_bp"] < 0.05]
    if impossible:
        m_ranks = []
        for race in impossible:
            mr = race["model_ranking"]
            w = race["winner"]
            m_rank = mr.index(w) + 1 if w in mr else len(mr) + 1
            m_ranks.append(m_rank)
        print(f"  4. 'Impossible' upsets (<5% streck): {len(impossible)}, model avg rank of winner: {statistics.mean(m_ranks):.1f}")

    elapsed = time.time() - start_time
    print(f"\n  Total analysis time: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
