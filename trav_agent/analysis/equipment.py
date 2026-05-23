"""Utrustningsfaktor — barfot, sulkybyte och skobyte.

Signalkälla: Datamining av 14 292 lopp visar tydliga mönster:
- Barfot (inga skor fram eller bak): 9.7% segerfrekvens vs 7.7% = 1.13x lift
- Sulkybyte: 9.5% vs 8.5% = 1.11x lift
- Skobyte (ej barfot): 8.1% vs 8.7% = 0.95x (svagt negativt ensamt)

Barfot är den starkaste signalen — tränaren visar att hästen har kapacitet
och att barfot ger en genuin hastighetsfördel. Sulkybyte indikerar
taktisk förändring, ofta kopplat till förbättring. Skobyte utan barfot
är en mildare taktisk signal.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class Equipment(AnalysisFactor):
    """Analyserar utrustningsförändringar: barfot, sulkybyte, skobyte."""

    name = "equipment"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på utrustningssignaler.

        Logik:
        - Bas = 50
        - Barfot (fram eller bak): +15 (starkaste signalen, 1.13x lift)
        - Sulkybyte: +10 (1.11x lift)
        - Skobyte utan barfot: +5 (taktisk förändring)
        - Resultat klämms till 0-100
        """
        shoe_front_off: bool = getattr(entry, "shoe_front_off", False)
        shoe_back_off: bool = getattr(entry, "shoe_back_off", False)
        shoe_changed: bool = getattr(entry, "shoe_changed", False)
        sulky_changed: bool = getattr(entry, "sulky_changed", False)

        score = 50.0

        # Barfot = starkaste signalen (1.13x lift)
        barefoot = shoe_front_off or shoe_back_off
        if barefoot:
            score += 15.0

        # Sulkybyte = taktisk förändring (1.11x lift)
        if sulky_changed:
            score += 10.0

        # Skobyte utan barfot = mild taktisk signal (0.95x ensamt, men
        # i kombination med andra förändringar kan det vara positivt)
        if shoe_changed and not barefoot:
            score += 5.0

        return min(100.0, max(0.0, score))
