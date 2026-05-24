"""Sammanvägd analys — kombinerar alla faktorer till en slutpoäng.

Varje faktor ger en poäng 0-100 per häst.
Dessa viktas enligt FactorWeights och summeras till composite_score.
super_score = 85% modellpoäng + 15% streckprocent (marknadssignal).

Version 2: Korrigerad modell utan data leakage.
- Vikter: tid 28%, form 22%, pris 18%, kategori 14%, spår 8%, kusk 5%, bana 5%
- Interaktionstermer borttagna (förstärkte brus)
- Marknadsvikt sänkt från 45% till 15% (streck ≠ facit)
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from ..config import AnalysisConfig, DEFAULT_CONFIG
from ..data.models import GameRound, Race, RaceEntry
from .base import AnalysisFactor
from .age_factor import AgeFactor
from .category_profile import CategoryProfile
from .driver_class import DriverClass
from .driver_trainer import DriverTrainer
from .equipment import Equipment
from .form_curve import FormCurve
from .post_position import PostPosition
from .prize_index import PrizeIndex
from .time_analysis import TimeAnalysis
from .track_profile import TrackProfile
from .recent_form_signals import LastWinFactor, CompetitionStrength, LayoffFactor

logger = logging.getLogger(__name__)


class CompositeAnalyzer:
    """Huvudanalysmotor — kör alla faktorer och viktar ihop."""

    def __init__(self, config: Optional[AnalysisConfig] = None):
        self.config = config or DEFAULT_CONFIG
        self.factors: list[AnalysisFactor] = [
            # v7.1: senaste 5 primart + 6-10 som stod med decay
            TimeAnalysis(recent_n=10),
            PrizeIndex(recent_n=self.config.recent_starts_count),
            FormCurve(recent_n=self.config.recent_starts_count),
            TrackProfile(),
            CategoryProfile(),
            DriverTrainer(),
            PostPosition(),
            # v6: Datamining-baserade
            DriverClass(),
            Equipment(),
            AgeFactor(),
            # v7: Dennis's tre formsignaler
            LastWinFactor(),
            CompetitionStrength(),
            LayoffFactor(),
        ]

    def analyze_race(self, race: Race) -> list[RaceEntry]:
        """Analysera alla hästar i ett lopp.

        Returnerar entries sorterade efter composite_score (högst först).

        Flöde:
        1. Beräkna råpoäng per faktor per entry
        2. Fältnormalisering — normalisera varje faktor inom fältet till 0-100
        3. Viktat medel → rå composite
        4. Sigmoid-spridning → sprider mittenfältet för bättre differentiering
        5. Sortera, ranka, klassificera
        """
        weights = self.config.weights.normalized()

        # Steg 1: Beräkna råpoäng per faktor per entry
        for entry in race.active_entries:
            factor_scores = {}

            for factor in self.factors:
                try:
                    score = factor.score(entry, race)
                    factor_scores[factor.name] = round(score, 1)
                except Exception as e:
                    logger.warning(
                        f"Faktor {factor.name} misslyckades för "
                        f"{entry.horse.name}: {e}"
                    )
                    factor_scores[factor.name] = 40.0  # Fallback

            entry.factor_scores = factor_scores

        # Steg 2: Fältnormalisering — per faktor, normalisera alla entries i loppet
        factor_names = [f.name for f in self.factors]
        for factor_name in factor_names:
            raw_scores = {
                e.post_position: e.factor_scores.get(factor_name, 40.0)
                for e in race.active_entries
            }

            # Säkerhetsgräns: normalisera inte om spridningen är <5 poäng
            # (förhindrar att brus i små fält blåses upp)
            min_s = min(raw_scores.values()) if raw_scores else 0
            max_s = max(raw_scores.values()) if raw_scores else 0
            if max_s - min_s < 5.0:
                continue  # Behåll råpoäng

            normalized = AnalysisFactor.normalize_scores(raw_scores)

            for entry in race.active_entries:
                entry.factor_scores[factor_name] = round(
                    normalized.get(entry.post_position, 50.0), 1
                )

        # Steg 3: Viktat medel → rå composite (med interaktioner)
        interactions = self.config.weights.interactions()
        for entry in race.active_entries:
            composite = 0.0
            for factor_name, weight in weights.items():
                score = entry.factor_scores.get(factor_name, 50.0)
                composite += score * weight

            # Interaktionstermer: f1 × f2 (normaliserade 0-1) × vikt × 100
            for f1, f2, iw in interactions:
                v1 = entry.factor_scores.get(f1, 50.0) / 100.0
                v2 = entry.factor_scores.get(f2, 50.0) / 100.0
                composite += v1 * v2 * 100.0 * iw

            entry.composite_score = round(composite, 1)

        # Steg 4: Sigmoid-spridning — sträcker ut mittenfältet
        for entry in race.active_entries:
            entry.composite_score = round(
                self._spread_score(entry.composite_score), 1
            )

        # Steg 4b: Super Score — 85% modell + 15% marknad
        self._compute_super_scores(race)

        # Steg 5: Sortera och ranka (baserat på super_score)
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )

        # Beräkna gap till rank 2 (för spik-bedömning, baserat på super_score)
        gap_to_second = 0.0
        if len(sorted_entries) >= 2:
            gap_to_second = sorted_entries[0].super_score - sorted_entries[1].super_score

        for rank, entry in enumerate(sorted_entries, start=1):
            entry.rank = rank
            entry.recommendation = self._classify(entry, race, rank, gap_to_second)

        # Steg 6: Skrällrisk-analys
        self._assess_upset_risk(race, sorted_entries)

        return sorted_entries

    @staticmethod
    def _spread_score(raw: float, k: float = 0.08) -> float:
        """Sigmoid-spridning för att sträcka ut poängfördelningen.

        Mappar (med k=0.08):
          30 → 16.8,  40 → 31.0,  50 → 50.0,  60 → 69.0,  70 → 83.2
        """
        return 100.0 / (1.0 + math.exp(-k * (raw - 50.0)))

    def _compute_super_scores(self, race: Race) -> None:
        """Beräkna super_score = 85% modell + 15% marknadssignal.

        Streckprocent ger kompletterande information men ska inte
        dominera. Tidigare 45% marknadsvikt orsakade data leakage
        i backtester (slutgiltig streck korrelerar med vinnare).

        Streckprocent konverteras till 0-100 via min-max
        normalisering inom fältet, så att skalan matchar composite.

        Om streckprocent saknas helt → super_score = composite_score.
        """
        model_weight = self.config.super_score_model_weight
        market_weight = 1.0 - model_weight

        active = race.active_entries
        has_market = any(e.bet_percentage and e.bet_percentage > 0 for e in active)

        if not has_market or market_weight <= 0:
            # Ingen marknadsdata → super = composite
            for entry in active:
                entry.super_score = entry.composite_score
            return

        # Normalisera streckprocent till 0-100 skala via min-max
        # (bevarar relativa skillnader, matchar composite-skala)
        bet_pcts = {
            e.post_position: (e.bet_percentage or 0.0) * 100.0
            for e in active
        }
        min_pct = min(bet_pcts.values())
        max_pct = max(bet_pcts.values())
        spread = max_pct - min_pct

        if spread < 1.0:
            # Alla har ~samma streckprocent → marknad ger ingen info
            for entry in active:
                entry.super_score = entry.composite_score
            return

        # Min-max normalisering till 5-95 (undviker 0 och 100 extremer)
        market_scores = {}
        for pos, pct in bet_pcts.items():
            market_scores[pos] = 5.0 + 90.0 * (pct - min_pct) / spread

        # Blend: super = model_weight * composite + market_weight * market
        for entry in active:
            market_s = market_scores.get(entry.post_position, 50.0)
            entry.super_score = round(
                model_weight * entry.composite_score
                + market_weight * market_s,
                1,
            )

    def analyze_round(self, game_round: GameRound) -> GameRound:
        """Analysera en hel spelomgång."""
        for race in game_round.races:
            self.analyze_race(race)
            logger.info(
                f"Avd {race.race_number}: "
                f"{race.active_entries[0].horse.name if race.active_entries else '?'} "
                f"→ {race.active_entries[0].recommendation if race.active_entries else '?'}"
            )
        return game_round

    def _classify(
        self, entry: RaceEntry, race: Race, rank: int, gap_to_second: float = 0.0
    ) -> str:
        """Klassificera en häst: spik, 2-val, 3-val, gardering, strykning.

        Spik-kriterier: rank 1, hög poäng, stort gap, max 30% streck,
        och value_index >= 1.0 (modellen ser mer potential än marknaden).
        """
        score = entry.super_score
        bet_pct = entry.bet_percentage or 0.0

        # Value index: modellpoäng relativt marknadsförväntning
        # Hög value_index = modellen ser mer potential än marknaden
        if bet_pct > 0:
            value_index = score / (bet_pct * 100)
        else:
            value_index = score / 10

        # Spik: rank 1, hög poäng, stort gap
        # Med marknadsdata: kräv max 30% streck + value_index >= 1.0
        # Utan marknadsdata: rena modellsignaler räcker
        if (
            rank == 1
            and score >= self.config.spike_min_score
            and gap_to_second >= self.config.spike_min_gap
        ):
            if bet_pct > 0:
                # Med marknadsdata: kräv att vi inte spikar en överspelad favorit
                if bet_pct <= self.config.spike_threshold and value_index >= 1.0:
                    return "spik"
                return "2-val"
            else:
                # Utan marknadsdata: modellens gap och score räcker
                return "spik"
        elif rank <= 2 and score >= self.config.choice2_min_score:
            return "2-val"
        elif rank <= 3 and score >= self.config.choice3_min_score:
            return "3-val"
        elif score >= self.config.gardering_min_score:
            return "gardering"
        else:
            return "strykning"

    def _assess_upset_risk(self, race: Race, sorted_entries: list[RaceEntry]) -> None:
        """Beräkna skrällrisk för ett lopp.

        Ombyggd version — den gamla modellen differentierade inte
        (25% skrällrate oavsett risknivå).

        Ny metod fokuserar på de signaler som FAKTISKT predicerar skrällar:
        1. Marknadssignal: favoriten har hög streck men modellen rankar ej #1
        2. Fälttightness: liten skillnad mellan top 5
        3. Gap-risk: liten skillnad rank 1 vs 2-3
        4. Fältstorlek + startmetod
        5. Antal "dolda" hästar: rank 4+ med bra tid/banprofil

        Sätter race.upset_risk (0-100) och race.upset_candidates (hästnummer).
        """
        if len(sorted_entries) < 3:
            return

        scores = [e.super_score for e in sorted_entries]
        num_starters = len(sorted_entries)

        # ── Signal 1: Modell vs Marknad oenighet (ny, starkast) ─────────
        # Om marknadens favorit (högst streck) inte är modellens #1
        # → hög skrällrisk för den hästen
        market_fav = max(sorted_entries, key=lambda e: e.bet_percentage or 0)
        market_fav_pct = market_fav.bet_percentage or 0
        model_rank_of_fav = market_fav.rank

        # Hög streck men inte modellens topp → oenighet
        if market_fav_pct > 0.20 and model_rank_of_fav >= 3:
            disagreement_risk = min(100, (model_rank_of_fav - 1) * 20 + market_fav_pct * 100)
        elif market_fav_pct > 0.15 and model_rank_of_fav >= 2:
            disagreement_risk = min(100, (model_rank_of_fav - 1) * 15 + market_fav_pct * 50)
        else:
            disagreement_risk = 0

        # ── Signal 2: Top-5 tightness ───────────────────────────────────
        top5_scores = scores[:min(5, len(scores))]
        if len(top5_scores) >= 3:
            top5_spread = top5_scores[0] - top5_scores[-1]
            tightness = max(0, min(100, (25 - top5_spread) * 5))
        else:
            tightness = 0

        # ── Signal 3: Gap rank 1 → 2-3 ─────────────────────────────────
        gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 30
        gap_1_3 = scores[0] - scores[2] if len(scores) >= 3 else 30
        gap_risk = max(0, min(100, (8 - gap_1_2) * 12))
        gap_risk_13 = max(0, min(100, (15 - gap_1_3) * 6))

        # ── Signal 4: Fältstorlek + voltstart ───────────────────────────
        field_size_risk = min(100, max(0, (num_starters - 8) * 15))
        is_volt = race.start_method.value == "volt"
        volt_risk = 25 if is_volt else 0

        # ── Signal 5: Dolda skrällkandidater ────────────────────────────
        # Hästar rank 4+ med stark enskild faktor (bra tid, banprofil, spår)
        upset_candidates: list[int] = []
        upset_strength = 0.0

        for entry in sorted_entries:
            if entry.rank <= 3:
                continue

            fs = entry.factor_scores
            time_s = fs.get("time_analysis", 50.0)
            track = fs.get("track_profile", 50.0)
            post = fs.get("post_position", 50.0)
            prize = fs.get("prize_index", 50.0)

            # En häst rank 4+ med toppscores i tid/ban/spår
            strong_factors = sum([
                time_s >= 70,
                track >= 70,
                post >= 65,
                prize >= 60,
            ])

            if strong_factors >= 2:
                upset_candidates.append(entry.post_position)
                profile_score = (
                    max(0, time_s - 50) * 0.3
                    + max(0, track - 50) * 0.3
                    + max(0, post - 40) * 0.2
                    + max(0, prize - 40) * 0.2
                )
                upset_strength = max(upset_strength, profile_score)

        candidate_score = min(100, len(upset_candidates) * 25)

        # ── Kombinera ───────────────────────────────────────────────────
        raw_risk = (
            disagreement_risk * 0.25      # Modell vs marknad oenighet
            + tightness * 0.20            # Top-5 tightness
            + gap_risk * 0.15             # Gap 1→2
            + gap_risk_13 * 0.10          # Gap 1→3
            + field_size_risk * 0.08      # Stort fält
            + volt_risk * 0.07            # Voltstart
            + min(100, upset_strength * 1.5) * 0.10  # Kandidatstyrka
            + candidate_score * 0.05      # Antal kandidater
        )
        race.upset_risk = round(min(100, max(0, raw_risk)), 0)
        race.upset_candidates = upset_candidates

    def get_value_picks(self, race: Race) -> list[RaceEntry]:
        """Hitta spelvärda hästar: hög poäng, lågt streck.

        Sweet spot: 5-15% streck med hög composite_score.
        """
        low, high = self.config.value_sweet_spot
        return [
            e for e in race.active_entries
            if e.bet_percentage
            and low <= e.bet_percentage <= high
            and e.super_score >= 50
        ]
