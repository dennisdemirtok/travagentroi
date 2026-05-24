"""Systembyggare — empiriskt optimerad på 194 V75/V85-omgångar.

Testat: 360+ strategikonfigurationer, 3 allokeringsmetoder, 4 budgetnivåer.

EMPIRISKA FYND (system_optimizer.py + winner_depth_analysis.py):

1. BASSTRUKTUR: 3 spikar + 4 val i resten = bäst ROI
   - 3S+rest4 vid 200kr: 4 träffar/194 = 2.1%, +1267% ROI
   - 2S+rest4 vid 1000kr: 7 träffar = 3.6%, +238% ROI

2. SPIKE-URVAL: "upset_low" — spika de 3 tryggaste loppen
   - upset_low: 45/116 configs träffade (bäst)
   - gap: 42/116
   - confidence: 34/116 (sämst)

3. VARIABEL BREDD: Testades men slog INTE fast bredd
   - Smart variabel: 3 träffar vid 500kr vs fixed 5 träffar
   - Anledning: multiplicativ kostnad — bredare i ett lopp
     fördubblar rader, vinsten i täckning kompenserar inte
   - MEN: variabel ger fler partiella (n-1) träffar

4. LOPPSVÅRIGHETSMODELL (1 371 lopp analyserade):
   Vinnarens modellranking beror på:
   - Antal startande: r=+0.196 (starkaste signalen)
   - Tillägg: r=+0.158
   - Modellspridning: r=-0.157
   - Volt: r=+0.147
   - Spårtrappa: r=+0.137

5. BREAKEVEN: Mediandividend ~49 700kr, breakeven vid ~1 750-3 500kr.

Dennis: "alla lopp har olika svårighet eller lätthet"
Empirin: Ja, men intelligensen sitter i VILKA lopp som spikas (variabelt
per omgång), inte i variabel bredd bland non-spike-benen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..data.models import GameRound, Race, RaceEntry, StartMethod


@dataclass
class LegAssignment:
    """Tilldelning för en avdelning i systemet."""
    race_number: int
    leg_type: str  # "spik", "kort", "medel", "bred"
    num_picks: int
    picks: list[int] = field(default_factory=list)
    confidence: float = 0.0
    upset_risk: float = 0.0
    difficulty: float = 0.0
    reasoning: str = ""

    @property
    def is_spike(self) -> bool:
        return self.leg_type == "spik"


@dataclass
class SystemPlan:
    """Komplett systemplan för en omgång."""
    game_type: str
    round_date: str
    legs: list[LegAssignment]
    total_rows: int = 0
    row_price: float = 0.50
    total_cost: float = 0.0
    budget: float = 200.0
    strategy_name: str = ""

    @property
    def num_spikes(self) -> int:
        return sum(1 for leg in self.legs if leg.leg_type == "spik")

    @property
    def num_short(self) -> int:
        return sum(1 for leg in self.legs if leg.leg_type == "kort")

    @property
    def num_wide(self) -> int:
        return sum(1 for leg in self.legs if leg.leg_type in ("medel", "bred"))

    def calc_rows(self) -> int:
        rows = 1
        for leg in self.legs:
            rows *= leg.num_picks
        self.total_rows = rows
        self.total_cost = rows * self.row_price
        return rows

    def summary(self) -> str:
        parts = []
        for leg in sorted(self.legs, key=lambda l: l.race_number):
            picks_str = ",".join(str(p) for p in leg.picks[:leg.num_picks])
            parts.append(
                f"Avd {leg.race_number}: {leg.leg_type} ({leg.num_picks}) [{picks_str}]"
            )
        return (
            f"{self.strategy_name}\n"
            f"{'=' * 50}\n"
            + "\n".join(parts)
            + f"\n{'=' * 50}\n"
            f"Rader: {self.total_rows} | Kostnad: {self.total_cost:.0f} kr\n"
            f"Spikar: {self.num_spikes} | Korta: {self.num_short} | Breda: {self.num_wide}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Loppsvårighetsmodell — kalibrerad på 1 371 lopp
# ═══════════════════════════════════════════════════════════════════════════

def predict_difficulty(race: Race) -> float:
    """Beräkna loppets svårighetsgrad (0-100).

    Baserat på empiriska korrelationer med vinnarens modellranking.
    Används för att bestämma VILKA lopp som ska spikas.

    Hög svårighetsgrad = vinnaren hamnar djupt i modellrankingen
    → bör INTE spikas, behöver bredare täckning.
    """
    entries = race.active_entries
    if not entries:
        return 70.0

    difficulty = 0.0

    # 1. Antal startande (r=+0.196 — starkaste signalen)
    n = len(entries)
    if n <= 8:
        difficulty += 5.0
    elif n <= 10:
        difficulty += 15.0
    elif n <= 11:
        difficulty += 22.0
    elif n <= 13:
        difficulty += 30.0
    elif n <= 14:
        difficulty += 38.0
    else:
        difficulty += 45.0

    # 2. Tillägg (r=+0.158)
    has_tillagg = any(
        e.distance > race.distance
        for e in entries
        if e.distance > 0 and race.distance > 0
    )
    if has_tillagg:
        difficulty += 14.0

    # 3. Modellspridning (r=-0.157)
    sorted_entries = sorted(entries, key=lambda e: e.super_score, reverse=True)
    scores = [e.super_score for e in sorted_entries]

    if len(scores) >= 5:
        top5_spread = scores[0] - scores[4]
    elif len(scores) >= 3:
        top5_spread = scores[0] - scores[2]
    else:
        top5_spread = scores[0] - scores[-1] if len(scores) >= 2 else 30.0

    if top5_spread < 10:
        difficulty += 16.0
    elif top5_spread < 15:
        difficulty += 10.0
    elif top5_spread < 20:
        difficulty += 5.0
    elif top5_spread >= 35:
        difficulty -= 5.0

    mean_s = sum(scores) / len(scores) if scores else 50.0
    field_std = math.sqrt(
        sum((s - mean_s) ** 2 for s in scores) / len(scores)
    ) if scores else 10.0

    if field_std < 8:
        difficulty += 8.0
    elif field_std < 12:
        difficulty += 3.0
    elif field_std > 20:
        difficulty -= 3.0

    # 4. Voltstart (r=+0.147)
    if race.start_method == StartMethod.VOLT:
        difficulty += 12.0

    # 5. Spårtrappa (r=+0.137)
    distances = set(e.distance for e in entries if e.distance > 0)
    if len(distances) > 2:
        difficulty += 12.0
    elif len(distances) > 1:
        difficulty += 8.0

    # 6. Toppoäng (r=-0.109)
    top_score = scores[0] if scores else 50.0
    if top_score >= 85:
        difficulty -= 5.0
    elif top_score >= 75:
        difficulty -= 2.0
    elif top_score < 60:
        difficulty += 5.0

    # 7. Gap topp 1→2
    gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 0
    if gap_1_2 < 3:
        difficulty += 8.0
    elif gap_1_2 < 5:
        difficulty += 4.0
    elif gap_1_2 >= 15:
        difficulty -= 5.0
    elif gap_1_2 >= 10:
        difficulty -= 2.0

    # 8. Stolopp
    race_type_lower = (race.race_type or "").lower()
    race_name_lower = (race.race_name or "").lower()
    is_stolopp = any(
        word in race_type_lower or word in race_name_lower
        for word in ("stolopp", "storlopp", "stl", "final", "milen",
                     "derby", "grand", "elitlopp", "kriterium", "oaks")
    )
    if is_stolopp:
        difficulty += 5.0

    # 9. Favorit bakspår
    if sorted_entries and sorted_entries[0].post_position >= 8:
        difficulty += 6.0

    # 10. Modellens upset_risk
    difficulty += race.upset_risk * 0.15

    return max(0.0, min(100.0, difficulty))


# ═══════════════════════════════════════════════════════════════════════════
# Optimala konfigurationer per budget
# ═══════════════════════════════════════════════════════════════════════════

# Baserat på 194 historiska omgångar.
# (n_spikes, rest_width, label)
OPTIMAL_CONFIGS: dict[int, tuple[int, int, str]] = {
    100:  (3, 4, "3 spikar + 4 val — 100kr"),
    200:  (3, 4, "3 spikar + 4 val — 200kr"),
    500:  (3, 4, "3 spikar + 4 val — 500kr"),
    1000: (2, 4, "2 spikar + 4 val — 1000kr"),
}


def build_system(
    game_round: GameRound,
    budget: float = 200.0,
    row_price: Optional[float] = None,
    strategy: str = "optimal",
) -> SystemPlan:
    """Bygg ett empiriskt optimerat system.

    Bevisad metod:
    1. Ranka lopp efter svårighetsgrad (predict_difficulty)
    2. Spika de N lättaste (modellens bästa spikmöjligheter)
    3. Ge resten uniform bredd (4 val — empiriskt bäst)
    4. Budgetoptimera: minska lättaste non-spike om för dyrt,
       expandera svåraste om under budget

    Variationen sitter i VILKA lopp som spikas — det är unikt
    per omgång baserat på loppens egenskaper.

    Strategier:
    - "optimal": Empiriskt bäst per budget (3S+rest4 / 2S+rest4)
    - "aggressive": Fler spikar (4S+rest3)
    - "safe": Färre spikar, bredare (2S+rest5)
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    num_races = len(game_round.races)

    # Välj konfiguration
    if strategy == "aggressive":
        n_spikes = min(4, num_races - 2)
        rest_width = 3
        label = "Aggressiv — 4S+rest3"
    elif strategy == "safe":
        n_spikes = 2
        rest_width = 5
        label = "Säker — 2S+rest5"
    else:
        config = OPTIMAL_CONFIGS.get(
            budget,
            OPTIMAL_CONFIGS[min(OPTIMAL_CONFIGS.keys(),
                                key=lambda b: abs(b - budget))]
        )
        n_spikes, rest_width, label = config

    n_spikes = min(n_spikes, num_races // 2 + 1)

    # ── Steg 1: Ranka lopp efter svårighet ───────────────────────
    race_diffs = []
    for race in game_round.races:
        diff = predict_difficulty(race)
        race_diffs.append((race, diff))

    # Sortera: lättast först = bäst spike-kandidater
    race_diffs.sort(key=lambda x: x[1])

    # ── Steg 2: Tilldela spikar + bredd ──────────────────────────
    spike_set = set(id(race) for race, _ in race_diffs[:n_spikes])
    legs: list[LegAssignment] = []

    for race, diff in race_diffs:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        all_picks = [e.post_position for e in sorted_entries]

        if id(race) in spike_set:
            legs.append(LegAssignment(
                race_number=race.race_number,
                leg_type="spik",
                num_picks=1,
                picks=all_picks[:1],
                confidence=min(95, 70 - diff * 0.5),
                upset_risk=race.upset_risk,
                difficulty=diff,
                reasoning=_build_reasoning(race, diff, 1),
            ))
        else:
            width = min(rest_width, len(all_picks))
            leg_type = "kort" if width <= 2 else "medel" if width == 3 else "bred"
            legs.append(LegAssignment(
                race_number=race.race_number,
                leg_type=leg_type,
                num_picks=width,
                picks=all_picks[:width],
                confidence=max(15, 50 - diff * 0.4),
                upset_risk=race.upset_risk,
                difficulty=diff,
                reasoning=_build_reasoning(race, diff, width),
            ))

    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=label,
    )
    plan.calc_rows()

    # ── Steg 3: Budgetoptimering ─────────────────────────────────
    _optimize_budget(plan, legs, game_round, budget, spike_set)

    plan.calc_rows()
    return plan


