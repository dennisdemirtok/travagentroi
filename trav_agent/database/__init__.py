"""Database — Supabase-integration för persistent lagring av travdata."""

from .client import get_client, is_configured
from .sync import sync_game_round, sync_analysis_results

__all__ = ["get_client", "is_configured", "sync_game_round", "sync_analysis_results"]
