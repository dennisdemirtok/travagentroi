"""Åldersfaktor — hästens ålder som prediktiv signal.

Signalkälla: 166 000+ starter visar ett tydligt åldersmönster:
- Ålder 4: 1.37x lift (TOPP — hästen är i sin bästa ålder)
- Ålder 3: 1.27x lift (ung och utvecklingsbar)
- Ålder 5: 1.16x lift (fortfarande stark)
- Ålder 2: 1.15x lift (tidig start, positiv signal om de tävlar)
- Ålder 6: 0.95x lift (börjar avta)
- Ålder 7: 0.78x lift
- Ålder 8: 0.63x lift
- Ålder 9+: 0.52x lift eller lägre

Mönstret är logiskt: hästar når sin fysiska topp runt 4-5 år.
Yngre hästar (2-3) har utvecklingspotential men saknar erfarenhet.
Äldre hästar (7+) tappar successivt i kapacitet.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor

# Ålder → poäng (baserat på lift-värden mappade till 0-100 skala)
_AGE_SCORES: dict[int, float] = {
    2: 60.0,   # 1.15x lift
    3: 75.0,   # 1.27x lift
    4: 85.0,   # 1.37x lift — TOPP
    5: 65.0,   # 1.16x lift
    6: 50.0,   # 0.95x lift
    7: 40.0,   # 0.78x lift
    8: 30.0,   # 0.63x lift
    9: 20.0,   # 0.52x lift
}

# Ålder 10+ (klart avtagande)
_MIN_SCORE = 15.0


def _interpolate_age(age: int) -> float:
    """Hämta poäng för en given ålder med linjär interpolation.

    Heltal mappas direkt. Ålder under 2 eller över 10 ger
    gränsvärdena.
    """
    if age <= 1:
        # Mycket ovanligt att 1-åringar tävlar, behandla som ålder 2
        return _AGE_SCORES[2]

    if age >= 10:
        return _MIN_SCORE

    if age in _AGE_SCORES:
        return _AGE_SCORES[age]

    # Linjär interpolation mellan omgivande hela åldrar
    # (Borde inte nås med heltalsåldrar, men säkerhetsnät)
    lower = max(k for k in _AGE_SCORES if k < age)
    upper = min(k for k in _AGE_SCORES if k > age)
    t = (age - lower) / (upper - lower)
    return _AGE_SCORES[lower] + t * (_AGE_SCORES[upper] - _AGE_SCORES[lower])


class AgeFactor(AnalysisFactor):
    """Analyserar hästens ålder som prediktiv faktor."""

    name = "age"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på hästens ålder.

        Logik:
        - Ålder 4 = 85 (topp), Ålder 3 = 75, Ålder 5 = 65, Ålder 2 = 60
        - Ålder 6 = 50, Ålder 7 = 40, Ålder 8 = 30, Ålder 9 = 20
        - Ålder 10+ = 15
        - Om ålder saknas (0): returnera 50 (neutral)
        """
        age = entry.horse.age

        # Saknad ålder — returnera neutral poäng
        if age <= 0:
            return 50.0

        return min(100.0, max(0.0, _interpolate_age(age)))
