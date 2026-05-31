"""Proffstips-pipeline — strukturera rå artikeltext → tips_cache + bygg konsensus.

Tvådelad:

1. ``structure_source_with_llm`` — tar rå artikeltext (klistrad från
   Aftonbladet/Expressen/Sharps/Travcash etc.) och låter Anthropic-modellen
   omvandla den till tips_cache-källschemat (picks / rankings / best_spike /
   best_drag / summary), keyat på avdelnings-nummer "V85-1".."V85-8".

2. ``build_consensus_from_sources`` — aggregerar alla källor i cachefilen till
   det top-level ``expert_tips``-blocket (leg 1-8 → topp-hästar) som system-
   byggaren auto-laddar, samt en syntetisk ``proffs_konsensus_auto``-källa.

Veckorutin::

    text = "<klistra in artikel>"
    await ingest_raw_text("V85", "2026-05-30", "expressen_edholm", text, roster)
    # → källan struktureras, sparas och konsensus räknas om automatiskt
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx

from .tips_scraper import (
    add_source_to_cache,
    load_tips_cache_raw,
)

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent.parent / "tips_cache"


def _write_tips_cache_raw(game_type: str, date_str: str, raw: dict) -> None:
    """Skriv tillbaka hela råa tips_cache-dicten till disk."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"{game_type}_{date_str}.json"
    cache_file.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# Återanvänd källvikterna från chat-agenten så konsensus blir konsekvent.
_SOURCE_WEIGHTS = {
    "model": 3.0,
    "sharps_berglund": 2.5,
    "sharps_jensa": 2.0,
    "sharps_jens_sjoden": 2.0,
    "expressen_edholm": 2.0,
    "proffs_konsensus": 2.5,
    "travcash": 1.5,
    "tom_parneborg": 1.2,
    "julia_bjorklund": 1.0,
    "magnus_nygren": 1.0,
    "aftonbladet_mario": 1.0,
    "aftonbladet_robert": 1.0,
    "aftonbladet_kim": 1.0,
    "aftonbladet_quist": 1.0,
    "aftonbladet_nils": 1.0,
}
_DEFAULT_WEIGHT = 0.8
_TIER_POINTS = {"A": 4.0, "B": 3.0, "BC": 2.0, "C": 1.0, "D": 0.0}


# ── 1. LLM-strukturerare ────────────────────────────────────────────────────

def _build_roster_text(roster: Optional[dict[int, list]]) -> str:
    """Format roster {leg: [(num, name), ...]} as a reference block for the LLM."""
    if not roster:
        return "(ingen startlista tillgänglig — använd hästnummer som de nämns i texten)"
    lines = []
    for leg in sorted(roster.keys()):
        horses = roster[leg]
        parts = []
        for h in horses:
            if isinstance(h, (list, tuple)) and len(h) >= 2:
                parts.append(f"{h[0]} {h[1]}")
            else:
                parts.append(str(h))
        lines.append(f"  Avd {leg}: " + ", ".join(parts))
    return "\n".join(lines)


