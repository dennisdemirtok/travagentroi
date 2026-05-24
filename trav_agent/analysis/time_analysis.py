"""Tidanalys v5 — Dennis's metod: distansjusterad tid, tillagg, auto/volt.

Dennis's insikter (senaste feedback):
- Tid = HUVUDSIGNAL, ska viktas tungt
- Senaste 5 starter ar starkast, senaste 10 kan kollas men med decay
  ("hastar tappar form eller blir aldre")
- "a" = auto, utan "a" = volt i km-tid
- Individuella distanser: 2140 vs 2160 = 20m tillagg
  ("de som startar med tillagg maste springa mycket fortare")
- Hog PRIS + lagt odds i historik = stark hast
- Amerikansk vagn (Am) vs vanlig vagn (Va) byte = signal

Nyckelkonstanter (empiriska):
  Volt→auto median: ~1.4s (1600-2200m), ~0.3s (2400+m)
  Distansfaktor: 0.20s/100m
  Tillagg: 20m = ~0.04s/km extra (hasten springer langre,
           plus ute i ytterspar = annu mer nackdel)
"""

from __future__ import annotations

import math
from typing import Optional

from ..data.models import Race, RaceEntry, PastStart, StartMethod
from .base import AnalysisFactor

# ── Empiriskt kalibrerade konstanter ─────────────────────────────────────────

# Distansfaktor: 0.2s/100m (median fran 455k startpar)
DISTANCE_FACTOR = 0.20

# Diminishing factor utover cap
DIMINISHING_FACTOR = 0.10

# Max distansskillnad i meter for linjar justering
MAX_DISTANCE_LINEAR_M = 400

# Volt→auto offset: distansberoende (empiriskt kalibrerad)
AUTO_VOLT_DIFF = {
    "short": 1.4,   # <=2200m
    "medium": 0.8,   # 2201-2500m
    "long": 0.3,     # 2501-2700m
    "stayer": 0.0,   # >2700m
}

# Kallblod offset
COLD_BLOOD_OFFSET = 6.0

# Benchmark km-tider vid standarddistanser (auto, varmblod)
DISTANCE_BENCHMARKS = {
    1640: 72.5,
    1700: 72.7,
    2140: 74.0,
    2640: 75.5,
    3140: 77.0,
}

# Tillagg: 20m extra distans i voltlopp
# Hasten springer langre OCH ute i ytterspar
# Empirisk effekt: ~0.04-0.06 s/km per 20m tillagg
TILLAGG_PENALTY_PER_20M = 0.05  # s/km slower per 20m tillagg

# Recency weights: 5 primary + 5 secondary (decay)
# Dennis: "senaste 5 ar starkast, senaste 10 kan kollas men med decay"
RECENCY_WEIGHTS_PRIMARY = [1.0, 0.90, 0.78, 0.65, 0.50]
RECENCY_WEIGHTS_SECONDARY = [0.30, 0.22, 0.15, 0.10, 0.06]


def _get_volt_auto_diff(distance: int) -> float:
    """Distansberoende volt→auto-offset."""
    if distance <= 2200:
        return AUTO_VOLT_DIFF["short"]
    elif distance <= 2500:
        return AUTO_VOLT_DIFF["medium"]
    elif distance <= 2700:
        return AUTO_VOLT_DIFF["long"]
    else:
        return AUTO_VOLT_DIFF["stayer"]


def _capped_distance_adjustment(dist_delta_m: int) -> float:
    """Distansjustering med cap + diminishing returns."""
    abs_delta = abs(dist_delta_m)
    sign = 1 if dist_delta_m >= 0 else -1

    if abs_delta <= MAX_DISTANCE_LINEAR_M:
        return dist_delta_m / 100 * DISTANCE_FACTOR
    else:
        linear_part = MAX_DISTANCE_LINEAR_M / 100 * DISTANCE_FACTOR
        excess = abs_delta - MAX_DISTANCE_LINEAR_M
        diminishing_part = excess / 100 * DIMINISHING_FACTOR
        return sign * (linear_part + diminishing_part)


