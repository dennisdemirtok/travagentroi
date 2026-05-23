"""Persistering av backtest-resultat — spara/ladda JSON."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)

BACKTEST_DIR = PROJECT_ROOT / "backtest_results"


def save_backtest_run(
    game_type: str,
    start_date: date,
    end_date: date,
    predictions: list,
    results,
) -> Path:
    """Spara en komplett backtest-körning till JSON.

    Args:
        predictions: list[RoundPrediction]
        results: BacktestResults
    """
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    filename = BACKTEST_DIR / f"backtest_{game_type}_{start_date}_{end_date}.json"

    data = {
        "game_type": game_type,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "generated": str(date.today()),
        "predictions": _serialize_predictions(predictions),
        "results": _serialize_results(results),
    }
    filename.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Backtest sparad: {filename}")
    return filename


def load_backtest_run(path: Path | str) -> dict | None:
    """Ladda en sparad backtest-körning."""
    p = Path(path)
    if not p.exists():
        logger.warning(f"Backtest-fil saknas: {p}")
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_saved_runs() -> list[Path]:
    """Lista alla sparade backtest-körningar."""
    if not BACKTEST_DIR.exists():
        return []
    return sorted(BACKTEST_DIR.glob("backtest_*.json"), reverse=True)


def deserialize_predictions(data: dict) -> list:
    """Deserialisera predictions från JSON till RoundPrediction-objekt."""
    from .runner import RacePrediction, RoundPrediction

    predictions = []
    for rp_data in data.get("predictions", []):
        races = []
        for r_data in rp_data.get("races", []):
            # Konvertera string-nycklar till int för horse dicts
            horse_scores = {int(k): v for k, v in r_data.get("horse_scores", {}).items()}
            horse_bet_pct = {int(k): v for k, v in r_data.get("horse_bet_pct", {}).items()}
            horse_factor_scores = {
                int(k): v for k, v in r_data.get("horse_factor_scores", {}).items()
            }

            race = RacePrediction(
                race_number=r_data["race_number"],
                date=date.fromisoformat(r_data["date"]),
                track=r_data["track"],
                predicted_ranking=r_data.get("predicted_ranking", []),
                actual_result=r_data.get("actual_result", []),
                horse_scores=horse_scores,
                horse_bet_pct=horse_bet_pct,
                horse_factor_scores=horse_factor_scores,
                spik=r_data.get("spik"),
                top_picks=r_data.get("top_picks", []),
            )
            races.append(race)

        rp = RoundPrediction(
            game_type=rp_data["game_type"],
            date=date.fromisoformat(rp_data["date"]),
            races=races,
        )
        predictions.append(rp)

    return predictions


def _serialize_predictions(predictions: list) -> list[dict]:
    """Serialisera RoundPrediction-lista till JSON-vänligt format."""
    result = []
    for rp in predictions:
        races = []
        for r in rp.races:
            races.append({
                "race_number": r.race_number,
                "date": str(r.date),
                "track": r.track,
                "predicted_ranking": r.predicted_ranking,
                "actual_result": r.actual_result,
                "horse_scores": {str(k): v for k, v in r.horse_scores.items()},
                "horse_bet_pct": {str(k): v for k, v in r.horse_bet_pct.items()},
                "horse_factor_scores": {
                    str(k): v for k, v in r.horse_factor_scores.items()
                },
                "spik": r.spik,
                "top_picks": r.top_picks,
            })
        result.append({
            "game_type": rp.game_type,
            "date": str(rp.date),
            "races": races,
        })
    return result


def _serialize_results(results) -> dict:
    """Serialisera BacktestResults till JSON-vänligt format."""
    d = asdict(results)
    return d