def _structure_prompt(game_type: str, date_str: str, roster_text: str, raw_text: str) -> str:
    n_legs = 8 if game_type in ("V85", "V75") else 7
    leg_keys = ", ".join(f'"{game_type}-{i}"' for i in range(1, n_legs + 1))
    return f"""Du är en expert på svensk travsport. Strukturera nedanstående RÅ artikeltext
till strikt JSON enligt schemat. Detta gäller {game_type} {date_str}.

STARTLISTA (hästnummer → namn per avdelning/leg 1-{n_legs}):
{roster_text}

VIKTIGT:
- Mappa hästnamn i texten till rätt NUMMER via startlistan ovan.
- Nyckla allt på leg-nummer: {leg_keys}. Avd 1 = första V-loppet osv.
- Om texten refererar till bana-loppnummer (t.ex. "lopp 9"), översätt till rätt leg via startlistan.
- Lämna fält tomma ({{}}) om informationen saknas. Hitta ALDRIG på hästar.
- Returnera ENBART giltig JSON, ingen text runt om.

SCHEMA:
{{
  "author": "<tipsterns/källans namn>",
  "summary": "<1-2 meningar sammanfattning>",
  "picks": {{ "{game_type}-1": "<t.ex. 'SPIK: 8 Clarissa' eller '3,6,2'>", ... }},
  "rankings": {{ "{game_type}-1": "<ordnad lista, t.ex. '8-4-9-5-7'>", ... }},
  "best_spike": "<bästa spik enligt källan, eller ''>",
  "best_drag": "<bästa skräll/drag, eller ''>",
  "skrallar": {{ "{game_type}-1": [<hästnummer>], ... }},
  "interviews": {{ "{game_type}-1": [{{ "num": <hästnummer>, "name": "<hästnamn>", "role": "kusk|tränare|expert|annan", "person": "<personens namn om nämnt, annars ''>", "quote": "<exakt citat eller kort parafras av vad personen säger om hästen>", "sentiment": "positiv|neutral|negativ" }}] }},
  "trainer_comments": {{ }},
  "spetstrid": {{ "{game_type}-1": {{ "predicted_leader": "<num namn>", "confidence": "låg|medel|hög", "contenders": ["<num namn>"], "notes": "<kort>" }} }}
}}

INTERVJUER ÄR VIKTIGT:
- Fånga ALLA uttalanden från kuskar och tränare om sina hästar (startkommentarer,
  citat, "körguide", "så resonerar tränaren"). Tränarnas tankar är värdefulla.
- En häst kan ha flera intervjuer (både kusk och tränare) — lägg en post per uttalande.
- Sätt sentiment utifrån hur positiv personen låter inför loppet.
- Mappa varje intervju till rätt leg + hästnummer via startlistan.

RÅ ARTIKELTEXT:
\"\"\"
{raw_text}
\"\"\"
"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: grab outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def structure_source_with_llm(
    raw_text: str,
    game_type: str,
    date_str: str,
    *,
    source_name: str,
    roster: Optional[dict[int, list]] = None,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """Convert raw article text into a tips_cache source dict via Anthropic API.

    Args:
        raw_text: Pasted/scraped article text.
        game_type: e.g. "V85".
        date_str: ISO date, e.g. "2026-05-30".
        source_name: cache key for the source, e.g. "expressen_edholm".
        roster: {leg: [(num, name), ...]} so the LLM can map names→numbers.
        api_key: Anthropic key (defaults to ANTHROPIC_API_KEY env).

    Returns:
        Source dict (picks/rankings/best_spike/...) or None on failure.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("structure_source_with_llm: ANTHROPIC_API_KEY saknas")
        return None
    if not raw_text or len(raw_text.strip()) < 40:
        logger.warning("structure_source_with_llm: för kort text, hoppar över")
        return None

    roster_text = _build_roster_text(roster)
    prompt = _structure_prompt(game_type, date_str, roster_text, raw_text)
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        if resp.status_code != 200:
            logger.error(f"Anthropic API {resp.status_code}: {resp.text[:300]}")
            return None
        text = resp.json()["content"][0]["text"]

    data = _extract_json(text)
    if not data:
        logger.error("structure_source_with_llm: kunde inte parsa JSON ur svaret")
        return None

    data.setdefault("author", source_name)
    # Drop empty optional blocks to keep the cache clean
    for k in ("trainer_comments", "spetstrid", "skrallar", "interviews"):
        if k in data and not data[k]:
            del data[k]
    return data


# ── 2. Konsensus-aggregator ─────────────────────────────────────────────────

