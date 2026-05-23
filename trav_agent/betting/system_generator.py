"""Systemgenerator v6 — Smart ABC + union-picks.

Livedata-analys (203 lopp V85 2026):
  Modell top-3: 63.1% per lopp
  Marknad top-3: 75.4% per lopp
  Union top-3: 81.8% per lopp (bäst)

Kritisk insikt: Modellen missar ofta marknadens favoriter (>20% streck
som rankas #4-#8 i modellen). Budget-reduceringen måste skydda dessa.

Fyra strategier:
  A_union:   Union-picks, ABC-reducering, balanserad
  B_marknad: Marknad-primär, modell adderar value-picks
  C_bred:    Bred gardering med poäng+streck-tröskel
  D_smart:   Smart ABC — 2 picks i trygga lopp, bred i osäkra (bäst ROI)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from ..data.models import GameRound, Race, RaceEntry

logger = logging.getLogger(__name__)


@dataclass
class RacePick:
    """Picks för ett enskilt lopp i systemet."""

    race_number: int
    track_name: str
    distance: int
    start_method: str
    num_starters: int
    confidence: float
    confidence_formula: str

    picks: list[int]
    pick_names: list[str]
    pick_scores: list[float]
    pick_streck: list[float]

    num_picks: int = 0
    combined_streck: float = 0.0
    upset_risk: float = 0.0

    def __post_init__(self):
        self.num_picks = len(self.picks)
        self.combined_streck = sum(self.pick_streck)


@dataclass
class BettingSystem:
    """Ett komplett V75/V85-system."""

    game_type: str
    game_date: str
    track_name: str
    budget: int
    strategy: str

    race_picks: list[RacePick] = field(default_factory=list)
    total_rows: int = 0
    row_price: float = 0.0
    total_cost: float = 0.0
    skip_round: bool = False
    skip_reason: str = ""

    avg_confidence: float = 0.0
    avg_upset_risk: float = 0.0
    estimated_coverage: float = 0.0

    def calculate_cost(self):
        """Beräkna total kostnad."""
        if not self.race_picks:
            return
        self.total_rows = 1
        for rp in self.race_picks:
            self.total_rows *= rp.num_picks

        from ..config import ROW_PRICES
        self.row_price = ROW_PRICES.get(self.game_type, 0.50)
        self.total_cost = self.total_rows * self.row_price

        self.avg_confidence = sum(rp.confidence for rp in self.race_picks) / len(self.race_picks)
        self.avg_upset_risk = sum(rp.upset_risk for rp in self.race_picks) / len(self.race_picks)

        coverage = 1.0
        for rp in self.race_picks:
            coverage *= rp.combined_streck
        self.estimated_coverage = coverage

    def summary(self) -> str:
        """Textsammanfattning."""
        lines = [
            f"{'='*60}",
            f"  SYSTEM: {self.game_type} {self.game_date} — {self.track_name}",
            f"  Strategi: {self.strategy}",
            f"{'='*60}",
        ]
        if self.skip_round:
            lines.append(f"  SKIPPA — {self.skip_reason}")
            lines.append(f"{'='*60}")
            return "\n".join(lines)

        lines.append(f"  Budget: {self.budget:,} kr | Rader: {self.total_rows:,} | "
                      f"Kostnad: {self.total_cost:,.0f} kr")
        lines.append(f"  Snittkonfidens: {self.avg_confidence:.0f} | "
                      f"Snitt skrällrisk: {self.avg_upset_risk:.0f}")
        lines.append(f"{'='*60}\n")

        for rp in self.race_picks:
            conf_bar = "#" * int(rp.confidence / 10) + "." * (10 - int(rp.confidence / 10))
            lines.append(
                f"  Avd {rp.race_number} ({rp.distance}m {rp.start_method}) "
                f"— {rp.num_picks} val  [{conf_bar}] {rp.confidence:.0f}"
            )
            lines.append(f"    Skrallrisk: {rp.upset_risk:.0f}%")
            for i, (num, name, score, streck) in enumerate(
                zip(rp.picks, rp.pick_names, rp.pick_scores, rp.pick_streck)
            ):
                marker = "*" if i == 0 else " "
                lines.append(f"    {marker} {num:>2}. {name:<20} {score:5.1f}p  {streck:5.1%}")
            lines.append(f"    Tackning: {rp.combined_streck:.0%}\n")

        lines.append(f"{'='*60}")
        return "\n".join(lines)


# ── Hjälpfunktioner ─────────────────────────────────────────────────────

def _combined_rank_score(entry: RaceEntry, model_rank: int, market_rank: int,
                         num_starters: int) -> float:
    """Kombinerad ranking-score för reducering.

    Lägre = bättre (mer värd att behålla).
    Kombinerar modell-rank och marknads-rank med lika vikt.
    En häst som är stark i ANTINGEN modell eller marknad
    ska inte tas bort.
    """
    # Normalisera till 0-1
    m_norm = model_rank / num_starters
    k_norm = market_rank / num_starters
    # Bästa av de två viktas tyngre (den starkaste signalen räknas mest)
    best = min(m_norm, k_norm)
    worst = max(m_norm, k_norm)
    return best * 0.6 + worst * 0.4


def _get_rankings(race: Race):
    """Returnera model-ranking och market-ranking som dicts."""
    by_model = sorted(race.active_entries, key=lambda e: (-e.super_score, e.post_position))
    by_market = sorted(race.active_entries, key=lambda e: (-(e.bet_percentage or 0), e.post_position))
    model_rank = {e.post_position: i + 1 for i, e in enumerate(by_model)}
    market_rank = {e.post_position: i + 1 for i, e in enumerate(by_market)}
    return by_model, by_market, model_rank, market_rank


class SystemGenerator:
    """Genererar V75/V85-system.

    Version 5: Budget-först med union-picks och kombinerad reducering.

    Tre strategier:
      A_union:   Union av modell + marknad, ABC-allokering
      B_marknad: Marknad-primär (streck) + modell-tillägg
      C_bred:    Bred gardering med score+streck-tröskel
    """

    def __init__(
        self,
        budget: int = 1000,
        strategy: str = "A_union",
        selective_filter: str = "none",
        max_spikes: int = 0,
        spike_conf_threshold: float = 80,
        spike_score_gap: float = 15,
    ):
        self.budget = budget
        self.strategy = strategy
        self.selective_filter = selective_filter
        self.max_spikes = max_spikes
        self.spike_conf_threshold = spike_conf_threshold
        self.spike_score_gap = spike_score_gap

    def generate(self, game_round: GameRound) -> BettingSystem:
        """Generera ett system för en spelomgång."""
        system = BettingSystem(
            game_type=game_round.game_type,
            game_date=str(game_round.round_date),
            track_name=game_round.track_name,
            budget=self.budget,
            strategy=self.strategy,
        )

        should_skip, reason = self._check_filter(game_round)
        if should_skip:
            system.skip_round = True
            system.skip_reason = reason
            return system

        if self.strategy == "D_smart":
            system.race_picks = self._picks_smart(game_round)
            system.calculate_cost()
            # D_smart hanterar budget internt, ingen extra reducering
        else:
            if self.strategy == "A_union":
                system.race_picks = self._picks_union(game_round)
            elif self.strategy == "B_marknad":
                system.race_picks = self._picks_marknad(game_round)
            elif self.strategy == "C_bred":
                system.race_picks = self._picks_bred(game_round)
            else:
                system.race_picks = self._picks_union(game_round)

            system.calculate_cost()

            if system.total_cost > self.budget:
                self._reduce_to_budget(system, game_round)
                system.calculate_cost()

        return system

    def _check_filter(self, game_round: GameRound) -> tuple[bool, str]:
        """Kontrollera om omgången ska skippas."""
        if self.selective_filter == "none":
            return False, ""

        avg_risk = sum(r.upset_risk for r in game_round.races) / len(game_round.races)

        if self.selective_filter == "risk_lt_25":
            if avg_risk >= 25:
                return True, f"Snitt skrallrisk {avg_risk:.0f}% >= 25%"

        elif self.selective_filter == "risk_lt_30":
            if avg_risk >= 30:
                return True, f"Snitt skrallrisk {avg_risk:.0f}% >= 30%"

        elif self.selective_filter == "selective_profitable":
            allowed_types = {"V64", "GS75", "V65"}
            if game_round.game_type not in allowed_types:
                return True, f"{game_round.game_type} ej i lonsamma speltyper"
            if avg_risk >= 20:
                return True, f"Snitt skrallrisk {avg_risk:.0f}% >= 20%"

        return False, ""

    # ── STRATEGI A: Union-picks med ABC ──────────────────────────────────

    def _picks_union(self, game_round: GameRound) -> list[RacePick]:
        """Union av modell top-N och marknad top-N per lopp.

        ABC-klassificering baserad på modell-marknad-överensstämmelse:
          A (överens):   n_model=2, n_market=2 → 2-3 picks (union)
          B (delvis):    n_model=3, n_market=3 → 4-5 picks
          C (ej överens): n_model=3, n_market=4 → 5-7 picks

        Picks sorteras efter kombinerad rank (modell + marknad).
        """
        result = []
        for race in game_round.races:
            entries = race.active_entries
            if len(entries) < 2:
                continue

            by_model, by_market, model_rank, market_rank = _get_rankings(race)

            model_top3 = set(e.post_position for e in by_model[:3])
            market_top3 = set(e.post_position for e in by_market[:3])
            overlap_3 = len(model_top3 & market_top3)

            model_top1 = by_model[0].post_position
            market_top1 = by_market[0].post_position
            top1_agree = model_top1 == market_top1

            # ABC-klassificering
            if top1_agree and overlap_3 >= 2:
                abc = "A"
                n_model, n_market = 2, 2
            elif overlap_3 >= 1:
                abc = "B"
                n_model, n_market = 3, 3
            else:
                abc = "C"
                n_model, n_market = 3, 4

            # Skrällrisk ökar bredden
            if race.upset_risk >= 45:
                n_model = max(n_model, 4)
                n_market = max(n_market, 4)
            elif race.upset_risk >= 30:
                n_model = max(n_model, 3)
                n_market = max(n_market, 3)

            # Union: top-N modell + top-N marknad
            model_set = set(e.post_position for e in by_model[:n_model])
            market_set = set(e.post_position for e in by_market[:n_market])
            union_posts = model_set | market_set

            # Sortera picks efter kombinerad rank-score
            n = race.num_starters
            selected = sorted(
                [e for e in entries if e.post_position in union_posts],
                key=lambda e: _combined_rank_score(e, model_rank[e.post_position],
                                                    market_rank[e.post_position], n),
            )

            conf = self._agreement_confidence(by_model, by_market, overlap_3, top1_agree)

            pick = self._make_pick(race, selected, conf, f"A_union_{abc}")
            result.append(pick)

        return result

    # ── STRATEGI B: Marknad-primär ──────────────────────────────────────

    def _picks_marknad(self, game_round: GameRound) -> list[RacePick]:
        """Marknad-primär: följ streckprocent, addera modell-value.

        Tar marknadens top-N (efter streck), lägger sedan till
        hästar som modellen rankar högt men marknaden underskattar.
        """
        result = []
        spikes_used = 0

        for race in game_round.races:
            entries = race.active_entries
            if len(entries) < 2:
                continue

            by_model, by_market, model_rank, market_rank = _get_rankings(race)

            model_gap = by_model[0].super_score - by_model[1].super_score
            top1_streck = by_market[0].bet_percentage or 0
            market_gap = top1_streck - (by_market[1].bet_percentage or 0)

            # Spik-möjlighet
            can_spike = (
                spikes_used < self.max_spikes
                and by_model[0].post_position == by_market[0].post_position
                and model_gap >= self.spike_score_gap
                and market_gap >= 0.12
                and top1_streck >= 0.25
            )

            if can_spike:
                selected = [by_market[0]]
                spikes_used += 1
                conf = min(90, model_gap * 2 + market_gap * 150)
                formula = "B_marknad_spik"
            else:
                # Marknadens top-3 som bas
                base_n = 3
                if race.upset_risk >= 40:
                    base_n = 4
                elif top1_streck >= 0.30 and market_gap >= 0.10:
                    base_n = 2  # Tydlig marknadsfavorit

                market_picks = set(e.post_position for e in by_market[:base_n])

                # Addera modellens top-2 om de inte redan finns (value-tillägg)
                for e in by_model[:2]:
                    market_picks.add(e.post_position)

                n = race.num_starters
                selected = sorted(
                    [e for e in entries if e.post_position in market_picks],
                    key=lambda e: _combined_rank_score(e, model_rank[e.post_position],
                                                        market_rank[e.post_position], n),
                )
                conf = min(85, market_gap * 200 + (15 if by_model[0].post_position == by_market[0].post_position else 0))
                formula = "B_marknad"

            pick = self._make_pick(race, selected, conf, formula)
            result.append(pick)

        return result

    # ── STRATEGI C: Bred gardering ──────────────────────────────────────

    def _picks_bred(self, game_round: GameRound) -> list[RacePick]:
        """Bred gardering: inkludera alla med rimlig score ELLER streck.

        Poängreducering: Exkludera hästar där BÅDE modell OCH marknad
        har dem lågt rankade (ingen tror på dem).
        """
        result = []
        for race in game_round.races:
            entries = race.active_entries
            if len(entries) < 2:
                continue

            by_model, by_market, model_rank, market_rank = _get_rankings(race)
            n = race.num_starters

            top_score = by_model[0].super_score
            # Inkludera om modell-rank <= 5 ELLER streck >= 8% ELLER modellscore >= 35% av top
            selected = []
            for e in entries:
                mr = model_rank[e.post_position]
                kr = market_rank[e.post_position]
                streck = e.bet_percentage or 0

                if mr <= 5 or kr <= 4 or streck >= 0.08 or e.super_score >= top_score * 0.35:
                    selected.append(e)

            # Min 3, max 8
            if len(selected) < 3:
                selected = list(by_model[:3])
            if len(selected) > 8:
                # Sortera och ta top-8
                selected = sorted(
                    selected,
                    key=lambda e: _combined_rank_score(e, model_rank[e.post_position],
                                                        market_rank[e.post_position], n),
                )[:8]
            else:
                selected = sorted(
                    selected,
                    key=lambda e: _combined_rank_score(e, model_rank[e.post_position],
                                                        market_rank[e.post_position], n),
                )

            conf = min(80, (top_score - selected[-1].super_score) * 1.0 if len(selected) > 1 else 50)

            pick = self._make_pick(race, selected, conf, "C_bred")
            result.append(pick)

        return result

    # ── STRATEGI D: Smart ABC ──────────────────────────────────────────

    def _picks_smart(self, game_round: GameRound) -> list[RacePick]:
        """Smart ABC: 2 picks i trygga lopp, bred i osäkra.

        Algoritm:
        1. Beräkna "security score" per lopp (modell-marknad-överensstämmelse)
        2. Sortera: tryggast → osäkrast
        3. Starta med 2 picks (union top-2) per lopp
        4. Öka picks i OSÄKRASTE loppet först tills budget nås
        5. Alla picks = union av modell + marknad (kombinerad ranking)

        Resultat 2026 V85 (14 omgångar):
          Budget 1000 kr → +6136% ROI (1x 8/8 = 670K)
          Budget 2000 kr → +2573% ROI
        """
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)
        max_rows = int(self.budget / row_price)

        # 1. Beräkna security score per lopp
        race_data = []
        for race in game_round.races:
            entries = race.active_entries
            if len(entries) < 2:
                continue
            by_model, by_market, model_rank, market_rank = _get_rankings(race)

            model_top3 = set(e.post_position for e in by_model[:3])
            market_top3 = set(e.post_position for e in by_market[:3])
            overlap = len(model_top3 & market_top3)

            top1_agree = by_model[0].post_position == by_market[0].post_position

            model_gap = by_model[0].super_score - by_model[1].super_score
            top1_streck = by_market[0].bet_percentage or 0
            market_gap = top1_streck - (by_market[1].bet_percentage or 0)

            # Security: higher = safer race
            security = (
                overlap * 20          # 0-60: överensstämmelse top-3
                + (25 if top1_agree else 0)  # favorit-agreement
                + min(20, model_gap * 2)     # modell-gap
                + min(15, market_gap * 80)   # marknad-gap
                - race.upset_risk * 0.5      # skrällrisk sänker
            )

            # All candidates sorted by combined rank
            all_sorted = sorted(
                entries,
                key=lambda e: _combined_rank_score(
                    e, model_rank[e.post_position],
                    market_rank[e.post_position], race.num_starters
                ),
            )

            race_data.append({
                "race": race,
                "security": security,
                "all_sorted": all_sorted,
                "model_rank": model_rank,
                "market_rank": market_rank,
                "by_model": by_model,
                "by_market": by_market,
                "overlap": overlap,
                "top1_agree": top1_agree,
                "max_picks": min(len(entries), 10),
            })

        if not race_data:
            return []

        # 2. Start med 2 picks per lopp
        num_races = len(race_data)
        picks_per_race = [2] * num_races

        # 3. Beräkna aktuella rader
        def calc_rows():
            r = 1
            for p in picks_per_race:
                r *= p
            return r

        # 4. Sortera efter security (lägst = osäkrast → öka först)
        order = sorted(range(num_races), key=lambda i: race_data[i]["security"])

        # Round-robin: öka 1 pick åt gången, osäkrast först
        # Detta sprider picks jämnare istället för att fylla en race till max
        while True:
            expanded = False
            for idx in order:  # osäkrast först
                current = picks_per_race[idx]
                max_p = race_data[idx]["max_picks"]
                if current >= max_p:
                    continue
                new_rows = calc_rows() // current * (current + 1)
                if new_rows <= max_rows:
                    picks_per_race[idx] = current + 1
                    expanded = True
                    # Fortsätt till nästa race (round-robin), break:a inte
            if not expanded:
                break

        # 5. Bygg RacePick per lopp
        result = []
        for i, rd in enumerate(race_data):
            race = rd["race"]
            n_picks = picks_per_race[i]
            selected = rd["all_sorted"][:n_picks]

            conf = self._agreement_confidence(
                rd["by_model"], rd["by_market"],
                rd["overlap"], rd["top1_agree"]
            )

            pick = self._make_pick(race, selected, conf, f"D_smart_s{rd['security']:.0f}")
            result.append(pick)

        return result

    # ── Gemensamma hjälpmetoder ──────────────────────────────────────────

    def _make_pick(self, race, selected_entries, confidence, formula):
        """Skapa RacePick från valda entries."""
        return RacePick(
            race_number=race.race_number,
            track_name=race.track_name,
            distance=race.distance,
            start_method=race.start_method.value,
            num_starters=race.num_starters,
            confidence=confidence,
            confidence_formula=formula,
            picks=[e.post_position for e in selected_entries],
            pick_names=[e.horse.name for e in selected_entries],
            pick_scores=[e.super_score for e in selected_entries],
            pick_streck=[e.bet_percentage or 0.05 for e in selected_entries],
            upset_risk=race.upset_risk,
        )

    def _agreement_confidence(self, by_model, by_market, overlap_3, top1_agree):
        """Confidence baserad på modell-marknad-överensstämmelse."""
        model_gap = by_model[0].super_score - by_model[1].super_score
        market_gap = (by_market[0].bet_percentage or 0) - (by_market[1].bet_percentage or 0)
        agree_score = overlap_3 * 15 + (20 if top1_agree else 0)
        gap_score = min(20, model_gap * 1.5) + min(15, market_gap * 100)
        return min(90, agree_score + gap_score)

    # ── Budget-reducering (KOMBINERAD score) ────────────────────────────

    def _reduce_to_budget(self, system: BettingSystem, game_round: GameRound) -> None:
        """Reducera systemet till budget.

        KRITISK SKILLNAD från v4: Reducerar baserat på KOMBINERAD
        modell+marknad-score, inte bara modellscore.

        En häst som marknaden har som #1 (30%+ streck) men modellen
        rankar som #6 ska INTE tas bort först — den tas bort sist.

        Aldrig under 2 picks per lopp (1 om spik).
        """
        # Bygg rank-lookup per race
        race_rankings = {}
        for race in game_round.races:
            _, _, model_rank, market_rank = _get_rankings(race)
            race_rankings[race.race_number] = (model_rank, market_rank, race.num_starters)

        min_floor = 1 if self.max_spikes > 0 else 2

        for _ in range(100):
            if system.total_cost <= self.budget:
                break

            # Hitta lopp med flest picks (de har mest att ge)
            candidates = [rp for rp in system.race_picks if rp.num_picks > 2]
            if not candidates:
                candidates = [rp for rp in system.race_picks if rp.num_picks > min_floor]
                if not candidates:
                    break

            # Välj loppet med flest picks (vid lika: högst confidence)
            candidates.sort(key=lambda rp: (-rp.num_picks, -rp.confidence))
            target = candidates[0]

            # Hitta den MINST värdefulla picken (kombinerad score)
            rr = race_rankings.get(target.race_number)
            if not rr:
                # Fallback: ta bort sista
                self._remove_last_pick(target)
                system.calculate_cost()
                continue

            model_rank, market_rank, n = rr

            # Beräkna combined score för varje pick
            worst_idx = -1
            worst_score = -1.0
            for i, post in enumerate(target.picks):
                mr = model_rank.get(post, n)
                kr = market_rank.get(post, n)
                cs = _combined_rank_score(None, mr, kr, n)
                if cs > worst_score:  # Högst score = sämst rankad
                    worst_score = cs
                    worst_idx = i

            if worst_idx >= 0:
                self._remove_pick_at(target, worst_idx)
            else:
                self._remove_last_pick(target)

            system.calculate_cost()

    def _remove_pick_at(self, rp: RacePick, idx: int) -> None:
        """Ta bort pick vid givet index."""
        rp.picks.pop(idx)
        rp.pick_names.pop(idx)
        rp.pick_scores.pop(idx)
        rp.pick_streck.pop(idx)
        rp.num_picks -= 1
        rp.combined_streck = sum(rp.pick_streck)

    def _remove_last_pick(self, rp: RacePick) -> None:
        """Ta bort sista pick (fallback)."""
        self._remove_pick_at(rp, -1)

    def generate_markdown(self, system: BettingSystem) -> str:
        """Generera markdown-rapport."""
        if system.skip_round:
            return (
                f"# {system.game_type} {system.game_date} — {system.track_name}\n\n"
                f"**SKIPPA** — {system.skip_reason}\n"
            )

        lines = [
            f"# {system.game_type} {system.game_date} — {system.track_name}",
            f"*Strategi: {system.strategy} | Budget: {system.budget:,} kr*",
            "",
            f"| | Rader | Kostnad | Konfidens | Skrallrisk |",
            f"|---|---|---|---|---|",
            f"| **System** | **{system.total_rows:,}** | **{system.total_cost:,.0f} kr** "
            f"| {system.avg_confidence:.0f}/100 | {system.avg_upset_risk:.0f}/100 |",
            "",
        ]

        for rp in system.race_picks:
            lines.append(
                f"## Avd {rp.race_number} — {rp.distance}m {rp.start_method} "
                f"({rp.num_starters} st) Konf: {rp.confidence:.0f} "
                f"Risk: {rp.upset_risk:.0f}"
            )
            lines.append("")
            lines.append("| # | Hast | Poang | Streck |")
            lines.append("|---|------|-------|--------|")

            for num, name, score, streck in zip(
                rp.picks, rp.pick_names, rp.pick_scores, rp.pick_streck
            ):
                lines.append(f"| **{num}** | {name} | {score:.1f} | {streck:.1%} |")

            lines.append(f"\n*{rp.num_picks} val — tackning {rp.combined_streck:.0%}*\n")

        return "\n".join(lines)
