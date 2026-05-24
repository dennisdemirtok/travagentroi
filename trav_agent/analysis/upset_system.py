"""Upset-targeting system builder — siktar på 25-100k utdelning.

Dennis: "jag vill att vi ska vinna mellan 25-100 000 kr...
         modellen ska träffa inom den ramen istället för att
         försöka träffa när favoriterna vinner."

═══ DATA-DRIVEN INSIKTER (2 206 omgångar, 15 255 lopp) ═══

NYCKELINSIKT: Målzonen (25-100k) kräver exakt 1-2 upsets per omgång.

V86 (259 omg): Målzon kräver snitt 1.9 upsets
  0 upsets → median 2,566 kr  (under målet)
  1 upset  → median 10,960 kr (under målet)
  2 upsets → median 87,315 kr ★ MÅLZON
  3 upsets → median 320,562 kr (över målet)

V75 (638 omg): Målzon kräver snitt 1.9 upsets
  1 upset  → median 9,279 kr
  2 upsets → median 73,262 kr ★ MÅLZON

V64 (1 085 omg): Målzon (5-20k) kräver snitt 1.5 upsets
  1 upset  → median 3,919 kr ★ MÅLZON
  2 upsets → median 20,143 kr ★ MÅLZON

PROFILERING AV MÅLZONSOMGÅNGAR:
  Snitt vinnare-streck: 24-26% (vs 30-32% för låga utdelningar)
  Snitt minsta vinnare: 4-5% (vs 8-10% för låga)
  1-2 vinnare med <10% streck per omgång

UPSET-PROFIL:
  Stora fält: 30.1% upsets (vs 16.7% i ≤8 startande)
  Bakspår: Upsets kommer oftare från spår 8-12
  Framspår: Sweet-spot vinnare (5-15%) kommer 49.6% från spår 1-4

STRATEGI: "Contrarian Spike" (chansspik + upset-gardering)
  1. Identifiera 2-3 lopp med hög skrällrisk (stora fält, tight topp)
  2. I dessa: välj en CHANSSPIK (5-15% streck, framspår)
  3. Gardera bredare i dessa lopp (5-6 val inkl. outsiders)
  4. I övriga lopp: standardspik/gardering som vanligt
  5. Mål: 1-2 av chansspikarna träffar → 25-100k
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Optional

from ..data.models import GameRound, Race, RaceEntry, StartMethod
from .system_builder import (
    LegAssignment, SystemPlan, predict_difficulty,
    _coverage_prob, _picks_to_type, _build_reasoning,
)


# ═══════════════════════════════════════════════════════════════════════════
# Payout target zones per game type
# ═══════════════════════════════════════════════════════════════════════════

PAYOUT_TARGETS: dict[str, tuple[float, float]] = {
    "V64": (5_000, 20_000),
    "V65": (5_000, 20_000),
    "V75": (25_000, 100_000),
    "V85": (25_000, 100_000),
    "V86": (25_000, 100_000),
    "GS75": (25_000, 100_000),
}

# Target upsets per game type (from empirical data)
TARGET_UPSETS: dict[str, tuple[int, int]] = {
    "V64": (1, 2),   # 1-2 upsets → median 5-20k
    "V65": (1, 2),
    "V75": (1, 2),   # 2 upsets → median 73k ★
    "V85": (1, 2),   # 2 upsets → median 141k
    "V86": (1, 2),   # 2 upsets → median 87k ★
    "GS75": (1, 2),  # 1-2 upsets → median 16-136k
}


@dataclass
class UpsetCandidate:
    """En häst identifierad som potentiell chansspik."""
    entry: RaceEntry
    post_position: int
    bet_pct: float
    super_score: float
    upset_score: float  # 0-100: hur bra chansspik
    reasons: list[str] = field(default_factory=list)


def _is_summer() -> bool:
    """April-September = sommar (framspår viktigare)."""
    return Date.today().month in [4, 5, 6, 7, 8, 9]


def _upset_potential(race: Race) -> float:
    """Beräkna loppets skrällpotential (0-100).

    Hög = hög sannolikhet att en outsider vinner.
    Baserat på empiriska fynd:
    - Stora fält: 30.1% upsets (vs 16.7% i ≤8)
    - Volt: +oförutsägbarhet
    - Tight top-5: alla nära varandra
    - Favorit med hög streck men tveksam form
    """
    entries = race.active_entries
    if len(entries) < 5:
        return 10.0

    potential = 0.0

    # 1. Fältstorlek (starkaste signalen: 30% upsets i 13+ vs 17% i ≤8)
    n = len(entries)
    if n >= 14:
        potential += 35.0
    elif n >= 12:
        potential += 28.0
    elif n >= 10:
        potential += 18.0
    elif n >= 8:
        potential += 10.0
    else:
        potential += 3.0

    # 2. Top-5 tightness — liten skillnad = svårt att veta vem som vinner
    sorted_e = sorted(entries, key=lambda e: e.super_score, reverse=True)
    scores = [e.super_score for e in sorted_e]

    if len(scores) >= 5:
        top5_spread = scores[0] - scores[4]
        if top5_spread < 10:
            potential += 25.0
        elif top5_spread < 15:
            potential += 18.0
        elif top5_spread < 20:
            potential += 10.0
        elif top5_spread < 25:
            potential += 5.0

    # 3. Gap rank 1→2 (liten gap = favoriten inte så säker)
    if len(scores) >= 2:
        gap = scores[0] - scores[1]
        if gap < 3:
            potential += 15.0
        elif gap < 5:
            potential += 10.0
        elif gap < 8:
            potential += 5.0

    # 4. Voltstart
    if race.start_method == StartMethod.VOLT:
        potential += 8.0

    # 5. Spårtrappa / tillägg
    distances = set(e.distance for e in entries if e.distance > 0)
    if len(distances) > 2:
        potential += 8.0
    elif len(distances) > 1:
        potential += 4.0

    # 6. Marknadsfavoriten inte modellens #1
    market_fav = max(entries, key=lambda e: e.bet_percentage or 0)
    if market_fav.rank and market_fav.rank >= 3:
        potential += 12.0
    elif market_fav.rank and market_fav.rank >= 2:
        potential += 6.0

    return min(100.0, potential)


def _find_chansspik_candidates(race: Race) -> list[UpsetCandidate]:
    """Hitta chansspik-kandidater i ett lopp.

    Chansspik = häst med 5-15% streck som har edge:
    - Framspår (1-4), speciellt på sommaren
    - Hög modellscore relativt streck (undervärdering)
    - Bra form (hög composite vs bet%)
    """
    candidates = []
    entries = race.active_entries
    summer = _is_summer()

    for entry in entries:
        bet_pct = entry.bet_percentage or 0
        if bet_pct <= 0:
            continue

        # Primärt intresse: 3-18% streck (den breda chansspik-zonen)
        if not (0.03 <= bet_pct <= 0.18):
            continue

        upset_score = 0.0
        reasons = []

        # 1. Sweet spot: 5-15% streck = bästa chansspik-zonen
        if 0.05 <= bet_pct <= 0.15:
            upset_score += 25.0
            reasons.append(f"sweet spot {bet_pct*100:.0f}%")
        elif 0.03 <= bet_pct < 0.05:
            upset_score += 15.0
            reasons.append(f"outsider {bet_pct*100:.0f}%")
        else:
            upset_score += 10.0
            reasons.append(f"halvfavorit {bet_pct*100:.0f}%")

        # 2. Modell-edge: composite score hög relativt streck
        # Value index = composite / (bet% * 100)
        value_idx = entry.composite_score / max(1, bet_pct * 100)
        if value_idx >= 5.0:
            upset_score += 25.0
            reasons.append(f"stark value ({value_idx:.1f}x)")
        elif value_idx >= 3.0:
            upset_score += 18.0
            reasons.append(f"value ({value_idx:.1f}x)")
        elif value_idx >= 2.0:
            upset_score += 10.0
            reasons.append(f"ok value ({value_idx:.1f}x)")

        # 3. Startspår — framspår = bättre chansspik
        pp = entry.post_position
        if pp <= 3:
            bonus = 20.0 if summer else 15.0
            upset_score += bonus
            reasons.append(f"framspår {pp}" + (" (sommar)" if summer else ""))
        elif pp <= 5:
            bonus = 12.0 if summer else 10.0
            upset_score += bonus
            reasons.append(f"mittspår {pp}")
        elif pp >= 9:
            upset_score -= 5.0  # Penalty for far outside
            reasons.append(f"bakspår {pp}")

        # 4. Modellranking — om modellen har högt förtroende
        if entry.rank and entry.rank <= 3:
            upset_score += 15.0
            reasons.append(f"modell rank {entry.rank}")
        elif entry.rank and entry.rank <= 5:
            upset_score += 8.0
            reasons.append(f"modell rank {entry.rank}")

        # 5. Volt + spår 1 = extra stark signal
        if race.start_method == StartMethod.VOLT and pp == 1:
            upset_score += 10.0
            reasons.append("volt spår 1")

        # 6. Form signals
        fs = entry.factor_scores
        time_s = fs.get("time_analysis", 50.0)
        driver_s = fs.get("driver_class", 50.0)
        post_s = fs.get("post_position", 50.0)
        age_s = fs.get("age", 50.0)

        strong_factors = sum([
            time_s >= 65,
            driver_s >= 65,
            post_s >= 60,
            age_s >= 60,
        ])
        if strong_factors >= 2:
            upset_score += 10.0
            reasons.append(f"{strong_factors} starka faktorer")

        if upset_score >= 30:  # Minimum threshold
            candidates.append(UpsetCandidate(
                entry=entry,
                post_position=pp,
                bet_pct=bet_pct,
                super_score=entry.super_score,
                upset_score=min(100, upset_score),
                reasons=reasons,
            ))

    # Sort by upset_score descending
    candidates.sort(key=lambda c: c.upset_score, reverse=True)
    return candidates


def build_upset_system(
    game_round: GameRound,
    budget: float = 300.0,
    row_price: Optional[float] = None,
) -> SystemPlan:
    """Bygg contrarian system som siktar på 25-100k utdelning.

    Strategi:
    1. Identifiera 2-3 lopp med HÖG skrällpotential
    2. I dessa lopp: bredare gardering (5-6 val) inkl. chansspik-kandidater
    3. I de LÄTTASTE loppen: spik som vanligt (spara rader)
    4. Mål: fånga 1-2 upsets per omgång = 25-100k

    Skillnad vs spike_easiest_rest4:
    - spike_easiest hittar FAVORITER bättre → låga utdelningar (240-1300kr)
    - upset_system hittar UPSETS bättre → höga utdelningar (25-100k)
    - Lägre hit rate men mycket högre genomsnittlig utdelning
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    max_rows = int(budget / row_price)
    num_races = len(game_round.races)

    # ── Steg 1: Analysera varje lopp ────────────────────────────────
    race_data = []
    for race in game_round.races:
        diff = predict_difficulty(race)
        upset_pot = _upset_potential(race)
        chansspik = _find_chansspik_candidates(race)
        n_starters = len(race.active_entries)

        race_data.append({
            "race": race,
            "difficulty": diff,
            "upset_potential": upset_pot,
            "chansspik": chansspik,
            "n_starters": n_starters,
        })

    # ── Steg 2: Identifiera upset-lopp och spike-lopp ───────────────
    # Sort by upset potential (highest first)
    race_by_upset = sorted(range(num_races), key=lambda i: race_data[i]["upset_potential"], reverse=True)

    # Top 2-3 highest upset potential = upset races (go wider)
    target_lo, target_hi = TARGET_UPSETS.get(game_round.game_type, (1, 2))
    n_upset_races = target_hi  # 2 races with wider coverage

    # Easiest non-upset race = spike race (save rows)
    upset_race_indices = set(race_by_upset[:n_upset_races])

    # Among remaining, find the easiest to spike
    remaining = [i for i in range(num_races) if i not in upset_race_indices]
    remaining.sort(key=lambda i: race_data[i]["difficulty"])

    # Spike 1-2 of the easiest remaining races
    spike_indices = set()
    if remaining:
        spike_indices.add(remaining[0])
        if len(remaining) >= 4 and max_rows >= 400:
            # If budget allows, spike 2
            spike_indices.add(remaining[1])

    # ── Steg 3: Allokera picks per lopp ─────────────────────────────
    picks_per_race = []

    for i in range(num_races):
        rd = race_data[i]
        ns = rd["n_starters"]

        if i in spike_indices:
            # Easy race → spike (1 pick, save rows for upset races)
            picks_per_race.append(1)
        elif i in upset_race_indices:
            # Upset race → wider coverage (5-6 picks)
            base = 5 if ns >= 10 else 4
            picks_per_race.append(min(base, ns))
        else:
            # Normal race → standard coverage (3-4 picks)
            picks_per_race.append(min(4, ns))

    # ── Steg 4: Trimma till budget ──────────────────────────────────
    def total_rows():
        r = 1
        for p in picks_per_race:
            r *= p
        return r

    # First: expand upset races if budget allows
    while total_rows() < max_rows * 0.6:
        # Find upset race with lowest picks that can expand
        expandable = [
            i for i in upset_race_indices
            if picks_per_race[i] < min(6, race_data[i]["n_starters"])
        ]
        if not expandable:
            # Try expanding normal races
            expandable = [
                i for i in range(num_races)
                if i not in spike_indices
                and picks_per_race[i] < min(5, race_data[i]["n_starters"])
            ]
        if not expandable:
            break

        # Expand the one with most upset potential
        best = max(expandable, key=lambda i: race_data[i]["upset_potential"])
        picks_per_race[best] += 1

    # Trim if over budget
    while total_rows() > max_rows:
        # Reduce the widest non-spike race (prefer reducing low-upset races)
        reducible = [
            (i, picks_per_race[i])
            for i in range(num_races)
            if picks_per_race[i] > 1 and i not in spike_indices
        ]
        if not reducible:
            # Even reduce spike races if necessary
            reducible = [
                (i, picks_per_race[i])
                for i in range(num_races)
                if picks_per_race[i] > 1
            ]
        if not reducible:
            break

        # Reduce the widest race with LOWEST upset potential
        reducible.sort(key=lambda x: (-x[1], race_data[x[0]]["upset_potential"]))
        picks_per_race[reducible[0][0]] -= 1

    # ── Steg 5: Välj hästar med chansspik-prioritering ──────────────
    legs: list[LegAssignment] = []
    total_log_prob = 0.0

    for i, race in enumerate(game_round.races):
        rd = race_data[i]
        n_picks = picks_per_race[i]
        diff = rd["difficulty"]
        chansspik = rd["chansspik"]

        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )

        if i in upset_race_indices and chansspik and n_picks >= 3:
            # UPSET RACE: Include chansspik candidates in picks
            # Start with model's top picks
            selected_entries = list(sorted_entries[:n_picks])

            # Ensure top chansspik candidate is included
            top_chansspik = chansspik[0]
            chansspik_entry = top_chansspik.entry

            if chansspik_entry not in selected_entries:
                # Replace the lowest-ranked model pick with chansspik
                selected_entries[-1] = chansspik_entry

            # If we have a second strong chansspik, include it too
            if len(chansspik) >= 2 and n_picks >= 5:
                second = chansspik[1]
                if second.entry not in selected_entries and second.upset_score >= 45:
                    selected_entries[-2] = second.entry

            selected = [e.post_position for e in selected_entries]
        else:
            # NORMAL/SPIKE: Standard model picks
            selected = [e.post_position for e in sorted_entries[:n_picks]]

        # Coverage probability
        cov_prob = _coverage_prob(diff, n_picks)
        total_log_prob += math.log(max(cov_prob, 1e-10))

        confidence = min(95, max(10, cov_prob * 100))
        leg_type = _picks_to_type(n_picks)

        # Build reasoning
        is_upset_race = i in upset_race_indices
        is_spike_race = i in spike_indices
        if is_upset_race:
            upset_note = f"CHANSSPIK-lopp (potential={rd['upset_potential']:.0f})"
            if chansspik:
                upset_note += f", bästa chansspik: {chansspik[0].entry.horse.name} ({chansspik[0].upset_score:.0f}p)"
            reasoning = upset_note
        elif is_spike_race:
            reasoning = f"SPIK-lopp (diff={diff:.0f}, lättast)"
        else:
            reasoning = _build_reasoning(race, diff, n_picks, cov_prob)

        legs.append(LegAssignment(
            race_number=race.race_number,
            leg_type=leg_type,
            num_picks=n_picks,
            picks=selected,
            confidence=confidence,
            upset_risk=rd["upset_potential"],
            difficulty=diff,
            reasoning=reasoning,
        ))

    predicted_prob = math.exp(total_log_prob)

    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=f"Chansspik — {budget:.0f}kr (mål: 25-100k)",
        predicted_hit_prob=predicted_prob,
    )
    plan.calc_rows()

    return plan


