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
# ABCD + DB2 Smart Strategy — ABCD-klassificering + Dennis Brain skräll-radar
# ═══════════════════════════════════════════════════════════════════════════


def _abcd_db2_smart(
    game_round: GameRound, max_rows: int
) -> list[LegAssignment]:
    """ABCD + DB2 Smart system — intelligent horsval baserat på ranking-data.

    Insikt (2 veckors backtest, 81 lopp):
    - A/B-hästar vinner 76.5% av loppen → ryggraden
    - DB2 (effective_form) rankar vinnaren top-3 i 44% av skrällarna
    - Marknad hittar 0% av skrällvinnarna i top-3

    Strategi:
    1. SPIKE: Loppet med starkast A-häst + lägst difficulty → 1 val (A-hästen)
    2. KÄRNA: I varje annat lopp — alla A+B hästar (top 35% av fältet)
    3. DB2-GARDERING: C-hästar med DB2 rank ≤ 3 läggs till i risklopp
       → dessa är outsiders som tidsanalysen flaggar som farliga
    4. BUDGET-TRIM: Om för dyrt, ta bort svagaste DB2-adds först

    Picks-ordning (per lopp):
      A-hästar → B-hästar → DB2-topprankade C-hästar → övriga C (om budget)

    Varför detta borde slå spike_easiest:
    - spike_easiest tar alltid top-4 by super_score → missar outsiders
    - ABCD+DB2 tar A+B (kärna) + DB2-flaggade outsiders → bättre skrälltäckning
    - DB2 hittar 44% av skrällar i top-3, super_score bara 20%
    """
    legs: list[LegAssignment] = []
    race_data = []

    for race in game_round.races:
        entries = race.active_entries
        if not entries:
            continue

        diff = predict_difficulty(race)
        n_starters = len(entries)

        # Sortera efter super_score (primär ranking)
        sorted_by_score = sorted(entries, key=lambda e: e.super_score, reverse=True)

        # DB2 ranking: sortera efter effective_form (lägre = snabbare = bättre)
        valid_ef = [
            e for e in entries
            if e.dennis_effective_form and e.dennis_effective_form > 0
        ]
        db2_sorted = sorted(valid_ef, key=lambda e: e.dennis_effective_form)
        db2_rank_map = {
            e.post_position: idx + 1
            for idx, e in enumerate(db2_sorted)
        }

        # Samla A/B/C/D-klassificerade hästar
        a_horses = []
        b_horses = []
        c_horses = []

        for entry in sorted_by_score:
            rec = entry.recommendation or ""
            if rec == "spik":
                a_horses.append(entry)
            elif rec in ("2-val", "3-val"):
                b_horses.append(entry)
            elif rec == "gardering":
                c_horses.append(entry)
            # D (strykning) ignoreras

        # DB2-flaggade C-hästar: C-rank men DB2 top-3 → skräll-kandidat
        db2_outsiders = []
        for entry in c_horses:
            db2_rank = db2_rank_map.get(entry.post_position, 99)
            if db2_rank <= 3:
                db2_outsiders.append((entry, db2_rank))
        # Sortera: bäst DB2 först
        db2_outsiders.sort(key=lambda x: x[1])

        # DB2-rank för alla hästar (för swap-beslut)
        b_with_db2 = [
            (entry, db2_rank_map.get(entry.post_position, 99))
            for entry in b_horses
        ]

        # Bestäm om A-häst finns (spike-kandidat)
        has_a = len(a_horses) > 0
        gap_to_second = 0
        if len(sorted_by_score) >= 2:
            gap_to_second = sorted_by_score[0].super_score - sorted_by_score[1].super_score

        race_data.append({
            "race": race,
            "diff": diff,
            "n_starters": n_starters,
            "a_horses": a_horses,
            "b_horses": b_horses,
            "b_with_db2": b_with_db2,
            "c_horses": c_horses,
            "db2_outsiders": db2_outsiders,
            "has_a": has_a,
            "gap": gap_to_second,
            "sorted": sorted_by_score,
        })

    # ── Hitta bästa spike-lopp: A-häst + lägst difficulty ──
    spike_candidates = [
        (i, rd) for i, rd in enumerate(race_data)
        if rd["has_a"]
    ]
    spike_idx = -1
    if spike_candidates:
        # Bästa spike = lägst difficulty bland de med A-häst
        spike_idx = min(spike_candidates, key=lambda x: x[1]["diff"])[0]
    else:
        # Fallback: spike lättaste loppet (som spike_easiest)
        spike_idx = min(range(len(race_data)), key=lambda i: race_data[i]["diff"])

    # ── Bygg picks per lopp ──
    all_picks_data = []  # (race_idx, picks_list, leg_type, reasoning)

    for i, rd in enumerate(race_data):
        race = rd["race"]
        diff = rd["diff"]

        if i == spike_idx:
            # SPIKE: 1 val — A-hästen eller rank-1
            if rd["a_horses"]:
                picks = [rd["a_horses"][0].post_position]
            else:
                picks = [rd["sorted"][0].post_position]

            all_picks_data.append({
                "race_idx": i,
                "picks": picks,
                "core_count": 1,
                "db2_adds": 0,
                "reasoning": f"SPIK — {'A-häst' if rd['has_a'] else 'rank 1'}, diff {diff:.0f}",
            })
            continue

        # ICKE-SPIKE: A+B kärna + DB2 outsiders
        #
        # Nyckelidé: i SVÅRA lopp (diff ≥ 35), byt ut svagaste B
        # mot DB2-outsider om DB2-outsider har bättre tidsform.
        # I LÄTTA lopp, behåll rena A+B (super_score driven).
        #
        # Varför: DB2 hittar 44% av skrällar i top-3, men i lätta
        # lopp vinner favoriten ändå → onödig risk att byta.

        core_picks = []
        for entry in rd["a_horses"]:
            core_picks.append(entry.post_position)

        # I svåra lopp: möjlig swap av svagaste B mot starkaste DB2 outsider
        b_list = list(rd["b_horses"])
        swapped = []
        if diff >= 35 and rd["db2_outsiders"] and len(b_list) >= 2:
            # Hitta svagaste B (högst DB2-rank = sämst tidsform)
            b_db2 = rd["b_with_db2"]
            if b_db2:
                weakest_b = max(b_db2, key=lambda x: x[1])
                best_db2_outsider = rd["db2_outsiders"][0]

                # Swap om DB2-outsider har klart bättre tidsform
                # (DB2 rank ≤ 2 och svagaste B har DB2 rank ≥ 5)
                if (
                    best_db2_outsider[1] <= 2
                    and weakest_b[1] >= 5
                    and len(b_list) >= 2  # Behåll minst 1 B
                ):
                    b_list = [e for e in b_list if e.post_position != weakest_b[0].post_position]
                    swapped.append(
                        f"↔{weakest_b[0].post_position}→"
                        f"{best_db2_outsider[0].post_position}(DB2:{best_db2_outsider[1]})"
                    )
                    # Lägg till DB2-outsider som ersättning
                    core_picks.append(best_db2_outsider[0].post_position)

        for entry in b_list:
            if entry.post_position not in core_picks:
                core_picks.append(entry.post_position)

        # Säkerställ minst 2 picks
        if len(core_picks) < 2:
            for entry in rd["sorted"]:
                if entry.post_position not in core_picks:
                    core_picks.append(entry.post_position)
                if len(core_picks) >= 2:
                    break

        # Lägg till DB2 outsiders som EXTRA gardering (utöver eventuell swap)
        db2_add_count = 0
        db2_added = []
        for entry, db2_rank in rd["db2_outsiders"]:
            if entry.post_position not in core_picks:
                core_picks.append(entry.post_position)
                db2_add_count += 1
                db2_added.append(f"{entry.post_position}(DB2:{db2_rank})")

        reasoning_parts = [f"{len(rd['a_horses'])}A+{len(b_list)}B"]
        if swapped:
            reasoning_parts.append(f"swap: {', '.join(swapped)}")
        if db2_add_count > 0:
            reasoning_parts.append(f"+{db2_add_count} DB2 [{', '.join(db2_added)}]")
        reasoning_parts.append(f"diff {diff:.0f}")

        all_picks_data.append({
            "race_idx": i,
            "picks": core_picks,
            "core_count": len(core_picks) - db2_add_count,
            "db2_adds": db2_add_count,
            "reasoning": " | ".join(reasoning_parts),
        })

    # ── Budget-trimning ──
    def total_rows():
        r = 1
        for pd in all_picks_data:
            r *= len(pd["picks"])
        return r

    # Fas 1: Om för dyrt, ta bort DB2-adds (svagaste först)
    while total_rows() > max_rows:
        # Hitta lopp med DB2-adds att ta bort
        removable = [
            (j, pd) for j, pd in enumerate(all_picks_data)
            if pd["db2_adds"] > 0 and len(pd["picks"]) > 2
        ]
        if not removable:
            break
        # Ta bort från det bredaste loppet
        widest_j = max(removable, key=lambda x: len(x[1]["picks"]))[0]
        pd = all_picks_data[widest_j]
        pd["picks"].pop()  # Ta bort sist tillagda (svagaste DB2)
        pd["db2_adds"] -= 1

    # Fas 2: Om fortfarande för dyrt, reducera B-hästar
    while total_rows() > max_rows:
        # Hitta bredaste icke-spike med >2 picks
        candidates = [
            (j, pd) for j, pd in enumerate(all_picks_data)
            if len(pd["picks"]) > 2 and j != spike_idx
        ]
        if not candidates:
            # Reducera ner till 2 om möjligt
            candidates = [
                (j, pd) for j, pd in enumerate(all_picks_data)
                if len(pd["picks"]) > 1 and j != spike_idx
            ]
        if not candidates:
            break
        widest_j = max(candidates, key=lambda x: len(x[1]["picks"]))[0]
        all_picks_data[widest_j]["picks"].pop()

    # ── Bygg LegAssignment-objekt ──
    total_log_prob = 0.0
    for pd in all_picks_data:
        rd = race_data[pd["race_idx"]]
        race = rd["race"]
        diff = rd["diff"]
        n_picks = len(pd["picks"])

        cov_prob = _coverage_prob(diff, n_picks)
        total_log_prob += math.log(max(cov_prob, 1e-10))
        confidence = min(95, max(10, cov_prob * 100))

        leg_type = _picks_to_type(n_picks)

        legs.append(LegAssignment(
            race_number=race.race_number,
            leg_type=leg_type,
            num_picks=n_picks,
            picks=pd["picks"],
            confidence=confidence,
            upset_risk=race.upset_risk,
            difficulty=diff,
            reasoning=pd["reasoning"],
        ))

    return legs, math.exp(total_log_prob)


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
    proffs_data: Optional[dict] = None,
) -> SystemPlan:
    """Bygg system — den ENDA bevisade strategin.

    Metod (strategy="optimal"):
    spike_easiest + rest4 — bevisat bäst av 40+ testade strategier.

    1. Beräkna svårighetsgrad per lopp (predict_difficulty)
    2. Spike det lättaste loppet (1 val)
    3. Alla andra lopp: 4 val (topp 4 enligt modellen)
    4. Proffs rescue: om proffs rank 1-3 häst ej bland picks, byt ut lägsta pick
    5. Trimma till budget om nödvändigt

    Resultat (336 omgångar):
    - 300kr: 18 hits (5.4%), +112% ROI, +101k profit
    - 500kr: 20 hits (6.0%), +41% ROI
    - 750kr: 25 hits (7.4%), +3% ROI

    Strategier:
    - "optimal": spike_easiest + rest4 (BEVISAT BÄST)
    - "smart": ABCD + DB2 — intelligent pick-selection (A/B kärna + DB2 skrällar)
    - "chansspik": Upset-targeting — siktar på 25-100k utdelning
    - "greedy": Balanced greedy (mer hits vid hög budget, lägre ROI)
    - "fixed": Legacy fast bredd (2S+rest4 etc)
    """
    if row_price is None:
        from ..config import ROW_PRICES
        row_price = ROW_PRICES.get(game_round.game_type, 0.50)

    max_rows = int(budget / row_price)

    if strategy == "smart":
        return _build_smart_system(game_round, budget, row_price, max_rows, proffs_data)

    if strategy == "chansspik":
        from .upset_system import build_upset_system
        return build_upset_system(game_round, budget=budget, row_price=row_price)

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

        # ── Proffs rescue: if proffs strongly endorse a horse the model
        # ranked 4-6 (and it has 5-20% streck), promote it into picks
        # by replacing the lowest-ranked model pick.
        # This captures the 7.4% of winners that "bara proffs top-3" find.
        # Max 1 rescue per system to preserve model's proven edge.
        if proffs_data and n_picks >= 2:
            proffs_races = proffs_data.get("races", [])
            proffs_horses = {}
            for pr in proffs_races:
                if pr.get("race_number") == race.race_number:
                    proffs_horses = {h["number"]: h for h in pr.get("horses", [])}
                    break

            if proffs_horses:
                # Find proffs top-3 horses not in our picks
                proffs_ranked = sorted(
                    proffs_horses.values(),
                    key=lambda h: h.get("proffs_weighted_pct", 0),
                    reverse=True,
                )
                for ph in proffs_ranked[:3]:
                    num = ph.get("number", 0)
                    proffs_pct = ph.get("proffs_weighted_pct", 0)
                    edge = ph.get("edge_pp", 0)
                    # Only rescue if:
                    # 1) not already in picks
                    # 2) proffs strongly endorse (≥15% weighted pct, edge ≥10pp)
                    # 3) horse has streck 5-20% (not a random outsider)
                    # 4) model ranks it 4-6 (not too far down)
                    if (
                        num not in selected
                        and proffs_pct >= 15
                        and edge >= 10
                    ):
                        # Find the entry
                        entry = next(
                            (e for e in sorted_entries if e.post_position == num),
                            None,
                        )
                        if entry and entry.bet_percentage:
                            model_rank = next(
                                (j + 1 for j, e in enumerate(sorted_entries)
                                 if e.post_position == num),
                                99,
                            )
                            if (
                                4 <= model_rank <= 6
                                and 0.05 <= entry.bet_percentage <= 0.20
                            ):
                                # Replace lowest pick with this proffs-endorsed horse
                                selected[-1] = num
                                break  # max 1 rescue per race

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


