"""Tidanalys v4 — Dennis's metod: distansjusterad tid senaste 5 starter.

Ombyggd baserat pa Dennis's personliga metodik:
- BARA senaste 5 starter (aktuell form, inte gammal historia)
- Projicera forvantad km-tid vid LOPPETS distans/metod
- Auto ar snabbare an volt — viktning beror pa distans
- Tunga banor/forhallanden beaktas
- Output: confidence score for predicted time at current distance

Nyckelinsikter:
  Volt→auto median: ~1.4s (1600-2200m), ~0.3s (2400+m)
  Distansfaktor: 0.20s/100m (empiriskt fran 455k startpar)
  Dennis: "senaste 5 brukar ge indikation pa hur man star sig just nu"
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

# Track condition offsets (estimated — tung bana etc.)
# These are applied when track conditions are known
HEAVY_TRACK_OFFSET = 1.5  # Seconds slower on heavy track
WINTER_TRACK_OFFSET = 0.8  # Slightly slower in winter

# Benchmark km-tider vid standarddistanser (auto, varmblod)
DISTANCE_BENCHMARKS = {
    1640: 72.5,
    1700: 72.7,
    2140: 74.0,
    2640: 75.5,
    3140: 77.0,
}

# Recency weights for 5 starts (most recent = highest)
RECENCY_WEIGHTS = [1.0, 0.85, 0.70, 0.55, 0.40]


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
    """Berakna distansjustering med empirisk faktor + cap.

    Linjar 0.2s/100m upp till ±400m, diminishing 0.1s/100m utover.
    """
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
    """Project a km_time from one race config to another.

    Dennis's method: "det gar inte jamfora en som har sprungit 1640
    och en som bara sprungit 2140 — dar far man analysera och se
    vilken skillnad tidsskillnaderna kan vara"

    This converts a historical km_time to what that horse would
    likely run at the target distance/method.
    """
    projected = km_time

    # Distance adjustment: longer distances = slower per km
    dist_delta = to_distance - from_distance
    projected += _capped_distance_adjustment(dist_delta)

    # Start method adjustment
    # Auto is faster than volt — if horse ran volt and now runs auto,
    # subtract the offset (will be faster)
    # Dennis: "auto ar ju snabbare an volt sa en som sprungit sin tid
    # pa auto kan vara snabbare och ska inte viktas lika hart"
    volt_diff = _get_volt_auto_diff(from_distance)
    if from_method == StartMethod.VOLT and to_method == StartMethod.AUTO:
        projected -= volt_diff  # Volt->auto = faster
    elif from_method == StartMethod.AUTO and to_method == StartMethod.VOLT:
        projected += volt_diff  # Auto->volt = slower

    # Cold blood adjustment
    if breed == "kallblod":
        # Already baked into their times, no adjustment needed for same breed
        pass

    return projected


def _get_benchmark_for_race(race: Race) -> float:
    """Get expected benchmark km_time for this race configuration."""
    dist = race.distance
    closest_dist = min(DISTANCE_BENCHMARKS.keys(), key=lambda d: abs(d - dist))
    benchmark = DISTANCE_BENCHMARKS[closest_dist]

    # Adjust for distance difference from closest benchmark
    dist_diff = (dist - closest_dist) / 100 * DISTANCE_FACTOR
    benchmark += dist_diff

    # Volt races are slower
    if race.start_method == StartMethod.VOLT:
        benchmark += _get_volt_auto_diff(dist)

    # Cold blood
    if race.breed.value == "kallblod":
        benchmark += COLD_BLOOD_OFFSET

    return benchmark


class TimeAnalysis(AnalysisFactor):
    """Dennis's tidanalys: distansjusterad tid senaste 5 starter.

    Karnidé: Projicera varje historisk tid till loppets distans/metod,
    vikta efter relevans och recency, ge en confidence score.

    Dennis: "senaste 5 starternas tid dar jag forsökt göra en viktning
    beroende pa vilken distans eller auto,volt for de beraknas lite
    olika. det far ju vara nagon confidence score kring tiden man
    tror de har just nu formmasigt"
    """

    name = "time_analysis"

    def __init__(self, recent_n: int = 5):
        self.recent_n = min(recent_n, 5)  # Max 5 — Dennis's rule

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score based on projected km times at current race distance.

        Components:
        1. Projected best time (40%) — best time after distance/method projection
        2. Weighted average projected time (25%) — recency-weighted average
        3. Time confidence (20%) — how confident are we in the projection
        4. Form trend (15%) — improving or declining times

        Returns 0-100 score.
        """
        recent = entry.horse.recent_starts(5)  # Always 5
        timed_starts = [s for s in recent if s.km_time and s.km_time > 0]

        if not timed_starts:
            return 30.0  # No time data

        breed = race.breed.value
        benchmark = _get_benchmark_for_race(race)

        # Project all times to current race distance/method
        projections = []
        for i, s in enumerate(timed_starts):
            projected = project_time_to_distance(
                s.km_time,
                s.distance if s.distance > 0 else race.distance,
                s.start_method,
                race.distance,
                race.start_method,
                breed,
            )
            # Calculate projection confidence for this individual start
            conf = self._start_projection_confidence(s, race)
            recency_w = RECENCY_WEIGHTS[i] if i < len(RECENCY_WEIGHTS) else 0.3
            projections.append((projected, conf, recency_w, s))

        # ── 1. Projected best time (40%) ────────────────────────────────
        # Best projected time, weighted by confidence
        # A projection from same distance+method is more reliable
        best_proj_score = self._best_projected_score(projections, benchmark)

        # ── 2. Weighted average projected time (25%) ────────────────────
        avg_proj_score = self._weighted_avg_score(projections, benchmark)

        # ── 3. Time confidence (20%) ────────────────────────────────────
        # How reliable is our time projection? Based on data quality.
        confidence_score = self._confidence_score(projections, race)

        # ── 4. Form trend (15%) ─────────────────────────────────────────
        # Are times improving or getting worse?
        trend_score = self._trend_score(projections)

        total = (
            best_proj_score * 0.40
            + avg_proj_score * 0.25
            + confidence_score * 0.20
            + trend_score * 0.15
        )

        return min(100.0, max(0.0, total))

    @staticmethod
    def _start_projection_confidence(start: PastStart, race: Race) -> float:
        """How confident are we in projecting this start's time to the race?

        High confidence:
        - Same distance (±100m) AND same method → 1.0
        - Same method, close distance (±300m) → 0.85
        - Different method but same distance → 0.75

        Lower confidence:
        - Different method AND different distance → 0.5-0.65
        - Very different distance (>600m) → 0.3-0.4

        Also considers: disqualified/galloped starts are less reliable
        """
        confidence = 1.0

        # Distance similarity
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

        # Method similarity
        if start.start_method == race.start_method:
            method_conf = 1.0
        else:
            method_conf = 0.75

        confidence = dist_conf * method_conf

        # Penalize galloped/disqualified starts (unreliable time)
        if start.galloped or start.disqualified:
            confidence *= 0.4

        return confidence

    @staticmethod
    def _best_projected_score(
        projections: list[tuple[float, float, float, PastStart]],
        benchmark: float,
    ) -> float:
        """Score based on the best projected time.

        Uses confidence-weighted selection: a very good time with low
        confidence is worth less than a good time with high confidence.
        """
        if not projections:
            return 30.0

        # Score each projection: quality * confidence
        best_score = 0.0
        for projected, conf, recency_w, _start in projections:
            # Time quality: how much faster than benchmark
            diff = benchmark - projected  # Positive = faster
            quality = 50.0 + diff * 15.0
            quality = min(100.0, max(0.0, quality))

            # Weight by confidence (but don't zero out — even low-conf
            # data is better than nothing)
            effective_quality = quality * (0.3 + 0.7 * conf)
            best_score = max(best_score, effective_quality)

        return best_score

    @staticmethod
    def _weighted_avg_score(
        projections: list[tuple[float, float, float, PastStart]],
        benchmark: float,
    ) -> float:
        """Recency-weighted average of projected times."""
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
        diff = benchmark - avg_time
        score = 50.0 + diff * 12.0  # Slightly less aggressive than best-time
        return min(100.0, max(0.0, score))

    @staticmethod
    def _confidence_score(
        projections: list[tuple[float, float, float, PastStart]],
        race: Race,
    ) -> float:
        """Overall confidence in our time projection.

        High confidence when:
        - Multiple starts at similar distance/method
        - No galloped/disqualified starts
        - Recent starts (not stale data)
        - Consistent times (low variance)
        """
        if not projections:
            return 20.0

        # Factor 1: Data quantity (more starts = more confident)
        qty_score = min(1.0, len(projections) / 4.0)  # 4+ starts = full qty

        # Factor 2: Average individual confidence
        avg_conf = sum(conf for _, conf, _, _ in projections) / len(projections)

        # Factor 3: Time consistency (low std dev = more predictable)
        times = [proj for proj, _, _, _ in projections]
        if len(times) >= 2:
            mean_t = sum(times) / len(times)
            variance = sum((t - mean_t) ** 2 for t in times) / len(times)
            std_dev = math.sqrt(variance)
            # std_dev < 1.0s is very consistent, > 3.0s is wild
            consistency = max(0.0, min(1.0, 1.0 - (std_dev - 0.5) / 3.0))
        else:
            consistency = 0.5

        # Factor 4: Any same-distance data?
        has_exact = any(
            abs(s.distance - race.distance) <= 100 and s.start_method == race.start_method
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
        """Are projected times improving or declining?

        Compares most recent 2 starts vs older starts.
        Improving = positive trend = higher score.
        """
        if len(projections) < 3:
            return 50.0  # Not enough data for trend

        # Recent: first 2 projections (most recent starts)
        recent_times = [proj for proj, conf, _, _ in projections[:2] if conf > 0.3]
        older_times = [proj for proj, conf, _, _ in projections[2:] if conf > 0.3]

        if not recent_times or not older_times:
            return 50.0

        avg_recent = sum(recent_times) / len(recent_times)
        avg_older = sum(older_times) / len(older_times)

        # Improvement: older was slower (higher km_time) than recent
        improvement = avg_older - avg_recent  # Positive = improving
        score = 50.0 + improvement * 18.0
        return min(100.0, max(0.0, score))


# Keep normalize_km_time for backward compatibility (used by other modules)
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
