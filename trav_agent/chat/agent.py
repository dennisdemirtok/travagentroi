"""AI chat agent — travanalys med Anthropic API."""

from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


def _build_ranking_tiers(game_round) -> str:
    """Build ranking tier context (A/B/BC/C/D) from race entries.

    Tiers based on recommendations:
      A  = spik (clear top pick)
      B  = 2-val (strong contender)
      BC = 3-val (decent chance)
      C  = gardering (outsider/hedge)
      D  = strykning (skip)
    """
    if not game_round:
        return ""

    tier_map = {
        "spik": "A",
        "2-val": "B",
        "3-val": "BC",
        "gardering": "C",
        "strykning": "D",
    }

    lines = []
    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        tiers: dict[str, list[str]] = {"A": [], "B": [], "BC": [], "C": [], "D": []}
        for entry in sorted_entries:
            tier = tier_map.get(entry.recommendation, "D")
            tiers[tier].append(
                f"#{entry.post_position} {entry.horse.name} "
                f"({entry.super_score:.0f}p, "
                f"{entry.bet_percentage:.0%})" if entry.bet_percentage
                else f"#{entry.post_position} {entry.horse.name} "
                f"({entry.super_score:.0f}p)"
            )

        tier_parts = []
        for t in ["A", "B", "BC", "C", "D"]:
            if tiers[t]:
                tier_parts.append(f"    {t}: {', '.join(tiers[t])}")

        lines.append(f"  Lopp {race.race_number}:")
        lines.extend(tier_parts)

    if not lines:
        return ""

    return "\nRANKING (A=spik, B=2-val, BC=3-val, C=gardering, D=strykning):\n" + "\n".join(lines)


def build_round_context(game_round) -> str:
    """Bygg textkontext om omgången för AI-chatten."""
    if not game_round:
        return "Ingen omgångsdata."

    lines = []
    lines.append(f"Speltyp: {game_round.game_type}")
    lines.append(f"Datum: {game_round.round_date}")
    lines.append(f"Bana: {game_round.track_name or 'okänd'}")
    lines.append(f"Status: {'Avslutad' if game_round.is_finished else 'Kommande/Pågående'}")
    lines.append(f"Antal lopp: {game_round.num_races}")
    if game_round.turnover:
        lines.append(f"Omsättning: {game_round.turnover:,} kr")
    if game_round.dividends:
        for tier, amount in sorted(game_round.dividends.items(), reverse=True):
            lines.append(f"Utdelning {tier} rätt: {amount:,.0f} kr")
    lines.append("")

    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        sm = (
            "auto"
            if str(race.start_method) == "StartMethod.AUTO"
            or race.start_method.value == "auto"
            else "volt"
        )
        lines.append(f"=== Lopp {race.race_number}: {race.race_name or ''} ===")
        lines.append(f"Bana: {race.track_name}, Distans: {race.distance}m, Start: {sm}")
        lines.append(f"Startande: {race.num_starters}, Prispott: {race.purse:,} kr")
        lines.append(f"Skrällrisk: {race.upset_risk:.0f}%")
        lines.append("")

        for i, entry in enumerate(sorted_entries[:6]):
            bet_str = f"{entry.bet_percentage:.1%}" if entry.bet_percentage else "-"
            form = entry.horse.recent_form_string
            factors = ", ".join(
                f"{k}:{v:.0f}"
                for k, v in sorted(entry.factor_scores.items(), key=lambda x: -x[1])[:3]
            )
            lines.append(
                f"  Rank {i+1}: #{entry.post_position} {entry.horse.name} "
                f"(poäng: {entry.super_score:.0f}, streck: {bet_str}, "
                f"rek: {entry.recommendation}, form: {form})"
            )
            if factors:
                lines.append(f"    Faktorer: {factors}")
            career = entry.horse.career
            if career.total_starts > 0:
                lines.append(
                    f"    Karriär: {career.total_starts}st, "
                    f"{career.wins}v-{career.seconds}p-{career.thirds}t, "
                    f"vinst: {career.win_rate:.0%}, pris: {career.total_prize_money:,} kr"
                )

        if race.result_order:
            winner_num = race.result_order[0]
            winner = next(
                (e for e in sorted_entries if e.post_position == winner_num), None
            )
            if winner:
                odds_str = f" (odds: {race.win_odds:.2f})" if race.win_odds else ""
                lines.append(f"\n  VINNARE: #{winner_num} {winner.horse.name}{odds_str}")
        lines.append("")

    # Add ranking tiers
    ranking = _build_ranking_tiers(game_round)
    if ranking:
        lines.append(ranking)

    return "\n".join(lines)


