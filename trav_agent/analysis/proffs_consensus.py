"""Viktat proffsstreck — konsensus från professionella tipsare.

Datakälla: beta.kungenstrav.se API
- 73+ professionella tipsare (spellaggare)
- 301+ system per omgång
- Viktning: spik=5p, 2 picks=3p, 3 picks=2p, 4-5 picks=1p, 6+=0.5p

Score = viktad andel proffs som pekar på hästen.
Redan normaliserat 0-100 av API:t.

OBS: Kräver att proffs_cache/ har data för aktuell omgång.
     Samlas in via fetch_proffs_data.py (körs ~1h före start).
     Om data saknas returneras neutral score (50.0).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor

logger = logging.getLogger(__name__)

PROFFS_CACHE_DIR = Path(__file__).parent.parent.parent / "proffs_cache"


class ProffsFactor(AnalysisFactor):
    """Viktat proffsstreck som analysfaktor.

    Score = viktad procentandel av professionella tipsare som valt hästen.
    Redan normaliserat 0-100 av datakällan.

    Startvikt: 0.0 (aktiveras efter insamling av 50+ omgångar)
    Förväntad optimal vikt: 0.05-0.15 baserat på liknande konsensussignaler
    """

    name = "proffs_consensus"

    def __init__(self):
        self._cache: dict[str, dict] = {}  # game_id -> proffs data

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Returnera viktad proffs-procent för hästen.

        Letar i proffs_cache/ efter data som matchar loppet.
        Om data saknas → 50.0 (neutral, påverkar inte ranking).
        """
        proffs_pct = self._get_proffs_pct(entry.post_position, race)
        if proffs_pct is None:
            return 50.0
        return proffs_pct

    def _get_proffs_pct(self, horse_number: int, race: Race) -> float | None:
        """Hitta proffs-procent för en häst i ett specifikt lopp."""
        # Build cache key from race date + track
        race_date_str = str(race.race_date)

        # Try to find matching proffs data file
        if not PROFFS_CACHE_DIR.exists():
            return None

        # Load data lazily
        if not self._cache:
            self._load_cache(race_date_str)

        # Search through cached data
        for game_id, data in self._cache.items():
            if race_date_str not in game_id:
                continue

            for race_data in data.get("races", []):
                if race_data.get("race_number") != race.race_number:
                    continue

                for horse in race_data.get("horses", []):
                    if horse.get("number") == horse_number:
                        return horse.get("proffs_weighted_pct")

        return None

    def _load_cache(self, date_str: str):
        """Ladda proffs-data från cache-filer för ett datum."""
        if not PROFFS_CACHE_DIR.exists():
            return

        for fp in PROFFS_CACHE_DIR.glob(f"*{date_str}*_pre.json"):
            try:
                with open(fp) as f:
                    data = json.load(f)
                game_id = data.get("game_id", fp.stem)
                self._cache[game_id] = data
            except Exception as e:
                logger.warning(f"Kunde inte ladda proffs-data: {fp}: {e}")
