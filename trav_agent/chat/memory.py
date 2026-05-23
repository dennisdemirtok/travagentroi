"""Agent memory — knowledge system and session persistence.

Provides track-pattern analysis, historical performance context,
and in-memory chat session storage.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Session persistence (in-memory) ─────────────────────────────────────────

_chat_sessions: dict[str, list] = {}


def save_session(round_key: str, messages: list) -> None:
    """Save chat messages for a round.

    Args:
        round_key: e.g. "V85/2026-05-23"
        messages: List of {role, content} dicts.
    """
    _chat_sessions[round_key] = list(messages)


def load_session(round_key: str) -> list:
    """Load previous chat messages for a round.

    Args:
        round_key: e.g. "V85/2026-05-23"

    Returns:
        List of {role, content} dicts, or empty list.
    """
    return list(_chat_sessions.get(round_key, []))


def clear_session(round_key: str) -> None:
    """Clear stored chat session for a round."""
    _chat_sessions.pop(round_key, None)


# ── Track knowledge ─────────────────────────────────────────────────────────

def build_track_knowledge(backlog_data: dict, track_name: str) -> str:
    """Extract track-specific patterns from backlog data.

    Analyzes:
    - Win rate by strategy on this track
    - Upset frequency at this track
    - Historical ROI on this track

    Args:
        backlog_data: Dict with "entries" key containing backlog entries.
        track_name: Name of the track to analyze.

    Returns:
        Formatted string with track patterns.
    """
    if not backlog_data or not track_name:
        return ""

    entries = backlog_data.get("entries", [])
    track_lower = track_name.lower()

    track_entries = [
        e for e in entries
        if (e.get("track", "") or "").lower() == track_lower
        and not e.get("live")
    ]

    if len(track_entries) < 3:
        return ""

    lines = []
    lines.append(f"\n--- Banmönster: {track_name} ({len(track_entries)} omgångar) ---")

    # Overall track stats
    total_cost = sum(e.get("cost", 0) or 0 for e in track_entries)
    total_payout = sum(e.get("payout", 0) or 0 for e in track_entries)
    hits = sum(1 for e in track_entries if (e.get("payout") or 0) > 0)
    roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
    win_rate = hits / len(track_entries) * 100

    lines.append(f"  Vinstfrekvens: {win_rate:.0f}% ({hits}/{len(track_entries)})")
    lines.append(f"  ROI: {roi:+.1f}%")

    # Per-strategy breakdown on this track
    by_strat: dict[str, dict] = {}
    for e in track_entries:
        s = e.get("strategy", "okänd")
        if s not in by_strat:
            by_strat[s] = {"cost": 0, "payout": 0, "n": 0, "hits": 0}
        by_strat[s]["cost"] += e.get("cost", 0) or 0
        by_strat[s]["payout"] += e.get("payout", 0) or 0
        by_strat[s]["n"] += 1
        if (e.get("payout") or 0) > 0:
            by_strat[s]["hits"] += 1

    if by_strat:
        lines.append(f"  Per strategi på {track_name}:")
        for s in sorted(by_strat, key=lambda x: -(by_strat[x]["payout"] - by_strat[x]["cost"])):
            d = by_strat[s]
            s_roi = (d["payout"] - d["cost"]) / d["cost"] * 100 if d["cost"] > 0 else 0
            s_wr = d["hits"] / d["n"] * 100 if d["n"] > 0 else 0
            lines.append(
                f"    {s}: {d['n']} omg, vinstfrekvens {s_wr:.0f}%, "
                f"ROI {s_roi:+.1f}%"
            )

    # Upset analysis: how often does this track produce upsets
    # (based on low-confidence hits and misses)
    high_conf_entries = [
        e for e in track_entries if (e.get("avg_confidence") or 0) > 70
    ]
    low_conf_entries = [
        e for e in track_entries if (e.get("avg_confidence") or 0) < 50
    ]
    if high_conf_entries:
        hc_hit = sum(1 for e in high_conf_entries if (e.get("payout") or 0) > 0)
        hc_rate = hc_hit / len(high_conf_entries) * 100
        lines.append(f"  Hög konfidens (>70): {hc_rate:.0f}% träff ({hc_hit}/{len(high_conf_entries)})")
    if low_conf_entries:
        lc_hit = sum(1 for e in low_conf_entries if (e.get("payout") or 0) > 0)
        lc_rate = lc_hit / len(low_conf_entries) * 100
        lines.append(f"  Låg konfidens (<50): {lc_rate:.0f}% träff ({lc_hit}/{len(low_conf_entries)})")

    return "\n".join(lines)


def build_historical_context(backlog_data: dict, game_type: str) -> str:
    """Summarize strategy performance for a game type.

    Args:
        backlog_data: Dict with "entries" and "strategies" keys.
        game_type: e.g. "V85", "V75"

    Returns:
        Formatted string with strategy performance summary.
    """
    if not backlog_data:
        return ""

    entries = backlog_data.get("entries", [])
    gt_entries = [
        e for e in entries
        if e.get("game_type") == game_type and not e.get("live")
    ]

    if not gt_entries:
        return ""

    lines = []
    lines.append(f"\n--- Strategiprestation {game_type} ---")

    # Per-strategy stats
    by_strat: dict[str, dict] = {}
    for e in gt_entries:
        s = e.get("strategy", "okänd")
        if s not in by_strat:
            by_strat[s] = {"cost": 0, "payout": 0, "n": 0, "hits": 0, "dates": []}
        by_strat[s]["cost"] += e.get("cost", 0) or 0
        by_strat[s]["payout"] += e.get("payout", 0) or 0
        by_strat[s]["n"] += 1
        if (e.get("payout") or 0) > 0:
            by_strat[s]["hits"] += 1
        by_strat[s]["dates"].append(e.get("date", ""))

    # Sort by net profit
    for s in sorted(by_strat, key=lambda x: -(by_strat[x]["payout"] - by_strat[x]["cost"])):
        d = by_strat[s]
        s_roi = (d["payout"] - d["cost"]) / d["cost"] * 100 if d["cost"] > 0 else 0
        s_wr = d["hits"] / d["n"] * 100 if d["n"] > 0 else 0
        netto = d["payout"] - d["cost"]

        # Recent streak
        recent_dates = sorted(d["dates"], reverse=True)[:5]
        recent_entries = [
            e for e in gt_entries
            if e.get("strategy") == s and e.get("date") in recent_dates
        ]
        recent_hits = sum(1 for e in recent_entries if (e.get("payout") or 0) > 0)

        lines.append(
            f"  {s}: {d['n']} omg, vinstfrekvens {s_wr:.0f}%, "
            f"ROI {s_roi:+.1f}%, netto {netto:+,.0f} kr "
            f"(senaste 5: {recent_hits} träff)"
        )

    return "\n".join(lines)


def build_learning_context(backlog_data: dict, game_round) -> str:
    """Combine all learned knowledge relevant to the current round.

    Args:
        backlog_data: Full backlog data dict.
        game_round: Current GameRound object.

    Returns:
        Combined context string for the AI agent.
    """
    if not backlog_data or not game_round:
        return ""

    parts = []

    # Track-specific knowledge
    track_name = game_round.track_name or ""
    if track_name:
        track_ctx = build_track_knowledge(backlog_data, track_name)
        if track_ctx:
            parts.append(track_ctx)

    # Game type strategy performance
    game_type = game_round.game_type
    if game_type:
        hist_ctx = build_historical_context(backlog_data, game_type)
        if hist_ctx:
            parts.append(hist_ctx)

    # Recent form: last 5 rounds of this game type
    entries = backlog_data.get("entries", [])
    gt_recent = sorted(
        [e for e in entries if e.get("game_type") == game_type and not e.get("live")],
        key=lambda e: e.get("date", ""),
        reverse=True,
    )[:5]

    if gt_recent:
        parts.append(f"\n--- Senaste 5 {game_type}-omgångarna ---")
        for e in gt_recent:
            cost = e.get("cost", 0) or 0
            payout = e.get("payout", 0) or 0
            netto = payout - cost
            strat = e.get("strategy", "?")
            track = e.get("track", "?")
            result = "TRÄFF" if payout > 0 else "MISS"
            parts.append(
                f"  {e.get('date', '')} {track} ({strat}): "
                f"netto {netto:+,.0f} kr {result}"
            )

    if not parts:
        return ""

    header = [
        "",
        "=" * 50,
        "INLÄRDA MÖNSTER & KONTEXT",
        "=" * 50,
    ]
    return "\n".join(header + parts)
