"""Empiriskt optimerad systembyggare — baserad på 144 historiska V75/V85-omgångar.

Empiriska fynd (system_optimizer.py, 360 strategikonfigurationer):

1. OPTIMALT ANTAL SPIKAR: 3 (ibland 4)
   - 0 spikar: träffar aldrig
   - 1-2 spikar: träffar sällan
   - 3 spikar: bäst ROI (+2427% vid 100kr budget)
   - 4 spikar: bra hitrate men lägre ROI

2. SPIKE-URVAL: "upset_low" vinner
   - Spika de lopp med LÄGST skrällrisk (= tryggast)
   - upset_low: 45/116 configs träffade
   - gap: 42/116
   - confidence: 34/116

3. ENKLA STRATEGIER VINNER
   - "N spikar + fast bredd i resten" slår komplexa variabla approaches
   - Inget behov av gap-baserad eller upset-variabel breddning

4. BÄSTA STRATEGIER PER BUDGET:
   - 100kr: 3S+rest4|upset_low → +2427% ROI, 2.8% hitrate
   - 200kr: 3S+rest4|upset_low → bäst ROI
   - 500kr: 3S+rest5|upset_low → balans ROI/hitrate
   - 1000kr: 3S+rest6|upset_low → 4.9% hitrate, +271% ROI

5. BREAKEVEN lätt att nå — mediandividend ~49 700 kr, breakeven
   vid 1 750-3 500 kr per träff.

Dennis: "ofta nar jag har vunnit stort sa har jag behövt spika 1-2-3 lopp"
Empirin visar: 3 spikar, alltid de tryggaste loppen, sedan bredda resten.
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
    num_picks: int  # antal hästar att ta med
    picks: list[int] = field(default_factory=list)  # spärnummer i ordning
    confidence: float = 0.0  # 0-100, hur säkra vi är
    upset_risk: float = 0.0  # 0-100 från analysen
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
        """Beräkna totalt antal systemrader."""
        rows = 1
        for leg in self.legs:
            rows *= leg.num_picks
        self.total_rows = rows
        self.total_cost = rows * self.row_price
        return rows

    def summary(self) -> str:
        """Mänskligt läsbar systemsammanfattning."""
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


# ── Empiriska strategikonfigurationer ──────────────────────────────────────

# Optimala konfigurationer baserade på 144 historiska V75/V85-omgångar.
# Format: (n_spikes, rest_width, strategy_label)
OPTIMAL_CONFIGS: dict[int, tuple[int, int, str]] = {
    # budget → (n_spikes, rest_width, label)
    100:  (3, 4, "Empirisk optimal — 100kr"),
    200:  (3, 4, "Empirisk optimal — 200kr"),
    500:  (3, 5, "Empirisk optimal — 500kr"),
    1000: (3, 6, "Empirisk optimal — 1000kr"),
}


def _compute_upset_risk(race: Race) -> float:
    """Beräkna skrällrisk för ett lopp (0-100).

    Kombinerar analysens upset_risk med egna signaler.
    Lägre = tryggare = bättre spike-kandidat.
    """
    entries = race.active_entries
    if not entries:
        return 80.0

    risk = 0.0

    # Bas: modellens upset_risk
    risk += race.upset_risk * 0.40

    # Stolopp/storlopp/final
    race_type_lower = (race.race_type or "").lower()
    race_name_lower = (race.race_name or "").lower()
    is_stolopp = any(
        word in race_type_lower or word in race_name_lower
        for word in ("stolopp", "storlopp", "stl", "final", "milen",
                     "derby", "grand", "elitlopp", "kriterium", "oaks")
    )
    if is_stolopp:
        risk += 18.0

    # Voltstart
    if race.start_method == StartMethod.VOLT:
        risk += 10.0 if race.num_starters < 12 else 15.0

    # Spårtrappa (flera distanser)
    distances = set(e.distance for e in entries if e.distance > 0)
    if len(distances) > 1:
        risk += 9.0

    # Gap-analys — litet gap topp 1→2 = öppet
    sorted_entries = sorted(entries, key=lambda e: e.super_score, reverse=True)
    scores = [e.super_score for e in sorted_entries]
    if len(scores) >= 2:
        gap_1_2 = scores[0] - scores[1]
        if gap_1_2 < 5:
            risk += 15.0
        elif gap_1_2 < 10:
            risk += 8.0

    # Ingen tydlig marknadsfavorit
    max_bet = max((e.bet_percentage or 0 for e in entries), default=0)
    if 0 < max_bet < 0.20:
        risk += 12.0
    elif 0 < max_bet < 0.25:
        risk += 6.0

    # Favorit från bakspår
    if sorted_entries and sorted_entries[0].post_position >= 8:
        risk += 10.0

    # Stort fält
    if race.num_starters >= 14:
        risk += 8.0
    elif race.num_starters >= 12:
        risk += 4.0

    return min(100.0, risk)


def _rank_legs_for_spiking(game_round: GameRound) -> list[tuple[Race, float]]:
    """Ranka loppen efter lämplighet för spikning.

    Empiriskt bäst: spika de med LÄGST upset_risk (upset_low).
    Returnerar (race, upset_risk) sorterat stigande (bäst spik först).
    """
    ranked = []
    for race in game_round.races:
        risk = _compute_upset_risk(race)
        ranked.append((race, risk))

    # Sortera: lägst risk först = bäst spike-kandidat
    ranked.sort(key=lambda x: x[1])
    return ranked


def build_system(
    game_round: GameRound,
    budget: float = 200.0,
    row_price: Optional[float] = None,
    strategy: str = "optimal",
) -> SystemPlan:
    """Bygg ett empiriskt optimerat system för en spelomgång.

    Empirisk metod (baserad på 144 V75/V85-omgångar):
    1. Ranka alla lopp efter skrällrisk (lägst = tryggast)
    2. Spika de 3 tryggaste (empiriskt optimalt)
    3. Bred i resten (4-6 val beroende på budget)
    4. Budgetoptimera: minska bredaste om för dyrt

    Strategier:
    - "optimal": Empiriskt bäst ROI per budget (3S + upset_low)
    - "hitrate": Maximera hitrate (3S + rest6, 1000kr budget)
    - "aggressive": Fler spikar, lägre kostnad (4S + rest3)
    - "safe": Färre spikar, bredare (2S + rest5)
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    num_races = len(game_round.races)

    # Välj konfiguration baserat på strategi
    if strategy == "hitrate":
        n_spikes = 3
        rest_width = 6
        label = "Max hitrate — 3S+rest6"
    elif strategy == "aggressive":
        n_spikes = min(4, num_races - 2)
        rest_width = 3
        label = "Aggressiv — 4S+rest3"
    elif strategy == "safe":
        n_spikes = 2
        rest_width = 5
        label = "Säker — 2S+rest5"
    else:
        # Optimal: välj baserat på budget
        config = OPTIMAL_CONFIGS.get(
            budget,
            # Närmaste budget
            OPTIMAL_CONFIGS[min(OPTIMAL_CONFIGS.keys(),
                                key=lambda b: abs(b - budget))]
        )
        n_spikes, rest_width, label = config

    # Begränsa spikar till max hälften av loppen
    n_spikes = min(n_spikes, num_races // 2 + 1)

    # Ranka lopp för spikning (upset_low = empiriskt bäst)
    ranked = _rank_legs_for_spiking(game_round)

    # Bygg leg-tilldelningar
    spike_races = set(id(race) for race, _ in ranked[:n_spikes])
    legs: list[LegAssignment] = []

    for race, risk in ranked:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        all_picks = [e.post_position for e in sorted_entries]

        if id(race) in spike_races:
            # Spik — modellens #1
            legs.append(LegAssignment(
                race_number=race.race_number,
                leg_type="spik",
                num_picks=1,
                picks=all_picks[:1],
                confidence=min(95, 50 + (ranked[0][1] - risk) * 2 if ranked else 70),
                upset_risk=risk,
                reasoning=f"Spik (låg risk {risk:.0f})",
            ))
        else:
            # Bred — ta rest_width val
            width = min(rest_width, len(all_picks))
            leg_type = "kort" if width <= 2 else "medel" if width == 3 else "bred"
            legs.append(LegAssignment(
                race_number=race.race_number,
                leg_type=leg_type,
                num_picks=width,
                picks=all_picks[:width],
                confidence=max(15, 40 - risk * 0.3),
                upset_risk=risk,
                reasoning=f"Bred {width} val (risk {risk:.0f})",
            ))

    # Bygg plan
    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=label,
    )
    plan.calc_rows()

    # ── Budgetoptimering ────────────────────────────────────────────
    # Om för dyrt: minska bredaste non-spike-benen
    attempts = 0
    while plan.total_cost > budget and attempts < 20:
        non_spike = [l for l in legs if l.leg_type != "spik"]
        if not non_spike:
            break
        widest = max(non_spike, key=lambda l: l.num_picks)
        if widest.num_picks <= 2:
            # Om vi inte kan minska mer: gör till spik
            widest.leg_type = "spik"
            widest.num_picks = 1
            widest.picks = widest.picks[:1]
        else:
            widest.num_picks -= 1
            widest.picks = widest.picks[:widest.num_picks]
            if widest.num_picks == 2:
                widest.leg_type = "kort"
            elif widest.num_picks == 3:
                widest.leg_type = "medel"
        plan.calc_rows()
        attempts += 1

    # Om rejält under budget (< 40% utnyttjat): expandera
    while plan.total_cost < budget * 0.40 and attempts < 30:
        candidates = [l for l in legs if l.leg_type != "spik" and l.num_picks < 7]
        if not candidates:
            break
        # Expandera det med högst skrällrisk
        expandable = max(candidates, key=lambda l: l.upset_risk)
        expandable.num_picks += 1
        expandable.leg_type = "bred"
        # Hämta alla picks för det loppet
        race = next(
            (r for r in game_round.races if r.race_number == expandable.race_number),
            None,
        )
        if race:
            all_entries = sorted(
                race.active_entries, key=lambda e: e.super_score, reverse=True
            )
            expandable.picks = [e.post_position for e in all_entries][
                : expandable.num_picks
            ]
        plan.calc_rows()
        attempts += 1

    plan.calc_rows()
    return plan


def build_multiple_systems(
    game_round: GameRound,
    budgets: list[float] | None = None,
) -> list[SystemPlan]:
    """Bygg system vid flera budgetnivåer med optimala strategier.

    Returnerar en lista med planer för jämförelse.
    Alla använder den empiriskt bästa metoden (upset_low spiking).
    """
    if budgets is None:
        budgets = [100, 200, 500]

    plans = []

    for budget in budgets:
        # Empiriskt optimal strategi per budget
        plan = build_system(game_round, budget=budget, strategy="optimal")
        plans.append(plan)

    # Lägg till hitrate-varianten vid högsta budget
    max_budget = max(budgets)
    hitrate_plan = build_system(game_round, budget=max_budget, strategy="hitrate")
    plans.append(hitrate_plan)

    return plans
