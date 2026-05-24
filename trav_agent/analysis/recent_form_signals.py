"""Dennis's tre formsignaler — seger senast, motstandsstyrka, uppehall.

Dennis: "Seger i sitt senaste lopp brukar kunna vara positivt.
Samma sak att ha hoga prissummor i de senaste loppen som man har
startat brukar kunna ge storre chans att hitta skrallar om de
inte ar betrodda sa ar de vana att mota bra motstand.
Samma sak om de inte startat pa lange att de liksom inte har
formen direkt om de har haft langt uppehall."

Tre separata faktorer for maximal vikt-flexibilitet:
1. LastWinFactor — seger senast + recent top placements
2. CompetitionStrength — prissumma i senaste loppen vs aktuellt falt
3. LayoffFactor — dagar sedan senaste start (negativt for langt uppehall)
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from ..data.models import Race, RaceEntry, PastStart
from .base import AnalysisFactor


class LastWinFactor(AnalysisFactor):
    """Seger senast + nara placeringar.

    Dennis: "Seger i sitt senaste lopp brukar kunna vara positivt."

    Signaler:
    - Vann senaste loppet = stark signal
    - Top 3 senast = positiv signal
    - Sekvens av bra resultat = momentum
    - Diskad/galopp senast = negativ signal
    """

    name = "last_win"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score 0-100 based on recent race results.

        Components:
        1. Last start result (45%) — won/top3/poor/dnf
        2. Last 3 starts momentum (30%) — pattern of results
        3. Last start quality (25%) — winning against tough field
        """
        recent = entry.horse.recent_starts(5)

        if not recent:
            return 35.0  # No data — below average

        # ── 1. Last start result (45%) ──────────────────────────────────
        last = recent[0]
        last_score = self._last_start_score(last)

        # ── 2. Momentum from last 3 (30%) ──────────────────────────────
        momentum = self._momentum_score(recent[:3])

        # ── 3. Quality of last win/placement (25%) ──────────────────────
        quality = self._win_quality_score(recent[:3], race)

        total = last_score * 0.45 + momentum * 0.30 + quality * 0.25
        return min(100.0, max(0.0, total))

    @staticmethod
    def _last_start_score(start: PastStart) -> float:
        """Score for last start result."""
        if start.disqualified or start.galloped:
            return 20.0  # DNF is very negative
        if start.placement is None:
            return 25.0

        if start.won:
            return 90.0
        elif start.placement == 2:
            return 72.0
        elif start.placement == 3:
            return 62.0
        elif start.placement == 4:
            return 50.0
        elif start.placement == 5:
            return 42.0
        elif start.placement <= 7:
            return 32.0
        else:
            return 22.0

    @staticmethod
    def _momentum_score(last_3: list[PastStart]) -> float:
        """Score based on pattern of last 3 results.

        Improving trend is valued, consistent top results are valued,
        declining trend is penalized.
        """
        if not last_3:
            return 40.0

        # Convert placements to quality scores
        qualities = []
        for s in last_3:
            if s.disqualified or s.galloped:
                qualities.append(15.0)
            elif s.placement is None:
                qualities.append(20.0)
            elif s.won:
                qualities.append(90.0)
            elif s.placement <= 3:
                qualities.append(65.0)
            elif s.placement <= 5:
                qualities.append(45.0)
            else:
                qualities.append(25.0)

        # Weighted average (most recent = most important)
        weights = [1.0, 0.7, 0.5]
        total_w = 0
        weighted_sum = 0
        for i, q in enumerate(qualities):
            w = weights[i] if i < len(weights) else 0.4
            weighted_sum += q * w
            total_w += w

        avg = weighted_sum / total_w if total_w > 0 else 40.0

        # Trend bonus: improving placements
        if len(qualities) >= 2:
            trend = qualities[0] - qualities[-1]  # Positive = improving
            avg += trend * 0.15

        return min(100.0, max(0.0, avg))

    @staticmethod
    def _win_quality_score(recent: list[PastStart], race: Race) -> float:
        """Quality of recent wins — winning in tough fields is more valuable."""
        if not recent:
            return 40.0

        score = 40.0
        for i, s in enumerate(recent):
            if not s.won and not s.top3:
                continue

            # Recency weight
            rw = [1.0, 0.7, 0.5][i] if i < 3 else 0.3

            # Won against tough field (high purse)?
            if s.race_purse > 0 and race.purse > 0:
                purse_ratio = s.race_purse / race.purse
                if purse_ratio >= 1.2:
                    # Won in tougher class than current race
                    bonus = 18 if s.won else 10
                    score += bonus * rw
                elif purse_ratio >= 0.8:
                    bonus = 12 if s.won else 6
                    score += bonus * rw
                else:
                    bonus = 6 if s.won else 3
                    score += bonus * rw
            else:
                # No purse data — basic bonus for win
                bonus = 10 if s.won else 5
                score += bonus * rw

            # Large field win is more impressive
            if s.won and s.num_starters >= 12:
                score += 5 * rw

        return min(100.0, max(0.0, score))


