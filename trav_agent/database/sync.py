"""Sync — synkroniserar travdata till Supabase.

Alla operationer använder upsert för att vara idempotenta.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ..data.models import GameRound, Horse, PastStart, Race, RaceEntry

logger = logging.getLogger(__name__)


def _get_client():
    """Lazy import för att undvika cirkulära beroenden."""
    from .client import get_client
    return get_client()


# ── Upsert-hjälpare ─────────────────────────────────────────────────────────


def _upsert(table: str, data: dict | list[dict], on_conflict: str = "id",
            client=None) -> bool:
    """Upsert en eller flera rader. Returnerar True vid framgång."""
    if client is None:
        client = _get_client()
    try:
        if isinstance(data, list):
            if not data:
                return True
            # Batch i grupper om 100
            for i in range(0, len(data), 100):
                batch = data[i:i + 100]
                client.table(table).upsert(
                    batch, on_conflict=on_conflict
                ).execute()
        else:
            client.table(table).upsert(
                data, on_conflict=on_conflict
            ).execute()
        return True
    except Exception as e:
        logger.error(f"Upsert-fel i {table}: {e}")
        return False


# ── Horse → dict ─────────────────────────────────────────────────────────────


def _horse_to_dict(horse: Horse) -> dict:
    """Konvertera Horse-modell till databas-dict."""
    if not horse.id:
        return {}
    return {
        "id": horse.id,
        "name": horse.name,
        "age": horse.age,
        "sex": horse.sex.value,
        "breed": horse.breed.value,
        "sire": horse.sire,
        "dam": horse.dam,
        "dam_sire": horse.dam_sire,
        "trainer_name": horse.trainer_name,
        "trainer_id": horse.trainer_id,
        "owner_name": horse.owner_name,
        "career_starts": horse.career.total_starts,
        "career_wins": horse.career.wins,
        "career_seconds": horse.career.seconds,
        "career_thirds": horse.career.thirds,
        "career_prize_money": horse.career.total_prize_money,
        "career_win_rate": horse.career.win_rate,
        "career_top3_rate": horse.career.top3_rate,
        "career_avg_km_time": horse.career.avg_km_time,
        "career_prize_per_start": horse.career.prize_per_start,
        "career_avg_odds": horse.career.avg_odds,
    }


def _past_start_to_dict(horse_id: int, ps: PastStart) -> dict:
    """Konvertera PastStart till databas-dict."""
    return {
        "horse_id": horse_id,
        "start_date": str(ps.start_date),
        "track_name": ps.track_name,
        "track_id": ps.track_id,
        "distance": ps.distance,
        "start_method": ps.start_method.value,
        "post_position": ps.post_position,
        "placement": ps.placement,
        "disqualified": ps.disqualified,
        "galloped": ps.galloped,
        "finish_time": ps.finish_time,
        "km_time": ps.km_time,
        "prize_money": ps.prize_money,
        "driver_name": ps.driver_name,
        "trainer_name": ps.trainer_name,
        "num_starters": ps.num_starters,
        "odds": ps.odds,
        "race_purse": ps.race_purse,
        "comment": ps.comment,
    }


def _race_entry_to_dict(race_id: str, entry: RaceEntry) -> dict:
    """Konvertera RaceEntry till databas-dict."""
    horse_id = entry.horse.id
    if not horse_id:
        return {}
    return {
        "race_id": race_id,
        "horse_id": horse_id,
        "post_position": entry.post_position,
        "distance": entry.distance,
        "driver_name": entry.driver_name,
        "driver_id": entry.driver_id,
        "trainer_name": entry.trainer_name,
        "weight": entry.weight,
        "shoes": entry.shoes,
        "scratched": entry.scratched,
        "odds": entry.odds,
        "bet_percentage": entry.bet_percentage,
        "trend": entry.trend,
        "points": entry.points,
        "factor_scores": entry.factor_scores or {},
        "composite_score": entry.composite_score,
        "rank": entry.rank,
        "recommendation": entry.recommendation,
    }
    # super_score läggs till om kolumnen finns i Supabase
    # (kan läggas till via SQL: ALTER TABLE race_entries ADD COLUMN super_score REAL DEFAULT 0.0)


def _race_to_dict(race: Race, game_id: str) -> dict:
    """Konvertera Race till databas-dict."""
    return {
        "id": race.id,
        "game_id": game_id,
        "race_number": race.race_number,
        "track_id": race.track_id,
        "track_name": race.track_name,
        "race_date": str(race.race_date),
        "distance": race.distance,
        "start_method": race.start_method.value,
        "purse": race.purse,
        "race_type": race.race_type,
        "race_name": race.race_name,
        "conditions": race.conditions,
        "breed": race.breed.value,
        "is_stair_draw": race.is_stair_draw,
        "status": race.status.value,
        "num_starters": race.num_starters,
        "result_order": race.result_order,
        "win_odds": race.win_odds,
        "upset_risk": race.upset_risk,
        "upset_candidates": race.upset_candidates,
    }


def _game_round_to_dict(gr: GameRound) -> dict:
    """Konvertera GameRound till databas-dict."""
    return {
        "game_id": gr.game_id,
        "game_type": gr.game_type,
        "round_date": str(gr.round_date),
        "track_id": gr.track_id,
        "track_name": gr.track_name,
        "status": gr.status or ("finished" if gr.is_finished else "upcoming"),
        "turnover": gr.turnover,
        "jackpot": gr.jackpot,
    }


# ── Huvudfunktioner ──────────────────────────────────────────────────────────


def sync_game_round(game_round: GameRound, client=None) -> dict[str, int]:
    """Synka en hel spelomgång till Supabase.

    Upsert:ar tracks, game_rounds, races, horses, race_entries, past_starts.
    Returnerar dict med antal upsertade rader per tabell.
    """
    if client is None:
        client = _get_client()

    counts = {
        "tracks": 0, "game_rounds": 0, "races": 0,
        "horses": 0, "race_entries": 0, "past_starts": 0,
    }

    # 1. Track
    tracks_seen = set()
    for race in game_round.races:
        if race.track_id and race.track_id not in tracks_seen:
            _upsert("tracks", {"id": race.track_id, "name": race.track_name},
                     client=client)
            tracks_seen.add(race.track_id)
            counts["tracks"] += 1

    # 2. GameRound
    _upsert("game_rounds", _game_round_to_dict(game_round),
             on_conflict="game_id", client=client)
    counts["game_rounds"] = 1

    # 3. Races + entries
    for race in game_round.races:
        if not race.id:
            continue

        _upsert("races", _race_to_dict(race, game_round.game_id),
                 client=client)
        counts["races"] += 1

        horse_batch = []
        entry_batch = []
        start_batch = []

        for entry in race.entries:
            if not entry.horse.id:
                continue

            # Horse
            horse_dict = _horse_to_dict(entry.horse)
            if horse_dict:
                horse_batch.append(horse_dict)
                counts["horses"] += 1

            # Race entry
            entry_dict = _race_entry_to_dict(race.id, entry)
            if entry_dict:
                entry_batch.append(entry_dict)
                counts["race_entries"] += 1

            # Past starts
            for ps in entry.horse.past_starts:
                ps_dict = _past_start_to_dict(entry.horse.id, ps)
                start_batch.append(ps_dict)
                counts["past_starts"] += 1

        # Batch upsert
        if horse_batch:
            # Deduplicera hästar (samma häst kan finnas i flera entries)
            seen_horses = {}
            for h in horse_batch:
                seen_horses[h["id"]] = h
            _upsert("horses", list(seen_horses.values()), client=client)

        if entry_batch:
            _upsert("race_entries", entry_batch,
                     on_conflict="race_id,post_position", client=client)

        if start_batch:
            # Deduplicera past_starts inom batchen
            seen_starts = {}
            for s in start_batch:
                key = (s["horse_id"], s["start_date"], s["track_name"],
                       s["distance"], s["post_position"])
                seen_starts[key] = s
            deduped = list(seen_starts.values())
            _upsert("past_starts", deduped,
                     on_conflict="horse_id,start_date,track_name,distance,post_position",
                     client=client)

    return counts


def sync_analysis_results(game_round: GameRound, client=None) -> int:
    """Uppdatera race_entries med analysresultat (factor_scores, composite_score etc).

    Returnerar antal uppdaterade entries.
    """
    if client is None:
        client = _get_client()

    updated = 0
    for race in game_round.races:
        if not race.id:
            continue

        # Uppdatera race-nivå: upset_risk, upset_candidates
        try:
            client.table("races").update({
                "upset_risk": race.upset_risk,
                "upset_candidates": race.upset_candidates,
                "status": race.status.value,
                "result_order": race.result_order,
                "win_odds": race.win_odds,
            }).eq("id", race.id).execute()
        except Exception as e:
            logger.warning(f"Kunde inte uppdatera race {race.id}: {e}")

        # Uppdatera entries
        for entry in race.active_entries:
            if not entry.horse.id:
                continue
            try:
                update_data = {
                    "factor_scores": entry.factor_scores or {},
                    "composite_score": entry.composite_score,
                    "rank": entry.rank,
                    "recommendation": entry.recommendation,
                }
                # Försök med super_score (kolumnen kanske inte finns ännu)
                try:
                    update_data["super_score"] = entry.super_score
                    client.table("race_entries").update(update_data).eq(
                        "race_id", race.id
                    ).eq("post_position", entry.post_position).execute()
                except Exception:
                    # Fallback utan super_score
                    update_data.pop("super_score", None)
                    client.table("race_entries").update(update_data).eq(
                        "race_id", race.id
                    ).eq("post_position", entry.post_position).execute()
                updated += 1
            except Exception as e:
                logger.warning(f"Kunde inte uppdatera entry: {e}")

    return updated


def sync_raw_response(url: str, data: dict, client=None) -> bool:
    """Spara rå API-svar i raw_api_responses."""
    if client is None:
        client = _get_client()

    url_hash = hashlib.md5(url.encode()).hexdigest()
    return _upsert(
        "raw_api_responses",
        {"url": url, "url_hash": url_hash, "response": data},
        on_conflict="url_hash",
        client=client,
    )


def sync_predictions(
    predictions: list,
    results: Any,
    game_type: str,
    start_date: date,
    end_date: date,
    client=None,
) -> int:
    """Spara backtest-prediktioner och resultat.

    Returnerar backtest_run_id.
    """
    if client is None:
        client = _get_client()

    # 1. Backtest run
    run_data = {
        "game_type": game_type,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "num_rounds": getattr(results, "num_rounds", 0),
        "num_races": getattr(results, "num_races", 0),
        "spik_accuracy": getattr(results, "spik_accuracy", 0),
        "top_pick_accuracy": getattr(results, "top_pick_accuracy", 0),
        "avg_top3_overlap": getattr(results, "avg_top3_overlap", 0),
        "roi_percent": getattr(results, "roi_percent", 0),
    }

    try:
        resp = client.table("backtest_runs").insert(run_data).execute()
        run_id = resp.data[0]["id"] if resp.data else None
    except Exception as e:
        logger.error(f"Kunde inte spara backtest_run: {e}")
        return 0

    if not run_id:
        return 0

    # 2. Race predictions
    pred_batch = []
    for rp in predictions:
        for race_pred in rp.races:
            pred_batch.append({
                "backtest_run_id": run_id,
                "race_number": race_pred.race_number,
                "race_date": str(race_pred.date),
                "track": race_pred.track,
                "game_type": rp.game_type,
                "predicted_ranking": race_pred.predicted_ranking,
                "actual_result": race_pred.actual_result,
                "horse_scores": {str(k): v for k, v in race_pred.horse_scores.items()},
                "horse_bet_pct": {str(k): v for k, v in race_pred.horse_bet_pct.items()},
                "horse_factor_scores": {
                    str(k): v for k, v in race_pred.horse_factor_scores.items()
                },
                "spik": race_pred.spik,
                "top_picks": race_pred.top_picks,
                "spik_correct": race_pred.spik_correct,
            })

    if pred_batch:
        _upsert("race_predictions", pred_batch, on_conflict="id", client=client)

    return run_id


# ── Spara analysresultat ─────────────────────────────────────────────────────


def sync_analysis_run(
    run_type: str,
    parameters: dict,
    results: dict,
    summary: str = "",
    num_rounds: int = 0,
    client=None,
) -> int:
    """Spara analysresultat (walk-forward, sensitivity, game-type) till Supabase.

    Args:
        run_type: 'walk_forward', 'walk_forward_extended', 'sensitivity', 'game_type_analysis'
        parameters: Test-parametrar (datum, config-värden etc.)
        results: Fullständigt resultat-dict (ROI per strategi, shrinkage etc.)
        summary: Läsbar sammanfattning
        num_rounds: Totalt antal analyserade omgångar

    Returns:
        analysis_run_id eller 0 vid fel
    """
    if client is None:
        client = _get_client()

    run_data = {
        "run_type": run_type,
        "parameters": json.dumps(parameters, default=str),
        "results": json.dumps(results, default=str),
        "summary": summary,
        "num_rounds": num_rounds,
    }

    try:
        resp = client.table("analysis_runs").insert(run_data).execute()
        run_id = resp.data[0]["id"] if resp.data else 0
        logger.info(f"Sparade analysis_run {run_type}: id={run_id}")
        return run_id
    except Exception as e:
        logger.error(f"Kunde inte spara analysis_run: {e}")
        return 0
