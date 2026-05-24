"""Kusk & tränare — vinstprocent, form, kombo, och utländsk tränare-edge.

Analyserar:
- Kuskens vinstprocent (generellt och på aktuell bana)
- Kusk+häst kombination (har de kört ihop förut?)
- Kuskbyte-effekt
- Tränare-form baserad på hästens historik
- UTLÄNDSK TRÄNARE-EDGE: Gocciadoro, italienska/utländska tränare

Data från 1559 backtestade lopp visar:
  Gocciadoro: 20.4% vinstfrekvens (baseline 8.6%) — +137% edge
  Gocciadoro + Djuse: 24.2% vinstfrekvens, sommar (jun-aug): 46.7%
  Utländska tränare generellt: ~12% vinstfrekvens (+40% edge)
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor

# Kända topptränare med dokumenterad edge
# Baserat på analys av 1559 lopp (V75+V85+V86 2024-2026)
ELITE_TRAINERS = {
    "alessandro gocciadoro": {
        "base_boost": 25.0,       # Stark edge generellt (20.4% win rate)
        "summer_boost": 15.0,     # Extra boost jun-aug
        "elite_drivers": {        # Specifika kusk-combos
            "magnus a djuse": 10.0,  # 24.2% win rate som team
        },
    },
}

# Länder med dokumenterad tränare-edge (utländska tränare som
# reser till Sverige med hästar i form)
FOREIGN_COUNTRIES = {
    "italien", "frankrike", "norge", "danmark", "finland",
    "tyskland", "belgien", "holland", "spanien", "australien",
    "usa", "kanada",
}


class DriverTrainer(AnalysisFactor):
    """Analyserar kusk och tränare."""

    name = "driver_trainer"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på kusk/tränare.

        Komponenter:
        1. Kusk+häst kombination (tidigare samarbete) → 25%
        2. Kuskbyte-effekt → 15%
        3. Tränare-form (baserat på hästens resultat) → 25%
        4. Tränare-edge (Gocciadoro/utländsk) → 20%
        5. Kusk-kvalitet (vinstprocent) → 15%
        """
        recent = entry.horse.recent_starts(10)

        if not recent:
            # Ingen historik — kolla ändå tränare-edge
            trainer_edge = self._trainer_edge_score(entry, race) * 0.35
            driver_qual = self._driver_quality_score(entry) * 0.15
            return min(100.0, max(0.0, 45.0 * 0.50 + trainer_edge + driver_qual))

        # 1. Kusk+häst combo
        combo_score = self._combo_score(entry, recent) * 0.25

        # 2. Kuskbyte-effekt
        driver_change_score = self._driver_change_score(entry, recent) * 0.15

        # 3. Tränare-form
        trainer_score = self._trainer_form_score(entry, recent) * 0.25

        # 4. Tränare-edge (Gocciadoro/utländsk)
        trainer_edge = self._trainer_edge_score(entry, race) * 0.20

        # 5. Kusk-kvalitet
        driver_qual = self._driver_quality_score(entry) * 0.15

        return min(100.0, max(0.0, combo_score + driver_change_score +
                              trainer_score + trainer_edge + driver_qual))

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
        """Effekt av kuskbyte."""
        if not recent:
            return 50.0

        last_driver = recent[0].driver_name.lower()
        current_driver = entry.driver_name.lower()

        if not current_driver or not last_driver:
            return 50.0

        if current_driver == last_driver:
            return 55.0  # Samma kusk — stabilt

        # Kuskbyte: i snitt svagt positivt (tränaren vill ha ändring)
        return 58.0

    @staticmethod
    def _trainer_form_score(entry: RaceEntry, recent: list) -> float:
        """Tränare-form baserat på hästens senaste resultat."""
        trainer = entry.horse.trainer_name.lower()
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

    @staticmethod
    def _trainer_edge_score(entry: RaceEntry, race: Race) -> float:
        """Tränare-edge baserat på känd tränare-kvalitet.

        Gocciadoro: 20.4% vinstfrekvens (baseline ~8.6%)
        Utländska tränare generellt: ~12% vinstfrekvens
        Sommarmånader (jun-aug): extra boost för italienska tränare
        """
        trainer_name = entry.horse.trainer_name.lower().strip()
        trainer_loc = entry.horse.trainer_location.lower().strip()

        if not trainer_name:
            return 45.0

        base_score = 45.0  # Neutral default

        # Check elite trainers
        for elite_name, config in ELITE_TRAINERS.items():
            if elite_name in trainer_name:
                base_score += config["base_boost"]

                # Summer boost (juni-augusti)
                month = race.race_date.month
                if 6 <= month <= 8:
                    base_score += config.get("summer_boost", 0)

                # Driver combo boost
                driver_lower = entry.driver_name.lower().strip()
                for driver_name, boost in config.get("elite_drivers", {}).items():
                    if driver_name in driver_lower:
                        base_score += boost
                        break

                return min(100.0, base_score)

        # Foreign trainer edge (non-elite but still foreign)
        if trainer_loc in FOREIGN_COUNTRIES:
            base_score += 12.0  # ~12% vinstfrekvens vs 8.6% baseline

            # Summer boost for foreign trainers too
            month = race.race_date.month
            if 6 <= month <= 8:
                base_score += 5.0

            return min(100.0, base_score)

        return base_score

    @staticmethod
    def _driver_quality_score(entry: RaceEntry) -> float:
        """Kusk-kvalitet baserat på vinstprocent.

        driver_win_pct is 0-1 (e.g., 0.15 = 15% wins).
        Average is ~8-10%, elite is 15%+.
        """
        win_pct = entry.driver_win_pct
        if win_pct <= 0:
            return 40.0

        # Scale: 0% → 20, 8% → 50, 15% → 75, 25%+ → 95
        if win_pct < 0.05:
            return 20.0 + win_pct / 0.05 * 20.0
        elif win_pct < 0.10:
            return 40.0 + (win_pct - 0.05) / 0.05 * 15.0
        elif win_pct < 0.15:
            return 55.0 + (win_pct - 0.10) / 0.05 * 15.0
        elif win_pct < 0.25:
            return 70.0 + (win_pct - 0.15) / 0.10 * 20.0
        else:
            return 90.0 + min((win_pct - 0.25) / 0.10, 1.0) * 5.0