def build_upset_systems(
    game_round: GameRound,
    budgets: list[float] | None = None,
) -> list[SystemPlan]:
    """Bygg chansspik-system vid flera budgetnivåer."""
    if budgets is None:
        budgets = [300, 500, 750]

    plans = []
    for budget in budgets:
        plan = build_upset_system(game_round, budget=budget)
        plans.append(plan)
    return plans


def analyze_upset_round(game_round: GameRound) -> dict:
    """Analysera en omgång för upset-potential.

    Returnerar en sammanfattning med:
    - Skrällpotential per lopp
    - Chansspik-kandidater
    - Rekommenderat antal upsets att jaga
    - Estimerad utdelningszon
    """
    num_races = len(game_round.races)
    race_analysis = []

    for race in game_round.races:
        diff = predict_difficulty(race)
        upset_pot = _upset_potential(race)
        chansspik = _find_chansspik_candidates(race)

        race_analysis.append({
            "race_number": race.race_number,
            "track": race.track_name,
            "difficulty": diff,
            "upset_potential": upset_pot,
            "n_starters": len(race.active_entries),
            "chansspik_candidates": [
                {
                    "horse": c.entry.horse.name,
                    "post": c.post_position,
                    "bet_pct": c.bet_pct,
                    "upset_score": c.upset_score,
                    "reasons": c.reasons,
                }
                for c in chansspik[:3]  # Top 3 per race
            ],
        })

    # Estimate payout zone based on upset count
    high_upset_races = sum(1 for r in race_analysis if r["upset_potential"] >= 50)
    medium_upset_races = sum(1 for r in race_analysis if 30 <= r["upset_potential"] < 50)

    target = PAYOUT_TARGETS.get(game_round.game_type, (25_000, 100_000))

    if high_upset_races >= 3:
        expected_payout = "Hög skrällrisk → 50-500k potential"
    elif high_upset_races >= 2:
        expected_payout = f"Bra för målzonen → {target[0]//1000}-{target[1]//1000}k potential"
    elif high_upset_races >= 1 and medium_upset_races >= 2:
        expected_payout = f"Medel → {target[0]//1000}-{target[1]//1000}k med rätt chansspik"
    else:
        expected_payout = "Låg skrällrisk → troligen låg utdelning (<10k)"

    return {
        "game_type": game_round.game_type,
        "date": str(game_round.round_date),
        "race_analysis": sorted(race_analysis, key=lambda r: r["upset_potential"], reverse=True),
        "high_upset_races": high_upset_races,
        "medium_upset_races": medium_upset_races,
        "expected_payout": expected_payout,
        "recommendation": f"Jaga {min(high_upset_races, 2)} chansspikar i de svåraste loppen",
    }
