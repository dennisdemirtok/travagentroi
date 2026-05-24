"""Systembyggare v9 — spike_easiest + rest4, den ENDA bevisade strategin.

Testat: ultimate_system_optimizer.py, 336 omgångar, 2 182 lopp, 40+ strategier.

Dennis: "en modell — den bästa, inget annat sätt att bygga systemet är lönt.
         jag vinner hellre 50 000-250 000kr oftare."

SLUTGILTIG ANALYS (ultimate_system_optimizer.py, 336 omgångar):

Alla 40+ strategier testade: fixed, gap-based, score-adaptive,
value-spike, greedy, hybrid, edge-baserad. Resultat:

═══ DEN OPTIMALA STRATEGIN: spike_easiest + rest4 @ 300kr ═══

  Spike det LÄTTASTE loppet (lägst difficulty) → 1 val
  Alla andra lopp → 4 val (topp 4 enligt modellen)
  Budget: 300kr (600 rader)

  RESULTAT (336 omgångar):
  - 18 hits (5.4% hit rate — vinner var ~19:e omgång)
  - +112% ROI ★
  - +101 000 kr profit
  - Avg utdelning: 10 623 kr (2 av 18 i 50-250k-klassen)

  JÄMFÖRELSE MOT ANDRA STRATEGIER:
  Strategi              Hits   ROI   Profit   Kommentar
  spike_easiest_rest4     18  +112%  +101k    ★ BÄST TOTAL
  spike_gap_norm_rest4    15   +92%   +83k    Färre hits
  gap10_rest4             10  +166%   +77k    Bättre ROI men färre hits
  greedy_balanced         10   +42%   +34k    Adaptiv men sämre
  value_spike              5  +259%   +97k    Hög ROI men för få hits
  gap8_rest3               4  +524%   +89k    Fantastisk ROI, 1.2% hit

  VARFÖR SPIKE EASIEST VINNER:
  - Modellens top-1 är 38.4% korrekt i lättaste loppen
  - Spara rader: 1 spike × 4^6 = 4 096 → trimmas till 600
  - 4-bred gardering: 77.2% täckning i lättaste, 61.1% i svåraste
  - Hög hit rate → konsekvent vinst, ej beroende av enstaka storvinst

  SPIKE-METOD JÄMFÖRELSE (alla med rest4, 300kr):
  spike_easiest:        18 hits, +112% ROI ★
  spike_best_gap_norm:  15 hits,  +92% ROI
  spike_best_score:     16 hits,  +46% ROI
  spike_best_gap:       13 hits,  +35% ROI
  → Lättaste loppet = bästa spiken. Inte gapet, inte poängen.

  BUDGET-SKALNING:
  300kr: 18 hits, +112% ROI (BÄST)
  500kr: 20 hits,  +41% ROI (fler hits men lägre ROI)
  750kr: 25 hits,   +3% ROI (fler hits men breakeven)
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
    predicted_hit_prob: float = 0.0

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
            f"Spikar: {self.num_spikes} | Korta: {self.num_short} | Breda: {self.num_wide}\n"
            f"P(alla rätt): {self.predicted_hit_prob:.2%}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Empiriska coverage-kurvor — kalibrerade på 1 862 lopp
# ═══════════════════════════════════════════════════════════════════════════

# P(vinnaren i topp-k | svårighetskvintil)
# Kvintilgränser: [16.0, 27.0, 39.5, 53.0]
DIFFICULTY_BOUNDARIES = [16.0, 27.0, 39.5, 53.0]

# {quintile: {picks: probability}}
COVERAGE_CURVES = {
    0: {  # easiest (diff < 16)
        1: 0.376, 2: 0.538, 3: 0.665, 4: 0.765, 5: 0.851,
        6: 0.911, 7: 0.949, 8: 0.968, 9: 0.986, 10: 0.997,
        11: 0.997, 12: 1.000, 13: 1.000, 14: 1.000, 15: 1.000,
    },
    1: {  # easy (16 <= diff < 27)
        1: 0.302, 2: 0.471, 3: 0.612, 4: 0.722, 5: 0.807,
        6: 0.848, 7: 0.893, 8: 0.941, 9: 0.968, 10: 0.984,
        11: 0.992, 12: 0.997, 13: 1.000, 14: 1.000, 15: 1.000,
    },
    2: {  # medium (27 <= diff < 39.5)
        1: 0.268, 2: 0.458, 3: 0.592, 4: 0.670, 5: 0.767,
        6: 0.828, 7: 0.871, 8: 0.925, 9: 0.952, 10: 0.973,
        11: 0.984, 12: 0.992, 13: 0.997, 14: 1.000, 15: 1.000,
    },
    3: {  # hard (39.5 <= diff < 53)
        1: 0.250, 2: 0.422, 3: 0.565, 4: 0.683, 5: 0.769,
        6: 0.823, 7: 0.882, 8: 0.917, 9: 0.946, 10: 0.973,
        11: 0.984, 12: 0.989, 13: 0.995, 14: 0.997, 15: 1.000,
    },
    4: {  # hardest (diff >= 53)
        1: 0.255, 2: 0.437, 3: 0.544, 4: 0.649, 5: 0.735,
        6: 0.799, 7: 0.853, 8: 0.901, 9: 0.933, 10: 0.949,
        11: 0.968, 12: 0.984, 13: 0.992, 14: 0.997, 15: 1.000,
    },
}


def _get_quintile(difficulty: float) -> int:
    """Mappa svårighetsgrad till kvintil (0-4)."""
    for i, boundary in enumerate(DIFFICULTY_BOUNDARIES):
        if difficulty < boundary:
            return i
    return 4


def _coverage_prob(difficulty: float, picks: int) -> float:
    """P(vinnaren bland topp-k | svårighetsgrad)."""
    q = _get_quintile(difficulty)
    picks = max(1, min(picks, 15))
    return COVERAGE_CURVES[q].get(picks, 0.95)


# ═══════════════════════════════════════════════════════════════════════════
# Loppsvårighetsmodell — kalibrerad på 1 862 lopp
# ═══════════════════════════════════════════════════════════════════════════

def predict_difficulty(race: Race) -> float:
    """Beräkna loppets svårighetsgrad (0-100).

    Baserat på empiriska korrelationer med vinnarens modellranking.
    Hög svårighetsgrad = vinnaren hamnar djupt → behöver fler picks.
    """
    entries = race.active_entries
    if not entries:
        return 70.0

    difficulty = 0.0

    # Sorterade entries (behövs av flera steg nedan)
    sorted_entries = sorted(entries, key=lambda e: e.super_score, reverse=True)
    scores = [e.super_score for e in sorted_entries]

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
    # Empiriskt: volt ökar oförutsägbarheten
    # MEN: om favoriten har spår 1 i volt → lägre svårighet (14.2% vinst!)
    if race.start_method == StartMethod.VOLT:
        difficulty += 12.0
        # Kompensera om favoriten har springspår i volt
        if sorted_entries:
            fav_pp = sorted_entries[0].post_position
            if fav_pp == 1:
                difficulty -= 8.0  # Spår 1 i volt = starkt (14.2% vinst)
            elif fav_pp in (6, 7):
                difficulty -= 3.0  # Springspår, måttlig fördel

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

    # 9. Favorit bakspår — med empirisk kalibrering
    if sorted_entries and sorted_entries[0].post_position >= 8:
        if race.start_method == StartMethod.VOLT:
            difficulty += 4.0  # Andra raden i volt, men data visar att det inte är så farligt (8.0% vinst spår 8)
        else:
            difficulty += 8.0  # Ytterspår i auto = markant nackdel (7.3% spår 8)

    # 10. Modellens upset_risk
    difficulty += race.upset_risk * 0.15

    # 11. Galopphistorik hos favoriten → ökad skrällrisk
    if sorted_entries:
        fav_recent = sorted_entries[0].horse.recent_starts(5)
        if fav_recent:
            gallop_count = sum(1 for s in fav_recent if s.galloped)
            if gallop_count >= 2:
                difficulty += 8.0  # Favorit med upprepade galopperingar
            elif fav_recent[0].galloped:
                difficulty += 5.0  # Galopperade senast

    return max(0.0, min(100.0, difficulty))


# ═══════════════════════════════════════════════════════════════════════════
# Greedy probability-maximizing allokering
# ═══════════════════════════════════════════════════════════════════════════

def _greedy_allocate(race_info: list[tuple[int, float, int]], max_rows: int) -> list[int]:
    """Allokera picks som maximerar P(alla rätt) inom radbudget.

    v8.2: Balanserad greedy — säkerställer minst 2 picks per lopp
    innan vidare expansion. Fixar under-coverage på lätta lopp.

    Empirisk bakgrund (deep_strategy_optimizer.py, 286 omgångar):
    - Ren greedy ger easiest-lopp 2.0 picks → bara 51.9% coverage
    - Med 3 picks på lätta lopp: 67.5% coverage (+15.6pp)
    - Hårdaste loppen får 5.2 picks → 71.6% (diminishing returns)
    - Balanserad approach: säkerställ golv, sedan greedy expansion

    Steg:
    1. Alla lopp startar med 1 pick
    2. Om budget tillåter: ge alla lopp minst 2 picks (floor)
    3. Greedy expansion med ratio-metoden för resterande budget

    race_info: list of (race_number, difficulty, n_starters)
    max_rows: max antal rader
    returns: list of picks per leg (same order as input)
    """
    n = len(race_info)
    picks = [1] * n

    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    # Steg 1: Floor — ge alla lopp minst 2 picks om budget tillåter
    # Prioritera de med lägst coverage (= mest nytta av extra pick)
    floor_order = sorted(
        range(n),
        key=lambda i: _coverage_prob(race_info[i][1], 1),  # Lägst coverage först
    )
    for i in floor_order:
        rn, diff, ns = race_info[i]
        if picks[i] >= 2 or picks[i] >= ns:
            continue
        new_rows = total_rows() * 2  # 1→2 always doubles
        if new_rows <= max_rows:
            picks[i] = 2

    # Steg 2: Greedy expansion med ratio-metoden
    for _ in range(100):
        if total_rows() >= max_rows:
            break

        best_ratio = -float("inf")
        best_idx = -1

        for i, (rn, diff, ns) in enumerate(race_info):
            if picks[i] >= ns:
                continue
            new_picks = picks[i] + 1
            new_rows = total_rows() * new_picks // picks[i]
            if new_rows > max_rows:
                continue

            # Coverage gain i log-space
            old_p = _coverage_prob(diff, picks[i])
            new_p = _coverage_prob(diff, new_picks)
            gain = math.log(max(new_p, 1e-10)) - math.log(max(old_p, 1e-10))

            # Cost i log-space
            cost = math.log(new_picks) - math.log(picks[i])

            ratio = gain / max(cost, 1e-10)

            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_idx < 0:
            break

        picks[best_idx] += 1

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# DEN OPTIMALA STRATEGIN: spike_easiest + rest4
# ═══════════════════════════════════════════════════════════════════════════


def _spike_easiest_rest4(
    race_info: list[tuple[int, float, int]], max_rows: int
) -> list[int]:
    """DEN OPTIMALA STRATEGIN — bevisad bäst av 40+ testade.

    Spike det lättaste loppet (lägst difficulty), 4 val i alla andra.
    Trimma till budget om nödvändigt.

    Testad på 336 omgångar:
    - 18 hits (5.4%), +112% ROI, +101k profit @ 300kr
    - Slår alla greedy, gap-based, score-adaptive, value-spike strategier

    Varför det fungerar:
    - Modellens top-1 är 38.4% korrekt i lättaste loppen
    - 4-bred = 77.2% coverage i lätta, 61.1% i svåra
    - Minsta kostnaden per hit bland 4+ hit-strategier
    """
    n = len(race_info)
    # Hitta lättaste loppet
    easiest_idx = min(range(n), key=lambda i: race_info[i][1])

    # Base width per budget range
    base_width = 4
    if max_rows >= 3000:  # 1500kr
        base_width = 5
    elif max_rows >= 1500:  # 750kr
        base_width = 5

    picks = [0] * n
    picks[easiest_idx] = 1

    for i in range(n):
        if i != easiest_idx:
            picks[i] = min(base_width, race_info[i][2])

    # Trim to budget
    rows = 1
    for p in picks:
        rows *= p

    while rows > max_rows:
        # Reducera bredaste icke-spike-loppet
        widest_idx = -1
        widest_val = 0
        for i in range(n):
            if picks[i] > 1 and picks[i] > widest_val:
                widest_val = picks[i]
                widest_idx = i
        if widest_idx < 0:
            break
        picks[widest_idx] -= 1
        rows = 1
        for p in picks:
            rows *= p

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# Legacy fixed configs (behålls för jämförelse)
# ═══════════════════════════════════════════════════════════════════════════

OPTIMAL_CONFIGS: dict[int, tuple[int, int, str]] = {
    75:   (3, 4, "Fixed 3S+rest4 — 75kr"),
    100:  (3, 4, "Fixed 3S+rest4 — 100kr"),
    200:  (3, 4, "Fixed 3S+rest4 — 200kr"),
    300:  (1, 4, "Spike easiest + rest4 — 300kr"),
    400:  (1, 4, "Spike easiest + rest4 — 400kr"),
    500:  (1, 4, "Spike easiest + rest4 — 500kr"),
    1000: (1, 5, "Spike easiest + rest5 — 1000kr"),
}


# ═══════════════════════════════════════════════════════════════════════════
# Huvudfunktion — build_system
# ═══════════════════════════════════════════════════════════════════════════

def build_system(
    game_round: GameRound,
    budget: float = 300.0,
    row_price: Optional[float] = None,
    strategy: str = "optimal",
) -> SystemPlan:
    """Bygg system — den ENDA bevisade strategin.

    Metod (strategy="optimal"):
    spike_easiest + rest4 — bevisat bäst av 40+ testade strategier.

    1. Beräkna svårighetsgrad per lopp (predict_difficulty)
    2. Spike det lättaste loppet (1 val)
    3. Alla andra lopp: 4 val (topp 4 enligt modellen)
    4. Trimma till budget om nödvändigt

    Resultat (336 omgångar):
    - 300kr: 18 hits (5.4%), +112% ROI, +101k profit
    - 500kr: 20 hits (6.0%), +41% ROI
    - 750kr: 25 hits (7.4%), +3% ROI

    Strategier:
    - "optimal": spike_easiest + rest4 (BEVISAT BÄST)
    - "greedy": Balanced greedy (mer hits vid hög budget, lägre ROI)
    - "fixed": Legacy fast bredd (2S+rest4 etc)
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    max_rows = int(budget / row_price)

    if strategy == "fixed":
        return _build_fixed_system(game_round, budget, row_price)

    # ── Steg 1: Samla loppinformation ────────────────────────────
    race_info = []
    for race in game_round.races:
        diff = predict_difficulty(race)
        ns = len(race.active_entries)
        race_info.append((race.race_number, diff, ns))

    # ── Steg 2: Spike easiest + rest 4 allokering ───────────────
    if strategy == "greedy":
        picks = _greedy_allocate(race_info, max_rows)
    else:
        # OPTIMAL: spike_easiest + rest4
        picks = _spike_easiest_rest4(race_info, max_rows)

    # ── Steg 3: Bygg plan ────────────────────────────────────────
    legs: list[LegAssignment] = []
    total_log_prob = 0.0

    for i, race in enumerate(game_round.races):
        diff = race_info[i][1]
        n_picks = picks[i]

        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        selected = [e.post_position for e in sorted_entries[:n_picks]]

        # Coverage probability
        cov_prob = _coverage_prob(diff, n_picks)
        total_log_prob += math.log(max(cov_prob, 1e-10))

        # Confidence: 0-100, baserat på coverage
        confidence = min(95, max(10, cov_prob * 100))

        leg_type = _picks_to_type(n_picks)

        legs.append(LegAssignment(
            race_number=race.race_number,
            leg_type=leg_type,
            num_picks=n_picks,
            picks=selected,
            confidence=confidence,
            upset_risk=race.upset_risk,
            difficulty=diff,
            reasoning=_build_reasoning(race, diff, n_picks, cov_prob),
        ))

    predicted_prob = math.exp(total_log_prob)

    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=f"Spike Easiest + Rest4 — {budget:.0f}kr",
        predicted_hit_prob=predicted_prob,
    )
    plan.calc_rows()

    return plan


