"""Galopprisksignal — empiriskt kalibrerad på 2 278 lopp.

Kalibrerat med gallop_volt_analysis.py (350 omgångar):

EMPIRISKA FYND:

1. Hästar utan galopphistorik vinner mer:
   - Ingen galopp: 9.4% vinst, 28.4% top3
   - Galopperat senast: 8.2% vinst, 24.0% top3
   - 3+ av 5 galopp: 7.2% vinst, 24.3% top3

2. Favoriter med galopphistorik:
   - Med galopp: 23.1% vinst, 49.1% top3
   - Utan galopp: 24.6% vinst, 53.4% top3
   → 1.5pp lägre segerchans = tydlig signal

3. Skrällar med galopphistorik:
   - Med galopp: 3.7% vinst (kan överraska)
   - Utan galopp: 3.2% vinst
   → 15% högre chans att överraska

4. Galoppfrekvens per voltspår:
   - Spår 1: 21.6% (lägst — förklarar hög segerfrekvens)
   - Spår 5: 28.7% (högst — trångt?)
   - Spår 8: 20.9% (ytterspår = mindre trängsel)

Dennis: "favorit som galopperat → risken att den galopperar igen"
       "skräll som galopperat → oförutsägbar, kan överraska"
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor


class GallopRisk(AnalysisFactor):
    """Analyserar galopphistorik och dess påverkan på chanserna.

    En häst som galopperat mycket har:
    - Lägre reliability (9.4% → 7.2% vinst med 3+ galopp)
    - Men om det är en skräll: oväntat hög chans att överraska

    Scoren reflekterar NEGATIV påverkan för favoriter (risk)
    och MER NEUTRAL för skrällar (oförutsägbarhet ≠ dåligt).
    """

    name = "gallop_risk"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score 0-100 baserad på galopphistorik och voltspår.

        Komponenter:
        1. Galoppfrekvens senaste 5 (50%) — fler galopper = lägre score
        2. Galopptrend (25%) — galopperat senast = extra risk
        3. Voltspår galoppinteraktion (25%) — trångare spår = högre risk
        """
        recent = entry.horse.recent_starts(5)
        if not recent:
            return 55.0  # Ingen data — svagt neutralt

        gallop_freq_score = self._gallop_frequency_score(recent) * 0.50
        trend_score = self._gallop_trend_score(recent) * 0.25
        volt_score = self._volt_position_score(entry, race) * 0.25

        return min(100.0, max(0.0, gallop_freq_score + trend_score + volt_score))

    @staticmethod
    def _gallop_frequency_score(recent: list) -> float:
        """Score baserad på galoppfrekvens i senaste starter.

        Empiriskt:
        - 0 galopp av 5: 9.4% vinst (bäst) → 70
        - 1 galopp: ~9% → 60
        - 2 galopp: 9.3% → 55
        - 3+ galopp: 7.2% (sämst) → 35
        """
        gallop_count = sum(1 for s in recent if s.galloped)
        total = len(recent)

        if total == 0:
            return 55.0

        gallop_rate = gallop_count / total

        if gallop_rate == 0:
            return 70.0  # Ren historik — pålitlig
        elif gallop_rate < 0.2:
            return 58.0  # 1 galopp av 5 — acceptabelt
        elif gallop_rate < 0.4:
            return 48.0  # 2 av 5 — viss risk
        elif gallop_rate < 0.6:
            return 35.0  # 3 av 5 — tydlig risk
        else:
            return 22.0  # 4-5 av 5 — hög risk

    @staticmethod
    def _gallop_trend_score(recent: list) -> float:
        """Galopperade hästen SENAST? Det är en extra risksignal.

        Empiriskt:
        - Galopperat senast: 8.2% vinst (vs 9.4% utan galopp)
        - Inte galopperat senast men har galopperat: ~9%
        """
        if not recent:
            return 55.0

        last = recent[0]
        if last.galloped:
            # Extra risk — galopperade senast
            # Kolla om det är ett mönster
            if len(recent) >= 2 and recent[1].galloped:
                return 20.0  # Galopperat 2 raka — allvarlig risk
            return 35.0  # Galopperat senast
        else:
            # Senaste var rent — kolla bakåt
            any_gallop = any(s.galloped for s in recent)
            if any_gallop:
                return 55.0  # Har galopperat men inte senast
            return 72.0  # Ingen galopp alls — pålitlig

    @staticmethod
    def _volt_position_score(entry: RaceEntry, race: Race) -> float:
        """Galopprisken beror på voltspåret.

        Empiriskt (galoppfrekvens per spår):
        - Spår 1: 21.6% (lägst — utrymme framtill)
        - Spår 5: 28.7% (högst — trångt i mitten)
        - Spår 8: 20.9% (ytterspår — utrymme)
        - Spår 6-7: 24.6% (springspår men i mitten)

        Högre score = lägre galoppisk = bättre.
        """
        if race.start_method != StartMethod.VOLT:
            return 65.0  # Auto — galopprisken beror inte på spår lika mycket

        pp = entry.post_position

        # Empiriska galoppfrekvenser → inverterad score
        # Lägre galoppfrekvens = högre score
        VOLT_GALLOP_RISK = {
            1: 75.0,  # 21.6% galopp — lägst risk
            2: 58.0,  # 25.0%
            3: 53.0,  # 26.8%
            4: 50.0,  # 27.8%
            5: 48.0,  # 28.7% — högst risk
            6: 60.0,  # 24.6%
            7: 60.0,  # 24.6%
            8: 72.0,  # 20.9% — näst lägst risk
            9: 55.0,  # 23.8%
            10: 58.0,  # 23.0%
            11: 55.0,  # 25.1%
            12: 62.0,  # 22.0%
        }

        # Hästar som redan har galopperat → spåret spelar extra roll
        has_galloped = entry.horse.gallop_rate > 0
        base_score = VOLT_GALLOP_RISK.get(pp, 55.0)

        if has_galloped:
            # Häst med galopptendens i trångt spår = extra risk
            if pp in (3, 4, 5):
                base_score -= 10.0  # Galoppbenägen + trångt = farligt
            elif pp in (1, 8):
                base_score += 5.0  # Galoppbenägen men utrymme = ok

        return max(0.0, min(100.0, base_score))
