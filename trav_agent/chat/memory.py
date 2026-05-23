"""Agent memory — knowledge system and session persistence.

Provides track-pattern analysis, historical performance context,
and file-backed chat session storage that survives server restarts.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Session persistence (file-backed) ─────────────────────────────────────

_SESSIONS_DIR = Path(__file__).parent.parent.parent / "chat_sessions"
_SESSIONS_DIR.mkdir(exist_ok=True)

# In-memory cache (hot layer)
_chat_sessions: dict[str, list] = {}
_session_meta: dict[str, dict] = {}


def _session_file(round_key: str) -> Path:
    """Return file path for a session, e.g. chat_sessions/V85__2026-05-23.json."""
    safe_key = round_key.replace("/", "__")
    return _SESSIONS_DIR / f"{safe_key}.json"


def save_session(round_key: str, messages: list) -> None:
    """Save chat messages for a round (both in-memory and to disk).

    Args:
        round_key: e.g. "V85/2026-05-23"
        messages: List of {role, content} dicts.
    """
    _chat_sessions[round_key] = list(messages)

    # Build metadata
    user_msgs = [m for m in messages if m.get("role") == "user"]
    first_q = user_msgs[0]["content"][:80] if user_msgs else ""
    meta = {
        "round_key": round_key,
        "message_count": len(messages),
        "first_question": first_q,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if round_key not in _session_meta:
        meta["created_at"] = meta["updated_at"]
    else:
        meta["created_at"] = _session_meta[round_key].get("created_at", meta["updated_at"])
    _session_meta[round_key] = meta

    # Persist to disk
    try:
        path = _session_file(round_key)
        data = {"meta": meta, "messages": messages}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save session {round_key}: {e}")


def load_session(round_key: str) -> list:
    """Load previous chat messages for a round.

    Checks in-memory cache first, falls back to disk.

    Args:
        round_key: e.g. "V85/2026-05-23"

    Returns:
        List of {role, content} dicts, or empty list.
    """
    if round_key in _chat_sessions:
        return list(_chat_sessions[round_key])

    # Try loading from disk
    path = _session_file(round_key)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            messages = data.get("messages", [])
            _chat_sessions[round_key] = messages
            if data.get("meta"):
                _session_meta[round_key] = data["meta"]
            return list(messages)
        except Exception as e:
            logger.warning(f"Failed to load session {round_key}: {e}")

    return []


def clear_session(round_key: str) -> None:
    """Clear stored chat session for a round (memory + disk)."""
    _chat_sessions.pop(round_key, None)
    _session_meta.pop(round_key, None)
    path = _session_file(round_key)
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed to delete session file {round_key}: {e}")


def list_sessions() -> list[dict]:
    """List all saved chat sessions with metadata.

    Returns:
        List of dicts with round_key, message_count, first_question,
        created_at, updated_at. Sorted by most recent first.
    """
    # Load any on-disk sessions not yet in memory
    try:
        for path in _SESSIONS_DIR.glob("*.json"):
            key = path.stem.replace("__", "/")
            if key not in _session_meta:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    meta = data.get("meta", {})
                    if meta:
                        _session_meta[key] = meta
                        _chat_sessions[key] = data.get("messages", [])
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"Failed to scan sessions dir: {e}")

    sessions = list(_session_meta.values())
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions


# ── Persistent learnings (cross-session memory) ───────────────────────────

_LEARNINGS_FILE = _SESSIONS_DIR / "_learnings.json"
_learnings: list[dict] | None = None


def _load_learnings() -> list[dict]:
    """Load learnings from disk, cached in memory."""
    global _learnings
    if _learnings is not None:
        return _learnings

    if _LEARNINGS_FILE.exists():
        try:
            _learnings = json.loads(_LEARNINGS_FILE.read_text(encoding="utf-8"))
            return _learnings
        except Exception as e:
            logger.warning(f"Failed to load learnings: {e}")

    _learnings = []
    return _learnings


def _save_learnings() -> None:
    """Persist learnings to disk."""
    if _learnings is None:
        return
    try:
        _SESSIONS_DIR.mkdir(exist_ok=True)
        _LEARNINGS_FILE.write_text(
            json.dumps(_learnings, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save learnings: {e}")


def add_learning(learning: str, category: str = "general", round_key: str = "") -> None:
    """Add a learning insight that persists across sessions.

    Args:
        learning: The insight text (e.g. "User prefers 0.50 kr/rad as base cost")
        category: One of "preference", "correction", "strategy", "general"
        round_key: Which round this came from
    """
    entries = _load_learnings()
    entries.append({
        "text": learning,
        "category": category,
        "round_key": round_key,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    # Keep max 100 learnings (FIFO)
    if len(entries) > 100:
        _learnings.clear()
        _learnings.extend(entries[-100:])
    _save_learnings()
    logger.info(f"Added learning [{category}]: {learning[:60]}")


def get_learnings_context() -> str:
    """Build a context string from all stored learnings for the agent.

    Returns:
        Formatted string for inclusion in the system prompt.
    """
    entries = _load_learnings()
    if not entries:
        return ""

    lines = [
        "## Minne från tidigare sessioner",
        "Dessa insikter har sparats från tidigare konversationer:\n",
    ]

    by_cat: dict[str, list[str]] = {}
    for e in entries:
        cat = e.get("category", "general")
        by_cat.setdefault(cat, []).append(e.get("text", ""))

    cat_labels = {
        "preference": "Användarpreferenser",
        "correction": "Rättelser & korrigeringar",
        "strategy": "Strategiinsikter",
        "general": "Allmänt",
    }
    for cat, label in cat_labels.items():
        items = by_cat.get(cat, [])
        if items:
            lines.append(f"**{label}:**")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines)


def extract_learnings_from_session(messages: list[dict], round_key: str = "") -> None:
    """Analyze a completed chat session and extract learnings automatically.

    Looks for:
    - User corrections ("nej", "fel", "inte så", "rätta")
    - Preferences ("jag vill", "jag föredrar", "använd alltid")
    - Strategy insights from assistant analysis
    """
    if not messages or len(messages) < 2:
        return

    # Simple heuristic: scan user messages for correction/preference patterns
    correction_patterns = [
        "nej ", "fel", "inte så", "rätta", "korrigera", "stämmer inte",
        "istället", "borde vara", "det ska vara", "ändra",
    ]
    preference_patterns = [
        "jag vill", "jag föredrar", "använd alltid", "visa alltid",
        "skippa", "inkludera alltid", "tänk på att", "kom ihåg",
        "från och med nu", "framöver",
    ]

    for msg in messages:
        if msg.get("role") != "user":
            continue
        text_lower = (msg.get("content") or "").lower()

        for pat in correction_patterns:
            if pat in text_lower:
                # Extract the correction (use the full user message, truncated)
                content = msg.get("content", "")[:200]
                add_learning(
                    f"Användarrättelse: {content}",
                    category="correction",
                    round_key=round_key,
                )
                break

        for pat in preference_patterns:
            if pat in text_lower:
                content = msg.get("content", "")[:200]
                add_learning(
                    f"Användarpreferens: {content}",
                    category="preference",
                    round_key=round_key,
                )
                break


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