def _parse_ordered(ranking_str: str) -> dict[str, str]:
    """'8-4-9-5-7' → {num: tier} by position."""
    nums = [n.strip() for n in str(ranking_str).split("-") if n.strip().isdigit()]
    if not nums:
        return {}
    total = len(nums)
    tiers: dict[str, str] = {}
    for i, num in enumerate(nums):
        pct = i / total
        if pct < 0.15:
            tiers[num] = "A"
        elif pct < 0.35:
            tiers[num] = "B"
        elif pct < 0.55:
            tiers[num] = "BC"
        elif pct < 0.80:
            tiers[num] = "C"
        else:
            tiers[num] = "D"
    return tiers


def _parse_tiered(ranking_dict: dict) -> dict[str, str]:
    """{'A': '3-11', 'B': '1-6'} → {num: tier}."""
    tiers: dict[str, str] = {}
    for tier, horses in ranking_dict.items():
        for num in re.findall(r"\d+", str(horses)):
            tiers[num] = tier.upper() if tier.upper() in _TIER_POINTS else "B"
    return tiers


def _leg_keys(game_type: str, n_legs: int) -> list[str]:
    return [f"{game_type}-{i}" for i in range(1, n_legs + 1)]


def build_consensus_from_sources(
    game_type: str,
    date_str: str,
    *,
    n_legs: int = 8,
    write: bool = True,
) -> Optional[dict]:
    """Aggregate all sources into the top-level ``expert_tips`` block.

    For each leg, accumulate weighted tier-points per horse number across every
    source (ranking strings, tiered rankings, and SPIK/Skräll picks). The
    leader is always included; 2nd/3rd are added when contested (score ≥ 55% of
    leader), capped at 3 — mirroring the manual consensus heuristic.

    Also writes a synthetic ``proffs_konsensus_auto`` source with the aggregated
    ordered ranking + best_spike per leg.

    Returns the computed dict {"expert_tips": {...}, "rankings": {...}} or None.
    """
    raw = load_tips_cache_raw(game_type, date_str)
    if not raw:
        logger.warning(f"build_consensus_from_sources: ingen cache för {game_type} {date_str}")
        return None

    sources = raw.get("sources", {})
    if not sources:
        logger.warning("build_consensus_from_sources: inga källor i cachen")
        return None

    leg_keys = _leg_keys(game_type, n_legs)
    expert_tips: dict[str, list[int]] = {}
    auto_rankings: dict[str, str] = {}
    auto_spikes: dict[str, str] = {}

    for leg_idx, race_key in enumerate(leg_keys, start=1):
        scores: dict[str, float] = {}
        weights: dict[str, float] = {}

        def add(num: str, pts: float, w: float) -> None:
            scores[num] = scores.get(num, 0.0) + pts * w
            weights[num] = weights.get(num, 0.0) + w

        for src_key, src in sources.items():
            # Skip our own auto-generated source to avoid feedback loops
            if src_key == "proffs_konsensus_auto":
                continue
            w = _SOURCE_WEIGHTS.get(src_key, _DEFAULT_WEIGHT)

            rankings = src.get("rankings", {})
            race_ranking = rankings.get(race_key)
            tier_map: dict[str, str] = {}
            if isinstance(race_ranking, dict):
                tier_map = _parse_tiered(race_ranking)
            elif isinstance(race_ranking, str) and race_ranking.strip():
                tier_map = _parse_ordered(race_ranking)
            for num, tier in tier_map.items():
                add(num, _TIER_POINTS.get(tier, 1.0), w)

            # picks: only mine SPIK / Skräll when no ranking present
            if not tier_map:
                pick_str = str(src.get("picks", {}).get(race_key, ""))
                if pick_str:
                    head = pick_str.split("(")[0]
                    nums = re.findall(r"\d+", head)
                    if "SPIK" in pick_str.upper() and nums:
                        add(nums[0], _TIER_POINTS["A"], w)
                    elif nums:
                        # plain comma list → light B-points to each
                        for num in nums[:6]:
                            add(num, _TIER_POINTS["B"], w * 0.6)

            # explicit skrällar boost
            skr = src.get("skrallar", {})
            if isinstance(skr, dict):
                for num in skr.get(race_key, []) or []:
                    add(str(num), _TIER_POINTS["B"], w * 0.5)

        if not scores:
            continue

        avg = {num: scores[num] / weights[num] for num in scores if weights[num] > 0}
        ranked = sorted(avg.items(), key=lambda x: -x[1])
        leader_num, leader_score = ranked[0]

        picks: list[int] = [int(leader_num)]
        for num, sc in ranked[1:]:
            if len(picks) >= 3:
                break
            if leader_score > 0 and sc >= 0.55 * leader_score:
                picks.append(int(num))
        expert_tips[str(leg_idx)] = picks

        auto_rankings[race_key] = "-".join(n for n, _ in ranked[:8])
        auto_spikes[race_key] = f"{leader_num} ({leader_score:.1f}p)"

    if not expert_tips:
        return None

    result = {"expert_tips": expert_tips, "rankings": auto_rankings}

    if write:
        # Persist expert_tips at top level (system builder auto-loads this)
        cache_file = _CACHE_DIR / f"{game_type}_{date_str}.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["expert_tips"] = expert_tips
        cache_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Synthetic aggregated source for the chat agent / transparency
        n_sources = len([k for k in sources if k != "proffs_konsensus_auto"])
        add_source_to_cache(
            game_type,
            date_str,
            "proffs_konsensus_auto",
            {
                "author": f"Auto-konsensus ({n_sources} källor)",
                "summary": (
                    f"Automatiskt aggregerad konsensus från {n_sources} källor. "
                    "Viktade tier-poäng per häst, topp-1-3 per avdelning."
                ),
                "rankings": auto_rankings,
                "best_spikes": auto_spikes,
            },
        )
        logger.info(
            f"build_consensus_from_sources: expert_tips uppdaterat för "
            f"{game_type} {date_str} ({len(expert_tips)} avd)"
        )

    return result


