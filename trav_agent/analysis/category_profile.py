"""Kategoriprofil — hur hästen matchar aktuellt lopp.

Dennis: "Kort distans 1640m + auto kräver snabbhet.
Jag kollar specifikt på 1640-tider. Om ingen tid finns,
dra av ~1.5-2s per distanshopp."

Nya signaler:
- Exakt distansmatch (1640-tider vid 1640-lopp)
- Distansomräkning om data saknas
- "Form slår klass" — senaste start vinst = bonus
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry, StartMethod
from .base import AnalysisFactor

# Sekunder per distanshopp (1640→2140, 2140→2640 etc)
SECS_PER_DISTANCE_STEP = 1.7


class CategoryProfile(AnalysisFactor):
    """Analyserar matchning mellan häst och loppkategori."""

    name = "category_profile"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poäng baserad på kategorimatchning.

        1. Startmetod-matchning → 25%
        2. Distansmatchning (exakt + omräknad) → 30%
        3. Åldersoptimum → 10%
        4. Form-vinst-bonus → 20%
        5. Startsnabbhet vid auto → 15%
        """
        method = self._start_method_score(entry, race) * 0.25
        distance = self._distance_match_score(entry, race) * 0.30
        age = self._age_score(entry, race) * 0.10
        form_win = self._recent_win_bonus(entry) * 0.20
        speed = self._auto_start_speed_score(entry, race) * 0.15

        return min(100.0, max(0.0, method + distance + age + form_win + speed))

    @staticmethod
    def _start_method_score(entry: RaceEntry, race: Race) -> float:
        """Hur bra presterar hästen med aktuell startmetod?"""
        method_starts = entry.horse.starts_with_method(race.start_method)
        if not method_starts:
            return 40.0

        wins = sum(1 for s in method_starts if s.won)
        top3 = sum(1 for s in method_starts if s.top3)
        total = len(method_starts)

        overall_win = entry.horse.career.win_rate if entry.horse.career.total_starts > 0 else 0
        method_win = wins / total

        base = (method_win * 60 + (top3 / total) * 30) * 100 / 90
        if method_win > overall_win:
            base = min(100, base * 1.15)

        return base

    @staticmethod
    def _distance_match_score(entry: RaceEntry, race: Race) -> float:
        """Distansmatch — exakt distans + omräkning.

        Dennis: "Jag kollar specifikt på tider vid loppets distans.
        Om ingen tid finns, kolla 2140 och dra av ~1.5-2s."
        """
        dist = race.distance
        margin = 100  # snävt ±100m för exakt match

        # Exakt distansmatch
        exact_starts = entry.horse.starts_at_distance_range(dist - margin, dist + margin)
        if exact_starts:
            wins = sum(1 for s in exact_starts if s.won)
            top3 = sum(1 for s in exact_starts if s.top3)
            total = len(exact_starts)

            # Bättre resultat vid exakt distans = starkt
            base = (wins / total * 60 + top3 / total * 30) * 100 / 90

            # Bonus: har hästen bra tider vid denna distans?
            timed = [s for s in exact_starts if s.km_time and s.km_time > 0]
            if timed:
                best_time = min(s.km_time for s in timed)
                # Vid 1640m auto: bra tid = under 72s (12,0a)
                # Vid 2140m auto: bra tid = under 74s (14,0a)
                expected_good = 70 + (dist / 1000) * 1.5
                if best_time < expected_good:
                    base += 15  # Bonus för snabb tid vid distansen

            return min(100.0, max(10.0, base))

        # Ingen exakt match: försök med bredare intervall
        broader = entry.horse.starts_at_distance_range(dist - 500, dist + 500)
        if broader:
            # Hästen har tider vid annan distans — ge partiell kredit
            wins = sum(1 for s in broader if s.won)
            top3 = sum(1 for s in broader if s.top3)
            total = len(broader)
            base = (wins / total * 50 + top3 / total * 25) * 100 / 90

            # Uppskatta tid vid rätt distans via omräkning
            timed = [s for s in broader if s.km_time and s.km_time > 0]
            if timed:
                # Bästa omräknade tid
                best_adjusted = float('inf')
                for s in timed:
                    dist_diff = abs(dist - s.distance)
                    steps = dist_diff / 500  # Antal 500m-hopp
                    adjusted = s.km_time - steps * SECS_PER_DISTANCE_STEP if dist < s.distance else s.km_time + steps * SECS_PER_DISTANCE_STEP
                    best_adjusted = min(best_adjusted, adjusted)

                expected_good = 70 + (dist / 1000) * 1.5
                if best_adjusted < expected_good:
                    base += 10  # Bonus för estimerat bra tid

            return min(100.0, max(10.0, base))

        return 35.0  # Ingen distansdata alls

    @staticmethod
    def _age_score(entry: RaceEntry, race: Race) -> float:
        """Åldersoptimum."""
        age = entry.horse.age
        if race.breed.value == "kallblod":
            if 6 <= age <= 10:
                return 70.0
            elif 4 <= age <= 5 or 11 <= age <= 12:
                return 55.0
            elif age >= 13:
                return 35.0
            return 45.0
        else:
            if 5 <= age <= 8:
                return 70.0
            elif 3 <= age <= 4 or 9 <= age <= 11:
                return 55.0
            elif age >= 12:
                return 35.0
            return 45.0

    @staticmethod
    def _recent_win_bonus(entry: RaceEntry) -> float:
        """Form slår klass — senaste start vinst = bonus.

        Dennis: "Form slår klass. Senaste vinst, även i lättare klass,
        är alltid positivt. Man ska inte underskatta att hästen kommer
        med form."
        """
        recent = entry.horse.recent_starts(5)
        if not recent:
            return 35.0

        score = 40.0  # Baspoäng

        # Senaste start
        last = recent[0]
        if last.won:
            score += 30  # Stor bonus — vann senast
        elif last.top3:
            score += 15  # Bra form

        # Kolla om det finns en vinst bland senaste 3
        recent_3_wins = sum(1 for s in recent[:3] if s.won)
        if recent_3_wins >= 2:
            score += 15  # Vunnit 2 av 3 senaste = stark form
        elif recent_3_wins == 1:
            score += 5

        # Bonus om senaste vinsten var i tuffare klass (hög prispott)
        if last.won and last.race_purse > 50000:
            score += 5  # Vann mot ok motstånd

        return min(100.0, max(0.0, score))

    @staticmethod
    def _auto_start_speed_score(entry: RaceEntry, race: Race) -> float:
        """Startsnabbhet vid autostart.

        Hästar med bra resultat från spår 1-3 i autostart =
        troligen startsnabba, vilket är viktigt i korta lopp.
        """
        if race.start_method != StartMethod.AUTO:
            return 50.0  # Irrelevant vid voltstart

        recent = entry.horse.recent_starts(10)
        if not recent:
            return 45.0

        # Hitta autostart-starter från innerspår (1-3)
        inner_auto = [
            s for s in recent
            if s.start_method == StartMethod.AUTO
            and s.post_position <= 3
        ]

        if not inner_auto:
            return 45.0  # Ingen data

        # Bra placering från innerspår auto = startsnabb
        wins = sum(1 for s in inner_auto if s.won)
        top3 = sum(1 for s in inner_auto if s.top3)
        total = len(inner_auto)

        base = (wins / total * 60 + top3 / total * 30) * 100 / 90

        # Extra bonus i korta lopp (1640m)
        if race.distance <= 1700 and base > 50:
            base = min(100, base * 1.10)  # 10% extra vid kort distans

        return min(100.0, max(0.0, base))