def build_backlog_context(game_round, backlog_data: dict) -> str:
    """Bygg statistikkontext från backlog för AI-chatten."""
    if not backlog_data:
        return ""
    entries = backlog_data.get("entries", [])
    if not entries:
        return ""

    gt = game_round.game_type
    track = (game_round.track_name or "").lower()
    lines = []
    lines.append("=" * 50)
    lines.append("HISTORISK STATISTIK FRÅN BACKLOG")
    lines.append("=" * 50)

    # Filtera entries per strategi och spelform
    strategies = backlog_data.get("strategies", {})
    strat_names = sorted(strategies.keys()) if strategies else []

    # Om inga strategies-dict finns, extrahera från entries
    if not strat_names:
        strat_names = sorted(
            set(e.get("strategy", "") for e in entries if e.get("strategy"))
        )

    gt_entries = [e for e in entries if e.get("game_type") == gt and not e.get("live")]

    if gt_entries:
        lines.append(f"\n--- {gt}: {len(gt_entries)} historiska omgångar ---")
        total_cost = sum(e.get("cost", 0) or 0 for e in gt_entries)
        total_payout = sum(e.get("payout", 0) or 0 for e in gt_entries)
        hits = sum(1 for e in gt_entries if (e.get("payout") or 0) > 0)
        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
        win_rate = hits / len(gt_entries) * 100 if gt_entries else 0
        lines.append(f"  Totalt: kostnad {total_cost:,.0f} kr, utbet {total_payout:,.0f} kr")
        lines.append(f"  ROI: {roi:+.1f}%, vinstfrekvens: {win_rate:.0f}%")

        # Per strategi
        by_strat = {}
        for e in gt_entries:
            s = e.get("strategy", "okänd")
            if s not in by_strat:
                by_strat[s] = {"cost": 0, "payout": 0, "n": 0, "hits": 0}
            by_strat[s]["cost"] += e.get("cost", 0) or 0
            by_strat[s]["payout"] += e.get("payout", 0) or 0
            by_strat[s]["n"] += 1
            if (e.get("payout") or 0) > 0:
                by_strat[s]["hits"] += 1

        lines.append(f"\n  Per strategi ({gt}):")
        for s in sorted(by_strat, key=lambda x: -(by_strat[x]["payout"] - by_strat[x]["cost"])):
            d = by_strat[s]
            s_roi = (d["payout"] - d["cost"]) / d["cost"] * 100 if d["cost"] > 0 else 0
            s_wr = d["hits"] / d["n"] * 100 if d["n"] > 0 else 0
            lines.append(
                f"    {s}: {d['n']} omg, ROI {s_roi:+.1f}%, "
                f"vinstfrekvens {s_wr:.0f}%, netto {d['payout']-d['cost']:+,.0f} kr"
            )

    # Senaste 10 resultat på samma spelform
    recent_gt = sorted(
        [e for e in gt_entries if not e.get("live")],
        key=lambda e: e.get("date", ""),
        reverse=True,
    )[:10]
    if recent_gt:
        lines.append(f"\n  Senaste 10 {gt}-omgångar:")
        for e in recent_gt:
            cost = e.get("cost", 0) or 0
            payout = e.get("payout", 0) or 0
            netto = payout - cost
            strat = e.get("strategy", "?")
            result = "HIT" if payout > 0 else "MISS"
            lines.append(
                f"    {e.get('date','')} {strat}: "
                f"insats {cost:,.0f}, utbet {payout:,.0f}, "
                f"netto {netto:+,.0f} kr {result}"
            )

    # Bana-specifik statistik
    if track:
        track_entries = [
            e
            for e in entries
            if (e.get("track", "") or "").lower() == track and not e.get("live")
        ]
        if track_entries:
            lines.append(
                f"\n--- Bana: {game_round.track_name} ({len(track_entries)} omg) ---"
            )
            t_cost = sum(e.get("cost", 0) or 0 for e in track_entries)
            t_payout = sum(e.get("payout", 0) or 0 for e in track_entries)
            t_roi = (t_payout - t_cost) / t_cost * 100 if t_cost > 0 else 0
            t_hits = sum(1 for e in track_entries if (e.get("payout") or 0) > 0)
            t_wr = t_hits / len(track_entries) * 100
            lines.append(
                f"  ROI: {t_roi:+.1f}%, vinstfrekvens: {t_wr:.0f}%, "
                f"netto: {t_payout-t_cost:+,.0f} kr"
            )

    # Månadsstatistik per strategi
    if strategies:
        MONTH_NAMES = {
            "01": "Januari",
            "02": "Februari",
            "03": "Mars",
            "04": "April",
            "05": "Maj",
            "06": "Juni",
            "07": "Juli",
            "08": "Augusti",
            "09": "September",
            "10": "Oktober",
            "11": "November",
            "12": "December",
        }
        lines.append("\n--- ROI per månad (alla spelformer, alla år aggregerat) ---")
        for s_name in strat_names[:3]:  # Top 3 strategier
            s_data = strategies.get(s_name, {})
            monthly = s_data.get("monthly", {})
            if not monthly:
                continue
            lines.append(f"\n  {s_name}:")
            for m_key in sorted(monthly.keys()):
                m = monthly[m_key]
                m_name = m.get("name", MONTH_NAMES.get(m_key, m_key))
                m_roi = m.get("roi", 0)
                m_netto = m.get("netto", 0)
                m_rounds = m.get("rounds", 0)
                m_wins = m.get("wins", 0)
                lines.append(
                    f"    {m_name}: {m_rounds} omg, {m_wins} vinster, "
                    f"ROI {m_roi:+.1f}%, netto {m_netto:+,.0f} kr"
                )

        # Årsstatistik per strategi
        lines.append("\n--- ROI per år (alla spelformer) ---")
        for s_name in strat_names[:3]:
            s_data = strategies.get(s_name, {})
            yearly = s_data.get("yearly", {})
            if not yearly:
                continue
            lines.append(f"\n  {s_name}:")
            for y_key in sorted(yearly.keys()):
                y = yearly[y_key]
                lines.append(
                    f"    {y_key}: {y.get('rounds',0)} omg, "
                    f"ROI {y.get('roi',0):+.1f}%, netto {y.get('netto',0):+,.0f} kr"
                )

    # Övergripande sammanfattning
    total_all_cost = sum(e.get("cost", 0) or 0 for e in entries if not e.get("live"))
    total_all_payout = sum(
        e.get("payout", 0) or 0 for e in entries if not e.get("live")
    )
    all_roi = (
        (total_all_payout - total_all_cost) / total_all_cost * 100
        if total_all_cost > 0
        else 0
    )
    lines.append("\n--- Totalt alla spelformer ---")
    lines.append(f"  {len(entries)} entries, ROI {all_roi:+.1f}%")
    lines.append(f"  Kostnad: {total_all_cost:,.0f} kr, Utbet: {total_all_payout:,.0f} kr")

    return "\n".join(lines)


