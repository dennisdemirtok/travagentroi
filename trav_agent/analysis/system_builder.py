"""Intelligent systembyggare — Dennis's spelmetod.

Dennis: "ofta nar jag har vunnit stort sa har jag behövt spika 1-2-3
lopp for att kanske ga kort 1-2 lopp och sedan gardera lite bredare
for att hitta skrallarna. en indikation pa skralllopp brukar ju
vara stolopp eller lopp med voltstart eller flera volter, eller
nar det inte finns nagon starka favoriter i loppet eller favoriter
fran bakspar."

Metod:
1. Analysera varje avdelning for skrallrisk
2. Klassificera: spik-avd, kort-avd, bred-avd
3. Tilldela 1-3 spikar, 1-2 korta, resten breda
4. Optimera systemkostnad baserat pa budget
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..data.models import GameRound, Race, RaceEntry, StartMethod


@dataclass
class LegAssignment:
    """Tilldelning for en avdelning i systemet."""
    race_number: int
    leg_type: str  # "spik", "kort", "medel", "bred"
    num_picks: int  # antal hastar att ta med
    picks: list[int] = field(default_factory=list)  # sparnum i ordning
    confidence: float = 0.0  # 0-100, hur sakra vi ar
    upset_risk: float = 0.0  # 0-100 fran analysen
    reasoning: str = ""

    @property
    def is_spike(self) -> bool:
        return self.leg_type == "spik"


@dataclass
class SystemPlan:
    """Komplett systemplan for en omgang."""
    game_type: str
    round_date: str
    legs: list[LegAssignment]
    total_rows: int = 0
    row_price: float = 0.50
    total_cost: float = 0.0
    budget: float = 200.0  # Default budget
    strategy_name: str = ""

    @property
    def num_spikes(self) -> int:
        return sum(1 for l in self.legs if l.leg_type == "spik")

    @property
    def num_short(self) -> int:
        return sum(1 for l in self.legs if l.leg_type == "kort")

    @property
    def num_wide(self) -> int:
        return sum(1 for l in self.legs if l.leg_type in ("medel", "bred"))

    def calc_rows(self) -> int:
        """Calculate total number of system rows."""
        rows = 1
        for leg in self.legs:
            rows *= leg.num_picks
        self.total_rows = rows
        self.total_cost = rows * self.row_price
        return rows

    def summary(self) -> str:
        """Human-readable system summary."""
        parts = []
        for leg in sorted(self.legs, key=lambda l: l.race_number):
            picks_str = ",".join(str(p) for p in leg.picks[:leg.num_picks])
            parts.append(f"Avd {leg.race_number}: {leg.leg_type} ({leg.num_picks}) [{picks_str}]")

        return (
            f"{self.strategy_name}\n"
            f"{'=' * 50}\n"
            + "\n".join(parts)
            + f"\n{'=' * 50}\n"
            f"Rader: {self.total_rows} | Kostnad: {self.total_cost:.0f} kr\n"
            f"Spikar: {self.num_spikes} | Korta: {self.num_short} | Breda: {self.num_wide}"
        )


def _upset_risk_indicators(race: Race) -> dict[str, float]:
    """Detect upset risk indicators for a race.

    Dennis's signals:
    - stolopp (big races) = higher upset chance
    - voltstart = more chaos, especially multiple volts
    - no strong favorites = open race
    - favorites from back posts = disadvantage

    Returns dict of signal_name -> contribution (0-100).
    """
    signals = {}
    entries = race.active_entries
    if not entries:
        return signals

    # ── 1. Stolopp/storlopp detection ───────────────────────────────
    race_type_lower = (race.race_type or "").lower()
    race_name_lower = (race.race_name or "").lower()
    is_stolopp = any(word in race_type_lower or word in race_name_lower
                     for word in ["stolopp", "storlopp", "stl", "final",
                                  "milen", "derby", "grand", "elitlopp",
                                  "kriterium", "oaks"])
    if is_stolopp:
        signals["stolopp"] = 30.0

    # ── 2. Voltstart ────────────────────────────────────────────────
    if race.start_method == StartMethod.VOLT:
        # Volt adds chaos — especially with large fields
        volt_risk = 15.0
        if race.num_starters >= 12:
            volt_risk = 25.0
        elif race.num_starters >= 10:
            volt_risk = 20.0
        signals["voltstart"] = volt_risk

    # ── 3. Multiple distances (volt with staggered starts) ──────────
    # Check if entries have different distances (spårtrappa)
    distances = set(e.distance for e in entries if e.distance > 0)
    if len(distances) > 1:
        signals["spårtrappa"] = 15.0

    # ── 4. No strong favorite ───────────────────────────────────────
    # Use both model scores and market data
    sorted_by_score = sorted(entries, key=lambda e: e.super_score, reverse=True)
    if len(sorted_by_score) >= 2:
        gap = sorted_by_score[0].super_score - sorted_by_score[1].super_score
        if gap < 5:
            signals["inget_gap"] = 25.0  # Very tight at top
        elif gap < 10:
            signals["litet_gap"] = 15.0  # Somewhat tight

    # Market: no horse > 25% streck = open race
    max_bet = max((e.bet_percentage or 0 for e in entries), default=0)
    if 0 < max_bet < 0.20:
        signals["ingen_favorit_marknad"] = 20.0
    elif 0 < max_bet < 0.25:
        signals["svag_favorit_marknad"] = 10.0

    # ── 5. Favorite from back post ──────────────────────────────────
    # Dennis: "favoriter fran bakspar" = upset indicator
    if sorted_by_score:
        fav = sorted_by_score[0]
        if fav.post_position >= 8:
            signals["favorit_bakspar"] = 18.0
        elif fav.post_position >= 6:
            signals["favorit_mittspår"] = 8.0

    # ── 6. Large field ──────────────────────────────────────────────
    if race.num_starters >= 14:
        signals["stort_falt"] = 12.0
    elif race.num_starters >= 12:
        signals["stort_falt"] = 6.0

    # ── 7. Race purse (higher purse races have more upsets) ─────────
    if race.purse >= 300000:
        signals["hog_prispott"] = 10.0
    elif race.purse >= 150000:
        signals["hog_prispott"] = 5.0

    return signals


def classify_leg(
    race: Race,
    race_number: int,
) -> LegAssignment:
    """Classify a race leg based on upset risk and confidence.

    Returns a LegAssignment with suggested leg_type and num_picks.
    """
    entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
    if not entries:
        return LegAssignment(
            race_number=race_number,
            leg_type="bred",
            num_picks=5,
            confidence=0,
            upset_risk=50,
            reasoning="Inga starter"
        )

    # Get upset signals
    upset_signals = _upset_risk_indicators(race)
    upset_total = sum(upset_signals.values())

    # Combine with model's upset_risk
    combined_risk = (race.upset_risk * 0.4 + min(100, upset_total) * 0.6)

    # Gap analysis
    scores = [e.super_score for e in entries]
    gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 0
    gap_1_3 = scores[0] - scores[2] if len(scores) >= 3 else 0

    # All picks in order of model score
    all_picks = [e.post_position for e in entries]

    # ── Classify ────────────────────────────────────────────────────

    # SPIK: high confidence, big gap, low upset risk
    if gap_1_2 >= 12 and combined_risk < 30:
        return LegAssignment(
            race_number=race_number,
            leg_type="spik",
            num_picks=1,
            picks=all_picks[:1],
            confidence=min(95, 50 + gap_1_2 * 2),
            upset_risk=combined_risk,
            reasoning=f"Stark favorit (gap {gap_1_2:.0f}p, risk {combined_risk:.0f})"
        )

    # KORT (2 val): decent gap, moderate risk
    if gap_1_3 >= 10 and combined_risk < 45:
        return LegAssignment(
            race_number=race_number,
            leg_type="kort",
            num_picks=2,
            picks=all_picks[:2],
            confidence=min(85, 40 + gap_1_3 * 1.5),
            upset_risk=combined_risk,
            reasoning=f"Tva tydliga (gap {gap_1_3:.0f}p, risk {combined_risk:.0f})"
        )

    # MEDEL (3-4 val): some separation, moderate risk
    if gap_1_3 >= 6 and combined_risk < 55:
        return LegAssignment(
            race_number=race_number,
            leg_type="medel",
            num_picks=3,
            picks=all_picks[:3],
            confidence=max(30, 35 + gap_1_3),
            upset_risk=combined_risk,
            reasoning=f"Nagon separation (gap {gap_1_3:.0f}p, risk {combined_risk:.0f})"
        )

    # BRED (5+ val): open race, high upset risk
    upset_reasons = ", ".join(upset_signals.keys()) if upset_signals else "tight field"
    num_picks = 5 if combined_risk > 60 else 4
    return LegAssignment(
        race_number=race_number,
        leg_type="bred",
        num_picks=num_picks,
        picks=all_picks[:num_picks],
        confidence=max(15, 30 - combined_risk * 0.2),
        upset_risk=combined_risk,
        reasoning=f"Oppet lopp ({upset_reasons}), risk {combined_risk:.0f}"
    )


def build_system(
    game_round: GameRound,
    budget: float = 200.0,
    row_price: Optional[float] = None,
    strategy: str = "dennis",
) -> SystemPlan:
    """Build an intelligent system for a game round.

    Dennis's method:
    - 1-3 spikes (safest legs)
    - 1-2 short (2 picks)
    - Rest: wide (3-5 picks for upset legs)
    - Total cost within budget

    Strategy options:
    - "dennis": Dennis's balanced approach (1-3S, 1-2K, rest B)
    - "spik": More spikes, lower cost, higher risk
    - "bred": Fewer spikes, higher cost, catches more upsets
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    # Classify all legs
    legs: list[LegAssignment] = []
    for race in game_round.races:
        leg = classify_leg(race, race.race_number)
        legs.append(leg)

    # Sort by confidence (highest confidence = best spike candidates)
    legs.sort(key=lambda l: l.confidence, reverse=True)

    # Dennis's method constraints
    num_races = len(legs)
    if strategy == "dennis":
        max_spikes = min(3, num_races // 3 + 1)
        min_spikes = 1
    elif strategy == "spik":
        max_spikes = min(4, num_races // 2)
        min_spikes = 2
    elif strategy == "bred":
        max_spikes = 2
        min_spikes = 0
    else:
        max_spikes = 3
        min_spikes = 1

    # Force spike classification on the highest-confidence legs
    spike_count = 0
    for leg in legs:
        if leg.leg_type == "spik" and spike_count < max_spikes:
            spike_count += 1
        elif spike_count < min_spikes and leg.confidence >= 50:
            # Promote to spike if we need more spikes
            leg.leg_type = "spik"
            leg.num_picks = 1
            leg.picks = leg.picks[:1]
            spike_count += 1

    # Calculate initial cost
    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=f"Dennis's {strategy}-strategi"
    )
    plan.calc_rows()

    # ── Budget optimization ─────────────────────────────────────────
    # If over budget: reduce widest legs first
    # If under budget: can expand some legs
    attempts = 0
    while plan.total_cost > budget and attempts < 20:
        # Find widest non-spike leg
        widest = max(
            (l for l in legs if l.leg_type != "spik"),
            key=lambda l: l.num_picks,
            default=None,
        )
        if widest and widest.num_picks > 2:
            widest.num_picks -= 1
            widest.picks = widest.picks[:widest.num_picks]
            if widest.num_picks == 2:
                widest.leg_type = "kort"
            elif widest.num_picks == 3:
                widest.leg_type = "medel"
        elif widest and widest.num_picks == 2:
            # Desperate: promote to spike
            widest.leg_type = "spik"
            widest.num_picks = 1
            widest.picks = widest.picks[:1]
        else:
            break
        plan.calc_rows()
        attempts += 1

    # If significantly under budget (< 50% used), expand some legs
    while plan.total_cost < budget * 0.4 and attempts < 30:
        # Find narrowest non-spike, highest-upset-risk leg
        candidates = [l for l in legs if l.leg_type != "spik" and l.num_picks < 6]
        if not candidates:
            break
        # Expand highest upset risk first
        expandable = max(candidates, key=lambda l: l.upset_risk)
        expandable.num_picks += 1
        if expandable.num_picks >= 4:
            expandable.leg_type = "bred"
        elif expandable.num_picks == 3:
            expandable.leg_type = "medel"
        # Add next pick from sorted entries
        race = next(
            (r for r in game_round.races if r.race_number == expandable.race_number),
            None
        )
        if race:
            all_entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
            all_picks = [e.post_position for e in all_entries]
            expandable.picks = all_picks[:expandable.num_picks]

        plan.calc_rows()
        attempts += 1

    plan.calc_rows()  # Final calculation
    return plan


def build_multiple_systems(
    game_round: GameRound,
    budgets: list[float] = None,
) -> list[SystemPlan]:
    """Build multiple system plans at different budgets/strategies.

    Returns a list of plans for comparison.
    """
    if budgets is None:
        budgets = [100, 200, 500]

    plans = []

    for budget in budgets:
        for strategy in ["dennis", "spik", "bred"]:
            plan = build_system(game_round, budget=budget, strategy=strategy)
            plans.append(plan)

    return plans