def project_time_to_distance(
    km_time: float,
    from_distance: int,
    from_method: StartMethod,
    to_distance: int,
    to_method: StartMethod,
    breed: str = "varmblod",
) -> float:
    """Projicera en km-tid fran en konfiguration till en annan.

    Dennis: "det gar inte jamfora en som har sprungit 1640 och en som
    bara sprungit 2140 — dar far man analysera och se vilken skillnad
    tidsskillnaderna kan vara for att fa en trolig tid"
    """
    projected = km_time

    # Distansjustering
    dist_delta = to_distance - from_distance
    projected += _capped_distance_adjustment(dist_delta)

    # Startmetodjustering
    volt_diff = _get_volt_auto_diff(from_distance)
    if from_method == StartMethod.VOLT and to_method == StartMethod.AUTO:
        projected -= volt_diff
    elif from_method == StartMethod.AUTO and to_method == StartMethod.VOLT:
        projected += volt_diff

    return projected


def _get_benchmark_for_race(race: Race) -> float:
    """Benchmark km-tid for loppets konfiguration."""
    dist = race.distance
    closest_dist = min(DISTANCE_BENCHMARKS.keys(), key=lambda d: abs(d - dist))
    benchmark = DISTANCE_BENCHMARKS[closest_dist]

    dist_diff = (dist - closest_dist) / 100 * DISTANCE_FACTOR
    benchmark += dist_diff

    if race.start_method == StartMethod.VOLT:
        benchmark += _get_volt_auto_diff(dist)

    if race.breed.value == "kallblod":
        benchmark += COLD_BLOOD_OFFSET

    return benchmark


def _tillagg_penalty(entry: RaceEntry, race: Race) -> float:
    """Berakna tillags-nackdel for hastar med extra distans.

    Dennis: "de som startar med tillagg maste springa mycket fortare
    ute i sparen for att hinna ikapp. de kan inte viktas pa samma
    satt i ett lopp nar det kommer till tid."

    Entry.distance > race.distance = hasten har tillagg.
    20m tillagg ar vanligast, 40m forekomemr.
    """
    if entry.distance <= 0 or race.distance <= 0:
        return 0.0

    tillagg_m = entry.distance - race.distance
    if tillagg_m <= 0:
        return 0.0

    # Varje 20m tillagg = ~0.05 s/km extra (langre distans + yttre spar)
    penalty = (tillagg_m / 20.0) * TILLAGG_PENALTY_PER_20M
    return penalty