def _build_system_prompt(
    round_context: str,
    backlog_context: str,
    tips_context: str = "",
    memory_context: str = "",
) -> str:
    """Build the enhanced system prompt for the agent."""
    parts = [
        "Du är **Kungens Trav AI** — en expertassistent för svensk travanalys "
        "integrerad i Kungens Trav-dashboarden. Du svarar alltid på svenska.\n",

        "## Dina uppgifter\n"
        "- Analysera hästar, kuskar och tränare i omgången\n"
        "- Presentera rankingar med tydliga kategorier: "
        "**A** (spik), **B** (2-val), **BC** (3-val), **C** (gardering), **D** (strykning)\n"
        "- Identifiera value-hästar (hög modellpoäng, låg streckprocent)\n"
        "- Bedöma skrällrisker och ge garderingsråd\n"
        "- Ge konkreta spelförslag — inte bara analys\n"
        "- Jämföra med tips från externa källor när tillgängligt\n"
        "- Referera till banspecifika mönster och historisk statistik\n",

        "## Format\n"
        "- Använd **fetstil** för viktiga namn och siffror\n"
        "- Använd listor och struktur för tydlighet\n"
        "- Var konkret: \"Spika #3 Hästen\" istället för \"Häst #3 ser bra ut\"\n"
        "- Avsluta gärna med en kort sammanfattning av dina rekommendationer\n",

        "## Data",
        f"\n### Omgångsdata\n{round_context}\n",
    ]

    if backlog_context:
        parts.append(f"\n### Historisk statistik\n{backlog_context}\n")

    if tips_context:
        parts.append(f"\n### Externa tips\n{tips_context}\n")

    if memory_context:
        parts.append(f"\n### Inlärda mönster\n{memory_context}\n")

    parts.append(
        "\n## Regler\n"
        "- Svara kortfattat och informativt men med substans\n"
        "- Basera alla rekommendationer på data — modellpoäng, streckprocent, form\n"
        "- Flagga alltid om en rekommendation går emot marknaden (streckprocent)\n"
        "- Om tips från externa källor finns, jämför med modellens egna analyser\n"
        "- Om banspecifika mönster finns, nämn dem i relevanta svar\n"
    )

    return "\n".join(parts)


