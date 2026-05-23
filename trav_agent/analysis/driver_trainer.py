"""Kusk & tränare — vinstprocent, form, och kombo-statistik.

Analyserar:
- Kuskens vinstprocent (generellt och på aktuell bana)
- Tränarens form
- Kusk+häst kombination (har de kört ihop förut?)
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class DriverTrainer(AnalysisFactor):
    """Analyserar kusk och tränare."""

    name = "driver_trainer"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på kusk/tränare.

        1. Kusk+häst kombination (tidigare samarbete) → 40%
        2. Kuskbyte-effekt → 30%
        3. Tränare-form → 30%

        OBS: Denna faktor förbättras kraftigt med extern data
        (kuskstatistik från travsport.se). I grundversion
        använder vi data tillgänglig i hästens historik.
        """
        recent = entry.horse.recent_starts(10)

        if not recent:
            return 45.0

        # 1. Kusk+häst combo
        combo_score = self._combo_score(entry, recent) * 0.40

        # 2. Kuskbyte-effekt
        driver_change_score = self._driver_change_score(entry, recent) * 0.30

        # 3. Tränare (baserat på hästens resultat under nuvarande tränare)
        trainer_score = self._trainer_form_score(entry, recent) * 0.30

        return min(100.0, max(0.0, combo_score + driver_change_score + trainer_score))

    @staticmethod
    def _combo_score(entry: RaceEntry, recent: list) -> float:
        """Hur bra har denna kusk presterat med denna häst?"""
        driver = entry.driver_name.lower()
        if not driver:
            return 45.0

        combo_starts = [s for s in recent if s.driver_name.lower() == driver]

        if not combo_starts:
            return 40.0  # Ny kombination — osäkert

        wins = sum(1 for s in combo_starts if s.won)
        top3 = sum(1 for s in combo_starts if s.top3)
        total = len(combo_starts)

        win_rate = wins / total
        top3_rate = top3 / total

        score = win_rate * 70 + top3_rate * 25 + min(total / 5, 1.0) * 5
        return min(100.0, score * 100 / 100)

    @staticmethod
    def _driver_change_score(entry: RaceEntry, recent: list) -> float:
        """Effekt av kuskbyte.

        Kuskbyte till en bättre kusk kan vara mycket positivt.
        Vi mäter genom att jämföra: senaste kusken vs ny kusk.
        """
        if not recent:
            return 50.0

        last_driver = recent[0].driver_name.lower()
        current_driver = entry.driver_name.lower()

        if not current_driver or not last_driver:
            return 50.0

        if current_driver == last_driver:
            return 55.0  # Samma kusk — stabilt

        # Kuskbyte: vi vet inte säkert om det är uppgradering
        # Men i genomsnitt är kuskbyten svagt positiva
        return 58.0  # Liten bonus för kuskbyte

    @staticmethod
    def _trainer_form_score(entry: RaceEntry, recent: list) -> float:
        """Tränare-form baserat på hästens senaste resultat.

        (Proxy: vi har inte global tränare-stat, men vi ser
        hur hästen presterat under aktuell tränare.)
        """
        trainer = entry.trainer_name.lower()
        if not trainer:
            return 45.0

        trainer_starts = [s for s in recent if s.trainer_name.lower() == trainer]

        if not trainer_starts:
            return 45.0

        wins = sum(1 for s in trainer_starts if s.won)
        top3 = sum(1 for s in trainer_starts if s.top3)
        total = len(trainer_starts)

        win_rate = wins / total
        top3_rate = top3 / total

        return min(100.0, (win_rate * 60 + top3_rate * 30) * 100 / 90 + 10)
