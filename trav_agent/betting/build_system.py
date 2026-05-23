"""Bygg reducerat V85-system — optimal strategi baserad på 2026-data.

STRATEGI (backtested på 14 V85-omgångar 2026):
  ✗ Spikar MINSKAR ROI — marknadens #1 vinner bara 37%, modellens 29%
  ✓ ALDRIG under 2 picks/lopp — en missad spik = hela loppet förlorat
  ✓ Bred i osäkra lopp, smal i trygga — round-robin-expansion
  ✓ Poängreducering — ta bort rader med för många outsiders
  ✓ Sweet spot: 7/8 × picks i missat lopp = 10K-100K kr

Utdelningsdata 2026 V85:
  8/8: snitt 1,536K kr/rad (median 544K, max 6.4M) — jackpot
  7/8: snitt 8,876 kr/rad × antal vinstrader — SWEET SPOT
  6/8: snitt 205 kr/rad — täcker insats ibland

Vinnarrankings (112 lopp):
  Modell top-3: 58.9%  |  Marknad top-3: 68.8%  |  Union top-3: ~82%

Usage:
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 1000
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 500 --reduce poang
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 2000 --xml system.xml
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import _get_rankings, _combined_rank_score


# ── Dataklasser ────────────────────────────────────────────────────────

@dataclass
class HorsePick:
    """En häst i rammen."""
    post: int
    name: str
    model_rank: int
    market_rank: int
    model_score: float
    streck: float           # 0-1
    poang: int              # 1-5 (ranking-baserad)
    combined_rank: float
    value_ratio: float
    driver: str = ""


@dataclass
class LegConfig:
    """Konfiguration för en avdelning."""
    leg_num: int
    race_number: int
    distance: int
    start_method: str
    num_starters: int
    typ: str                # TRYGG, ÖPPEN, OSÄKER
    security: float         # högt = tryggare
    horses: list[HorsePick] = field(default_factory=list)
    selected: list[int] = field(default_factory=list)


@dataclass
class ReducedSystem:
    """Ett komplett reducerat system."""
    game_type: str
    game_date: str
    track_name: str
    track_code: int
    budget: int
    legs: list[LegConfig] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    total_ram: int = 0
    total_cost: float = 0.0
    max_poang: int = 0
    row_price: float = 0.50
    reduce_method: str = "none"


# ── Poängtilldelning ───────────────────────────────────────────────────

def _assign_poang(horses: list[HorsePick]) -> None:
    """Ge poäng 1-5 baserat på kombinerad ranking (bäst=1).

    Poängen styr reduceringen:
      1 = toppkandidat (stark i modell OCH marknad)
      2 = stark andraplats
      3 = tredjeval
      4 = gardering (rimlig chans)
      5 = outsider (lång odds, svag ranking)
    """
    sorted_h = sorted(horses, key=lambda h: h.combined_rank)
    for i, h in enumerate(sorted_h):
        h.poang = min(i + 1, 5)


def _classify_leg(horses: list[HorsePick], start_method: str) -> tuple[str, float]:
    """Klassificera avdelning och beräkna security score.

    Returnerar (typ, security).
    Typ: TRYGG (favorit dominerar), ÖPPEN (2-3 kandidater), OSÄKER (brett fält).
    """
    if not horses:
        return "OSÄKER", 0.0

    top = horses[0]
    top2_streck = sum(h.streck for h in horses[:2])
    top3_streck = sum(h.streck for h in horses[:3])

    # Security: modell-marknad-överensstämmelse
    model_top3 = set(h.post for h in sorted(horses, key=lambda h: h.model_rank)[:3])
    market_top3 = set(h.post for h in sorted(horses, key=lambda h: h.market_rank)[:3])
    overlap = len(model_top3 & market_top3)

    # Kontrollera om modell och marknad pekar på samma #1
    model_1 = min(horses, key=lambda h: h.model_rank)
    market_1 = min(horses, key=lambda h: h.market_rank)
    top1_agree = model_1.post == market_1.post

    # Gap-mått
    model_sorted = sorted(horses, key=lambda h: h.model_rank)
    model_gap = model_sorted[0].model_score - model_sorted[1].model_score if len(model_sorted) > 1 else 0
    market_gap = (market_1.streck - sorted(horses, key=lambda h: -h.streck)[1].streck) if len(horses) > 1 else 0

    security = (
        overlap * 20
        + (25 if top1_agree else 0)
        + min(20, model_gap * 2)
        + min(15, market_gap * 80)
        + min(15, top.streck * 30)  # stark favorit = tryggare
    )

    # Typ-klassificering
    if top.streck >= 0.40 and top1_agree:
        typ = "TRYGG"
    elif top2_streck >= 0.45 and overlap >= 2:
        typ = "ÖPPEN"
    else:
        typ = "OSÄKER"

    return typ, security


# ── Rambygge ───────────────────────────────────────────────────────────

def build_ram(game_round) -> list[LegConfig]:
    """Bygg ram från GameRound med streck + modelldata."""
    legs = []

    for race in game_round.races:
        entries = race.active_entries
        if len(entries) < 2:
            continue

        _, by_market, model_rank, market_rank = _get_rankings(race)
        n = race.num_starters

        horses = []
        for e in entries:
            mr = model_rank[e.post_position]
            kr = market_rank[e.post_position]
            streck = e.bet_percentage or 0
            cs = _combined_rank_score(e, mr, kr, n)
            value = (n - mr + 1) / max(1, kr) if kr > 0 else 1.0

            horses.append(HorsePick(
                post=e.post_position,
                name=e.horse.name,
                model_rank=mr,
                market_rank=kr,
                model_score=e.super_score,
                streck=streck,
                poang=1,
                combined_rank=cs,
                value_ratio=value,
                driver=e.driver_name or "",
            ))

        horses.sort(key=lambda h: h.combined_rank)
        _assign_poang(horses)

        typ, security = _classify_leg(horses, race.start_method.value)

        leg = LegConfig(
            leg_num=race.race_number - game_round.races[0].race_number + 1,
            race_number=race.race_number,
            distance=race.distance,
            start_method=race.start_method.value,
            num_starters=n,
            typ=typ,
            security=security,
            horses=horses,
        )
        legs.append(leg)

    return legs


def select_horses(legs: list[LegConfig], budget: int, row_price: float = 0.50) -> None:
    """Välj hästar per avdelning — datadriven allokering.

    Backtestdata (112 lopp V85 2026):
      TRYGG (agree+streck≥40%): 74% top-2, 82% top-3
      ÖPPEN:                    39% top-2, 58% top-3 ← PROBLEMET
      OSÄKER:                   50% top-2, 50% top-3

    Strategi:
      TRYGG → 2 picks (74% räcker, sparar budget)
      ÖPPEN → 3-4 picks (39%→58% med +1 pick)
      OSÄKER → 3-5 picks (bred gardering)

    51% av alla missar hamnar på combined rank 3-4.
    Att gå från 2→3 picks i ÖPPNA lopp fångar dessa.
    """
    max_rows = int(budget / row_price)
    num_legs = len(legs)

    # Steg 1: Basallokering per lopptyp
    for leg in legs:
        if leg.typ == "TRYGG":
            n = 2   # 74% top-2 — sparar budget
        elif leg.typ == "ÖPPEN":
            n = 3   # 39% → 58% med 3 picks
        else:  # OSÄKER
            n = 3   # bred gardering
        n = min(n, len(leg.horses))
        leg.selected = [h.post for h in leg.horses[:n]]

    # Steg 2: Reducera om ram > budget (ta bort i TRYGG först)
    reduce_order = sorted(range(num_legs), key=lambda i: -legs[i].security)
    for _ in range(50):
        rows = 1
        for l in legs:
            rows *= len(l.selected)
        if rows <= max_rows:
            break
        for idx in reduce_order:
            leg = legs[idx]
            if len(leg.selected) > 2:
                leg.selected = leg.selected[:-1]
                break
        else:
            break

    # Steg 3: Expandera — ÖPPEN och OSÄKER först (de behöver mest)
    expand_order = sorted(range(num_legs), key=lambda i: legs[i].security)

    while True:
        expanded = False
        for idx in expand_order:
            leg = legs[idx]
            current = len(leg.selected)
            # TRYGG max 2, ÖPPEN max 5, OSÄKER max 6
            type_max = {"TRYGG": 3, "ÖPPEN": 5, "OSÄKER": 6}
            max_p = min(len(leg.horses), type_max.get(leg.typ, 5))
            if current >= max_p:
                continue
            rows = 1
            for l in legs:
                rows *= len(l.selected)
            new_rows = rows // current * (current + 1)
            if new_rows <= max_rows:
                next_horse = leg.horses[current]
                leg.selected.append(next_horse.post)
                expanded = True
        if not expanded:
            break


# ── Poängreducering ────────────────────────────────────────────────────

def poang_reduce(legs: list[LegConfig], max_poang: int) -> list[tuple]:
    """Generera rader som uppfyller poängvillkoret.

    Poängsumma = sum av varje vald hästs poäng (1-5).
    Rader med fler outsiders (poäng 4-5) kräver att andra lopp
    har favoriter (poäng 1-2) för att klara gränsen.

    Effekt: "Om en outsider vinner i ett lopp, kräv att favoriten
    sitter i de andra loppen." Precis som V85-experternas reducering.
    """
    leg_data = []
    for leg in legs:
        selected_set = set(leg.selected)
        selected_horses = [h for h in leg.horses if h.post in selected_set]
        leg_data.append([(h.post, h.poang) for h in selected_horses])

    valid_rows = []
    for combo in itertools.product(*leg_data):
        total_p = sum(p for _, p in combo)
        if total_p <= max_poang:
            valid_rows.append(tuple(p for p, _ in combo))

    return valid_rows


def find_optimal_max_poang(legs: list[LegConfig], max_rows: int) -> int:
    """Hitta maxpoäng som ger närmast max_rows utan att överstiga."""
    num_legs = len(legs)
    # Binary search
    lo, hi = num_legs, num_legs * 5
    best = hi

    while lo <= hi:
        mid = (lo + hi) // 2
        n_rows = len(poang_reduce(legs, mid))
        if n_rows <= max_rows:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


# ── Viktad slumpreducering ─────────────────────────────────────────────

def weighted_sample_rows(legs: list[LegConfig], all_rows: list[tuple],
                         n_sample: int) -> list[tuple]:
    """Sampla rader proportionellt mot sqrt(streck × value_ratio).

    Rader med favoriter viktas högre, men outsider-rader kan ändå komma med.
    Deterministisk seed baserad på antal rader (reproducerbart).
    """
    horse_lookup = {}
    for leg_idx, leg in enumerate(legs):
        for h in leg.horses:
            horse_lookup[(leg_idx, h.post)] = h

    weights = []
    for row in all_rows:
        w = 1.0
        for leg_idx, post in enumerate(row):
            h = horse_lookup.get((leg_idx, post))
            if not h:
                w *= 0.01
                continue
            streck = max(h.streck, 0.01)
            value = max(h.value_ratio, 0.1)
            w *= (streck * value) ** 0.5
        weights.append(w)

    if not weights or sum(weights) == 0:
        return all_rows[:n_sample]

    rng = random.Random(len(all_rows))  # Reproducerbart
    sampled_set = set()
    attempts = 0
    while len(sampled_set) < n_sample and attempts < n_sample * 10:
        idx = rng.choices(range(len(all_rows)), weights=weights, k=1)[0]
        sampled_set.add(idx)
        attempts += 1

    return [all_rows[i] for i in sorted(sampled_set)]


# ── ATG XML ────────────────────────────────────────────────────────────

def generate_xml(system: ReducedSystem) -> str:
    """Generera ATG filinlämnings-XML."""
    coupon_type = {
        "V85": "v85Coupon", "V75": "v75Coupon",
        "V86": "v86Coupon", "GS75": "gs75Coupon",
    }.get(system.game_type, "v85Coupon")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<issuer schemaversion="ATG File Betting XSD ver 1.8"',
        '        version="1.0" product="Custom" company="Custom"',
        '        xsi:noNamespaceSchemaLocation='
        '"https://www.atg.se/services/schemas/filebet/1.8.2/atg_filebetting.xsd"',
        '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        '  <betcoupons>',
    ]

    for row_idx, row in enumerate(system.rows):
        lines.append(
            f'    <{coupon_type} couponid="{row_idx + 1}" '
            f'date="{system.game_date}" '
            f'trackcode="{system.track_code}" betmultiplier="1">'
        )
        for leg_idx, post in enumerate(row):
            marks = ['0'] * 15
            marks[post - 1] = '1'
            marks_str = ''.join(marks)
            lines.append(f'      <leg legno="{leg_idx + 1}" marks="{marks_str}"/>')
        lines.append(f'    </{coupon_type}>')

    lines.append('  </betcoupons>')
    lines.append('</issuer>')
    return '\n'.join(lines)


# ── Huvudfunktion ──────────────────────────────────────────────────────

def build_and_display(game_round, budget: int, reduce: str = "auto",
                      max_poang: int = 0, xml_path: str = None) -> ReducedSystem:
    """Bygg komplett system och visa.

    reduce:
      "none"   — full ram, ingen reducering
      "poang"  — poängreducering (ta bort outsider-tunga rader)
      "sample" — viktad slumpreducering (sampla proportionellt)
      "auto"   — poäng först, sedan sampla om fortfarande för dyrt
    """
    from trav_agent.config import ROW_PRICES
    row_price = ROW_PRICES.get(game_round.game_type, 0.50)
    max_rows = int(budget / row_price)

    # 1. Bygg ram
    legs = build_ram(game_round)

    # 2. Välj hästar (min 2/lopp, round-robin osäkrast först)
    select_horses(legs, budget, row_price)

    # 3. Beräkna full ram
    full_rows = 1
    for leg in legs:
        full_rows *= len(leg.selected)

    # 4. Allokering-sammanfattning
    alloc = "×".join(str(len(l.selected)) for l in legs)

    print(f"\n{'═'*70}")
    print(f"  V85 {game_round.round_date} — {game_round.track_name or '?'}")
    print(f"  Budget: {budget:,} kr | Radpris: {row_price} kr | Max: {max_rows:,} rader")
    print(f"  Ram: {alloc} = {full_rows:,} rader ({full_rows * row_price:,.0f} kr)")
    print(f"  Reducering: {reduce}")
    print(f"{'═'*70}")

    # 5. Visa avdelningar
    for leg in legs:
        selected_set = set(leg.selected)
        n_sel = len(leg.selected)

        typ_emoji = {"TRYGG": "🟢", "ÖPPEN": "🟡", "OSÄKER": "🔴"}[leg.typ]
        print(f"\n  AVD {leg.leg_num} ({leg.distance}m {leg.start_method}) "
              f"{typ_emoji} {leg.typ} — {n_sel} val av {leg.num_starters} "
              f"(security: {leg.security:.0f})")

        print(f"  {'':>3} {'Häst':<22} {'Rank':>5} {'Streck':>7} {'Poäng':>5}  ")
        print(f"  {'':>3} {'─'*22} {'─'*5} {'─'*7} {'─'*5}  ")

        for h in leg.horses:
            if h.post in selected_set or h.streck >= 0.02:
                if h.post in selected_set:
                    marker = f" ★ p{h.poang}"
                else:
                    marker = "     "
                rank_str = f"M{h.model_rank}/K{h.market_rank}"
                print(f"  {h.post:>3} {h.name:<22} {rank_str:>5} "
                      f"{h.streck:>6.1%} {h.poang:>5} {marker}")

    # 6. Reducering
    final_rows = list(itertools.product(*[
        [h.post for h in leg.horses if h.post in set(leg.selected)]
        for leg in legs
    ]))

    reduce_method = reduce
    applied_poang = 0

    if reduce in ("poang", "auto"):
        if max_poang == 0:
            max_poang = find_optimal_max_poang(legs, max_rows)
        all_rows = poang_reduce(legs, max_poang)
        pct = len(all_rows) / len(final_rows) * 100 if final_rows else 0
        print(f"\n  ─── Poängreducering (max {max_poang}p) ───")
        print(f"  {len(final_rows):,} → {len(all_rows):,} rader ({pct:.0f}% kvar)")
        final_rows = all_rows
        applied_poang = max_poang

        if reduce == "auto" and len(final_rows) > max_rows:
            sampled = weighted_sample_rows(legs, final_rows, max_rows)
            print(f"  + Viktad slumpreducering: {len(final_rows):,} → {len(sampled):,} rader")
            final_rows = sampled
            reduce_method = "auto(poang+sample)"

    elif reduce == "sample":
        if len(final_rows) > max_rows:
            sampled = weighted_sample_rows(legs, final_rows, max_rows)
            print(f"\n  ─── Viktad slumpreducering ───")
            print(f"  {len(final_rows):,} → {len(sampled):,} rader")
            final_rows = sampled

    total_cost = len(final_rows) * row_price

    # 7. Poängfördelning
    horse_lookup = {}
    for leg_idx, leg in enumerate(legs):
        for h in leg.horses:
            horse_lookup[(leg_idx, h.post)] = h

    if final_rows:
        poang_sums = []
        for row in final_rows:
            ps = sum(horse_lookup.get((i, p), HorsePick(0, "", 0, 0, 0, 0, 5, 0, 0)).poang
                     for i, p in enumerate(row))
            poang_sums.append(ps)

        print(f"\n  ┌─────────────────────────────────┐")
        print(f"  │  SLUTSYSTEM                      │")
        print(f"  │  {len(final_rows):>6,} rader × {row_price} kr = {total_cost:>6,.0f} kr │")
        print(f"  │  Poäng: {min(poang_sums)}-{max(poang_sums)} "
              f"(snitt {sum(poang_sums)/len(poang_sums):.1f}){' '*(14-len(str(max(poang_sums))))}│")
        print(f"  └─────────────────────────────────┘")

    # 8. Uppskattad utdelning
    print(f"\n  Uppskattad utdelning vid:")
    avg_picks = sum(len(l.selected) for l in legs) / len(legs)
    print(f"    8/8: 1 rad  × ~544K kr = ~544,000 kr (median V85 2026)")
    print(f"    7/8: ~{avg_picks:.0f} rader × ~8,876 kr = ~{avg_picks * 8876:,.0f} kr")
    print(f"    6/8: ~{avg_picks**2:.0f} rader × ~205 kr = ~{avg_picks**2 * 205:,.0f} kr")

    # 9. XML-export
    system = ReducedSystem(
        game_type=game_round.game_type,
        game_date=str(game_round.round_date),
        track_name=game_round.track_name or "",
        track_code=23,  # TODO: lookup
        budget=budget,
        legs=legs,
        rows=final_rows,
        total_ram=full_rows,
        total_cost=total_cost,
        max_poang=applied_poang,
        row_price=row_price,
        reduce_method=reduce_method,
    )

    if xml_path:
        xml_str = generate_xml(system)
        Path(xml_path).write_text(xml_str, encoding="utf-8")
        print(f"\n  XML sparad: {xml_path}")

    print(f"\n{'═'*70}")
    return system


# ── CLI ────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Bygg reducerat V85-system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exempel:
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 1000
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 500 --reduce poang
  python -m trav_agent.betting.build_system V85 2026-04-05 --budget 2000 --reduce none --xml system.xml
        """,
    )
    parser.add_argument("game_type", default="V85", nargs="?")
    parser.add_argument("game_date", default=str(date.today()), nargs="?")
    parser.add_argument("--budget", type=int, default=1000, help="Budget i kr")
    parser.add_argument("--reduce", choices=["none", "poang", "sample", "auto"],
                        default="auto", help="Reduceringsmetod")
    parser.add_argument("--max-poang", type=int, default=0, help="Maxpoäng (0=auto)")
    parser.add_argument("--xml", type=str, default=None, help="Spara ATG XML-fil")
    args = parser.parse_args()

    game_date = date.fromisoformat(args.game_date)

    client = ATGClient()
    print(f"Hämtar {args.game_type} {game_date}...")
    gr = await client.fetch_full_round(args.game_type, game_date)

    if not gr or not gr.races:
        print(f"Ingen data hittad för {args.game_type} {game_date}")
        return

    # Uppdatera streckdata (skip cache)
    updated = await client.refresh_odds(gr)
    if updated:
        print(f"Streckdata uppdaterad ({updated} hästar)")

    print("Analyserar...")
    analyzer = CompositeAnalyzer()
    analyzer.analyze_round(gr)

    build_and_display(gr, args.budget, args.reduce, args.max_poang, args.xml)


if __name__ == "__main__":
    asyncio.run(main())
