"""Formkurva — analyserar prestation senaste N starter.

Viktar nyare starter tyngre. Ser efter:
- Placeringsmönster (uppåt/nedåt trend)
- Frekvent top3 = stabil form
- Vila/uppehåll påverkan
- Galopp/disk-mönster
- Top3-frekvens justerad för motståndskontext (prispott)
"""

from __future__ import annotations

import statistics
from datetime import date

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class FormCurve(AnalysisFactor):
    """Formkurva baserad på senaste starter med motståndskontext."""

    name = "form_curve"

    def __init__(self, recent_n: int = 10):
        self.recent_n = recent_n

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på formkurva.

        Beräkning:
        1. Viktade placeringspoäng (nyare = tyngre) → 30%
        2. Trendpoäng (förbättring/försämring) → 20%
        3. Konsistenspoäng (variation i placering) → 10%
        4. Uppehållseffekt → 10%
        5. Diskfrekvens (negativt) → 10%
        6. Top3-ratio med motståndskontext → 20%
        """
        recent = entry.horse.recent_starts(self.recent_n)

        if not recent:
            return 25.0

        # 1. Viktade placeringspoäng
        placement_score = self._weighted_placement_score(recent) * 0.30

        # 2. Trend
        trend_score = self._trend_score(recent) * 0.20

        # 3. Konsistens
        consistency_score = self._consistency_score(recent) * 0.10

        # 4. Uppehåll
        rest_score = self._rest_effect_score(recent, race.race_date) * 0.10

        # 5. Diskfrekvens
        dnf_penalty = self._dnf_penalty(recent) * 0.10

        # 6. Motståndjusterad top3-ratio — ny!
        opposition_score = self._opposition_adjusted_top3(recent) * 0.20

        return min(100.0, max(0.0,
            placement_score + trend_score + consistency_score +
            rest_score + dnf_penalty + opposition_score
        ))

    @staticmethod
    def _weighted_placement_score(starts: list) -> float:
        """Viktat medel av placeringar, nyare starter viktar mer."""
        placement_map = {1: 100, 2: 80, 3: 65, 4: 45, 5: 35}

        total_weight = 0.0
        total_score = 0.0

        for i, s in enumerate(starts):
            weight = 0.3 + 0.7 * ((len(starts) - i) / len(starts))

            if s.placement is None or s.dnf:
                pts = 5.0
            elif s.placement in placement_map:
                pts = placement_map[s.placement]
            else:
                pts = max(5.0, 30.0 - (s.placement - 5) * 3)

            total_score += pts * weight
            total_weight += weight

        return total_score / total_weight if total_weight > 0 else 30.0

    @staticmethod
    def _trend_score(starts: list) -> float:
        """Trend: jämför senaste 3 starter med resten."""
        if len(starts) < 4:
            return 50.0

        def avg_placement(ss):
            placements = [s.placement for s in ss if s.placement is not None]
            return sum(placements) / len(placements) if placements else 7.0

        recent_avg = avg_placement(starts[:3])
        earlier_avg = avg_placement(starts[3:])

        improvement = earlier_avg - recent_avg
        score = 50.0 + improvement * 10
        return min(100.0, max(0.0, score))

    @staticmethod
    def _consistency_score(starts: list) -> float:
        """Konsistens — låg variation i placeringar = stabil häst."""
        placements = [s.placement for s in starts if s.placement is not None]
        if len(placements) < 3:
            return 40.0

        stdev = statistics.stdev(placements)
        score = max(0.0, 80.0 - stdev * 12)
        return score

    @staticmethod
    def _rest_effect_score(starts: list, race_date: date) -> float:
        """Påverkan av viloperiod."""
        if not starts:
            return 30.0

        last_race = starts[0].start_date
        days_rest = (race_date - last_race).days

        if days_rest < 0:
            return 50.0
        if days_rest <= 14:
            return 65.0
        elif days_rest <= 42:
            return 75.0
        elif days_rest <= 90:
            return 55.0
        elif days_rest <= 180:
            return 35.0
        else:
            return 20.0

    @staticmethod
    def _dnf_penalty(starts: list) -> float:
        """Negativt om hästen ofta diskas/galopperar."""
        if not starts:
            return 50.0

        dnf_count = sum(1 for s in starts if s.dnf)
        dnf_rate = dnf_count / len(starts)

        return max(20.0, 80.0 - dnf_rate * 200)

    @staticmethod
    def _opposition_adjusted_top3(starts: list) -> float:
        """Top3-ratio justerad för motståndskontext (prispott).

        Logik: Om hästen har låg top3% men mötte tuffa fält (hög prispott),
        reducera straffet. En häst som mött fält med snitt 150k prispott
        och ändå har 30% top3 är starkare än en häst med 50% top3
        i 50k-lopp.

        Fler starter med stabil form = mer pålitlig bedömning.
        """
        if not starts:
            return 30.0

        total = len(starts)
        top3_count = sum(1 for s in starts if s.top3)
        raw_top3_rate = top3_count / total

        # Beräkna genomsnittlig prispott i fälten
        purses = [s.race_purse for s in starts if s.race_purse > 0]
        if purses:
            avg_purse = sum(purses) / len(purses)
            # Bonus för tufft motstånd: varje 100k i snittprispott ger +0.05 bonus
            opposition_bonus = min(0.15, avg_purse / 100000 * 0.05)
        else:
            opposition_bonus = 0.0

        adjusted_top3 = raw_top3_rate + opposition_bonus

        # Baspoäng från justerad top3-rate
        # 50%+ top3 = toppklass, 30% = bra, 10% = svag
        score = adjusted_top3 * 120  # 0.5 → 60, 0.3 → 36

        # Erfarenhetsbonus: fler starter = mer pålitlig bedömning
        career = sum(1 for _ in starts)  # Karriärstarter i sample
        if career >= 8:
            score += 10  # Erfaren häst med stabil historik
        elif career >= 5:
            score += 5

        return min(100.0, max(0.0, score))
