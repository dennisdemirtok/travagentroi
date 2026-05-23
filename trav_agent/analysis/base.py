"""Baseklass för alla analysfaktorer.

Varje faktor tar ett lopp och returnerar en poäng 0-100 per häst.
Faktorerna kan testas isolerat i backtesting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..data.models import Race, RaceEntry


class AnalysisFactor(ABC):
    """Abstrakt basklass för en analysfaktor."""

    # Unikt namn, används som nyckel i factor_scores
    name: str = "base"

    @abstractmethod
    def score(self, entry: RaceEntry, race: Race) -> float:
        """Beräkna poäng 0-100 för en häst i ett specifikt lopp.

        Args:
            entry: Hästen med historik
            race: Loppet med alla deltagare

        Returns:
            float 0-100 där 100 = högsta chans
        """
        ...

    def score_race(self, race: Race) -> dict[int, float]:
        """Poängsätt alla hästar i ett lopp.

        Returns:
            dict med post_position → score
        """
        scores = {}
        for entry in race.active_entries:
            scores[entry.post_position] = self.score(entry, race)
        return scores

    @staticmethod
    def normalize_scores(scores: dict[int, float]) -> dict[int, float]:
        """Normalisera poäng till 0-100 relativt fältet."""
        if not scores:
            return scores
        min_s = min(scores.values())
        max_s = max(scores.values())
        spread = max_s - min_s
        if spread == 0:
            return {k: 50.0 for k in scores}
        return {k: ((v - min_s) / spread) * 100 for k, v in scores.items()}