class CompetitionStrength(AnalysisFactor):
    """Motstandsstyrka — prissummor i senaste loppen vs aktuellt falt.

    Dennis: "att ha hoga prissummor i de senaste loppen som man har
    startat brukar kunna ge storre chans att hitta skrallar om de
    inte ar betrodda sa ar de vana att mota bra motstand"

    This identifies horses that have been competing at a higher level
    than the current race — class droppers who are undervalued.
    """

    name = "competition_strength"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score 0-100 based on competition level of recent starts.

        Components:
        1. Absolute prize level (35%) — recent avg purse (higher = tougher competition)
        2. Relative prize ratio (25%) — recent purse vs current race purse
        3. Recent earnings efficiency (25%) — prize money won / starts
        4. Class drop detection (15%) — dropping from higher class = upset value

        Note: when race.purse is 0 (common in ATG data), we fall back
        to absolute prize level comparison across the field.
        """
        recent = entry.horse.recent_starts(5)

        if not recent:
            return 35.0

        # ── 1. Absolute prize level (35%) ───────────────────────────────
        abs_score = self._absolute_prize_score(recent)

        # ── 2. Relative prize ratio (25%) ───────────────────────────────
        if race.purse > 0:
            ratio_score = self._avg_purse_ratio_score(recent, race)
        else:
            # Fallback: use field average prize for comparison
            ratio_score = self._field_relative_score(recent, race)

        # ── 3. Recent earnings efficiency (25%) ─────────────────────────
        efficiency = self._earnings_efficiency(recent)

        # ── 4. Class drop detection (15%) ───────────────────────────────
        class_drop = self._class_drop_score(recent, race, entry)

        total = (
            abs_score * 0.35
            + ratio_score * 0.25
            + efficiency * 0.25
            + class_drop * 0.15
        )
        return min(100.0, max(0.0, total))

    @staticmethod
    def _absolute_prize_score(recent: list[PastStart]) -> float:
        """Score based on absolute level of recent competition.

        Higher purses = tougher competition = better horse.
        Uses log scale since purse differences are exponential.
        """
        purses = [s.race_purse for s in recent if s.race_purse > 0]
        if not purses:
            return 35.0

        avg_purse = sum(purses) / len(purses)

        # Log-scale scoring: 25k=40, 50k=55, 100k=65, 200k=75, 500k=85
        if avg_purse <= 0:
            return 30.0
        score = 20.0 + 12.0 * math.log10(avg_purse / 10000 + 1)
        return min(95.0, max(15.0, score))

    @staticmethod
    def _field_relative_score(recent: list[PastStart], race: Race) -> float:
        """When race.purse is 0, compare horse's recent purses to field avg.

        A horse that has been racing in higher-purse races than its
        competitors has an advantage.
        """
        my_purses = [s.race_purse for s in recent if s.race_purse > 0]
        if not my_purses:
            return 45.0

        my_avg = sum(my_purses) / len(my_purses)

        # Get field average
        field_avgs = []
        for entry in race.active_entries:
            ep = [s.race_purse for s in entry.horse.recent_starts(5) if s.race_purse > 0]
            if ep:
                field_avgs.append(sum(ep) / len(ep))

        if not field_avgs:
            return 45.0

        field_avg = sum(field_avgs) / len(field_avgs)
        if field_avg <= 0:
            return 45.0

        ratio = my_avg / field_avg
        if ratio >= 1.5:
            return 82.0
        elif ratio >= 1.2:
            return 68.0
        elif ratio >= 1.0:
            return 55.0
        elif ratio >= 0.8:
            return 42.0
        else:
            return 30.0

    @staticmethod
    def _earnings_efficiency(recent: list[PastStart]) -> float:
        """Prize money earned per start — efficiency indicator.

        A horse that earns a lot per start is productive.
        """
        if not recent:
            return 35.0

        total_earned = sum(s.prize_money for s in recent)
        per_start = total_earned / len(recent)

        # Log scale: 5k/start=45, 15k=55, 30k=65, 60k=75, 100k=85
        if per_start <= 0:
            return 25.0
        score = 25.0 + 12.0 * math.log10(per_start / 3000 + 1)
        return min(90.0, max(15.0, score))

    @staticmethod
    def _avg_purse_ratio_score(recent: list[PastStart], race: Race) -> float:
        """Average prize pool of recent starts vs current race."""
        purses = [s.race_purse for s in recent if s.race_purse > 0]
        if not purses or race.purse <= 0:
            return 45.0

        avg_purse = sum(purses) / len(purses)
        ratio = avg_purse / race.purse

        # ratio > 1.0 = has been competing at higher level
        if ratio >= 2.0:
            return 90.0
        elif ratio >= 1.5:
            return 78.0
        elif ratio >= 1.2:
            return 65.0
        elif ratio >= 1.0:
            return 55.0
        elif ratio >= 0.7:
            return 42.0
        elif ratio >= 0.5:
            return 32.0
        else:
            return 22.0

    @staticmethod
    def _best_purse_score(recent: list[PastStart], race: Race) -> float:
        """Highest class race recently — shows what level horse can handle."""
        purses = [s.race_purse for s in recent if s.race_purse > 0]
        if not purses or race.purse <= 0:
            return 45.0

        best_purse = max(purses)
        ratio = best_purse / race.purse

        if ratio >= 2.5:
            return 92.0
        elif ratio >= 2.0:
            return 80.0
        elif ratio >= 1.5:
            return 68.0
        elif ratio >= 1.0:
            return 55.0
        else:
            return 35.0

    @staticmethod
    def _prize_consistency_score(recent: list[PastStart], race: Race) -> float:
        """How consistently has the horse been racing at high level?

        A horse with 4/5 starts at high purse is more reliable than
        one with 1 high-purse outlier.
        """
        if race.purse <= 0:
            return 45.0

        high_class_count = sum(
            1 for s in recent
            if s.race_purse > 0 and s.race_purse >= race.purse * 0.8
        )

        ratio = high_class_count / max(len(recent), 1)
        return 30.0 + ratio * 60.0  # 30 (none) to 90 (all high-class)

    @staticmethod
    def _class_drop_score(
        recent: list[PastStart], race: Race, entry: RaceEntry
    ) -> float:
        """Detect class droppers — horses dropping to easier competition.

        Dennis: "vana att mota bra motstand" + "ge storre chans att
        hitta skrallar om de inte ar betrodda"

        A class dropper with low betting percentage = high upset value.
        """
        purses = [s.race_purse for s in recent if s.race_purse > 0]
        if not purses or race.purse <= 0:
            return 45.0

        avg_purse = sum(purses) / len(purses)
        drop_ratio = avg_purse / race.purse

        if drop_ratio < 1.2:
            return 40.0  # Not dropping class

        # Base score for class drop
        drop_score = min(85.0, 45.0 + (drop_ratio - 1.2) * 50.0)

        # Extra bonus: dropping class AND not heavily bet
        # (this is where the upset value is)
        bet_pct = entry.bet_percentage or 0
        if bet_pct > 0 and bet_pct < 0.10:
            # Under 10% streck but dropping class = great value
            drop_score += 15.0
        elif bet_pct > 0 and bet_pct < 0.15:
            drop_score += 8.0

        return min(100.0, max(0.0, drop_score))


class LayoffFactor(AnalysisFactor):
    """Uppehall — dagar sedan senaste start.

    Dennis: "om de inte startat pa lange att de liksom inte har
    formen direkt om de har haft langt uppehall"

    Pattern:
    - 7-21 days: normal interval, no adjustment
    - 22-35 days: slightly negative (rested but ok)
    - 36-60 days: moderate layoff, fitness concern
    - 60-90 days: significant layoff, likely not sharp
    - 90+ days: long layoff, questionable form
    - Fresh from win + short layoff = confident trainer
    """

    name = "layoff"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Score 0-100 based on days since last start.

        Components:
        1. Days since last start (50%) — core layoff signal
        2. Racing rhythm (30%) — regularity of starts
        3. Return quality (20%) — how horse performs after breaks
        """
        recent = entry.horse.recent_starts(5)

        if not recent:
            return 25.0  # No starts = max concern

        # ── 1. Days since last start (50%) ──────────────────────────────
        days_off = self._days_since_last(recent[0], race)
        layoff_score = self._layoff_to_score(days_off)

        # ── 2. Racing rhythm (30%) ──────────────────────────────────────
        rhythm = self._rhythm_score(recent)

        # ── 3. Return quality (20%) ─────────────────────────────────────
        return_quality = self._return_quality(recent, days_off)

        total = layoff_score * 0.50 + rhythm * 0.30 + return_quality * 0.20
        return min(100.0, max(0.0, total))

    @staticmethod
    def _days_since_last(last_start: PastStart, race: Race) -> int:
        """Calculate days between last start and race date."""
        try:
            delta = race.race_date - last_start.start_date
            return max(0, delta.days)
        except Exception:
            return 60  # Default to moderate layoff

    @staticmethod
    def _layoff_to_score(days: int) -> float:
        """Convert days off to a score.

        Trotting horses typically race every 2-4 weeks.
        Very short turnaround (< 7 days) is fine — trainers choose when to start.
        Long breaks (> 60 days) are concerning.

        Dennis: "om de inte startat pa lange att de liksom inte har
        formen direkt om de har haft langt uppehall"
        """
        if days <= 7:
            return 65.0  # Quick turnaround — trainer is confident
        elif days <= 14:
            return 70.0  # Optimal — well rested, still sharp
        elif days <= 21:
            return 68.0  # Good interval
        elif days <= 28:
            return 62.0  # Normal 4-week cycle
        elif days <= 42:
            return 52.0  # 5-6 weeks — starting to get cold
        elif days <= 60:
            return 38.0  # Notable layoff — fitness concern
        elif days <= 90:
            return 25.0  # Long break — likely not sharp
        elif days <= 150:
            return 15.0  # Very long — form is questionable
        else:
            return 8.0   # Extreme layoff — major concern

    @staticmethod
    def _rhythm_score(recent: list[PastStart]) -> float:
        """How regularly has the horse been racing?

        Regular racing (every 2-3 weeks) = in rhythm = good
        Irregular (big gaps) = concern
        """
        if len(recent) < 2:
            return 40.0

        # Calculate intervals between starts
        intervals = []
        sorted_starts = sorted(recent, key=lambda s: s.start_date, reverse=True)
        for i in range(len(sorted_starts) - 1):
            delta = (sorted_starts[i].start_date - sorted_starts[i + 1].start_date).days
            if delta > 0:
                intervals.append(delta)

        if not intervals:
            return 40.0

        avg_interval = sum(intervals) / len(intervals)

        # Optimal: 14-28 day intervals
        if 14 <= avg_interval <= 28:
            score = 75.0
        elif 10 <= avg_interval <= 35:
            score = 60.0
        elif avg_interval < 10:
            score = 50.0  # Racing too frequently
        elif avg_interval <= 50:
            score = 42.0
        else:
            score = 28.0  # Very irregular

        # Consistency bonus: low variance in intervals = predictable
        if len(intervals) >= 2:
            mean = sum(intervals) / len(intervals)
            variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
            std = math.sqrt(variance)
            if std < 7:
                score += 10.0  # Very consistent intervals
            elif std < 14:
                score += 5.0

        return min(100.0, max(0.0, score))

    @staticmethod
    def _return_quality(recent: list[PastStart], current_days_off: int) -> float:
        """How does the horse typically perform after breaks?

        Look for patterns: if a horse performs well after long breaks,
        the layoff is less concerning.
        """
        if len(recent) < 3:
            return 50.0  # Not enough data

        # Check last start after any longer break in the 5-start history
        sorted_starts = sorted(recent, key=lambda s: s.start_date, reverse=True)

        comeback_results = []
        for i in range(len(sorted_starts) - 1):
            gap = (sorted_starts[i].start_date - sorted_starts[i + 1].start_date).days
            if gap >= 30:  # Was a notable break
                s = sorted_starts[i]
                if s.won:
                    comeback_results.append(85)
                elif s.top3:
                    comeback_results.append(65)
                elif s.placement and s.placement <= 5:
                    comeback_results.append(45)
                elif s.disqualified or s.galloped:
                    comeback_results.append(15)
                else:
                    comeback_results.append(30)

        if not comeback_results:
            # No previous long breaks in data — if current layoff is long,
            # that's extra uncertain
            if current_days_off > 40:
                return 35.0  # Unknown — penalize slightly
            return 55.0  # Normal interval, no concern

        return sum(comeback_results) / len(comeback_results)
