"""Spåranalys — påverkan av spårposition per bana och startmetod.

Vid autostart: Innerspår är normalt en fördel.
Vid voltstart: Spår 7+ = andra raden = stor nackdel.

Spårtrappa: Spår 1 = svagast häst (lägst pengar), spår 12 = starkast.
Data visar att spår 5-8 har högst segerfrekvens i spårtrappor —
tillräcklig kapacitet utan alltför stort spårhandikapp.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor

# Generella spåreffekter vid autostart (spår 1-12)
AUTO_START_ADVANTAGE = {
    1: 1.25, 2: 1.15, 3: 1.10, 4: 1.05, 5: 1.00, 6: 0.98,
    7: 0.95, 8: 0.92, 9: 0.88, 10: 0.85, 11: 0.82, 12: 0.80,
}

# Vid voltstart — andra raden (7+) är en markant nackdel
VOLT_START_ADVANTAGE = {
    1: 1.08, 2: 1.06, 3: 1.04, 4: 1.02, 5: 1.00, 6: 0.98,
    7: 0.92, 8: 0.88, 9: 0.86, 10: 0.84, 11: 0.82, 12: 0.80,
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
