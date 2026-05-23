"""Rapportgenerering — skapar analysrapporter per omgång.

Genererar markdown-rapporter med:
- Översikt per lopp
- Ranking med poäng per faktor
- Spelvärda hästar (sweet spot)
- Rekommendationer (spik, 2-val, etc.)
"""

from __future__ import annotations

from ..data.models import GameRound, Race, RaceEntry


class RaceReporter:
    """Genererar analysrapporter."""

    def generate_round_report(self, game_round: GameRound) -> str:
        """Skapa en komplett rapport för en spelomgång."""
        lines = [
            f"# {game_round.game_type} — {game_round.round_date}",
            f"**Bana:** {game_round.track_name}",
            "",
        ]

        for race in game_round.races:
            lines.append(self._race_section(race))

        # Sammanfattning
        lines.append(self._summary_section(game_round))

        return "\n".join(lines)

    def _race_section(self, race: Race) -> str:
        """Rapport för ett enskilt lopp."""
        lines = [
            f"## Avd {race.race_number} — {race.distance}m {race.start_method.value}start",
            f"Prispott: {race.purse:,} kr | {race.num_starters} startande | {race.breed.value}",
            "",
        ]

        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )

        # Tabell
        lines.append(
            "| # | Häst | Poäng | Streck | Rek | "
            "Tid | Form | Pris | Bana | Kat | Kusk | Spår |"
        )
        lines.append(
            "|---|------|-------|--------|-----|"
            "-----|------|------|------|-----|------|------|"
        )

        for entry in sorted_entries:
            fs = entry.factor_scores
            bet_str = f"{entry.bet_percentage:.1%}" if entry.bet_percentage else "-"
            lines.append(
                f"| {entry.post_position} "
                f"| {entry.horse.name[:20]} "
                f"| **{entry.super_score:.0f}** "
                f"| {bet_str} "
                f"| {entry.recommendation} "
                f"| {fs.get('time_analysis', 0):.0f} "
                f"| {fs.get('form_curve', 0):.0f} "
                f"| {fs.get('prize_index', 0):.0f} "
                f"| {fs.get('track_profile', 0):.0f} "
                f"| {fs.get('category_profile', 0):.0f} "
                f"| {fs.get('driver_trainer', 0):.0f} "
                f"| {fs.get('post_position', 0):.0f} |"
            )

        # Spelvärda
        value_picks = [
            e for e in sorted_entries
            if e.bet_percentage and 0.05 <= e.bet_percentage <= 0.15
            and e.super_score >= 50
        ]

        if value_picks:
            lines.append("")
            lines.append(
                "**Spelvärda (5-15% streck, 50+ poäng):** "
                + ", ".join(
                    f"{e.post_position} {e.horse.name} "
                    f"({e.super_score:.0f}p / {e.bet_percentage:.0%})"
                    for e in value_picks
                )
            )

        lines.append("")
        return "\n".join(lines)

    def _summary_section(self, game_round: GameRound) -> str:
        """Sammanfattning med alla spikar och rekommendationer."""
        lines = [
            "---",
            "## Sammanfattning",
            "",
            "| Avd | Spik/Förstaval | Poäng | Streck | Gardering |",
            "|-----|---------------|-------|--------|-----------|",
        ]

        for race in game_round.races:
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )

            if not sorted_entries:
                continue

            top = sorted_entries[0]
            gardering = [
                e for e in sorted_entries[1:]
                if e.recommendation in ("2-val", "3-val")
            ]
            gard_str = ", ".join(
                f"{e.post_position} {e.horse.name[:12]}"
                for e in gardering[:3]
            )
            bet_str = f"{top.bet_percentage:.0%}" if top.bet_percentage else "-"

            lines.append(
                f"| {race.race_number} "
                f"| {top.post_position} {top.horse.name[:15]} ({top.recommendation}) "
                f"| {top.composite_score:.0f} "
                f"| {bet_str} "
                f"| {gard_str} |"
            )

        lines.append("")
        return "\n".join(lines)

    def generate_horse_deep_dive(self, entry: RaceEntry, race: Race) -> str:
        """Djupanalys av en enskild häst."""
        h = entry.horse
        recent = h.recent_starts(10)

        lines = [
            f"# {h.name} — Djupanalys",
            f"Ålder: {h.age} | Kön: {h.sex.value} | Ras: {h.breed.value}",
            f"Tränare: {h.trainer_name} | Kusk: {entry.driver_name}",
            f"Far: {h.sire} | Mor: {h.dam} (av {h.dam_sire})",
            "",
            f"**Karriär:** {h.career.total_starts} starter, "
            f"{h.career.wins}-{h.career.seconds}-{h.career.thirds}, "
            f"{h.career.total_prize_money:,} kr",
            f"**Vinstprocent:** {h.career.win_rate:.1%} | "
            f"**Top3:** {h.career.top3_rate:.1%}",
            f"**Form:** {h.recent_form_string}",
            f"**Galoppfrekvens:** {h.gallop_rate:.1%}",
            "",
            "## Composite Score: " + f"**{entry.super_score:.0f}**",
            "",
        ]

        # Faktorpoäng
        lines.append("| Faktor | Poäng |")
        lines.append("|--------|-------|")
        for factor, score in sorted(
            entry.factor_scores.items(), key=lambda x: x[1], reverse=True
        ):
            bar = "█" * int(score / 5) + "░" * (20 - int(score / 5))
            lines.append(f"| {factor} | {score:.0f} {bar} |")

        lines.append("")

        # Senaste starter
        if recent:
            lines.append("## Senaste 10 starter")
            lines.append("| Datum | Bana | Dist | Start | Spår | Plac | Km-tid | Pris |")
            lines.append("|-------|------|------|-------|------|------|--------|------|")
            for s in recent:
                km_str = f"{s.km_time:.1f}" if s.km_time else "-"
                plac_str = str(s.placement) if s.placement else "disk"
                lines.append(
                    f"| {s.start_date} | {s.track_name[:10]} | {s.distance} "
                    f"| {s.start_method.value} | {s.post_position} "
                    f"| {plac_str} | {km_str} | {s.prize_money:,} |"
                )

        return "\n".join(lines)
