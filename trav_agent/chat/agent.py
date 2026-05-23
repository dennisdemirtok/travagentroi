"""AI chat agent — travanalys med Anthropic API."""

from __future__ import annotations
from typing import Optional


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


async def chat(
    messages: list[dict],
    round_context: str,
    backlog_context: str,
    api_key: str,
) -> str:
    """Skicka meddelanden till Anthropic API och returnera svaret."""
    import httpx

    system_prompt = (
        "Du är en AI-assistent för travanalys integrerad i Kungens Trav-dashboarden. "
        "Du svarar på svenska.\n\n"
        f"Här är data för den aktuella omgången:\n\n{round_context}\n\n"
        f"{backlog_context}\n\n"
        "Du kan svara på frågor om:\n"
        "- Hästar, kuskar, tränare i omgången\n"
        "- Modellpoäng, rekommendationer och skrällrisker\n"
        "- Historisk statistik: ROI per strategi, spelform och bana\n"
        "- Senaste resultat och trender\n"
        "- Strategianalys och spelförslag\n\n"
        "Svara kortfattat och informativt. Använd data från omgången och backlog-statistiken."
    )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-sonnet-4-6",
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
        resp.raise_for_status()
        result = resp.json()
        return result["content"][0]["text"]
