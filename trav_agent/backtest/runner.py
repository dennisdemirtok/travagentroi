"""Backtesting runner — kör analysmodellen på historiska omgångar.

Hämtar gamla omgångar, kör analysen, och jämför med faktiskt utfall.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..analysis.composite import CompositeAnalyzer
from ..config import AnalysisConfig
from ..data.atg_client import ATGClient
from ..data.models import GameRound, Race

logger = logging.getLogger(__name__)


@dataclass
class RacePrediction:
    """En prediktion för ett lopp."""

    race_number: int
    date: date
    track: str

    # Vår modells ranking (hästnummer i rankordning)
    predicted_ranking: list[int] = field(default_factory=list)

    # Faktiskt resultat
    actual_result: list[int] = field(default_factory=list)

    # Detaljerad data per häst
    horse_scores: dict[int, float] = field(default_factory=dict)  # nummer → score
    horse_bet_pct: dict[int, float] = field(default_factory=dict)  # nummer → streck%
    horse_factor_scores: dict[int, dict[str, float]] = field(default_factory=dict)  # nummer → {faktor: score}

    # Rekommendationer
    spik: Optional[int] = None
    top_picks: list[int] = field(default_factory=list)  # 2-val + 3-val

    @property
    def spik_correct(self) -> Optional[bool]:
        """Vann vår spik?"""
        if self.spik and self.actual_result:
            return self.spik == self.actual_result[0]
        return None

    @property
    def top_pick_hit(self) -> bool:
        """Vann någon av våra top picks?"""
        if self.actual_result and self.top_picks:
            return self.actual_result[0] in self.top_picks
        return False

    @property
    def top3_picks_in_top3(self) -> int:
        """Hur många av våra top 3 hamnade i topp 3?"""
        if not self.actual_result or not self.top_picks:
            return 0
        actual_top3 = set(self.actual_result[:3])
        our_top3 = set(self.top_picks[:3])
        return len(actual_top3 & our_top3)


@dataclass
class RoundPrediction:
    """Prediktioner för en hel omgång."""

    game_type: str
    date: date
    races: list[RacePrediction] = field(default_factory=list)

    @property
    def num_spik_correct(self) -> int:
        return sum(1 for r in self.races if r.spik_correct)

    @property
    def num_spik_total(self) -> int:
        return sum(1 for r in self.races if r.spik is not None)


class BacktestRunner:
    """Kör analysmodellen på historiska omgångar."""

    def __init__(
        self,
        client: Optional[ATGClient] = None,
        config: Optional[AnalysisConfig] = None,
    ):
        self.client = client or ATGClient()
        self.analyzer = CompositeAnalyzer(config)

    @staticmethod
    def _filter_future_starts(game_round: GameRound) -> None:
        """Filtrera bort past_starts som skedde EFTER loppets datum.

        Förhindrar data leakage i backtesting — modellen ska bara se
        data som var tillgänglig vid loppets tidpunkt.
        """
        for race in game_round.races:
            race_date = race.race_date
            for entry in race.entries:
                original = entry.horse.past_starts
                filtered = [s for s in original if s.start_date < race_date]
                if len(filtered) < len(original):
                    entry.horse.past_starts = filtered

    async def run_single(self, game_round: GameRound) -> RoundPrediction:
        """Kör analys på en enskild omgång och jämför med resultat.

        Kräver att game_round har resultatdata (finished races).
        """
        # Filtrera bort framtida starter för att undvika data leakage
        self._filter_future_starts(game_round)

        # Kör vår analys
        self.analyzer.analyze_round(game_round)

        round_pred = RoundPrediction(
            game_type=game_round.game_type,
            date=game_round.round_date,
        )

        for race in game_round.races:
            pred = self._evaluate_race(race)
            round_pred.races.append(pred)

        return round_pred

    async def run_range(
        self,
        game_type: str,
        start_date: date,
        end_date: date,
    ) -> list[RoundPrediction]:
        """Kör backtesting över ett datumintervall med progress-feedback."""
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

        logger.info(f"Backtesting {game_type} från {start_date} till {end_date}")

        predictions: list[RoundPrediction] = []
        rounds_found = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed} omgångar"),
            transient=False,
        ) as progress:
            task = progress.add_task(
                f"Hämtar {game_type} {start_date} → {end_date}",
                total=None,
            )

            async for day, game_round in self.client.fetch_historical_rounds_iter(
                game_type, start_date, end_date
            ):
                if game_round:
                    rounds_found += 1
                    progress.update(task, completed=rounds_found, description=f"{game_type} {day}")

                    if game_round.is_finished:
                        pred = await self.run_single(game_round)
                        predictions.append(pred)
                        logger.info(
                            f"{game_type} {game_round.round_date}: "
                            f"{pred.num_spik_correct}/{pred.num_spik_total} spikar rätt"
                        )

            progress.update(task, description=f"Klart — {rounds_found} omgångar")

        return predictions

    def _evaluate_race(self, race: Race) -> RacePrediction:
        """Utvärdera en enskild loppprediktion."""
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )

        pred = RacePrediction(
            race_number=race.race_number,
            date=race.race_date,
            track=race.track_name,
            predicted_ranking=[e.post_position for e in sorted_entries],
            actual_result=race.result_order,
            horse_scores={e.post_position: e.super_score for e in sorted_entries},
            horse_bet_pct={
                e.post_position: e.bet_percentage or 0
                for e in sorted_entries
            },
            horse_factor_scores={
                e.post_position: dict(e.factor_scores)
                for e in sorted_entries
            },
        )

        # Spikar och top picks
        spik_entries = [e for e in sorted_entries if e.recommendation == "spik"]
        if spik_entries:
            pred.spik = spik_entries[0].post_position

        pred.top_picks = [
            e.post_position
            for e in sorted_entries
            if e.recommendation in ("spik", "2-val", "3-val")
        ]

        return pred