def _build_fixed_system(
    game_round: GameRound,
    budget: float,
    row_price: float,
) -> SystemPlan:
    """Legacy: fast bredd (2S+rest4 etc)."""
    config = OPTIMAL_CONFIGS.get(
        int(budget),
        OPTIMAL_CONFIGS[min(OPTIMAL_CONFIGS.keys(),
                            key=lambda b: abs(b - budget))]
    )
    n_spikes, rest_width, label = config
    num_races = len(game_round.races)
    n_spikes = min(n_spikes, num_races // 2 + 1)

    race_diffs = []
    for race in game_round.races:
        diff = predict_difficulty(race)
        race_diffs.append((race, diff))

    race_diffs.sort(key=lambda x: x[1])

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
            n_picks = 1
        else:
            n_picks = min(rest_width, len(all_picks))

        cov_prob = _coverage_prob(diff, n_picks)

        legs.append(LegAssignment(
            race_number=race.race_number,
            leg_type=_picks_to_type(n_picks),
            num_picks=n_picks,
            picks=all_picks[:n_picks],
            confidence=min(95, max(10, cov_prob * 100)),
            upset_risk=race.upset_risk,
            difficulty=diff,
            reasoning=_build_reasoning(race, diff, n_picks, cov_prob),
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

    # Budget trim for fixed
    max_rows = int(budget / row_price)
    while plan.total_rows > max_rows:
        widest = max(
            (leg for leg in legs if leg.num_picks > 1),
            key=lambda l: l.num_picks,
            default=None,
        )
        if widest is None:
            break
        widest.num_picks -= 1
        widest.picks = widest.picks[:widest.num_picks]
        _update_leg_type(widest)
        plan.calc_rows()

    return plan


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _picks_to_type(n: int) -> str:
    if n == 1:
        return "spik"
    elif n == 2:
        return "kort"
    elif n == 3:
        return "medel"
    else:
        return "bred"


def _update_leg_type(leg: LegAssignment) -> None:
    """Uppdatera leg_type baserat på antal val."""
    leg.leg_type = _picks_to_type(leg.num_picks)


def _build_reasoning(race: Race, difficulty: float, picks: int,
                     coverage: float = 0.0) -> str:
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
    cov_str = f", täckning {coverage:.0%}" if coverage > 0 else ""
    return f"{picks} val (diff {difficulty:.0f} — {features}{cov_str})"


def build_multiple_systems(
    game_round: GameRound,
    budgets: list[float] | None = None,
) -> list[SystemPlan]:
    """Bygg system vid flera budgetnivåer.

    Default: 300kr (bäst ROI), 500kr, 750kr.
    Alla använder spike_easiest + rest4 (den ENDA bevisade strategin).

    Resultat (336 omgångar):
    - 300kr: 18 hits (5.4%), +112% ROI ★ REKOMMENDERAD
    - 500kr: 20 hits (6.0%), +41% ROI
    - 750kr: 25 hits (7.4%), +3% ROI
    """
    if budgets is None:
        budgets = [300, 500, 750]

    plans = []
    for budget in budgets:
        plan = build_system(game_round, budget=budget, strategy="optimal")
        plans.append(plan)
    return plans
