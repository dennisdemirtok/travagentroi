"""Spåranalys v9 — empiriskt kalibrerad på 15 255+ lopp.

Kalibrerat med gallop_volt_analysis.py (350 omgångar, 563 volt + 1715 auto).
Säsongseffekt kalibrerad på 15 255 lopp (alla V-spel 2015-2026).

SÄSONGSEFFEKT (Dennis hypotes — BEKRÄFTAD):
  Sommar (apr-sep): inner/outer ratio = 1.73x
  Vinter (okt-mar): inner/outer ratio = 1.56x
  → Framspår viktigare på sommaren (+11% relativ skillnad)

  Specifikt:
  Auto spår 4: 13.83% sommar vs 12.27% vinter (+1.55pp)
  Spår 11: 5.94% sommar vs 7.04% vinter (-1.09pp)
  Spår 12: 4.02% sommar vs 5.48% vinter (-1.46pp)

EMPIRISKA FYND:

VOLT (563 lopp):
  Spår 1: 14.2% vinst ★ — springspår, dominant
  Spår 2: 10.0%, Spår 3: 9.1%, Spår 4: 8.3%
  Spår 6: 8.5%, Spår 7: 7.7% — måttlig springspårsfördel
  Spår 9: 6.2%, Spår 13-15: 5.5%
  Springspår (1,6,7): 10.1% vs innerspår (2-5): 8.4% vs ytter (8+): 7.3%

AUTO (1715 lopp):
  Spår 5: 14.0% vinst ★ — BÄST (inte spår 1!)
  Spår 4: 12.0%, Spår 2: 11.6%, Spår 3: 11.2%
  Spår 1: 7.7% — överraskande dåligt i auto!
  Spår 8: 7.3%, Spår 9: 5.9%

TILLÄGG (2 991 starter i andra volten):
  +60m tillägg: 15.8% vinst ★ — starkaste hästarna
  +40m tillägg: 8.1% vinst
  +20m tillägg: 7.2% vinst (vanligast, svag nackdel)
  Position i andra volten:
    Pos 1: 10.0% ★ (springspår)
    Pos 8: 3.1% (sämst)
    Tillägg totalt: 7.6% vs ej tillägg 9.0%

Dennis: "voltlopp gynnas spår 1, 6, 7 — springspår"
        "häst nr 13 kan ha springspår i andra volten"
→ Spår 1 bekräftat starkt, 6 och 7 har viss fördel.
→ Pos 1 i andra volten = 10% vinst (springspår-effekt gäller även volt 2).
"""

from __future__ import annotations

from datetime import date as Date

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor

# Empiriskt kalibrerat på 1715 autolopp
# Normaliserat mot snitt ~9.6% segerfrekvens
AUTO_START_ADVANTAGE = {
    1: 0.80, 2: 1.21, 3: 1.17, 4: 1.25, 5: 1.46, 6: 1.13,
    7: 1.11, 8: 0.76, 9: 0.61, 10: 0.76, 11: 0.73, 12: 0.73,
}

# Empiriskt kalibrerat på 563 voltlopp
# Normaliserat mot snitt ~8.3% segerfrekvens
VOLT_START_ADVANTAGE = {
    1: 1.71, 2: 1.20, 3: 1.10, 4: 1.00, 5: 0.76, 6: 1.02,
    7: 0.93, 8: 0.96, 9: 0.75, 10: 0.94, 11: 1.02, 12: 1.06,
}

# Spårtrappa: spår 5-8 = sweet spot (data visar högst segerfrekvens)
STAIR_DRAW_ADVANTAGE = {
    1: 0.85, 2: 0.90, 3: 0.95, 4: 1.00, 5: 1.10, 6: 1.12,
    7: 1.12, 8: 1.10, 9: 1.00, 10: 0.95, 11: 0.88, 12: 0.82,
}

