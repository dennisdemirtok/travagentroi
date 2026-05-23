"""Kuskklassfaktor — kuskvinstprocent som prediktiv signal.

Signalkälla: Analys av 166 000+ starter visar att kuskvinstprocent
är den STARKASTE oupptäckta signalen i datamaterialet:
- 20%+ vinstprocent: 1.89x lift (nästan dubbel segerfrekvens)
- 15-20%: 1.44x lift
- 10-15%: 1.05x (nära genomsnitt)
- 5-10%: 0.72x (klart under genomsnitt)
- 0-5%: 0.40x (mycket negativt)

Kusken har en enorm påverkan — en bra kusk gör rätt taktiska beslut
(position i fältet, timing av slutspurt, tempo) som inte syns i
hästens historik men påverkar utgången kraftigt.

Kräver minst 30 starter under året för tillförlitlig data.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor

# Brytpunkter: (vinstprocent, poäng)
# Baserade på lift-värden mappade till 0-100 skala
_BREAKPOINTS: list[tuple[float, float]] = [
    (0.00, 15.0),   # 0.40x lift — mycket negativt
    (0.05, 35.0),   # 0.72x lift
    (0.10, 55.0),   # 1.05x lift — nära genomsnitt
    (0.15, 75.0),   # 1.44x lift — starkt
    (0.20, 90.0),   # 1.89x lift — elit
]

# Poäng för otillräcklig data (< 30 starter)
_INSUFFICIENT_DATA_SCORE = 45.0


def _interpolate(win_pct: float) -> float:
    """Linjär interpolation mellan brytpunkter."""
    # Under lägsta brytpunkt
    if win_pct <= _BREAKPOINTS[0][0]:
        return _BREAKPOINTS[0][1]

    # Över högsta brytpunkt
    if win_pct >= _BREAKPOINTS[-1][0]:
        return _BREAKPOINTS[-1][1]

    # Hitta rätt intervall och interpolera
    for i in range(len(_BREAKPOINTS) - 1):
        x0, y0 = _BREAKPOINTS[i]
        x1, y1 = _BREAKPOINTS[i + 1]
        if x0 <= win_pct <= x1:
            t = (win_pct - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    # Fallback (borde aldrig nås)
    return 50.0


class DriverClass(AnalysisFactor):
    """Analyserar kuskens klassnivå baserat på vinstprocent."""

    name = "driver_class"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på kuskvinstprocent.

        Logik:
        - Om kuskdata saknas eller < 30 starter: returnera 45 (osäker)
        - Annars: linjär interpolation mellan brytpunkter
          0% → 15, 5% → 35, 10% → 55, 15% → 75, 20%+ → 90
        """
        driver_win_pct: float = getattr(entry, "driver_win_pct", 0.0)
        driver_starts_year: int = getattr(entry, "driver_starts_year", 0)

        # Otillräcklig data — returnera neutral-låg poäng
        if driver_starts_year < 30:
            return _INSUFFICIENT_DATA_SCORE

        return min(100.0, max(0.0, _interpolate(driver_win_pct)))
