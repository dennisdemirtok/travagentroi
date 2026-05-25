"""Dennis Brain — Post-analys som flaggar vinnarspel-picks baserat pa Dennis metod.

Dennis analysmetod i modellform:
1. Tidskant: Hastens rekordtid vs GLOBAL median per distans/startmetod
   - "1.14 pa 2140m auto — ar det bra eller daligt for den har klassen?"
   - Anvander globala medianer fran 76k hastar (backtest-validerade)
2. Klassdropp: Karriarpengar vs faltets median — kor hasten under sin klass?
   - "Pengarna sager vilken klass hasten tillhor"
3. Barfota: Positiv utrustningssignal — tranaren provar ngt nytt
   - "Fran skor till barfota brukar vara indikationer"
4. Modellrank 1-5 + streck 5-25% — rimlig outsider som modellen gillar

Backtestresultat (293 spel, 2021-2026, riktiga ATG-odds):
  Vinnarspel ROI: +45.2% (p < 0.0001)
  Platsspel ROI: +4.6%
  5/6 ar positiva

Koers efter CompositeAnalyzer.analyze_race() och satter:
  - entry.dennis_time_edge (sekunder snabbare an faltet)
  - entry.dennis_class_ratio (karriarpengar / faltets median)
  - entry.dennis_pick (True om alla S4-kriterier uppfylls)
"""

from __future__ import annotations

import logging
import statistics
from typing import Optional

from ..data.models import Race, RaceEntry

logger = logging.getLogger(__name__)

# S4 strategy thresholds (from backtest: +45.2% vinnarspel ROI, p<0.0001)
S4_MAX_RANK = 5          # model rank 1-5
S4_MIN_STRECK = 0.05     # 5%
S4_MAX_STRECK = 0.25     # 25%
S4_MIN_TIME_EDGE = 1.0   # 1 second faster than field median
S4_MIN_CLASS_RATIO = 1.5  # 1.5x field median career money

# ── Global time medians per (distance, start_method) ──────────────────────
# Extracted from 76,158 horses across 880 rounds (2021-2026).
# Only combos with n>=50 included. Values = median km_time in seconds.
# km_time = record_time / (distance_km), e.g. 73.5s / 2.14km = 34.35 s/km
GLOBAL_TIME_MEDIANS: dict[tuple[int, str], float] = {
    (1600, "auto"): 44.25,
    (1609, "auto"): 44.75,
    (1620, "auto"): 44.20,
    (1640, "auto"): 44.15,
    (1640, "volte"): 46.49,
    (1646, "auto"): 44.29,
    (1680, "auto"): 43.69,
    (1700, "auto"): 42.88,
    (2000, "auto"): 36.50,
    (2000, "volte"): 37.73,
    (2011, "auto"): 34.86,
    (2060, "auto"): 35.44,
    (2080, "auto"): 35.48,
    (2080, "volte"): 36.63,
    (2100, "auto"): 34.81,
    (2100, "volte"): 36.24,
    (2120, "auto"): 34.39,
    (2120, "volte"): 35.80,
    (2140, "auto"): 34.02,
    (2140, "volte"): 35.19,
    (2148, "auto"): 34.31,
    (2148, "volte"): 35.06,
    (2413, "auto"): 29.88,
    (2480, "auto"): 29.76,
    (2480, "volte"): 34.50,
    (2600, "auto"): 28.19,
    (2609, "auto"): 28.23,
    (2640, "auto"): 27.61,
    (2640, "volte"): 28.30,
    (2650, "auto"): 27.96,
    (2650, "volte"): 28.30,
    (2880, "auto"): 25.45,
    (2900, "volte"): 25.90,
    (2950, "volte"): 25.12,
    (3120, "volte"): 23.81,
    (3140, "auto"): 23.17,
    (3140, "volte"): 23.60,
    (3654, "volte"): 20.22,
}


def _lookup_global_time_median(distance: int, start_method: str) -> Optional[float]:
    """Look up global time median for a distance/method combo.

    Exact match first, then fuzzy match (nearest distance within 100m,
    same start method).
    """
    key = (distance, start_method)
    if key in GLOBAL_TIME_MEDIANS:
        return GLOBAL_TIME_MEDIANS[key]

    # Fuzzy: find nearest distance with same method within 100m
    best_dist = None
    best_diff = 101  # > max allowed
    for (d, m), _ in GLOBAL_TIME_MEDIANS.items():
        if m != start_method:
            continue
        diff = abs(d - distance)
        if diff < best_diff:
            best_diff = diff
            best_dist = d

    if best_dist is not None and best_diff <= 100:
        return GLOBAL_TIME_MEDIANS[(best_dist, start_method)]

    return None