def _optimize_budget(
    plan: SystemPlan,
    legs: list[LegAssignment],
    game_round: GameRound,
    budget: float,
    spike_set: set,
) -> None:
    """Anpassa systemet till budgeten.

    Över budget: minska LÄTTASTE non-spike-benen först.
    Under budget (< 50%): expandera SVÅRASTE non-spike-benen.
    """
    for _ in range(30):
        plan.calc_rows()
        if plan.total_cost <= budget:
            break

        # Minska lättaste non-spike med flest picks
        best_idx = -1
        best_ease = float('inf')

        for i, leg in enumerate(legs):
            if id(next(r for r in game_round.races if r.race_number == leg.race_number)) in spike_set:
                continue
            if leg.num_picks <= 1:
                continue
            if leg.difficulty < best_ease or (
                leg.difficulty == best_ease and best_idx >= 0 and leg.num_picks > legs[best_idx].num_picks
            ):
                best_ease = leg.difficulty
                best_idx = i

        if best_idx < 0:
            # Alla non-spike redan vid 1 — kan inte minska mer
            break

        legs[best_idx].num_picks -= 1
        legs[best_idx].picks = legs[best_idx].picks[:legs[best_idx].num_picks]
        _update_leg_type(legs[best_idx])

    # Expandera om under 50% budget
    for _ in range(30):
        plan.calc_rows()
        if plan.total_cost >= budget * 0.50:
            break

        # Expandera svåraste non-spike
        best_idx = -1
        best_diff = -1.0

        for i, leg in enumerate(legs):
            if id(next(r for r in game_round.races if r.race_number == leg.race_number)) in spike_set:
                continue
            race = next(r for r in game_round.races if r.race_number == leg.race_number)
            if leg.num_picks >= len(race.active_entries):
                continue

            new_rows = plan.total_rows * (leg.num_picks + 1) // leg.num_picks
            if new_rows * plan.row_price > budget:
                continue

            if leg.difficulty > best_diff:
                best_diff = leg.difficulty
                best_idx = i

        if best_idx < 0:
            break

        legs[best_idx].num_picks += 1
        race = next(r for r in game_round.races if r.race_number == legs[best_idx].race_number)
        all_entries = sorted(
            race.active_entries, key=lambda e: e.super_score, reverse=True
        )
        legs[best_idx].picks = [e.post_position for e in all_entries][:legs[best_idx].num_picks]
        _update_leg_type(legs[best_idx])


