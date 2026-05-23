"""Prispengaindex — mäter motståndsstyrka + V-odds signal + skrällprofil.

Skrällar dyker upp bland hästar med hög prissumma och högt odds
men som ibland tar top3 — det visar att de tål bra konkurrens
men spelarna undervärderar dem.
"""

from __future__ import annotations

import math

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class PrizeIndex(AnalysisFactor):
    """Analyserar motståndsstyrka via prispengar, V-odds och skrällprofil."""

    name = "prize_index"

    def __init__(self, recent_n: int = 10):
        self.recent_n = recent_n

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på prispengaindex.

        1. Motståndsindex: snitt prispott → 15%
        2. Klasshopp-bonus → 18%
        3. Vinsteffektivitet → 18%
        4. V-odds signal → 15%
        5. Skrällprofil → 17%
        6. Senaste klass-prestation → 17%
        """
        recent = entry.horse.recent_starts(self.recent_n)

        if not recent:
            return 30.0

        avg_purse = self._avg_race_purse(recent)
        resistance = self._resistance_to_score(avg_purse) * 0.15
        class_drop = self._class_change_score(recent, race) * 0.18
        efficiency = self._win_efficiency_score(recent) * 0.18
        odds_signal = self._odds_signal_score(recent) * 0.15
        upset = self._upset_potential_score(entry) * 0.17
        recent_class = self._recent_class_performance(recent, race) * 0.17

        return min(100.0, max(0.0,
            resistance + class_drop + efficiency + odds_signal + upset + recent_class))

    @staticmethod
    def _avg_race_purse(starts: list) -> float:
        purses = [s.race_purse for s in starts if s.race_purse > 0]
        return sum(purses) / len(purses) if purses else 0

    @staticmethod
    def _resistance_to_score(avg_purse: float) -> float:
        if avg_purse <= 0:
            return 30.0
        score = 20 + 15 * math.log10(max(avg_purse, 1) / 40000 + 1)
        return min(100.0, max(0.0, score))

    @staticmethod
    def _class_change_score(recent: list, current_race: Race) -> float:
        avg_purse = sum(s.race_purse for s in recent if s.race_purse > 0) or 1
        count = sum(1 for s in recent if s.race_purse > 0) or 1
        avg_purse /= count

        current_purse = current_race.purse
        if current_purse <= 0 or avg_purse <= 0:
            return 50.0

        ratio = avg_purse / current_purse
        if ratio > 1.5:
            return min(100.0, 60.0 + (ratio - 1.5) * 20)
        elif ratio < 0.7:
            return max(0.0, 40.0 + (ratio - 0.7) * 30)
        else:
            return 50.0

    @staticmethod
    def _win_efficiency_score(recent: list) -> float:
        if not recent:
            return 30.0
        wins = sum(1 for s in recent if s.won)
        top3 = sum(1 for s in recent if s.top3)
        total = len(recent)
        score = (wins / total) * 60 + (top3 / total) * 30
        return min(100.0, score * 100 / 90)

    @staticmethod
    def _odds_signal_score(recent: list) -> float:
        """V-odds signal: lågt odds + hög prispott = stark häst."""
        starts_with_data = [
            s for s in recent if s.odds and s.odds > 0 and s.race_purse > 0
        ]
        if not starts_with_data:
            return 45.0

        signals = [s.race_purse / (s.odds + 1) for s in starts_with_data]
        avg_signal = sum(signals) / len(signals)

        if avg_signal <= 0:
            return 30.0
        score = 20 + 15 * math.log10(avg_signal / 10000 + 1)
        return min(100.0, max(0.0, score))

    @staticmethod
    def _upset_potential_score(entry: RaceEntry) -> float:
        """Skrällprofil: högt odds + tufft motstånd + ok resultat = undervärde.

        Dennis: "Skrällar brukar dyka upp bland de med hög prissumma
        och högt odds men som ibland tar top3 — visar att de tål bra
        konkurrens men spelarna undervärderar dem."

        Signaler:
        - snittodds > 15 (inte favorit)
        - krStart > 15000 (möter tufft motstånd)
        - top3_rate > 0.2 (levererar ibland)
        - trend > 0 (förbättring)
        """
        career = entry.horse.career
        if career.total_starts < 5:
            return 45.0  # Otillräcklig data

        avg_odds = career.avg_odds or 0
        kr_start = career.prize_per_start or 0
        top3_rate = career.top3_rate
        trend = entry.trend or 0

        score = 40.0  # Baspoäng

        # Högt snittodds = inte favorit (spelarna tror inte på den)
        if avg_odds > 15:
            score += 10  # Potential att vara undervärderad
        elif avg_odds > 10:
            score += 5

        # Hög kr/start = möter tuffa fält
        if kr_start > 20000:
            score += 12
        elif kr_start > 15000:
            score += 8
        elif kr_start > 10000:
            score += 4

        # Levererar ändå top3 ibland
        if top3_rate > 0.3:
            score += 15  # Stark leverans trots tufft motstånd
        elif top3_rate > 0.2:
            score += 10
        elif top3_rate > 0.1:
            score += 5

        # Positiv trend = förbättring
        if trend > 1.0:
            score += 8
        elif trend > 0:
            score += 4

        # Kombination: högt odds + tufft motstånd + levererar = skräll
        if avg_odds > 15 and kr_start > 15000 and top3_rate > 0.2:
            score += 10  # Extra bonus för den perfekta skräll-profilen

        return min(100.0, max(0.0, score))

    @staticmethod
    def _recent_class_performance(recent: list, current_race: Race) -> float:
        """Senaste klass-prestation: pott + odds + placering i senaste 3 starter.

        Fångar momentum från nyliga högklassiga prestationer som
        karriärsnitt urvattnar. En häst som nyligen vann i 150k-klassen
        med låga odds rankas högre än en vars vinster var 6+ månader sedan.

        Signaler per start (viktat: senaste=1.0, näst-senaste=0.7, tredje=0.5):
        - Prispott relativt nuvarande lopp (högre klass → bonus)
        - Odds (låga = betrodd → bonus)
        - Placering (vann/top3 → bonus)
        - Kombobonus: hög klass + låga odds + bra placering
        """
        last_3 = recent[:3]
        if not last_3:
            return 35.0

        score = 30.0  # Baspoäng
        recency_weights = [1.0, 0.7, 0.5]

        for i, start in enumerate(last_3):
            rw = recency_weights[i] if i < len(recency_weights) else 0.4

            # A: Prispott relativt nuvarande lopp
            if start.race_purse > 0 and current_race.purse > 0:
                purse_ratio = start.race_purse / current_race.purse
                if purse_ratio >= 1.5:
                    score += 8 * rw   # Tävlat i mycket tuffare klass
                elif purse_ratio >= 1.0:
                    score += 5 * rw   # Samma klass eller högre
                elif purse_ratio >= 0.5:
                    score += 2 * rw   # Lägre klass

            # B: Odds-signal (låga odds = betrodd)
            if start.odds and start.odds > 0:
                if start.odds < 4.0:
                    score += 5 * rw   # Stor favorit
                elif start.odds < 8.0:
                    score += 3 * rw   # Chansad
                elif start.odds < 15.0:
                    score += 1 * rw   # I matchen

            # C: Placering
            if start.won:
                score += 8 * rw
            elif start.top3:
                score += 4 * rw
            elif start.placement and start.placement <= 5:
                score += 1 * rw

            # D: Kombobonus — vann/top3 med låga odds i hög klass
            if start.race_purse > 0 and current_race.purse > 0:
                high_class = start.race_purse >= current_race.purse
                low_odds = start.odds is not None and start.odds > 0 and start.odds < 8.0
                good_placement = start.won or start.top3
                if high_class and low_odds and good_placement:
                    score += 5 * rw  # Stark nylig klassprestation

        return min(100.0, max(0.0, score))