def _build_smart_system(
    game_round: GameRound,
    budget: float,
    row_price: float,
    max_rows: int,
    proffs_data: Optional[dict] = None,
) -> SystemPlan:
    """ABCD + DB2 Smart system.

    Använder ABCD-klassificering som kärna + Dennis Brain DB2
    som skräll-radar. Se _abcd_db2_smart() för detaljer.
    """
    legs, predicted_prob = _abcd_db2_smart(game_round, max_rows)

    plan = SystemPlan(
        game_type=game_round.game_type,
        round_date=str(game_round.round_date),
        legs=legs,
        row_price=row_price,
        budget=budget,
        strategy_name=f"ABCD + DB2 Smart — {budget:.0f}kr",
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
    proffs_data: Optional[dict] = None,
) -> list[SystemPlan]:
    """Bygg system vid flera budgetnivåer — TRE strategier.

    Inkluderar:
    1. ABCD + DB2 Smart (ABCD kärna + DB2 skräll-radar) ★ NY
    2. Spike Easiest (favorit-optimerad, bevisad +112% ROI)
    3. Chansspik (upset-optimerad, siktar på 25-100k utdelning)

    ABCD+DB2 Smart (ny):
    - A/B-hästar vinner 76.5% → ryggrad
    - DB2 hittar 44% av skrällar i top-3 → gardering
    - Intelligent picks istället för mekanisk top-4

    Resultat spike_easiest (336 omgångar):
    - 300kr: 18 hits (5.4%), +112% ROI ★ SÄKRAST
    - 500kr: 20 hits (6.0%), +41% ROI

    Chansspik:
    - Lägre hit rate men högre snitt-utdelning
    - Mål: fånga 1-2 upsets per omgång
    """
    if budgets is None:
        budgets = [300, 500]

    plans = []
    for budget in budgets:
        # Strategy 1: ABCD + DB2 Smart (intelligent picks) ★ NY
        plan_smart = build_system(game_round, budget=budget, strategy="smart",
                                  proffs_data=proffs_data)
        plans.append(plan_smart)

        # Strategy 2: Spike Easiest (proven, consistent)
        plan = build_system(game_round, budget=budget, strategy="optimal",
                           proffs_data=proffs_data)
        plans.append(plan)

        # Strategy 3: Chansspik (upset-targeting, higher payout)
        plan_upset = build_system(game_round, budget=budget, strategy="chansspik")
        plans.append(plan_upset)

    return plans
