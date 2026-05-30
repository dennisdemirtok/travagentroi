"""Skrällindex — kopplar expertkonsensus-data till skrällrisk per avdelning.

Dennis tumregel (CLAUDE.md): "om 3+ expertsystem gardar 8+ hästar = hög skrällrisk".
Här kombineras tre signaler till ett skrällindex 0-100 per avdelning:

1. **Breddkonsensus** — hur många källor gardar brett (≥6 / ≥8 hästar). Många
   breda system = öppet lopp = hög skrällrisk.
2. **Modellsvårighet** — ``predict_difficulty`` (kalibrerad på 1 862 lopp).
   Hög svårighet = vinnaren hamnar djupt = skräll.
3. **Spetstrid** — låg spets-konfidens = öppet tempo = skräll.

Returnerar även de utpekade skräll-hästarna (explicita ``skrallar`` + ``best_drag``).
"""

from __future__ import annotations

import re
from typing import Optional


def _horse_nums(value) -> list[int]:
    """Extract distinct horse numbers from a pick string / ranking / list."""
    if value is None:
        return []
    if isinstance(value, dict):
        # tiered ranking {'A': '3-11', ...}
        nums: list[int] = []
        for v in value.values():
            nums += [int(n) for n in re.findall(r"\d+", str(v))]
    elif isinstance(value, (list, tuple)):
        nums = [int(n) for n in re.findall(r"\d+", " ".join(str(x) for x in value))]
    else:
        s = str(value)
        if "SPIK" in s.upper():
            # spik = 1 häst, inte bredd
            m = re.findall(r"\d+", s.split("(")[0])
            return [int(m[0])] if m else []
        nums = [int(n) for n in re.findall(r"\d+", s)]
    # de-dup, behåll ordning
    seen: list[int] = []
    for n in nums:
        if n not in seen:
            seen.append(n)
    return seen


def _leg_lookup(block: dict, gt: str, leg: int):
    """Hämta värde ur ett block som kan vara keyat 'N' eller 'GT-N'."""
    if not isinstance(block, dict):
        return None
    for key in (str(leg), f"{gt}-{leg}", f"{gt}-{leg}".lower()):
        if key in block:
            return block[key]
    return None


def compute_skrall_index(
    game_round,
    tips_raw: Optional[dict] = None,
    *,
    wide_threshold: int = 6,
    very_wide_threshold: int = 8,
) -> dict[int, dict]:
    """Beräkna skrällindex per avdelning (race_number → info-dict).

    Varje värde: {
        "score": float 0-100,
        "level": "hög" | "medel" | "låg",
        "n_wide": int,           # källor som gardar ≥ wide_threshold
        "n_very_wide": int,      # källor som gardar ≥ very_wide_threshold
        "difficulty": float,
        "spets_conf": str|None,
        "skrall_horses": [int],  # utpekade skräll-hästar
        "reasons": [str],
    }
    """
    from .system_builder import predict_difficulty

    tips_raw = tips_raw or {}
    sources = tips_raw.get("sources", {})
    spetstrid = tips_raw.get("spetstrid", {})
    gt = game_round.game_type or "V85"

    # Explicita skräll-hästar kan ligga i en proffs_konsensus-källa
    konsensus = sources.get("proffs_konsensus", {}) or sources.get("proffs_konsensus_auto", {})
    global_skrallar = konsensus.get("skrallar", {}) if isinstance(konsensus, dict) else {}

    out: dict[int, dict] = {}
    for race in game_round.races:
        leg = race.race_number
        difficulty = predict_difficulty(race)

        # 1. Breddkonsensus över alla källor
        n_wide = 0
        n_very_wide = 0
        for src_key, src in sources.items():
            if not isinstance(src, dict):
                continue
            picks = _leg_lookup(src.get("picks", {}), gt, leg)
            ranking = _leg_lookup(src.get("rankings", {}), gt, leg)
            count = max(len(_horse_nums(picks)), 0)
            # rankings räknas bara om de inte är en full ordnad lista (>10 = hela fältet)
            rcount = len(_horse_nums(ranking))
            if 0 < rcount <= 10:
                count = max(count, rcount)
            if count >= wide_threshold:
                n_wide += 1
            if count >= very_wide_threshold:
                n_very_wide += 1

        # 2. Spetstrid-konfidens
        sp = _leg_lookup(spetstrid, gt, leg) or {}
        spets_conf = sp.get("confidence") if isinstance(sp, dict) else None
        spets_open = 0.0
        if spets_conf == "låg":
            spets_open = 1.0
        elif spets_conf == "medel":
            spets_open = 0.5

        # 3. Skräll-hästar (explicita)
        skrall_horses = _horse_nums(_leg_lookup(global_skrallar, gt, leg))
        for src in sources.values():
            if isinstance(src, dict):
                skrall_horses += _horse_nums(_leg_lookup(src.get("skrallar", {}), gt, leg))
        # best_drag (kan nämna denna avd)
        seen: list[int] = []
        for n in skrall_horses:
            if n not in seen:
                seen.append(n)
        skrall_horses = seen

        # ── Sammanvägt index ──
        n_sources = max(len(sources), 1)
        wide_ratio = n_wide / n_sources
        score = (
            0.50 * difficulty
            + 30.0 * wide_ratio
            + 20.0 * spets_open
        )
        # Dennis-regel: 3+ system gardar 8+ hästar → tvinga upp till hög
        if n_very_wide >= 3:
            score = max(score, 60.0)
        score = max(0.0, min(100.0, score))

        if score >= 55:
            level = "hög"
        elif score >= 35:
            level = "medel"
        else:
            level = "låg"

        reasons = []
        if difficulty >= 53:
            reasons.append(f"modellsvårighet {difficulty:.0f}")
        if n_very_wide >= 3:
            reasons.append(f"{n_very_wide} system gardar 8+ hästar")
        elif n_wide >= 2:
            reasons.append(f"{n_wide} källor gardar brett")
        if spets_conf == "låg":
            reasons.append("öppen spetsstrid")
        if skrall_horses:
            reasons.append("utpekad skräll: " + ",".join(str(h) for h in skrall_horses[:3]))

        out[leg] = {
            "score": round(score, 1),
            "level": level,
            "n_wide": n_wide,
            "n_very_wide": n_very_wide,
            "difficulty": round(difficulty, 1),
            "spets_conf": spets_conf,
            "skrall_horses": skrall_horses,
            "reasons": reasons,
        }

    return out
