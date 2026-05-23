"""Supabase-klient — anslutning och singleton-hantering."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ladda .env från projektrot
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_KEY", "")

_client = None


def is_configured() -> bool:
    """Returnera True om Supabase-credentials finns."""
    return bool(SUPABASE_URL and SUPABASE_KEY)


def get_client():
    """Hämta eller skapa Supabase-klient (singleton).

    Returns:
        supabase.Client instans

    Raises:
        RuntimeError om credentials saknas
    """
    global _client
    if _client is not None:
        return _client

    if not is_configured():
        raise RuntimeError(
            "Supabase ej konfigurerad. Skapa .env med SUPABASE_URL och SUPABASE_KEY"
        )

    from supabase import create_client
    _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info(f"Supabase ansluten: {SUPABASE_URL}")
    return _client


def get_table_counts() -> dict[str, int]:
    """Hämta radantal för alla tabeller."""
    client = get_client()
    # Tabell → vilken kolumn att räkna (PK)
    tables = {
        "tracks": "id",
        "game_rounds": "game_id",
        "races": "id",
        "horses": "id",
        "past_starts": "id",
        "race_entries": "id",
        "backtest_runs": "id",
        "race_predictions": "id",
        "raw_api_responses": "id",
    }
    counts = {}
    for table, pk_col in tables.items():
        try:
            resp = client.table(table).select(pk_col, count="exact").limit(0).execute()
            counts[table] = resp.count if resp.count is not None else 0
        except Exception as e:
            counts[table] = -1
            logger.warning(f"Kunde inte räkna {table}: {e}")
    return counts