def compute_dennis_signals(race: Race) -> None:
    """Berakna Dennis-brain signaler for alla hastar i ett lopp.

    Satter dennis_time_edge, dennis_class_ratio och dennis_pick
    pa varje active entry.

    Maste koras EFTER analyze_race() sa att rank redan ar satt.
    """
    active = race.active_entries
    if not active:
        return

    # ── 1. Tid-edge: hastens rekord vs GLOBAL median ─────────────────
    # Backtest-validerad metod: jamfor varje hasts km_time mot den
    # globala medianen for (distance, start_method) fran 76k hastar.
    # Detta ger mycket storre spreads an lopp-level median (dar alla
    # hastar i samma lopp ar snarlika).

    distance = race.distance
    if distance <= 0:
        distance = 2140  # fallback

    method_str = race.start_method.value  # "auto" or "volt"
    # ATG API uses "volt", backtest data uses "volte" — normalize
    if method_str == "volt":
        method_str = "volte"

    global_median = _lookup_global_time_median(distance, method_str)
    if global_median:
        logger.debug(
            f"Avd {race.race_number}: Global time median for "
            f"{distance}m {method_str} = {global_median:.2f} s/km"
        )

    for entry in active:
        km_time = None
        rt = entry.horse.record_time
        if rt and rt > 0 and distance > 0:
            km_time = rt / (distance / 1000.0)
            if not (15 < km_time < 90):
                km_time = None

        if km_time is None:
            # Fallback: basta km_time fran past_starts
            best_km = _best_km_time_from_starts(entry)
            if best_km:
                km_time = best_km / (distance / 1000.0)
                if not (15 < km_time < 90):
                    km_time = None

        if km_time is not None and global_median is not None:
            # Positive = faster (lower time = better)
            entry.dennis_time_edge = round(global_median - km_time, 2)
        else:
            entry.dennis_time_edge = 0.0

    # ── 2. Klass-ratio: karriarpengar vs faltets median ─────────────
    # Use api_career_money (preserved from ATG API, not wiped by temporal filtering)
    career_moneys: dict[int, float] = {}
    for entry in active:
        # api_career_money is the ATG API career earnings (preserved pre-filtering)
        # Falls back to career.total_prize_money for live use
        cm = entry.horse.api_career_money or entry.horse.career.total_prize_money
        if cm and cm > 0:
            career_moneys[entry.post_position] = float(cm)

    if len(career_moneys) >= 3:
        money_median = statistics.median(career_moneys.values())
        if money_median > 0:
            for entry in active:
                if entry.post_position in career_moneys:
                    entry.dennis_class_ratio = round(
                        career_moneys[entry.post_position] / money_median, 2
                    )
                else:
                    entry.dennis_class_ratio = 0.0
        else:
            for entry in active:
                entry.dennis_class_ratio = 0.0
    else:
        for entry in active:
            entry.dennis_class_ratio = 0.0

    # ── 3. Dennis pick: S4-kvalificering ────────────────────────────
    n_picks = 0
    for entry in active:
        is_barefoot = entry.shoe_front_off and entry.shoe_back_off
        streck = entry.bet_percentage or 0

        entry.dennis_pick = (
            entry.rank <= S4_MAX_RANK
            and S4_MIN_STRECK <= streck <= S4_MAX_STRECK
            and is_barefoot
            and entry.dennis_time_edge >= S4_MIN_TIME_EDGE
            and entry.dennis_class_ratio >= S4_MIN_CLASS_RATIO
        )

        if entry.dennis_pick:
            n_picks += 1
            logger.info(
                f"DENNIS PICK: {entry.horse.name} "
                f"(rank {entry.rank}, streck {streck:.1%}, "
                f"tid +{entry.dennis_time_edge:.1f}s, "
                f"klass {entry.dennis_class_ratio:.1f}x, barfota)"
            )

    if n_picks > 0:
        logger.info(
            f"Avd {race.race_number}: {n_picks} Dennis-pick(s) hittade"
        )


def _best_km_time_from_starts(entry: RaceEntry) -> Optional[float]:
    """Hamta basta km-tid fran hastens senaste starter (fallback om record saknas)."""
    best = None
    for ps in entry.horse.past_starts[:20]:  # senaste 20
        if ps.km_time and ps.km_time > 0:
            if best is None or ps.km_time < best:
                best = ps.km_time
    return best