class TimeAnalysis(AnalysisFactor):
    """Tidanalys v5 — distansjusterad tid, tillagg, 5+10 starter.

    Dennis: "distans tid ocksa viktigt att det ar senaste tiden.
    5 senaste starter ar starkast och sedan kan man kolla senaste
    10 men dar man inte fa bli for blind pa for gamla resultat
    hastar tappar form eller blir aldre"

    Arkitektur:
    1. Senaste 5: hog vikt (primart signal)
    2. Senaste 6-10: lag vikt (stod/bakgrund med decay)
    3. Tillagg-kompensation (entry.distance vs race.distance)
    4. Distans/metod-projektion till loppets konfiguration
    """

    name = "time_analysis"

    def __init__(self, recent_n: int = 10):
        # Anvander 10 starter men viktar: 5 primart, 5 sekundart
        self.recent_n = 10

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score 0-100 baserat pa projicerade km-tider.

        Komponenter:
        1. Basta projicerade tid (35%) — basta tid efter justering
        2. Viktat medeltal senaste 5 (25%) — aktuell form
        3. Bakgrunds-medeltal 6-10 (10%) — historisk kapacitet med decay
        4. Tidskonfidens (15%) — hur sakra ar vi
        5. Formtrend (15%) — forbattras eller forsamras tider
        """
        recent = entry.horse.recent_starts(self.recent_n)
        timed_starts = [s for s in recent if s.km_time and s.km_time > 0]

        if not timed_starts:
            return 30.0

        breed = race.breed.value
        benchmark = _get_benchmark_for_race(race)

        # Tillagg-kompensation for denna hast
        tillagg = _tillagg_penalty(entry, race)

        # Projicera alla tider till loppets distans/metod
        # Anvand HASTENS individuella distans (entry.distance) som maldistans
        target_dist = entry.distance if entry.distance > 0 else race.distance
        target_method = race.start_method

        primary_projs = []    # Senaste 5
        secondary_projs = []  # Starter 6-10

        for i, s in enumerate(timed_starts):
            projected = project_time_to_distance(
                s.km_time,
                s.distance if s.distance > 0 else race.distance,
                s.start_method,
                target_dist,
                target_method,
                breed,
            )
            conf = self._start_projection_confidence(s, race)

            if i < 5:
                w = RECENCY_WEIGHTS_PRIMARY[i] if i < len(RECENCY_WEIGHTS_PRIMARY) else 0.4
                primary_projs.append((projected, conf, w, s))
            else:
                idx = i - 5
                w = RECENCY_WEIGHTS_SECONDARY[idx] if idx < len(RECENCY_WEIGHTS_SECONDARY) else 0.05
                secondary_projs.append((projected, conf, w, s))

        # ── 1. Basta projicerade tid (35%) ──────────────────────────────
        best_score = self._best_projected_score(
            primary_projs + secondary_projs, benchmark, tillagg
        )

        # ── 2. Viktat medeltal senaste 5 (25%) ─────────────────────────
        avg5_score = self._weighted_avg_score(primary_projs, benchmark, tillagg)

        # ── 3. Bakgrunds-medeltal 6-10 (10%) ───────────────────────────
        if secondary_projs:
            bg_score = self._weighted_avg_score(secondary_projs, benchmark, tillagg)
        else:
            bg_score = avg5_score  # Fallback till senaste 5

        # ── 4. Tidskonfidens (15%) ──────────────────────────────────────
        confidence = self._confidence_score(primary_projs, race)

        # ── 5. Formtrend (15%) ──────────────────────────────────────────
        trend = self._trend_score(primary_projs)

        total = (
            best_score * 0.35
            + avg5_score * 0.25
            + bg_score * 0.10
            + confidence * 0.15
            + trend * 0.15
        )

        return min(100.0, max(0.0, total))

    @staticmethod
    def _start_projection_confidence(start: PastStart, race: Race) -> float:
        """Konfidens for att projicera denna starts tid till loppet.

        Hog konfidens:
        - Samma distans (±100m) + samma metod → 1.0
        - Samma metod, nara distans (±300m) → 0.85
        - Annan metod men samma distans → 0.75

        Lag konfidens:
        - Annan metod + annan distans → 0.5-0.65
        - Mycket annorlunda distans (>600m) → 0.3-0.4
        - Diskad/galopp → reduceras 60%
        """
        dist_diff = abs(start.distance - race.distance) if start.distance > 0 else 500

        if dist_diff <= 100:
            dist_conf = 1.0
        elif dist_diff <= 300:
            dist_conf = 0.85
        elif dist_diff <= 500:
            dist_conf = 0.65
        elif dist_diff <= 800:
            dist_conf = 0.45
        else:
            dist_conf = 0.30

        method_conf = 1.0 if start.start_method == race.start_method else 0.75
        confidence = dist_conf * method_conf

        if start.galloped or start.disqualified:
            confidence *= 0.4

        return confidence

    @staticmethod
    def _best_projected_score(
        projections: list[tuple[float, float, float, PastStart]],
        benchmark: float,
        tillagg: float = 0.0,
    ) -> float:
        """Score baserat pa basta projicerade tid.

        Med konfidens-viktning: bra tid med lag konfidens
        ar vard mindre an ok tid med hog konfidens.
        """
        if not projections:
            return 30.0

        best_score = 0.0
        for projected, conf, recency_w, _start in projections:
            # Justera for tillagg (tillagg gor tiden mer imponerande)
            adjusted = projected - tillagg  # Ta bort tillaggs-handikapp

            diff = benchmark - adjusted  # Positivt = snabbare
            quality = 50.0 + diff * 15.0
            quality = min(100.0, max(0.0, quality))

            effective_quality = quality * (0.3 + 0.7 * conf)
            best_score = max(best_score, effective_quality)

        return best_score

    @staticmethod
    def _weighted_avg_score(
        projections: list[tuple[float, float, float, PastStart]],
        benchmark: float,
        tillagg: float = 0.0,
    ) -> float:
        """Recency-viktat medeltal av projicerade tider."""
        if not projections:
            return 30.0

        total_weight = 0.0
        weighted_time = 0.0

        for projected, conf, recency_w, _start in projections:
            w = recency_w * conf
            weighted_time += projected * w
            total_weight += w

        if total_weight == 0:
            return 30.0

        avg_time = weighted_time / total_weight
        # Justera for tillagg
        avg_time -= tillagg

        diff = benchmark - avg_time
        score = 50.0 + diff * 12.0
        return min(100.0, max(0.0, score))

    @staticmethod
    def _confidence_score(
        projections: list[tuple[float, float, float, PastStart]],
        race: Race,
    ) -> float:
        """Overall konfidens i tidsprojektionen.

        Hog konfidens nar:
        - Manga starter vid liknande distans/metod
        - Inga galopperade/diskade starter
        - Konsekventa tider (lag varians)
        - Exakt distans-data finns
        """
        if not projections:
            return 20.0

        qty_score = min(1.0, len(projections) / 4.0)
        avg_conf = sum(conf for _, conf, _, _ in projections) / len(projections)

        times = [proj for proj, _, _, _ in projections]
        if len(times) >= 2:
            mean_t = sum(times) / len(times)
            variance = sum((t - mean_t) ** 2 for t in times) / len(times)
            std_dev = math.sqrt(variance)
            consistency = max(0.0, min(1.0, 1.0 - (std_dev - 0.5) / 3.0))
        else:
            consistency = 0.5

        has_exact = any(
            abs(s.distance - race.distance) <= 100
            and s.start_method == race.start_method
            for _, _, _, s in projections
        )
        exact_bonus = 0.2 if has_exact else 0.0

        raw_conf = (
            qty_score * 0.25
            + avg_conf * 0.35
            + consistency * 0.25
            + exact_bonus
        ) * 100.0

        return min(100.0, max(0.0, raw_conf))

    @staticmethod
    def _trend_score(
        projections: list[tuple[float, float, float, PastStart]],
    ) -> float:
        """Forbattras eller forsamras tiderna?

        Jamfor senaste 2 med aldre starter.
        Dennis: "hastar tappar form eller blir aldre"
        """
        if len(projections) < 3:
            return 50.0

        recent_times = [proj for proj, conf, _, _ in projections[:2] if conf > 0.3]
        older_times = [proj for proj, conf, _, _ in projections[2:] if conf > 0.3]

        if not recent_times or not older_times:
            return 50.0

        avg_recent = sum(recent_times) / len(recent_times)
        avg_older = sum(older_times) / len(older_times)

        improvement = avg_older - avg_recent  # Positivt = forbattring
        score = 50.0 + improvement * 18.0
        return min(100.0, max(0.0, score))


# Bakåtkompatibilitet
def normalize_km_time(
    km_time: float,
    distance: int,
    start_method: StartMethod,
    breed_value: str = "varmblod",
    target_method: StartMethod = StartMethod.AUTO,
    target_distance: int = 2140,
) -> float:
    """Backward-compatible normalization wrapper."""
    return project_time_to_distance(
        km_time, distance, start_method,
        target_distance, target_method, breed_value,
    )
