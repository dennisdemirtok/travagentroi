"""Tidanalys — empiriskt kalibrerad km-tid per distans, startmetod, breed.

Version 3: Ombyggd baserat på empirisk analys av 695 lopp (29k starter).

Nyckelförändringar vs v2:
- Primärt signal: rå samma-distans-tid (Top-1: 16.2% vs 11.8% normaliserad)
- Volt→auto: 1.4s (empiriskt 1.28s median, var 3.0s)
- Distansfaktor: 0.2s/100m (empiriskt, var 0.8s)
- Trend: distansviktad (korr 0.048 vs 0.045 current)
- Best-time: blend exact*0.7 + other*0.3 (Top-3: 35.5% vs 32.9%)

Empirisk data:
  Volt→auto median: 1.28s (1600-2200m: ~1.4s, 2400+m: ~0.3s)
  Distansfaktor: 0.20s/100m (var 0.8s — 4x för högt)
  Rå samma-distans-tid: 21.4% Top-1 på 2140m auto (vs 13.2% normaliserad)
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor

# ── Empiriskt kalibrerade konstanter ─────────────────────────────────────────

# Referensdistans för normalisering
REFERENCE_DISTANCE = 2140

# Empirisk distansfaktor: 0.2s/100m (median från 455k startpar)
DISTANCE_FACTOR = 0.20

# Diminishing factor utöver cap
DIMINISHING_FACTOR = 0.10

# Max distansskillnad i meter för linjär justering
MAX_DISTANCE_ADJUSTMENT_M = 400

# Max distansskillnad för att inkludera en start
MAX_DISTANCE_INCLUDE = 800

# Max distansskillnad för "bästa tid"-beräkning
MAX_DISTANCE_BEST = 600

# Volt→auto offset: empiriskt kalibrerad (median 1.28s, mean 2.1s)
# Distansberoende: ~1.4s kort/medel, ~0.3s lång, ~0s stayer
AUTO_VOLT_DIFF_SHORT = 1.4   # ≤2200m
AUTO_VOLT_DIFF_LONG = 0.3    # 2201-2700m
AUTO_VOLT_DIFF_STAYER = 0.0  # >2700m

# Kallblod offset
COLD_BLOOD_OFFSET = 6.0

# Benchmark km-tider vid standarddistanser (auto, varmblod)
DISTANCE_BENCHMARKS = {
    1640: 72.5,
    2140: 74.0,
    2640: 75.5,
    3140: 77.0,
}

# "Nära" distans: samma distans ±100m, samma startmetod
EXACT_DISTANCE_THRESHOLD = 100

# "Ganska nära" distans: ±300m
CLOSE_DISTANCE_THRESHOLD = 300


def _get_volt_auto_diff(distance: int) -> float:
    """Distansberoende volt→auto-offset.

    Empiriskt kalibrerad:
      ≤2200m: 1.4s (median ~1.4s på 24k starter)
      2201-2700m: 0.3s (median ~0.3s)
      >2700m: 0.0s (ingen signifikant skillnad)
    """
    if distance <= 2200:
        return AUTO_VOLT_DIFF_SHORT
    elif distance <= 2700:
        return AUTO_VOLT_DIFF_LONG
    else:
        return AUTO_VOLT_DIFF_STAYER


def _capped_distance_adjustment(dist_delta: int) -> float:
    """Beräkna distansjustering med empirisk faktor + cap.

    Linjär 0.2s/100m upp till ±400m, diminishing 0.1s/100m utöver.

    Exempel:
      +200m → +0.4s
      +400m → +0.8s (cap)
      +800m → +0.8s + 0.4s = +1.2s
    """
    abs_delta = abs(dist_delta)
    sign = 1 if dist_delta >= 0 else -1

    if abs_delta <= MAX_DISTANCE_ADJUSTMENT_M:
        return dist_delta / 100 * DISTANCE_FACTOR
    else:
        linear_part = MAX_DISTANCE_ADJUSTMENT_M / 100 * DISTANCE_FACTOR
        excess = abs_delta - MAX_DISTANCE_ADJUSTMENT_M
        diminishing_part = excess / 100 * DIMINISHING_FACTOR
        return sign * (linear_part + diminishing_part)


def normalize_km_time(
    km_time: float,
    distance: int,
    start_method: StartMethod,
    breed_value: str = "varmblod",
    target_method: StartMethod = StartMethod.AUTO,
    target_distance: int = REFERENCE_DISTANCE,
) -> float:
    """Normalisera en km-tid till referensdistans och startmetod.

    Använder empiriskt kalibrerade konstanter:
    - Distans: 0.2s/100m (cap vid ±400m, diminishing utöver)
    - Startmetod: distansberoende offset (1.4s kort, 0.3s lång, 0s stayer)
    - Breed: kallblod ≈ 6s långsammare

    Returnerar normaliserad km-tid (lägre = snabbare).
    """
    normalized = km_time

    # Distansjustering
    dist_delta = target_distance - distance
    normalized += _capped_distance_adjustment(dist_delta)

    # Startmetodjustering (distansberoende)
    volt_diff = _get_volt_auto_diff(distance)
    if start_method == StartMethod.VOLT and target_method == StartMethod.AUTO:
        normalized -= volt_diff
    elif start_method == StartMethod.AUTO and target_method == StartMethod.VOLT:
        normalized += volt_diff

    return normalized


class TimeAnalysis(AnalysisFactor):
    """Analyserar km-tider med empiriskt kalibrerad normalisering.

    Arkitektur: "Rå tid först, normaliserad som fallback"
    - Primärt: Bästa rå km-tid vid samma distans+metod (starkaste signal)
    - Sekundärt: Normaliserad tid som fallback/komplement
    - Trend: Distansviktad för att undvika distansbyten-artefakter
    """

    name = "time_analysis"

    def __init__(self, recent_n: int = 10):
        self.recent_n = recent_n

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på km-tider.

        Beräkning:
        1. Bästa samma-distans-tid (±100m, samma metod) → 30%
        2. Bästa normaliserad tid (blend exact/other) → 20%
        3. Distansviktad trend → 20%
        4. Dold form: bra tid + dålig placering → 15%
        5. Spårbonus: bra tid från dåligt spår → 10%
        6. Recency-bonus: bästa tid bland senaste 3 → 5%

        Starter >800m från loppets distans exkluderas.
        """
        recent = entry.horse.recent_starts(self.recent_n)
        timed_starts = [s for s in recent if s.km_time and s.km_time > 0]

        if not timed_starts:
            return 30.0

        target_method = race.start_method
        breed = race.breed.value

        # Kategorisera starter
        exact_times: list[float] = []       # ±100m, samma metod: rå tider
        close_times: list[float] = []       # ±300m: rå tider
        norm_times: list[float] = []        # Alla inkluderade: normaliserade
        time_weights: list[float] = []      # Distansvikt per start
        filtered_starts: list = []
        close_norm_times: list[float] = []  # ≤600m normaliserade

        for s in timed_starts:
            dist_diff = abs(s.distance - race.distance) if s.distance > 0 else 0

            # Exkludera starter >800m
            if dist_diff > MAX_DISTANCE_INCLUDE:
                continue

            # Samma distans + samma metod → rå tid (starkaste signal)
            if dist_diff <= EXACT_DISTANCE_THRESHOLD and s.start_method == target_method:
                exact_times.append(s.km_time)

            # Ganska nära distans → rå tid
            if dist_diff <= CLOSE_DISTANCE_THRESHOLD and s.start_method == target_method:
                close_times.append(s.km_time)

            # Normaliserad tid (för alla inkluderade starter)
            nt = normalize_km_time(
                s.km_time,
                s.distance if s.distance > 0 else race.distance,
                s.start_method,
                breed,
                target_method,
                REFERENCE_DISTANCE,
            )
            norm_times.append(nt)
            filtered_starts.append(s)

            # Distansrelevans-vikt
            if dist_diff <= 200:
                w = 1.0
            else:
                w = max(0.3, 1.0 - (dist_diff - 200) / 1000)
            time_weights.append(w)

            if dist_diff <= MAX_DISTANCE_BEST:
                close_norm_times.append(nt)

        if not norm_times:
            return 30.0

        # Benchmark
        benchmark = self._get_benchmark(race)

        # ── 1. Bästa samma-distans-tid (30%) ────────────────────────────
        # Rå tid vid exakt distans+metod = starkaste prediktorn
        if exact_times:
            best_exact = min(exact_times)
            # Konvertera rå tid till score med distans-specifik benchmark
            raw_benchmark = self._get_raw_benchmark(race)
            same_dist_score = self._time_to_score(best_exact, raw_benchmark) * 0.30
        elif close_times:
            # Fallback: nära distans, samma metod
            best_close = min(close_times)
            raw_benchmark = self._get_raw_benchmark(race)
            same_dist_score = self._time_to_score(best_close, raw_benchmark) * 0.30 * 0.85
        else:
            # Ingen samma-distans-data: använd normaliserad
            if close_norm_times:
                best_norm = min(close_norm_times)
            else:
                best_norm = min(norm_times)
            same_dist_score = self._time_to_score(best_norm, benchmark) * 0.30 * 0.70

        # ── 2. Bästa normaliserad tid — blend (20%) ─────────────────────
        # exact*0.7 + other*0.3 ger +2.6pp Top-3
        if close_norm_times:
            best_close_norm = min(close_norm_times)
            other_norm = [t for t in norm_times if t not in close_norm_times]
            if other_norm:
                best_other = min(other_norm)
                blended_best = best_close_norm * 0.7 + best_other * 0.3
            else:
                blended_best = best_close_norm
        else:
            blended_best = min(norm_times)
        norm_best_score = self._time_to_score(blended_best, benchmark) * 0.20

        # ── 3. Distansviktad trend (20%) ────────────────────────────────
        trend_score = self._weighted_trend_score(
            filtered_starts, norm_times, time_weights, race.distance
        ) * 0.20

        # ── 4. Dold form (15%) ──────────────────────────────────────────
        hidden_form = self._hidden_form_score(
            filtered_starts, norm_times, benchmark
        ) * 0.15

        # ── 5. Spårbonus (10%) ──────────────────────────────────────────
        spar_bonus = self._bad_post_good_time_score(
            filtered_starts, norm_times, benchmark
        ) * 0.10

        # ── 6. Recency-bonus (5%) ──────────────────────────────────────
        # Bästa tid bland de 3 senaste starterna
        recency_score = self._recency_score(
            filtered_starts[:3], norm_times[:3], benchmark
        ) * 0.05

        total = same_dist_score + norm_best_score + trend_score + hidden_form + spar_bonus + recency_score
        return min(100.0, max(0.0, total))

    @staticmethod
    def _get_benchmark(race: Race) -> float:
        """Benchmark normaliserad km-tid (vid referensdistans)."""
        dist = race.distance
        closest_dist = min(DISTANCE_BENCHMARKS.keys(), key=lambda d: abs(d - dist))
        benchmark = DISTANCE_BENCHMARKS[closest_dist]

        # Justera för avvikelse från standarddistans
        dist_diff = (dist - closest_dist) / 100 * DISTANCE_FACTOR
        benchmark += dist_diff

        # Normalisera till referensdistans
        delta_to_ref = REFERENCE_DISTANCE - dist
        benchmark += _capped_distance_adjustment(delta_to_ref)

        # Volt
        if race.start_method == StartMethod.VOLT:
            benchmark += _get_volt_auto_diff(dist)

        # Kallblod
        if race.breed.value == "kallblod":
            benchmark += COLD_BLOOD_OFFSET

        return benchmark

    @staticmethod
    def _get_raw_benchmark(race: Race) -> float:
        """Benchmark rå km-tid (vid loppets faktiska distans/metod).

        Används för samma-distans-jämförelse utan normalisering.
        """
        dist = race.distance
        closest_dist = min(DISTANCE_BENCHMARKS.keys(), key=lambda d: abs(d - dist))
        benchmark = DISTANCE_BENCHMARKS[closest_dist]

        # Justera för avvikelse från standarddistans
        dist_diff = (dist - closest_dist) / 100 * DISTANCE_FACTOR
        benchmark += dist_diff

        # Volt
        if race.start_method == StartMethod.VOLT:
            benchmark += _get_volt_auto_diff(dist)

        # Kallblod
        if race.breed.value == "kallblod":
            benchmark += COLD_BLOOD_OFFSET

        return benchmark

    @staticmethod
    def _time_to_score(km_time: float, benchmark: float) -> float:
        """Konvertera km-tid till poäng 0-100.

        Snabbare tid = högre poäng.
        Varje sekund under benchmark ≈ +15 poäng.
        """
        diff = benchmark - km_time  # Positivt = snabbare
        score = 50.0 + diff * 15.0
        return min(100.0, max(0.0, score))

    @staticmethod
    def _weighted_trend_score(
        starts: list, norm_times: list[float],
        dist_weights: list[float], race_distance: int
    ) -> float:
        """Distansviktad trend — undviker distansbytes-artefakter.

        Starter nära loppets distans viktas högre i trendberäkningen.
        En 1640m-start ska inte förstöra trenden för ett 2140m-lopp.
        """
        if len(norm_times) < 4:
            return 50.0

        # Viktade medelvärden: senaste 3 vs resten
        recent_times = []
        recent_wts = []
        earlier_times = []
        earlier_wts = []

        for i, (nt, w) in enumerate(zip(norm_times, dist_weights)):
            if i < 3:
                recent_times.append(nt)
                recent_wts.append(w)
            else:
                earlier_times.append(nt)
                earlier_wts.append(w)

        if not earlier_times:
            return 50.0

        # Viktat medel
        r_total = sum(recent_wts)
        e_total = sum(earlier_wts)

        if r_total == 0 or e_total == 0:
            return 50.0

        avg_recent = sum(t * w for t, w in zip(recent_times, recent_wts)) / r_total
        avg_earlier = sum(t * w for t, w in zip(earlier_times, earlier_wts)) / e_total

        improvement = avg_earlier - avg_recent  # Positivt = snabbare nyligen
        score = 50.0 + improvement * 20.0
        return min(100.0, max(0.0, score))

    @staticmethod
    def _hidden_form_score(
        timed_starts: list, norm_times: list[float], benchmark: float
    ) -> float:
        """Dold form: bra normaliserad tid men dålig placering."""
        hidden_scores = []
        for s, nt in zip(timed_starts[:5], norm_times[:5]):
            if s.placement and s.placement > 4:
                time_quality = benchmark - nt
                if time_quality > 0:
                    hidden_scores.append(time_quality * 10)

        if not hidden_scores:
            return 40.0
        return min(100.0, 40.0 + sum(hidden_scores) / len(hidden_scores))

    @staticmethod
    def _bad_post_good_time_score(
        timed_starts: list, norm_times: list[float], benchmark: float
    ) -> float:
        """Bonus för bra tid från dåligt spår (7+)."""
        bonus_scores = []
        for s, nt in zip(timed_starts[:5], norm_times[:5]):
            if s.post_position >= 7:
                time_quality = benchmark - nt
                if time_quality > -1.0:
                    spar_penalty = (s.post_position - 6) * 3
                    bonus = time_quality * 8 + spar_penalty
                    bonus_scores.append(max(0, bonus))

        if not bonus_scores:
            return 50.0
        return min(100.0, 50.0 + sum(bonus_scores) / len(bonus_scores))

    @staticmethod
    def _recency_score(
        recent_starts: list, recent_norm_times: list[float], benchmark: float
    ) -> float:
        """Bonus för bra tid bland de 3 senaste starterna.

        Fångar aktuell form — en häst som nyligen sprang snabbt
        är troligare i form än en vars bästa tid var 8 starter sedan.
        """
        if not recent_norm_times:
            return 40.0

        best_recent = min(recent_norm_times)
        return min(100.0, max(0.0, 50.0 + (benchmark - best_recent) * 15.0))
