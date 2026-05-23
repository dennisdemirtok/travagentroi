"""Banprofil — hur hästen presterar på den aktuella banan.

Vissa hästar trivs bättre på specifika banor (kurvhästar, storovalhästar etc.)
Analyserar vinstprocent och placeringshistorik per bana.
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class TrackProfile(AnalysisFactor):
    """Analyserar hästens prestation på den aktuella banan."""

    name = "track_profile"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på banstatistik.

        1. Placeringshistorik på denna bana → 60%
        2. Antal starter på banan (erfarenhet) → 20%
        3. Stall/tränare-prestation på banan → 20%
        """
        track_starts = entry.horse.starts_at_track(race.track_name)

        if not track_starts:
            # Ingen erfarenhet på banan — neutral
            return 45.0

        # 1. Placeringshistorik
        wins = sum(1 for s in track_starts if s.won)
        top3 = sum(1 for s in track_starts if s.top3)
        total = len(track_starts)

        win_rate = wins / total
        top3_rate = top3 / total
        placement_score = (win_rate * 70 + top3_rate * 30) * 100 / 100 * 0.60

        # 2. Erfarenhet — fler starter = mer pålitlig data
        experience = min(1.0, total / 10)  # Maxar vid 10 starter
        experience_score = (30 + experience * 50) * 0.20

        # 3. Tränare på banan — placeholder (kan berikas med tränare-stats)
        trainer_score = 50.0 * 0.20

        return min(100.0, max(0.0, placement_score + experience_score + trainer_score))