# Empiriskt: position i andra volten (tillägg) — 2 991 starter
# Normaliserat mot snitt ~7.6% segerfrekvens för tilläggshästar
SECOND_VOLT_ADVANTAGE = {
    1: 1.32,   # 10.0% — springspår i andra volten
    2: 1.04,   # 7.9%
    3: 1.13,   # 8.6%
    4: 1.09,   # 8.3%
    5: 0.86,   # 6.5%
    6: 0.79,   # 6.0%
    7: 1.00,   # 7.6%
    8: 0.41,   # 3.1% — sämsta positionen
    9: 1.12,   # 8.5%
    10: 0.97,  # 7.4%
    11: 1.51,  # 11.5% (liten sample men tydlig)
    12: 0.41,  # 3.1%
}

# Tillägg distans → kvalitetssignal
# +60m = 15.8% vinst, +40m = 8.1%, +20m = 7.2%
TILLAGG_DISTANCE_FACTOR = {
    20: 0.95,   # Svag nackdel
    40: 1.07,   # Neutral/lite plus
    60: 2.08,   # Stark häst → 15.8% vinst!
    80: 1.88,   # Mycket stark (liten sample)
}


class PostPosition(AnalysisFactor):
    """Analyserar spårpositionens påverkan inklusive spårtrappa och tillägg."""

    name = "post_position"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på spårposition.

        1. Spårets fördel/nackdel (normal eller spårtrappa) → 40%
        2. Hästens historik från liknande spår → 30%
        3. Överprestationsbonus: bra resultat från dåligt spår → 30%
        + Tillägg-justering: position i andra volten (additiv, ±5 poäng)
        + Säsongsjustering: framspår viktigare apr-sep (additiv, ±3 poäng)
        """
        general_score = self._general_position_score(entry, race) * 0.40
        history_score = self._position_history_score(entry, race) * 0.30
        overperf_score = self._overperformance_bonus(entry) * 0.30

        base = general_score + history_score + overperf_score

        # Tillägg: additiv justering baserad på position i andra volten
        # Påverkar BARA tilläggshästar, inga vikter ändras för andra
        is_tillagg = (
            entry.distance > 0
            and race.distance > 0
            and entry.distance > race.distance
        )
        if is_tillagg and race.start_method == StartMethod.VOLT:
            tillagg_adj = self._tillagg_adjustment(entry, race)
            base += tillagg_adj

        # Säsongsjustering: Dennis hypotes bekräftad på 15 255 lopp
        # Sommar → framspår ännu bättre, bakspår ännu sämre
        seasonal_adj = self._seasonal_adjustment(entry, race)
        base += seasonal_adj

        return min(100.0, max(0.0, base))

    @staticmethod
    def _general_position_score(entry: RaceEntry, race: Race) -> float:
        """Spåradvantage — med spårtrappa om relevant."""
        pos = entry.post_position

        if race.is_stair_draw:
            # Spårtrappa: sweet spot spår 5-8
            advantage = STAIR_DRAW_ADVANTAGE.get(pos, 0.85)

            # Justera baserat på pengaskillnad i fältet
            # Hästar med hög kapacitet (mycket pengar) kompenserar dåligt spår
            field_prizes = [
                e.horse.career.total_prize_money
                for e in race.active_entries
                if e.horse.career.total_prize_money > 0
            ]
            if field_prizes:
                avg_prize = sum(field_prizes) / len(field_prizes)
                horse_prize = entry.horse.career.total_prize_money
                if avg_prize > 0 and horse_prize > 0:
                    # prize_ratio > 1 = starkare än snittet
                    prize_ratio = horse_prize / avg_prize
                    # Ge kapacitetshästar (ratio > 1.3) en liten bonus
                    if prize_ratio > 1.3:
                        advantage += min(0.1, (prize_ratio - 1.3) * 0.15)
                    # Svaga hästar (ratio < 0.7) med bra spår = inte lika stor fördel
                    elif prize_ratio < 0.7 and pos <= 4:
                        advantage -= 0.05  # Reducera spårbonus

        elif race.start_method == StartMethod.AUTO:
            advantage = AUTO_START_ADVANTAGE.get(pos, 0.80)
        else:
            advantage = VOLT_START_ADVANTAGE.get(pos, 0.80)

        score = 50 + (advantage - 1.0) * 200
        return min(100.0, max(0.0, score))

    @staticmethod
    def _position_history_score(entry: RaceEntry, race: Race) -> float:
        """Hur presterar hästen från liknande spår?"""
        recent = entry.horse.recent_starts(15)
        if not recent:
            return 50.0

        pos = entry.post_position
        margin = 2
        nearby = [
            s for s in recent
            if abs(s.post_position - pos) <= margin and s.post_position > 0
        ]
        if not nearby:
            return 50.0

        wins = sum(1 for s in nearby if s.won)
        top3 = sum(1 for s in nearby if s.top3)
        total = len(nearby)
        score = (wins / total * 60 + top3 / total * 30) * 100 / 90 + 10
        return min(100.0, score)

    @staticmethod
    def _overperformance_bonus(entry: RaceEntry) -> float:
        """Bonus för bra resultat från dåligt spår (7+)."""
        recent = entry.horse.recent_starts(15)
        if not recent:
            return 50.0

        bad_post_starts = [s for s in recent if s.post_position >= 7]
        if not bad_post_starts:
            return 50.0

        scores = []
        for s in bad_post_starts:
            score = 30.0
            expected_penalty = (s.post_position - 6) * 5

            if s.won:
                score = 90.0 + expected_penalty
            elif s.top3:
                score = 70.0 + expected_penalty * 0.7
            elif s.placement and s.placement <= 5:
                score = 55.0 + expected_penalty * 0.4
            elif s.km_time and s.km_time > 0:
                score = 45.0 + expected_penalty * 0.2

            if s.start_method == StartMethod.VOLT and s.post_position >= 7:
                score += 5.0

            scores.append(min(100.0, score))

        return sum(scores) / len(scores)

    @staticmethod
    def _seasonal_adjustment(entry: RaceEntry, race: Race) -> float:
        """Säsongsjustering för startspår.

        Bekräftad på 15 255 lopp (alla V-spel 2015-2026):
        Sommar (apr-sep): inner/outer ratio = 1.73x
        Vinter (okt-mar): inner/outer ratio = 1.56x

        Autostart specifikt:
        Spår 4 sommar: 13.83% vs 12.27% vinter (+1.55pp)
        Spår 12 sommar: 4.02% vs 5.48% vinter (-1.46pp)

        Returnerar ±3 poäng — liten men signifikant justering.
        """
        # Determine season from race date or current date
        try:
            race_date = race.date if hasattr(race, 'date') and race.date else None
            if race_date and hasattr(race_date, 'month'):
                month = race_date.month
            else:
                month = Date.today().month
        except Exception:
            month = Date.today().month

        is_summer = month in [4, 5, 6, 7, 8, 9]

        if not is_summer:
            return 0.0  # No adjustment in winter (winter is baseline)

        pos = entry.post_position

        # Summer adjustments (empirical, relative to winter baseline)
        # Inner posts: slightly better in summer
        if pos <= 4:
            return 2.0   # +2 points — framspår viktigare sommar
        elif pos <= 6:
            return 0.5   # Minimal effect
        elif pos <= 8:
            return -1.0  # Slight penalty
        elif pos <= 10:
            return -1.5  # Moderate penalty
        else:
            return -3.0  # Strong penalty — bakspår svårt sommar

    @staticmethod
    def _tillagg_adjustment(entry: RaceEntry, race: Race) -> float:
        """Additiv justering för tilläggshästar baserad på position i andra volten.

        Empiriskt (2 991 starter):
        - Pos 1 i andra volten: 10.0% vinst (springspår → bonus)
        - Pos 8: 3.1% (sämsta → straff)
        - Snitt tillägg: 7.6%

        Returnerar ±5 poäng (liten justering, undviker att störa modellen).
        Distans-kvalitet (+60m etc) fångas redan av prize_index-faktorn.
        """
        n_first_volt = len([
            e for e in race.active_entries
            if e.distance == race.distance
        ])
        second_volt_pos = entry.post_position - n_first_volt
        if second_volt_pos <= 0:
            second_volt_pos = entry.post_position

        # Normaliserat advantage (1.0 = snitt)
        advantage = SECOND_VOLT_ADVANTAGE.get(second_volt_pos, 0.90)

        # Skalera till ±5 poäng: pos 1 (1.32) → +2.4, pos 8 (0.41) → -4.4
        return (advantage - 1.0) * 7.5
