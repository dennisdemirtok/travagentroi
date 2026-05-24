"""Spåranalys v8 — empiriskt kalibrerad på 2 278 lopp.

Kalibrerat med gallop_volt_analysis.py (350 omgångar, 563 volt + 1715 auto).

EMPIRISKA FYND:

VOLT (563 lopp):
  Spår 1: 14.2% vinst ★ — springspår, dominant
  Spår 2: 10.0%, Spår 3: 9.1%, Spår 4: 8.3%
  Spår 6: 8.5%, Spår 7: 7.7% — måttlig springspårsfördel
  Spår 9: 6.2%, Spår 13-15: 5.5%
  Springspår (1,6,7): 10.1% vs innerspår (2-5): 8.4% vs ytter (8+): 7.3%

AUTO (1715 lopp):
  Spår 5: 14.0% vinst ★ — BÄST (inte spår 1!)
  Spår 4: 12.0%, Spår 2: 11.6%, Spår 3: 11.2%
  Spår 1: 7.7% — överraskande dåligt i auto!
  Spår 8: 7.3%, Spår 9: 5.9%

Dennis: "voltlopp gynnas spår 1, 6, 7 — springspår"
→ Spår 1 bekräftat starkt, 6 och 7 har viss fördel.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor

# Empiriskt kalibrerat på 1715 autolopp
# Normaliserat mot snitt ~9.6% segerfrekvens
AUTO_START_ADVANTAGE = {
    1: 0.80, 2: 1.21, 3: 1.17, 4: 1.25, 5: 1.46, 6: 1.13,
    7: 1.11, 8: 0.76, 9: 0.61, 10: 0.76, 11: 0.73, 12: 0.73,
}

# Empiriskt kalibrerat på 563 voltlopp
# Normaliserat mot snitt ~8.3% segerfrekvens
VOLT_START_ADVANTAGE = {
    1: 1.71, 2: 1.20, 3: 1.10, 4: 1.00, 5: 0.76, 6: 1.02,
    7: 0.93, 8: 0.96, 9: 0.75, 10: 0.94, 11: 1.02, 12: 1.06,
}

# Spårtrappa: spår 5-8 = sweet spot (data visar högst segerfrekvens)
STAIR_DRAW_ADVANTAGE = {
    1: 0.85, 2: 0.90, 3: 0.95, 4: 1.00, 5: 1.10, 6: 1.12,
    7: 1.12, 8: 1.10, 9: 1.00, 10: 0.95, 11: 0.88, 12: 0.82,
}


class PostPosition(AnalysisFactor):
    """Analyserar spårpositionens påverkan inklusive spårtrappa."""

    name = "post_position"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på spårposition.

        1. Spårets fördel/nackdel (normal eller spårtrappa) → 40%
        2. Hästens historik från liknande spår → 30%
        3. Överprestationsbonus: bra resultat från dåligt spår → 30%
        """
        general_score = self._general_position_score(entry, race) * 0.40
        history_score = self._position_history_score(entry, race) * 0.30
        overperf_score = self._overperformance_bonus(entry) * 0.30

        return min(100.0, max(0.0, general_score + history_score + overperf_score))

    @staticmethod
    def _general_position_score(entry: RaceEntry, race: Race) -> float:
        """Spåradvantage — med spårtrappa om relevant."""
        pos = entry.post_position

        if race.is_stair_draw:
            # Spårtrappa: sweet spot spår 5-8
            advantage = STAIR_DRAW_ADVANTAGE.get(pos, 0.85)

            # Justera baserat på pengaskillnad i fältet
            # Hästar med hög kapacitet (mycket pengar) kompenserar dåligt spår
            field_prizes = [
                e.horse.career.total_prize_money
                for e in race.active_entries
                if e.horse.career.total_prize_money > 0
            ]
            if field_prizes:
                avg_prize = sum(field_prizes) / len(field_prizes)
                horse_prize = entry.horse.career.total_prize_money
                if avg_prize > 0 and horse_prize > 0:
                    # prize_ratio > 1 = starkare än snittet
                    prize_ratio = horse_prize / avg_prize
                    # Ge kapacitetshästar (ratio > 1.3) en liten bonus
                    if prize_ratio > 1.3:
                        advantage += min(0.1, (prize_ratio - 1.3) * 0.15)
                    # Svaga hästar (ratio < 0.7) med bra spår = inte lika stor fördel
                    elif prize_ratio < 0.7 and pos <= 4:
                        advantage -= 0.05  # Reducera spårbonus

        elif race.start_method == StartMethod.AUTO:
            advantage = AUTO_START_ADVANTAGE.get(pos, 0.80)
        else:
            advantage = VOLT_START_ADVANTAGE.get(pos, 0.80)

        score = 50 + (advantage - 1.0) * 200
        return min(100.0, max(0.0, score))

    @staticmethod
    def _position_history_score(entry: RaceEntry, race: Race) -> float:
        """Hur presterar hästen från liknande spår?"""
        recent = entry.horse.recent_starts(15)
        if not recent:
            return 50.0

        pos = entry.post_position
        margin = 2
        nearby = [
            s for s in recent
            if abs(s.post_position - pos) <= margin and s.post_position > 0
        ]
        if not nearby:
            return 50.0

        wins = sum(1 for s in nearby if s.won)
        top3 = sum(1 for s in nearby if s.top3)
        total = len(nearby)
        score = (wins / total * 60 + top3 / total * 30) * 100 / 90 + 10
        return min(100.0, score)

    @staticmethod
    def _overperformance_bonus(entry: RaceEntry) -> float:
        """Bonus för bra resultat från dåligt spår (7+)."""
        recent = entry.horse.recent_starts(15)
        if not recent:
            return 50.0

        bad_post_starts = [s for s in recent if s.post_position >= 7]
        if not bad_post_starts:
            return 50.0

        scores = []
        for s in bad_post_starts:
            score = 30.0
            expected_penalty = (s.post_position - 6) * 5

            if s.won:
                score = 90.0 + expected_penalty
            elif s.top3:
                score = 70.0 + expected_penalty * 0.7
            elif s.placement and s.placement <= 5:
                score = 55.0 + expected_penalty * 0.4
            elif s.km_time and s.km_time > 0:
                score = 45.0 + expected_penalty * 0.2

            if s.start_method == StartMethod.VOLT and s.post_position >= 7:
                score += 5.0

            scores.append(min(100.0, score))

        return sum(scores) / len(scores)