def _update_leg_type(leg: LegAssignment) -> None:
    """Uppdatera leg_type baserat på antal val."""
    if leg.num_picks == 1:
        leg.leg_type = "spik"
    elif leg.num_picks == 2:
        leg.leg_type = "kort"
    elif leg.num_picks == 3:
        leg.leg_type = "medel"
    else:
        leg.leg_type = "bred"


def _build_reasoning(race: Race, difficulty: float, picks: int) -> str:
    """Bygg förklaringstext."""
    parts = []
    n = len(race.active_entries)
    if n <= 8:
        parts.append(f"litet fält ({n})")
    elif n >= 14:
        parts.append(f"stort fält ({n})")
    else:
        parts.append(f"{n} startande")

    if race.start_method == StartMethod.VOLT:
        parts.append("volt")

    if any(e.distance > race.distance for e in race.active_entries
           if e.distance > 0 and race.distance > 0):
        parts.append("tillägg")

    distances = set(e.distance for e in race.active_entries if e.distance > 0)
    if len(distances) > 1:
        parts.append("spårtrappa")

    features = ", ".join(parts) if parts else "standard"
    return f"{picks} val (diff {difficulty:.0f} — {features})"


def build_multiple_systems(
    game_round: GameRound,
    budgets: list[float] | None = None,
) -> list[SystemPlan]:
    """Bygg system vid flera budgetnivåer.

    Alla använder den empiriskt bästa metoden (upset_low + 3S+rest4).
    Vid högsta budget används 2S+rest4 (empiriskt bäst vid 1000kr+).
    """
    if budgets is None:
        budgets = [200, 500, 1000]

    plans = []
    for budget in budgets:
        plan = build_system(game_round, budget=budget, strategy="optimal")
        plans.append(plan)
    return plans