async def chat(
    messages: list[dict],
    round_context: str,
    backlog_context: str,
    api_key: str,
    tips_context: str = "",
    memory_context: str = "",
) -> str:
    """Skicka meddelanden till Anthropic API och returnera svaret."""
    import httpx

    system_prompt = _build_system_prompt(
        round_context, backlog_context, tips_context, memory_context
    )

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": model,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": messages,
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text}")
        result = resp.json()
        return result["content"][0]["text"]


async def chat_stream(
    messages: list[dict],
    round_context: str,
    backlog_context: str,
    api_key: str,
    tips_context: str = "",
    memory_context: str = "",
) -> AsyncIterator[str]:
    """Stream chat response from Anthropic API, yielding text chunks.

    Uses the Anthropic messages API with stream=true.
    SSE events of interest:
      - content_block_delta: has delta.text with the text chunk
      - message_stop: signals completion
      - error: signals an error
    """
    import httpx

    system_prompt = _build_system_prompt(
        round_context, backlog_context, tips_context, memory_context
    )

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            json={
                "model": model,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": messages,
                "stream": True,
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                raise RuntimeError(
                    f"Anthropic API {resp.status_code}: {error_body.decode()}"
                )

            # Parse SSE events from the stream
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line:
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            return

                        try:
                            event_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event_data.get("type", "")

                        if event_type == "content_block_delta":
                            delta = event_data.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                yield text

                        elif event_type == "message_stop":
                            return

                        elif event_type == "error":
                            error_msg = event_data.get("error", {}).get(
                                "message", "Okänt fel"
                            )
                            raise RuntimeError(f"Anthropic API error: {error_msg}")
