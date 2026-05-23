"""Scrape horse racing tips from external sources."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Simple TTL cache ────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 3600  # 1 hour

# ── Manual tips store ───────────────────────────────────────────────────────

_manual_tips: dict[str, str] = {}


def _cache_get(key: str) -> Optional[str]:
    """Get value from cache if not expired."""
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return val
        del _cache[key]
    return None


def _cache_set(key: str, val: str) -> None:
    """Store value in cache with timestamp."""
    _cache[key] = (time.time(), val)


# ── Travcash scraper ────────────────────────────────────────────────────────

def _extract_article_content(html_text: str) -> str:
    """Extract article content from travcash HTML using regex/string parsing."""
    # Try to find the main article content
    # Travcash typically wraps tips in <article> or <div class="entry-content">
    content = ""

    # Strategy 1: Look for entry-content div
    match = re.search(
        r'<div[^>]*class="[^"]*entry-content[^"]*"[^>]*>(.*?)</div>\s*(?:<div|<footer|</article)',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        content = match.group(1)

    # Strategy 2: Look for article tag content
    if not content:
        match = re.search(
            r'<article[^>]*>(.*?)</article>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            content = match.group(1)

    # Strategy 3: Look for post-content
    if not content:
        match = re.search(
            r'<div[^>]*class="[^"]*post-content[^"]*"[^>]*>(.*?)</div>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            content = match.group(1)

    if not content:
        return ""

    # Clean HTML tags to extract text
    # Remove script and style blocks
    content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)

    # Convert <br>, <p>, <h*>, <li> to newlines for readability
    content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
    content = re.sub(r'</p>', '\n', content, flags=re.IGNORECASE)
    content = re.sub(r'<p[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<h[1-6][^>]*>', '\n### ', content, flags=re.IGNORECASE)
    content = re.sub(r'</h[1-6]>', '\n', content, flags=re.IGNORECASE)
    content = re.sub(r'<li[^>]*>', '- ', content, flags=re.IGNORECASE)
    content = re.sub(r'</li>', '\n', content, flags=re.IGNORECASE)

    # Bold/strong -> **
    content = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<b[^>]*>(.*?)</b>', r'**\1**', content, flags=re.DOTALL | re.IGNORECASE)

    # Remove remaining HTML tags
    content = re.sub(r'<[^>]+>', '', content)

    # Clean up HTML entities
    content = content.replace('&amp;', '&')
    content = content.replace('&lt;', '<')
    content = content.replace('&gt;', '>')
    content = content.replace('&quot;', '"')
    content = content.replace('&#39;', "'")
    content = content.replace('&nbsp;', ' ')
    content = content.replace('&ndash;', '-')
    content = content.replace('&mdash;', '--')

    # Clean up whitespace
    content = re.sub(r'\n{3,}', '\n\n', content)
    content = re.sub(r' {2,}', ' ', content)
    content = content.strip()

    return content


def _find_tips_url(html_text: str, game_type: str) -> Optional[str]:
    """Find the URL to the latest tips article for a game type."""
    gt_lower = game_type.lower()
    # Look for links containing the game type in the URL or text
    pattern = rf'<a[^>]*href="(https?://travcash\.se/[^"]*{gt_lower}[^"]*)"[^>]*>'
    matches = re.findall(pattern, html_text, re.IGNORECASE)
    if matches:
        return matches[0]

    # Broader search: any link with game type text
    pattern = rf'<a[^>]*href="(https?://travcash\.se/[^"]*tips[^"]*)"[^>]*>[^<]*{gt_lower}[^<]*</a>'
    matches = re.findall(pattern, html_text, re.IGNORECASE)
    if matches:
        return matches[0]

    return None


async def scrape_travcash(game_type: str, date: str) -> str:
    """Scrape tips from travcash.se for a given game type.

    Args:
        game_type: e.g. "V75", "V85", "V86"
        date: ISO date string e.g. "2026-05-23"

    Returns:
        Tips text or empty string on failure.
    """
    cache_key = f"travcash:{game_type}:{date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    gt_lower = game_type.lower()
    # Try common URL patterns
    urls_to_try = [
        f"https://travcash.se/{gt_lower}-tips/",
        f"https://travcash.se/{gt_lower}-tips-{date}/",
        f"https://travcash.se/kategori/{gt_lower}/",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        ) as client:
            # First, try direct URLs
            for url in urls_to_try:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        content = _extract_article_content(resp.text)
                        if content and len(content) > 100:
                            result = f"[Källa: travcash.se]\n\n{content}"
                            _cache_set(cache_key, result)
                            return result

                        # Maybe this is a listing page — find the actual article
                        article_url = _find_tips_url(resp.text, game_type)
                        if article_url:
                            resp2 = await client.get(article_url)
                            if resp2.status_code == 200:
                                content2 = _extract_article_content(resp2.text)
                                if content2 and len(content2) > 100:
                                    result = f"[Källa: travcash.se]\n\n{content2}"
                                    _cache_set(cache_key, result)
                                    return result
                except httpx.HTTPError:
                    continue

    except Exception as e:
        logger.warning(f"Travcash scraping failed: {e}")

    _cache_set(cache_key, "")
    return ""


# ── Manual tips (for sources requiring login) ──────────────────────────────

def add_manual_tips(source: str, text: str) -> None:
    """Store manually pasted tips from a named source.

    Args:
        source: e.g. "sharps_berglund", "aftonbladet", "expressen"
        text: The raw tips text pasted by the user.
    """
    _manual_tips[source] = text
    logger.info(f"Manual tips stored from source: {source} ({len(text)} chars)")


def get_manual_tips() -> dict[str, str]:
    """Return all stored manual tips."""
    return dict(_manual_tips)


def clear_manual_tips() -> None:
    """Clear all manual tips."""
    _manual_tips.clear()


# ── Aggregator ──────────────────────────────────────────────────────────────

def _load_tips_cache_file(game_type: str, date_str: str) -> dict[str, str]:
    """Load tips from local JSON cache file (e.g. tips_cache/V85_2026-05-23.json)."""
    cache_dir = Path(__file__).parent.parent.parent / "tips_cache"
    cache_file = cache_dir / f"{game_type}_{date_str}.json"
    if not cache_file.exists():
        return {}

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read tips cache {cache_file}: {e}")
        return {}

    results: dict[str, str] = {}
    sources = data.get("sources", {})

    for src_key, src_data in sources.items():
        author = src_data.get("author", src_key)
        lines = [f"[Källa: {author}]"]
        if src_data.get("url"):
            lines.append(f"URL: {src_data['url']}")
        if src_data.get("summary"):
            lines.append(f"Sammanfattning: {src_data['summary']}")

        picks = src_data.get("picks", {})
        if picks:
            lines.append("\nVal per avdelning:")
            for race, pick in sorted(picks.items()):
                lines.append(f"  {race}: {pick}")

        rankings = src_data.get("rankings", {})
        if rankings:
            lines.append("\nRanking:")
            for race, ranking in sorted(rankings.items()):
                if isinstance(ranking, dict):
                    parts = [f"{tier}: {horses}" for tier, horses in ranking.items()]
                    lines.append(f"  {race}: {', '.join(parts)}")
                else:
                    lines.append(f"  {race}: {ranking}")

        if src_data.get("best_spike"):
            lines.append(f"\nBästa spik: {src_data['best_spike']}")
        if src_data.get("best_drag"):
            lines.append(f"Bästa drag: {src_data['best_drag']}")

        trainer_comments = src_data.get("trainer_comments", {})
        if trainer_comments:
            lines.append("\nTränarkommentarer:")
            for race, horses in sorted(trainer_comments.items()):
                lines.append(f"  {race}:")
                for num, info in sorted(horses.items(), key=lambda x: int(x[0])):
                    pct = info.get("pct", "?")
                    lines.append(
                        f"    {num} {info['name']} ({info['driver']}) {pct}%: "
                        f"{info['comment']}"
                    )

        tidsranken = src_data.get("tidsranken", {})
        if tidsranken:
            lines.append("\nTidsranken:")
            for race, times in sorted(tidsranken.items()):
                lines.append(f"  {race}: {times}")

        results[src_key] = "\n".join(lines)

    spetstrid = data.get("spetstrid", {})
    if spetstrid:
        sp_lines = ["[Spetstrid — Edholms analys av spetsstriden per lopp]"]
        for race, sp in sorted(spetstrid.items()):
            contenders = ", ".join(sp.get("contenders", []))
            leader = sp.get("predicted_leader", "?")
            conf = sp.get("confidence", "?")
            notes = sp.get("notes", "")
            sp_lines.append(
                f"  {race}: Spets → {leader} (conf: {conf}). "
                f"Utmanare: {contenders}. {notes}"
            )
        results["_spetstrid"] = "\n".join(sp_lines)

    return results


def load_tips_cache_raw(game_type: str, date_str: str) -> Optional[dict]:
    """Load raw JSON data from tips cache file for consensus ranking."""
    cache_dir = Path(__file__).parent.parent.parent / "tips_cache"
    cache_file = cache_dir / f"{game_type}_{date_str}.json"
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read tips cache {cache_file}: {e}")
        return None


async def scrape_tips(game_type: str, date: str) -> dict:
    """Aggregate tips from all available sources.

    Args:
        game_type: e.g. "V75", "V85"
        date: ISO date string

    Returns:
        Dict with source names as keys and tips text as values.
    """
    results: dict[str, str] = {}

    # 1. Load from local JSON cache file first
    cached = _load_tips_cache_file(game_type, date)
    results.update(cached)

    # 2. Automated scrapers (only if not already in cache)
    if "travcash" not in results:
        travcash_tips = await scrape_travcash(game_type, date)
        if travcash_tips:
            results["travcash"] = travcash_tips

    # 3. Manual tips
    for source, text in _manual_tips.items():
        if text:
            results[source] = f"[Källa: {source} (manuellt)]\n\n{text}"

    return results


def build_tips_context(tips: dict[str, str]) -> str:
    """Build a text context string from tips for the AI agent.

    Args:
        tips: Dict of source -> tips text

    Returns:
        Formatted string for inclusion in agent context.
    """
    if not tips:
        return ""

    lines = []
    lines.append("=" * 50)
    lines.append("TIPS FRÅN EXTERNA KÄLLOR")
    lines.append("=" * 50)

    for source, text in tips.items():
        lines.append(f"\n--- {source.upper()} ---")
        lines.append(text)
        lines.append("")

    lines.append(
        "OBS: Tips från externa källor ska användas som extra information, "
        "inte som enda underlag. Jämför alltid med modellens egna analyser."
    )

    return "\n".join(lines)
