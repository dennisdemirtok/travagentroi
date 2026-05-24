"""Kuskklassfaktor — kuskvinstprocent, top3-andel och hemmabanebonus.

Signalkälla: Analys av 166 000+ starter visar att kuskvinstprocent
är den STARKASTE oupptäckta signalen i datamaterialet:
- 20%+ vinstprocent: 1.89x lift (nästan dubbel segerfrekvens)
- 15-20%: 1.44x lift
- 10-15%: 1.05x (nära genomsnitt)
- 5-10%: 0.72x (klart under genomsnitt)
- 0-5%: 0.40x (mycket negativt)

Utöver vinstprocent mäter vi:
- Top-3 andel: konsistent hög top3% = säker kusk
- Hemmabanebonus: kuskar kör bättre på sin hemmabana (+3-5pp edge)
- Earnings-per-start: samlar ihop kvalitet+antal

Kräver minst 30 starter under året för tillförlitlig data.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor

# Brytpunkter: (vinstprocent, poäng)
# Baserade på lift-värden mappade till 0-100 skala
_WIN_BREAKPOINTS: list[tuple[float, float]] = [
    (0.00, 15.0),   # 0.40x lift — mycket negativt
    (0.05, 35.0),   # 0.72x lift
    (0.10, 55.0),   # 1.05x lift — nära genomsnitt
    (0.15, 75.0),   # 1.44x lift — starkt
    (0.20, 90.0),   # 1.89x lift — elit
]

# Top-3 breakpoints (top3_pct → score)
_TOP3_BREAKPOINTS: list[tuple[float, float]] = [
    (0.00, 20.0),
    (0.15, 35.0),
    (0.25, 50.0),
    (0.35, 65.0),
    (0.45, 80.0),
    (0.55, 90.0),
]

# Hemmabanebonus (kör på sin hemmabana)
_HOME_TRACK_BONUS = 5.0

# Poäng för otillräcklig data (< 30 starter)
_INSUFFICIENT_DATA_SCORE = 45.0


def _interpolate(value: float, breakpoints: list[tuple[float, float]]) -> float:
    """Linjär interpolation mellan brytpunkter."""
    if value <= breakpoints[0][0]:
        return breakpoints[0][1]
    if value >= breakpoints[-1][0]:
        return breakpoints[-1][1]

    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return 50.0


class DriverClass(AnalysisFactor):
    """Analyserar kuskens klassnivå baserat på vinstprocent, top3 och hemmabana."""

    name = "driver_class"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på kusk-statistik.

        Komponenter (viktade):
        - Vinstprocent: 60% (starkaste signalen)
        - Top-3 andel: 25% (konsistens)
        - Hemmabanebonus: 15% (kör bättre på sin bana)
        """
        driver_win_pct: float = getattr(entry, "driver_win_pct", 0.0)
        driver_starts_year: int = getattr(entry, "driver_starts_year", 0)
        driver_top3_pct: float = getattr(entry, "driver_top3_pct", 0.0)
        driver_home_track: str = getattr(entry, "driver_home_track", "")

        # Otillräcklig data — returnera neutral-låg poäng
        if driver_starts_year < 30:
            return _INSUFFICIENT_DATA_SCORE

        # 1. Vinstprocent (60%)
        win_score = _interpolate(driver_win_pct, _WIN_BREAKPOINTS) * 0.60

        # 2. Top-3 andel (25%)
        top3_score = _interpolate(driver_top3_pct, _TOP3_BREAKPOINTS) * 0.25

        # 3. Hemmabanebonus (15%)
        home_score = 50.0  # Neutral default
        if driver_home_track and hasattr(race, "track_name"):
            track_name = (race.track_name or "").lower().strip()
            home_lower = driver_home_track.lower().strip()
            if track_name and home_lower:
                # Fuzzy match: "Solvalla" ∈ "Solvalla" or partial
                if home_lower in track_name or track_name in home_lower:
                    home_score = 50.0 + _HOME_TRACK_BONUS / 0.15  # Boost
        home_component = home_score * 0.15

        total = win_score + top3_score + home_component
        return min(100.0, max(0.0, total))
