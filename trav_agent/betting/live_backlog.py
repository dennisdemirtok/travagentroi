"""Live-backlog: generera backlog-entries för pågående omgångar.

Skapar entries i samma format som generate_backlog.py men med
live-specifika fält (delresultat, status per lopp).

Version 2: Modell-driven (85% modell + 15% marknad).
Streckprocent vid speltillfället (~1h före start) används
som komplement, inte primär signal. Backtester använder nu
temporal filtrering + omberäknad karriärstatistik.
"""

from __future__ import annotations

from trav_agent.betting.system_generator import SystemGenerator, BettingSystem


# ── Strategier v5: Union + Marknad + Bred ────────────────────────────────
# (strategy, filter, budget, max_spikes, spike_conf, spike_gap, label)
#
# V85-fokus: Tre strategier, budget 500-2000 kr
#   A_union:   Balanserad union modell+marknad, ABC-reducering
#   B_marknad: Marknadsstyrd (streck), modell som value-tillägg
#   C_bred:    Bred ABC — smal i trygga, bred i öppna lopp
STRATEGIES = [
    # Bara D_smart (minst olönsamma systemstrategin i no-leakage backtest)
    ("D_smart",   "none", 500,  0, 0, 0, "D_smart_500"),
    ("D_smart",   "none", 1000, 0, 0, 0, "D_smart_1000"),
]


def _count_correct_partial(race_picks, races):
    """Räkna antal rätt bland avslutade lopp."""
    correct = 0
    finished = 0
    for rp, race in zip(race_picks, races):
        if not race.result_order:
            continue  # Ej avslutat
        finished += 1
        winner = race.result_order[0]
        if winner in rp.picks:
            correct += 1
    return correct, finished


def _count_winning_rows(race_picks, races, num_correct):
    """Räkna vinstrader — samma logik som roi_backtest_integrated."""
    num_races = len(race_picks)
    if num_correct == num_races:
        return 1

    missed_product = 1
    for rp, race in zip(race_picks, races):
        if not race.result_order:
            missed_product *= rp.num_picks
            continue
        winner = race.result_order[0]
        if winner not in rp.picks:
            missed_product *= rp.num_picks

    return missed_product


def _system_to_entry(system: BettingSystem, game_round, label: str) -> dict:
    """Konvertera ett BettingSystem till backlog-entry dict."""
    num_races = len(game_round.races)
    game_date = str(game_round.round_date)
    game_type = game_round.game_type
    track_name = game_round.track_name or "?"

    partial_correct, races_finished = _count_correct_partial(
        system.race_picks, game_round.races
    )

    all_finished = races_finished == num_races
    num_correct = partial_correct
    payout_per_row = 0.0
    winning_rows = 0
    total_payout = 0.0

    if all_finished and game_round.dividends:
        payout_per_row = game_round.dividends.get(num_correct, 0.0)
        if payout_per_row > 0:
            winning_rows = _count_winning_rows(
                system.race_picks, game_round.races, num_correct
            )
            total_payout = payout_per_row * winning_rows

    race_details = []
    for race, rp in zip(game_round.races, system.race_picks):
        has_result = bool(race.result_order)
        winner = race.result_order[0] if has_result else None
        winner_name = ""
        winner_streck = 0.0

        if winner:
            for e in race.active_entries:
                if e.post_position == winner:
                    winner_name = e.horse.name
                    winner_streck = e.bet_percentage or 0
                    break

        ratt = None
        if has_result:
            ratt = winner in rp.picks

        race_details.append({
            "avd": rp.race_number,
            "dist": rp.distance,
            "metod": rp.start_method,
            "startande": rp.num_starters,
            "conf": round(rp.confidence, 0),
            "upset": round(rp.upset_risk, 0),
            "picks": rp.num_picks,
            "pick_list": [
                {
                    "nr": nr,
                    "namn": namn[:16],
                    "score": round(sc, 1),
                    "streck": round(st * 100, 1),
                }
                for nr, namn, sc, st in zip(
                    rp.picks, rp.pick_names, rp.pick_scores, rp.pick_streck
                )
            ],
            "tackning": round(rp.combined_streck * 100, 0),
            "vinnare": winner,
            "vinnare_namn": winner_name[:16] if winner_name else "",
            "vinnare_streck": round(winner_streck * 100, 1),
            "ratt": ratt,
            "status": "finished" if has_result else "pending",
        })

    return {
        "date": game_date,
        "game_type": game_type,
        "track": track_name,
        "strategy": label,
        "cost": round(system.total_cost, 0),
        "rows": system.total_rows,
        "num_correct": partial_correct,
        "num_races": num_races,
        "hit": all_finished and partial_correct == num_races,
        "payout": round(total_payout, 0),
        "payout_per_row": round(payout_per_row, 0),
        "winning_rows": winning_rows,
        "avg_confidence": round(system.avg_confidence, 1),
        "avg_upset": round(system.avg_upset_risk, 1),
        "races": race_details,
        "live": not all_finished or not game_round.dividends,
        "races_finished": races_finished,
        "races_total": num_races,
        "partial_correct": partial_correct,
    }


def generate_live_entries(game_round) -> list[dict]:
    """Generera backlog-entries för en omgång.

    Regenererar system vid varje anrop med aktuell streckdata.
    Detta matchar backtestets beteende som använder slutgiltig streck.

    Returnerar:
        list[dict] — en entry per strategi som inte skippas.
    """
    entries = []

    for strategy, filt, budget, max_spikes, spike_conf, spike_gap, label in STRATEGIES:
        gen = SystemGenerator(
            budget=budget,
            strategy=strategy,
            selective_filter=filt,
            max_spikes=max_spikes,
            spike_conf_threshold=spike_conf,
            spike_score_gap=spike_gap,
        )
        system = gen.generate(game_round)

        if system.skip_round:
            continue

        entry = _system_to_entry(system, game_round, label)
        entries.append(entry)

    return entries