# ── 3. Orkestrering ─────────────────────────────────────────────────────────

async def ingest_raw_text(
    game_type: str,
    date_str: str,
    source_name: str,
    raw_text: str,
    *,
    roster: Optional[dict[int, list]] = None,
    api_key: Optional[str] = None,
    rebuild_consensus: bool = True,
    n_legs: int = 8,
) -> dict:
    """Full pipeline: structure raw text → save source → rebuild consensus.

    Returns a status dict: {"ok": bool, "source": str, "expert_tips": {...}}.
    """
    structured = await structure_source_with_llm(
        raw_text,
        game_type,
        date_str,
        source_name=source_name,
        roster=roster,
        api_key=api_key,
    )
    if not structured:
        return {"ok": False, "error": "strukturering misslyckades", "source": source_name}

    add_source_to_cache(game_type, date_str, source_name, structured)

    consensus = None
    if rebuild_consensus:
        consensus = build_consensus_from_sources(game_type, date_str, n_legs=n_legs)

    return {
        "ok": True,
        "source": source_name,
        "structured": structured,
        "expert_tips": (consensus or {}).get("expert_tips", {}),
    }


async def detect_round_meta(
    text: str,
    *,
    title: str = "",
    url: str = "",
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """Lista ut vilken spelomgång en artikel handlar om.

    Returnerar {"game_type": "V85", "date": "2026-05-31"|None, "track": "Solvalla",
    "source_name": "aftonbladet"}. ``date`` kan vara None om spelform hittades men
    inte datum — då löser anroparen datumet via ATG-kalendern. Returnerar None
    bara om spelform inte gick att avgöra alls.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    # Bättre textunderlag: ta med början OCH ett fönster runt första trav-ordet
    # (tipsdelen ligger ofta långt ner medan menyer äter upp toppen).
    full = text or ""
    snippet = full[:9000]
    m = re.search(r"\b(V75|V86|V85|V64|V65|GS75)\b", full, re.IGNORECASE)
    if m and m.start() > 7000:
        s = max(0, m.start() - 1500)
        snippet += "\n[...]\n" + full[s:s + 4000]

    today = _dt.date.today().isoformat()
    prompt = f"""Avgör vilken svensk trav-spelomgång nedanstående webbsida handlar om.
Dagens datum är {today}. Sidans titel: "{title}". URL: "{url}".

Returnera ENBART giltig JSON:
{{"game_type": "<V75|V85|V86|V64|GS75|V65|okänt>", "date": "<YYYY-MM-DD eller okänt>", "track": "<bana eller ''>", "source_name": "<kort källnyckel, t.ex. aftonbladet, expressen, travronden, kungenstrav>"}}

Regler:
- game_type = den spelform artikeln tipsar om (oftast i titeln, t.ex. "V85"). Detta är VIKTIGAST.
- date = speldagens datum (tolka "idag/imorgon/lördag/söndag" relativt {today}). Om årtal saknas, anta innevarande år. Sätt "okänt" bara om du verkligen inte kan gissa.
- source_name = härled från URL-domänen + ev. skribent (gemener, inga mellanslag).
- Om du inte kan avgöra game_type, sätt det till "okänt".

SIDTEXT:
\"\"\"
{snippet}
\"\"\""""
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
        if resp.status_code != 200:
            logger.error(f"detect_round_meta API {resp.status_code}: {resp.text[:200]}")
            return None
        data = _extract_json(resp.json()["content"][0]["text"])
    except Exception as e:
        logger.error(f"detect_round_meta error: {e}")
        return None
    if not data:
        return None
    gt = str(data.get("game_type", "")).upper().strip()
    dt = str(data.get("date", "")).strip()
    if gt in ("", "OKÄND", "OKAND", "OKÄNT", "OKANT"):
        return None  # utan spelform kan vi inte göra något
    # Datum är valfritt — None om okänt/ogiltigt, anroparen löser via kalendern
    date_val: Optional[str] = None
    if dt and dt.lower() not in ("okänt", "okant"):
        try:
            _dt.date.fromisoformat(dt)
            date_val = dt
        except ValueError:
            date_val = None
    return {
        "game_type": gt,
        "date": date_val,
        "track": str(data.get("track", "")).strip(),
        "source_name": str(data.get("source_name", "") or "webb").strip().lower().replace(" ", "_"),
    }


def merge_interviews_from_sources(game_type: str, date_str: str) -> dict:
    """Slå ihop alla källors interviews → top-level block {leg: [post, ...]}.

    Skriver tillbaka resultatet i tips_cache under nyckeln ``interviews`` och
    returnerar det. Avdupliceras på (num, role, person, quote[:60]).
    """
    raw = load_tips_cache_raw(game_type, date_str)
    if not raw:
        return {}
    merged: dict[str, list] = {}
    seen: set = set()
    for src in (raw.get("sources", {}) or {}).values():
        if not isinstance(src, dict):
            continue
        ivs = src.get("interviews") or {}
        if not isinstance(ivs, dict):
            continue
        author = src.get("author", "")
        for leg_key, posts in ivs.items():
            if not isinstance(posts, list):
                continue
            for p in posts:
                if not isinstance(p, dict):
                    continue
                key = (
                    leg_key,
                    p.get("num"),
                    p.get("role", ""),
                    p.get("person", ""),
                    str(p.get("quote", ""))[:60],
                )
                if key in seen:
                    continue
                seen.add(key)
                entry = dict(p)
                entry.setdefault("source", author)
                merged.setdefault(leg_key, []).append(entry)
    raw["interviews"] = merged
    _write_tips_cache_raw(game_type, date_str, raw)
    return merged


def build_roster_from_round(game_round) -> dict[int, list]:
    """Build a {leg: [(num, name), ...]} roster from a GameRound for the LLM."""
    roster: dict[int, list] = {}
    for race in getattr(game_round, "races", []) or []:
        leg = race.race_number
        horses = []
        for entry in race.active_entries:
            horses.append((entry.post_position, entry.horse.name))
        roster[leg] = sorted(horses, key=lambda x: x[0])
    return roster
