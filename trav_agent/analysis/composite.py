"""Sammanvagd analys — kombinerar alla faktorer till en slutpoang.

Varje faktor ger en poang 0-100 per hast.
Dessa viktas enligt FactorWeights och summeras till composite_score.

Version 10: Triple blend — 33% composite + 33% effform + 33% marknad.
- 4 analysfaktorer → composite_score (spar, alder, kusk, kategori)
- Dennis Brain v2 → effective_form (tidsrank + confidence + klass + kusk)
- Streckprocent → marknadssignal
- super_score = 0.33 * comp + 0.33 * effform + 0.33 * marknad
- Backtesterat pa 350 lopp: 43.4% rank-1 (+6.9pp vs marknad)
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
from .gallop_risk import GallopRisk
from .proffs_consensus import ProffsFactor
from .dennis_brain import compute_dennis_form_signals, compute_dennis_picks

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
            # v8.1: Galopprisksignal
            GallopRisk(),
            # v9: Viktat proffsstreck (vikt 0.0 tills data samlats in)
            ProffsFactor(),
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

        # Steg 4b: Dennis Brain v2 form-signaler (effective_form)
        # Kors FORE super_score sa att effform kan anvandas i triple-blenden
        compute_dennis_form_signals(race)

        # Steg 4c: Super Score — 33% comp + 33% effform + 33% marknad
        self._compute_super_scores(race)

        # Steg 5: Sortera och ranka (baserat på super_score)
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )

        # Dynamisk A/B/C/D baserat på poängfördelning
        # I jämna lopp: fler B, ingen ensam A
        # I klara lopp: 1 A (spik), tydlig hierarki
        self._classify_dynamic(race, sorted_entries)

        # Steg 6: Skrällrisk-analys
        self._assess_upset_risk(race, sorted_entries)

        # Steg 7: Dennis Brain — S4 vinnarspel-picks (behover rank)
        compute_dennis_picks(race)

        return sorted_entries

    @staticmethod
    def _spread_score(raw: float, k: float = 0.08) -> float:
        """Sigmoid-spridning för att sträcka ut poängfördelningen.

        Mappar (med k=0.08):
          30 → 16.8,  40 → 31.0,  50 → 50.0,  60 → 69.0,  70 → 83.2
        """
        return 100.0 / (1.0 + math.exp(-k * (raw - 50.0)))

    def _compute_super_scores(self, race: Race) -> None:
        """Berakna super_score = 33% composite + 33% effform + 33% marknad.

        Triple blend — backtesterad pa 350 lopp (8 veckor, 2026):
          Ren marknad:         36.6% rank-1, 75.4% top-3
          Comp 25% + Mark 75%: 37.7% rank-1, 75.7% top-3 (gammal)
          Triple 33/33/33:     43.4% rank-1, 75.7% top-3 (ny!)

        Tre oberoende signaler:
        - Composite: strukturella faktorer (spar, alder, kusk, kategori)
        - EffForm (Dennis Brain v2): tidsbaserad ranking med confidence,
          klassdropp och kuskbonus
        - Marknad: streckprocent (kollektiv bedomning)

        Alla tre normaliseras till 0-100 och blandas lika.
        Om effform saknas for en hast → fallback till comp+marknad (50/50).
        Om marknad saknas helt → comp+effform (50/50).
        """
        active = race.active_entries
        has_market = any(e.bet_percentage and e.bet_percentage > 0 for e in active)

        # ── Normalisera composite till 0-100 (min-max) ──
        comps = [e.composite_score for e in active]
        min_c, max_c = min(comps), max(comps)
        comp_range = max_c - min_c if max_c > min_c else 1.0

        # ── Normalisera marknad till 0-100 (min-max) ──
        market_scores: dict[int, float] = {}
        if has_market:
            bet_pcts = {
                e.post_position: (e.bet_percentage or 0.0) * 100.0
                for e in active
            }
            min_pct = min(bet_pcts.values())
            max_pct = max(bet_pcts.values())
            spread = max_pct - min_pct
            if spread >= 1.0:
                for pos, pct in bet_pcts.items():
                    market_scores[pos] = 5.0 + 90.0 * (pct - min_pct) / spread

        # ── Normalisera effective_form till 0-100 (inverterad: lagre tid = hogre score) ──
        eff_forms = [
            e.dennis_effective_form for e in active
            if e.dennis_effective_form and e.dennis_effective_form > 0
        ]
        eff_scores: dict[int, float] = {}
        if len(eff_forms) >= 3:
            min_e, max_e = min(eff_forms), max(eff_forms)
            eff_range = max_e - min_e if max_e > min_e else 1.0
            for e in active:
                ef = e.dennis_effective_form
                if ef and ef > 0:
                    # Inverterad: lagst tid (snabbast) = 95, hogst = 5
                    eff_scores[e.post_position] = 5.0 + 90.0 * (max_e - ef) / eff_range

        # ── Triple blend ──
        for entry in active:
            comp_norm = 5.0 + 90.0 * (entry.composite_score - min_c) / comp_range
            market_s = market_scores.get(entry.post_position)
            eff_s = eff_scores.get(entry.post_position)

            # Dynamisk viktning beroende pa vilka signaler som finns
            if market_s is not None and eff_s is not None:
                # Alla tre signaler — 33/33/33
                entry.super_score = round(
                    0.333 * comp_norm + 0.333 * eff_s + 0.334 * market_s, 1
                )
            elif market_s is not None:
                # Ingen effform — comp+marknad 50/50
                entry.super_score = round(
                    0.50 * comp_norm + 0.50 * market_s, 1
                )
            elif eff_s is not None:
                # Ingen marknad — comp+effform 50/50
                entry.super_score = round(
                    0.50 * comp_norm + 0.50 * eff_s, 1
                )
            else:
                # Bara composite
                entry.super_score = round(comp_norm, 1)

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

    def _classify_dynamic(
        self, race: Race, sorted_entries: list[RaceEntry]
    ) -> None:
        """Dynamisk A/B/C/D-klassificering baserad på fältposition.

        Dennis-metod: A/B/C/D reflekterar analysens syn, inte bettingvärde.
        Distributionen skalar med fältstorleken:
          - 6 hästar:  1A, 1-2B, 2C, 1-2D
          - 10 hästar: 0-1A, 3B, 3-4C, 3D
          - 14 hästar: 0-1A, 4-5B, 5-6C, 3-4D

        A (spik): rank 1 med TYDLIG ledning (relativt gap ≥ 30%).
                  Streck spelar INGEN roll — A = analysens klar toppval.
        B (2-val/3-val): topp 35% av fältet = starka alternativ.
        C (gardering): mellangrupp 35-70% = outsidechans.
        D (strykning): botten 30% = inga realistiska chanser.
        """
        if not sorted_entries:
            return

        n = len(sorted_entries)
        scores = [e.super_score for e in sorted_entries]
        spread = scores[0] - scores[-1] if n > 1 else 0

        # Gap till rank 2
        gap_to_second = scores[0] - scores[1] if n >= 2 else 0

        # Relativ gap: hur stor del av total spread är gapet till 2:an?
        relative_gap = gap_to_second / spread if spread > 0 else 0

        for rank, entry in enumerate(sorted_entries, start=1):
            entry.rank = rank

            # Fältposition: 0.0 = bäst, 1.0 = sämst
            position_pct = (rank - 1) / max(n - 1, 1)

            # ── A (spik): bara vid TYDLIG ledare ──
            if (
                rank == 1
                and relative_gap >= 0.30
                and entry.super_score >= self.config.spike_min_score
                and gap_to_second >= self.config.spike_min_gap
            ):
                entry.recommendation = "spik"
                continue

            # ── B (2-val/3-val): topp 35% av fältet ──
            if position_pct < 0.35:
                entry.recommendation = "2-val" if rank <= 3 else "3-val"
            # ── C (gardering): mellangrupp 35-70% ──
            elif position_pct < 0.70:
                entry.recommendation = "gardering"
            # ── D (strykning): botten 30% ──
            else:
                entry.recommendation = "strykning"

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
