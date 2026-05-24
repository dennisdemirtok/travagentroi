"""CLI — Kommandorads-interface för Trav Agent.

Användning:
    python -m trav_agent.cli fetch V85 2026-02-21
    python -m trav_agent.cli analyze V85 2026-02-21
    python -m trav_agent.cli report V85 2026-02-21
    python -m trav_agent.cli backtest V85 --from 2025-01-01 --to 2025-12-31
    python -m trav_agent.cli explore V85 2026-02-21 --race 3 --horse 5
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta

import click

from .analysis.composite import CompositeAnalyzer
from .backtest.evaluator import BacktestEvaluator
from .backtest.runner import BacktestRunner
from .config import CACHE_DIR, DEFAULT_CONFIG, GAME_TYPES, GAME_SCHEDULE, OUTPUT_DIR, PROJECT_ROOT, get_todays_game_types
from .data.atg_client import ATGClient
from .data.enrich import enrich_game_round
from .output.dashboard import generate_dashboard_html
from .output.report import RaceReporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_date(date_str: str) -> date:
    return date.fromisoformat(date_str)


@click.group()
def cli():
    """Trav Agent — AI-driven travanalys."""
    pass


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
def fetch(game_type: str, day: str):
    """Hämta en spelomgång från ATG."""

    async def _run():
        client = ATGClient()
        d = parse_date(day)
        click.echo(f"Hämtar {game_type} {d}...")

        game_round = await client.fetch_full_round(game_type.upper(), d)
        if game_round:
            click.echo(
                f"✓ {game_round.num_races} lopp, "
                f"{sum(r.num_starters for r in game_round.races)} hästar"
            )
            for race in game_round.races:
                click.echo(
                    f"  Avd {race.race_number}: {race.distance}m "
                    f"{race.start_method.value} — {race.num_starters} hästar"
                )
        else:
            click.echo(f"✗ Ingen {game_type} hittad för {d}")

    asyncio.run(_run())


@cli.command()
@click.option("--days", default=7, help="Antal dagar framåt att hämta")
def fetch_upcoming(days):
    """Hämta kommande startlistor för de närmaste dagarna."""

    async def _run():
        client = ATGClient()
        today = date.today()
        fetched = 0
        skipped = 0
        scheduled = 0
        errors = 0

        weekday_names = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
        click.echo(f"Hämtar startlistor {today} → {today + timedelta(days=days - 1)}")
        click.echo()

        for offset in range(days):
            d = today + timedelta(days=offset)
            weekday = d.weekday()
            day_name = weekday_names[weekday]

            # Kolla kalendern direkt — ATG kan ha extra spelformer utöver schemat
            try:
                calendar = await client.get_calendar(d, skip_cache=True)
                cal_games = calendar.get("games", {})
                # Filtrera på spelformer vi bryr oss om
                relevant = {
                    gt: gl for gt, gl in cal_games.items()
                    if gt.upper() in ("V75", "V85", "V86", "V64", "GS75")
                }
            except Exception:
                # Fallback: använd schemat
                game_types_today = GAME_SCHEDULE.get(weekday, [])
                relevant = {gt: [] for gt in game_types_today}

            if not relevant:
                continue

            for gt, game_list in relevant.items():
                # Kolla om omgången redan finns i cache
                cached_dates = _discover_cached_dates(gt)
                if str(d) in cached_dates:
                    click.echo(f"  ✓ {gt} {d} ({day_name}) — redan cachad")
                    skipped += 1
                    continue

                # Kolla om omgången har game_id (= bettable/startlista publicerad)
                has_id = False
                if game_list:
                    has_id = any(g.get("id") for g in game_list)

                if not has_id:
                    status = game_list[0].get("status", "?") if game_list else "ej i kalender"
                    click.echo(f"  ⏳ {gt} {d} ({day_name}) — schemalagd ({status}), startlista ej publicerad")
                    scheduled += 1
                    continue

                click.echo(f"  Hämtar {gt} {d} ({day_name})...", nl=False)
                try:
                    game_round = await client.fetch_full_round(gt, d)
                    if game_round:
                        click.echo(
                            f" ✓ {game_round.num_races} lopp, "
                            f"{sum(r.num_starters for r in game_round.races)} hästar"
                        )
                        fetched += 1
                    else:
                        click.echo(f" ✗ ingen omgång hittad")
                except Exception as e:
                    click.echo(f" ✗ fel: {e}")
                    errors += 1

        click.echo()
        parts = [f"Hämtade: {fetched}"]
        if skipped:
            parts.append(f"redan cachade: {skipped}")
        if scheduled:
            parts.append(f"schemalagda (väntar startlista): {scheduled}")
        if errors:
            parts.append(f"fel: {errors}")
        click.echo(f"Klart! {', '.join(parts)}")

    asyncio.run(_run())


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
def analyze(game_type: str, day: str):
    """Analysera en spelomgång."""

    async def _run():
        client = ATGClient()
        d = parse_date(day)
        click.echo(f"Hämtar och analyserar {game_type} {d}...")

        game_round = await client.fetch_full_round(game_type.upper(), d)
        if not game_round:
            click.echo(f"✗ Ingen {game_type} hittad")
            return

        analyzer = CompositeAnalyzer()
        analyzer.analyze_round(game_round)

        # Skriv ut kort sammanfattning
        for race in game_round.races:
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )
            click.echo(f"\nAvd {race.race_number} ({race.distance}m {race.start_method.value}):")
            for e in sorted_entries[:5]:
                bet_str = f"{e.bet_percentage:.0%}" if e.bet_percentage else "-"
                click.echo(
                    f"  {e.rank}. [{e.post_position}] {e.horse.name:<20} "
                    f"{e.super_score:5.1f}p  {bet_str:>5}  {e.recommendation}"
                )

    asyncio.run(_run())


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
@click.option("--budget", "-b", type=float, default=300.0, help="Budget i kr")
def chansspik(game_type: str, day: str, budget: float):
    """Chansspik-analys — siktar på 25-100k utdelning."""

    async def _run():
        client = ATGClient()
        d = parse_date(day)
        click.echo(f"Chansspik-analys {game_type} {d} (budget {budget:.0f} kr)...")

        game_round = await client.fetch_full_round(game_type.upper(), d)
        if not game_round:
            click.echo(f"✗ Ingen {game_type} hittad")
            return

        analyzer = CompositeAnalyzer()
        analyzer.analyze_round(game_round)

        from .analysis.upset_system import analyze_upset_round, build_upset_system

        # Upset-analys
        analysis = analyze_upset_round(game_round)
        click.echo(f"\n{'='*60}")
        click.echo(f"CHANSSPIK-ANALYS — {game_type} {d}")
        click.echo(f"{'='*60}")
        click.echo(f"Estimerad utdelningszon: {analysis['expected_payout']}")
        click.echo(f"Rekommendation: {analysis['recommendation']}")
        click.echo(f"Hög-skrällrisklonpp: {analysis['high_upset_races']}")
        click.echo(f"Medium-skrällrisklopp: {analysis['medium_upset_races']}")

        click.echo(f"\n{'─'*60}")
        click.echo("SKRÄLLPOTENTIAL PER LOPP:")
        click.echo(f"{'─'*60}")

        for ra in analysis["race_analysis"]:
            icon = "🔴" if ra["upset_potential"] >= 50 else ("🟡" if ra["upset_potential"] >= 30 else "🟢")
            click.echo(
                f"\n  Avd {ra['race_number']} ({ra['track']}, {ra['n_starters']} st) "
                f"— {icon} Potential: {ra['upset_potential']:.0f}"
            )
            for c in ra["chansspik_candidates"]:
                click.echo(
                    f"    🎲 {c['horse']:<20} spår {c['post']:>2d} "
                    f"({c['bet_pct']*100:.0f}%) — {c['upset_score']:.0f}p "
                    f"[{', '.join(c['reasons'])}]"
                )

        # Bygg system
        plan = build_upset_system(game_round, budget=budget)
        click.echo(f"\n{'='*60}")
        click.echo(f"CHANSSPIK-SYSTEM ({budget:.0f} kr)")
        click.echo(f"{'='*60}")
        click.echo(plan.summary())

    asyncio.run(_run())


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
@click.option("--output", "-o", type=str, default=None, help="Output-fil (markdown)")
def report(game_type: str, day: str, output: str):
    """Generera en fullständig analysrapport."""

    async def _run():
        client = ATGClient()
        d = parse_date(day)
        click.echo(f"Skapar rapport för {game_type} {d}...")

        game_round = await client.fetch_full_round(game_type.upper(), d)
        if not game_round:
            click.echo(f"✗ Ingen {game_type} hittad")
            return

        analyzer = CompositeAnalyzer()
        analyzer.analyze_round(game_round)

        reporter = RaceReporter()
        report_text = reporter.generate_round_report(game_round)

        if output:
            from pathlib import Path
            Path(output).write_text(report_text, encoding="utf-8")
            click.echo(f"✓ Rapport sparad: {output}")
        else:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            filename = OUTPUT_DIR / f"{game_type}_{d}.md"
            filename.write_text(report_text, encoding="utf-8")
            click.echo(f"✓ Rapport sparad: {filename}")

    asyncio.run(_run())


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.option("--from", "from_date", required=True, type=str, help="Startdatum YYYY-MM-DD")
@click.option("--to", "to_date", required=True, type=str, help="Slutdatum YYYY-MM-DD")
@click.option("--factor", type=str, default=None, help="Testa enskild faktor isolerat")
@click.option("--save/--no-save", default=True, help="Spara resultat till disk")
@click.option("--load", "load_path", type=str, default=None, help="Ladda sparade resultat")
def backtest(game_type: str, from_date: str, to_date: str, factor: str, save: bool, load_path: str):
    """Backtesta analysmodellen på historiska omgångar."""
    from .backtest.storage import (
        deserialize_predictions,
        load_backtest_run,
        save_backtest_run,
    )

    async def _run():
        start = parse_date(from_date)
        end = parse_date(to_date)

        # Ladda sparade resultat om --load angetts
        if load_path:
            click.echo(f"Laddar sparade resultat: {load_path}")
            saved = load_backtest_run(load_path)
            if not saved:
                click.echo("✗ Kunde inte ladda fil")
                return
            predictions = deserialize_predictions(saved)
            click.echo(f"✓ Laddade {len(predictions)} omgångar")
        else:
            click.echo(f"Backtesting {game_type} {start} → {end}...")
            client = ATGClient()
            runner = BacktestRunner(client=client)
            predictions = await runner.run_range(game_type.upper(), start, end)

        if not predictions:
            click.echo("✗ Inga omgångar hittade i intervallet")
            return

        evaluator = BacktestEvaluator()
        results = evaluator.evaluate(predictions)

        # Sammanfattning
        summary = evaluator.print_summary(results)
        click.echo(summary)

        # Faktorkorrelation
        if results.factor_evaluations:
            evaluator.print_factor_report(results)

        # Spara om --save
        if save and not load_path:
            path = save_backtest_run(
                game_type.upper(), start, end, predictions, results
            )
            click.echo(f"\n✓ Resultat sparade: {path}")

    asyncio.run(_run())


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
@click.option("--race", "-r", type=int, help="Avdelningsnummer")
@click.option("--horse", "-h", "horse_num", type=int, help="Hästnummer för djupanalys")
def explore(game_type: str, day: str, race: int, horse_num: int):
    """Utforska en häst eller ett lopp i detalj."""

    async def _run():
        client = ATGClient()
        d = parse_date(day)

        game_round = await client.fetch_full_round(game_type.upper(), d)
        if not game_round:
            click.echo(f"✗ Ingen {game_type} hittad")
            return

        analyzer = CompositeAnalyzer()
        analyzer.analyze_round(game_round)

        if race and horse_num:
            r = game_round.get_race(race)
            if r:
                for entry in r.active_entries:
                    if entry.post_position == horse_num:
                        reporter = RaceReporter()
                        deep_dive = reporter.generate_horse_deep_dive(entry, r)
                        click.echo(deep_dive)
                        return
                click.echo(f"✗ Häst {horse_num} hittades inte i avd {race}")
            else:
                click.echo(f"✗ Avdelning {race} hittades inte")
        elif race:
            r = game_round.get_race(race)
            if r:
                reporter = RaceReporter()
                click.echo(reporter._race_section(r))
        else:
            click.echo("Ange --race och/eller --horse")

    asyncio.run(_run())


def _discover_cached_dates(game_type: str) -> list[str]:
    """Hitta alla datum med cachade omgångar för en spelform.

    Scannar cache-katalogen efter filer som matchar V85_YYYY-MM-DD_*
    och returnerar sorterade datum (nyast först).
    """
    import re
    pattern = re.compile(
        rf"^{game_type.upper()}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$"
    )
    dates = set()
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            m = pattern.match(f.name)
            if m:
                dates.add(m.group(1))
    return sorted(dates, reverse=True)


def _discover_cached_dates_multi(game_types: list[str]) -> list[tuple[str, str]]:
    """Hitta cachade omgångar för flera speltyper.

    Returnerar: [(game_type, date_str), ...] sorterade nyast först.
    """
    import re
    results = []
    for gt in game_types:
        for d in _discover_cached_dates(gt):
            results.append((gt.upper(), d))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


@cli.command()
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False),
                required=False, default=None)
@click.argument("days", nargs=-1, type=str)
@click.option("--port", "-p", type=int, default=8585, help="Port för lokal server")
@click.option("--history", "-n", type=int, default=0,
              help="Ladda de N senaste cachade omgångarna automatiskt")
@click.option("--all", "load_all", is_flag=True, default=False,
              help="Ladda ALLA cachade omgångar")
@click.option("--today", "auto_today", is_flag=True, default=False,
              help="Auto-detektera dagens speltyp och ladda omgången")
def dashboard(game_type: str | None, days: tuple[str, ...], port: int,
              history: int, load_all: bool, auto_today: bool):
    """Starta en lokal dashboard i webbläsaren.

    Ange ett eller flera datum (YYYY-MM-DD) för att jämföra omgångar.

    \b
    Exempel:
      dashboard --today                  (dagens speltyp automatiskt)
      dashboard --today --history 3      (idag + 3 senaste)
      dashboard V85 2026-02-21 2026-02-14
      dashboard V85 --history 8          (senaste 8 omgångarna)
      dashboard V85 --all                (alla cachade omgångar)
    """
    import http.server
    import threading
    import webbrowser

    # ── Bestäm vilka omgångar som ska laddas ──
    # rounds_to_load: [(game_type, date_str), ...]
    rounds_to_load: list[tuple[str, str]] = []

    if auto_today:
        today_types = get_todays_game_types()
        today_str = str(date.today())
        weekday_names = ["Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"]
        day_name = weekday_names[date.today().weekday()]

        if not today_types:
            click.echo(f"✗ Inga spel schemalagda idag ({day_name})")
            return

        click.echo(f"Idag ({day_name}): {', '.join(today_types)}")

        # Lägg till dagens omgångar
        for gt in today_types:
            rounds_to_load.append((gt, today_str))

        # Lägg till gårdagens omgångar (kan ha oavslutade resultat)
        yesterday = date.today() - timedelta(days=1)
        yesterday_str = str(yesterday)
        yesterday_types = GAME_SCHEDULE.get(yesterday.weekday(), [])
        for gt in yesterday_types:
            if gt not in today_types:  # undvik dubbletter
                rounds_to_load.append((gt, yesterday_str))
        diff_types = [gt for gt in yesterday_types if gt not in today_types]
        if diff_types:
            click.echo(f"  + Gårdag ({yesterday_str}): {', '.join(diff_types)}")

        # Lägg till kommande omgångar (redan hämtade via fetch-upcoming)
        upcoming_added = []
        for offset in range(1, 8):  # 1-7 dagar framåt
            future_date = date.today() + timedelta(days=offset)
            future_str = str(future_date)
            future_types = GAME_SCHEDULE.get(future_date.weekday(), [])
            for gt in future_types:
                cached = _discover_cached_dates(gt)
                if future_str in cached:
                    if (gt, future_str) not in rounds_to_load:
                        rounds_to_load.append((gt, future_str))
                        upcoming_added.append(f"{gt} {future_str}")
        if upcoming_added:
            click.echo(f"  + Kommande: {', '.join(upcoming_added)}")

        # Om --history: lägg till N senaste cachade per speltyp
        all_types = list(set(today_types + yesterday_types))
        if history > 0:
            for gt in all_types:
                cached = _discover_cached_dates(gt)
                for d in cached[:history]:
                    if (gt, d) not in rounds_to_load:
                        rounds_to_load.append((gt, d))
            click.echo(f"  + {history} senaste cachade per speltyp")

    elif game_type:
        gt = game_type.upper()
        if load_all or history > 0:
            cached = _discover_cached_dates(gt)
            if not cached:
                click.echo(f"✗ Inga cachade {gt}-omgångar hittades")
                return
            selected = cached if load_all else cached[:history]
            rounds_to_load = [(gt, d) for d in selected]
            click.echo(f"{'Hittade' if load_all else 'Laddar'} {len(selected)} "
                        f"{'cachade' if load_all else 'senaste'} {gt}-omgångar")
        elif days:
            rounds_to_load = [(gt, d) for d in days]
        else:
            # Inget datum angivet — visa hjälp
            cached = _discover_cached_dates(gt)
            if cached:
                click.echo(f"Ange datum eller använd --history N / --all / --today")
                click.echo(f"\nTillgängliga cachade {gt}-datum ({len(cached)} st):")
                for d in cached[:10]:
                    click.echo(f"  {d}")
                if len(cached) > 10:
                    click.echo(f"  ... och {len(cached) - 10} till")
                click.echo(f"\nExempel:")
                click.echo(f"  dashboard {gt} {cached[0]}")
                click.echo(f"  dashboard {gt} --history 8")
                click.echo(f"  dashboard --today")
            else:
                click.echo("Ange minst ett datum, t.ex: dashboard V85 2026-02-21")
            return
    else:
        click.echo("Ange speltyp eller använd --today")
        click.echo("\nExempel:")
        click.echo("  dashboard --today")
        click.echo("  dashboard V75 2026-02-22")
        click.echo("  dashboard V64 --history 5")
        return

    # ── Gemensam logik: ladda, analysera, bygg HTML ──
    html_pages: dict[str, bytes] = {}       # "V75/2026-02-22" → HTML
    live_rounds: dict[str, object] = {}     # "V75/2026-02-22" → GameRound
    all_game_rounds: dict[str, object] = {} # "V75/2026-02-22" → GameRound (alla)
    backlog_data_holder: list = [None]

    async def _run():
        nonlocal html_pages, live_rounds
        client = ATGClient()
        analyzer = CompositeAnalyzer()

        # Fas 1: Hämta och analysera alla omgångar
        rounds: list[tuple[str, str, object]] = []  # (game_type, date_str, GameRound)
        for gt, day_str in rounds_to_load:
            d = parse_date(day_str)
            click.echo(f"Hämtar {gt} {d}...")

            game_round = await client.fetch_full_round(gt, d)
            if not game_round:
                click.echo(f"  ✗ Ingen {gt} hittad för {d}")
                continue

            # Berika med scrapead data om den finns
            scraped_path = CACHE_DIR / f"scraped_{gt.lower()}_{d}.json"
            if scraped_path.exists():
                enrich_game_round(game_round, scraped_path)
                click.echo(f"  ✓ Berikad med scrapead data")

            # För pågående omgångar: hämta senaste resultat (skip cache)
            if not game_round.is_finished:
                n_odds, n_fin, has_div = await client.refresh_results(game_round)
                if n_fin > 0:
                    click.echo(f"  ✓ Hämtade resultat: {n_fin} lopp avslutade")

            analyzer.analyze_round(game_round)
            rounds.append((gt, str(d), game_round))

            finished_races = sum(1 for r in game_round.races if r.result_order)
            total_races = len(game_round.races)
            status = "avslutad" if game_round.is_finished else f"live ({finished_races}/{total_races} klara)"
            click.echo(
                f"  ✓ {game_round.num_races} lopp, "
                f"{sum(r.num_starters for r in game_round.races)} hästar ({status})"
            )

        if not rounds:
            click.echo("✗ Inga omgångar hittades")
            return

        # Fas 2: Ladda backlog om den finns
        backlog_data = None
        backlog_path = PROJECT_ROOT / "backlog.json"
        if backlog_path.exists():
            try:
                import json as _json
                with open(backlog_path) as f:
                    bl = _json.load(f)
                backlog_data = bl
                backlog_data_holder[0] = backlog_data
                n_entries = len(bl.get("entries", []))
                n_strats = len(bl.get("strategies", {}))
                click.echo(f"  ✓ Backlog laddad: {n_entries} entries, {n_strats} strategier")
            except Exception as e:
                click.echo(f"  ⚠ Kunde inte ladda backlog: {e}")

        # Fas 2.5: Generera live-entries för pågående omgångar
        from trav_agent.betting.live_backlog import generate_live_entries

        for gt, d_str, game_round in rounds:
            if not game_round.is_finished:
                live_entries = generate_live_entries(game_round)
                key = f"{gt}/{d_str}"
                if live_entries:
                    if backlog_data is None:
                        backlog_data = {"entries": [], "strategies": {}, "generated": d_str}
                    backlog_data["entries"] = live_entries + backlog_data.get("entries", [])
                    backlog_data_holder[0] = backlog_data
                    click.echo(f"  ✓ Live-entries: {len(live_entries)} strategier för {gt} {d_str}")

        # Fas 3: Bygg available_rounds och generera HTML
        available_rounds = [
            (f"{gt}/{d_str}", gt, d_str, gr.is_finished, gr.track_name or "")
            for gt, d_str, gr in rounds
        ]

        # Backward-compat: available_dates (används om dashboard.py inte stöder available_rounds ännu)
        available_dates = [
            (d_str, gr.is_finished) for _, d_str, gr in rounds
        ]

        for gt, d_str, game_round in rounds:
            key = f"{gt}/{d_str}"
            html = generate_dashboard_html(
                game_round,
                available_dates=available_dates,
                backlog_data=backlog_data,
                available_rounds=available_rounds,
            )
            html_pages[key] = html.encode("utf-8")
            all_game_rounds[key] = game_round
            if not game_round.is_finished:
                live_rounds[key] = game_round

        click.echo(f"\n✓ Dashboard redo — {len(rounds)} omgång(ar)")
        if live_rounds:
            click.echo(f"  Live-omgångar: {', '.join(live_rounds.keys())} (auto-refresh var 10 min)")

    asyncio.run(_run())

    if not html_pages:
        return

    # ── Auto-refresh av resultat för live-omgångar ──
    # Regenererar system med senaste streckdata vid varje refresh.
    def _refresh_live_odds():
        """Bakgrundstråd: uppdatera resultat var 10:e minut."""
        import time as _time
        from trav_agent.betting.live_backlog import generate_live_entries
        import json as _json

        while True:
            _time.sleep(600)  # 10 minuter

            async def _do_refresh():
                client = ATGClient()
                analyzer = CompositeAnalyzer()

                # ── Auto-detect nya omgångar (idag/igår/imorgon) ──
                _today = date.today()
                check_dates = [
                    _today - timedelta(days=1),
                    _today,
                    _today + timedelta(days=1),
                ]
                new_rounds_added = False
                for check_date in check_dates:
                    scheduled = GAME_SCHEDULE.get(check_date.weekday(), [])
                    for gt in scheduled:
                        key = f"{gt}/{check_date}"
                        if key in html_pages:
                            continue
                        try:
                            gr = await client.fetch_full_round(gt, check_date)
                            if not gr:
                                continue
                            if not gr.is_finished:
                                n_odds, n_fin, has_div = await client.refresh_results(gr)
                            analyzer.analyze_round(gr)
                            live_entries = generate_live_entries(gr)
                            bd = backlog_data_holder[0]
                            if live_entries and bd:
                                bd["entries"] = live_entries + bd["entries"]
                            all_game_rounds[key] = gr
                            if not gr.is_finished:
                                live_rounds[key] = gr
                            new_rounds_added = True
                            click.echo(f"  + Ny omgång hittad: {key}")
                        except Exception:
                            continue

                # Beräkna available_rounds (inkl eventuella nya)
                available_rounds = [
                    (key, key.split("/")[0], key.split("/")[1], key not in live_rounds,
                     (all_game_rounds[key].track_name or "") if key in all_game_rounds else "")
                    for key in sorted(html_pages.keys(), reverse=True)
                ]
                available_dates = [
                    (key.split("/")[1], key not in live_rounds)
                    for key in sorted(html_pages.keys(), reverse=True)
                ]

                # Om nya omgångar: generera HTML + uppdatera alla sidors date-picker
                if new_rounds_added:
                    all_keys = set(html_pages.keys()) | set(all_game_rounds.keys())
                    available_rounds = [
                        (k, k.split("/")[0], k.split("/")[1], k not in live_rounds,
                         (all_game_rounds[k].track_name or "") if k in all_game_rounds else "")
                        for k in sorted(all_keys, reverse=True)
                    ]
                    available_dates = [
                        (k.split("/")[1], k not in live_rounds)
                        for k in sorted(all_keys, reverse=True)
                    ]
                    for key, gr in list(all_game_rounds.items()):
                        html_out = generate_dashboard_html(
                            gr,
                            available_dates=available_dates,
                            backlog_data=backlog_data_holder[0],
                            available_rounds=available_rounds,
                        )
                        html_pages[key] = html_out.encode("utf-8")

                for key, game_round in list(live_rounds.items()):
                    gt, d_str = key.split("/", 1)
                    n_odds, n_finished, has_divs = await client.refresh_results(game_round)

                    if n_odds > 0 or n_finished > 0:
                        bd = backlog_data_holder[0]
                        if bd:
                            new_live = generate_live_entries(game_round)
                            bd["entries"] = [
                                e for e in bd["entries"]
                                if not (e.get("live") and e.get("date") == d_str
                                        and e.get("game_type") == gt)
                            ]
                            bd["entries"] = new_live + bd["entries"]

                        html = generate_dashboard_html(
                            game_round,
                            available_dates=available_dates,
                            backlog_data=backlog_data_holder[0],
                            available_rounds=available_rounds,
                        )
                        html_pages[key] = html.encode("utf-8")
                        click.echo(
                            f"  ↻ Uppdaterat: {key} ({n_odds} odds, "
                            f"{n_finished} lopp avslutade)"
                        )

                    if game_round.is_finished:
                        bd = backlog_data_holder[0]
                        if bd and has_divs:
                            final_entries = generate_live_entries(game_round)
                            for e in final_entries:
                                e["live"] = False
                            bd["entries"] = [
                                e for e in bd["entries"]
                                if not (e.get("date") == d_str
                                        and e.get("game_type") == gt)
                            ]
                            bd["entries"] = final_entries + bd["entries"]
                            backlog_path = PROJECT_ROOT / "backlog.json"
                            try:
                                file_data = {"entries": [], "strategies": {}}
                                if backlog_path.exists():
                                    with open(backlog_path) as f:
                                        file_data = _json.load(f)
                                file_data["entries"] = [
                                    e for e in file_data["entries"]
                                    if e.get("date") != d_str
                                    or e.get("game_type") != gt
                                ]
                                file_data["entries"] = final_entries + file_data["entries"]
                                with open(backlog_path, "w") as f:
                                    _json.dump(file_data, f, indent=2, default=str)
                                click.echo(f"  ✓ {key} sparad till backlog.json")
                            except Exception as e:
                                click.echo(f"  ⚠ Kunde inte spara backlog: {e}")

                        del live_rounds[key]
                        click.echo(f"  ✓ {key} avslutad — stoppar auto-refresh")

            try:
                asyncio.run(_do_refresh())
            except Exception as e:
                click.echo(f"  ⚠ Refresh-fel: {e}")

    # Alltid starta refresh-tråd (auto-detect nya omgångar + uppdatera live-odds)
    refresh_thread = threading.Thread(target=_refresh_live_odds, daemon=True)
    refresh_thread.start()

    # Sortera nycklar: idag först, sedan kronologiskt framåt, sedan bakåt
    today_iso = str(date.today())
    def _sort_key(k):
        d = k.split("/")[1]
        if d == today_iso:
            return (0, d)          # idag alltid först
        elif d > today_iso:
            return (1, d)          # kommande i datumordning
        else:
            return (2, "-" + d)    # historiska, nyast först
    def _get_sorted_keys():
        return sorted(html_pages.keys(), key=_sort_key)

    def _build_backlog_context(game_round, backlog_data):
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
            strat_names = sorted(set(e.get("strategy", "") for e in entries if e.get("strategy")))

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
                result = "✓" if payout > 0 else "✗"
                lines.append(
                    f"    {e.get('date','')} {strat}: "
                    f"insats {cost:,.0f}, utbet {payout:,.0f}, "
                    f"netto {netto:+,.0f} kr {result}"
                )

        # Bana-specifik statistik
        if track:
            track_entries = [
                e for e in entries
                if (e.get("track", "") or "").lower() == track
                and not e.get("live")
            ]
            if track_entries:
                lines.append(f"\n--- Bana: {game_round.track_name} ({len(track_entries)} omg) ---")
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
                '01': 'Januari', '02': 'Februari', '03': 'Mars', '04': 'April',
                '05': 'Maj', '06': 'Juni', '07': 'Juli', '08': 'Augusti',
                '09': 'September', '10': 'Oktober', '11': 'November', '12': 'December',
            }
            lines.append(f"\n--- ROI per månad (alla spelformer, alla år aggregerat) ---")
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
            lines.append(f"\n--- ROI per år (alla spelformer) ---")
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
        total_all_payout = sum(e.get("payout", 0) or 0 for e in entries if not e.get("live"))
        all_roi = (total_all_payout - total_all_cost) / total_all_cost * 100 if total_all_cost > 0 else 0
        lines.append(f"\n--- Totalt alla spelformer ---")
        lines.append(f"  {len(entries)} entries, ROI {all_roi:+.1f}%")
        lines.append(f"  Kostnad: {total_all_cost:,.0f} kr, Utbet: {total_all_payout:,.0f} kr")

        return "\n".join(lines)

    def _build_round_context(game_round):
        """Bygg textkontext om omgången för AI-chatten."""
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
            sm = "auto" if str(race.start_method) == "StartMethod.AUTO" or race.start_method.value == "auto" else "volt"
            lines.append(f"=== Lopp {race.race_number}: {race.race_name or ''} ===")
            lines.append(f"Bana: {race.track_name}, Distans: {race.distance}m, Start: {sm}")
            lines.append(f"Startande: {race.num_starters}, Prispott: {race.purse:,} kr")
            lines.append(f"Skrällrisk: {race.upset_risk:.0f}%")
            lines.append("")

            for i, entry in enumerate(sorted_entries[:6]):
                bet_str = f"{entry.bet_percentage:.1%}" if entry.bet_percentage else "-"
                form = entry.horse.recent_form_string
                factors = ", ".join(
                    f"{k}:{v:.0f}" for k, v in
                    sorted(entry.factor_scores.items(), key=lambda x: -x[1])[:3]
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

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            path = self.path.strip("/").split("?")[0].split("#")[0]

            if path == "api/chat":
                import json as _json
                import urllib.request
                from .config import ANTHROPIC_API_KEY

                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)

                try:
                    data = _json.loads(body.decode("utf-8"))
                except Exception:
                    resp = _json.dumps({"error": "Ogiltigt JSON"}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return

                messages = data.get("messages", [])
                round_key = data.get("round_key", "")

                if not ANTHROPIC_API_KEY:
                    resp = _json.dumps({"error": "ANTHROPIC_API_KEY saknas i .env"}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return

                # Bygg kontext
                game_round = all_game_rounds.get(round_key)
                context = _build_round_context(game_round) if game_round else "Ingen omgångsdata."
                bl_context = _build_backlog_context(game_round, backlog_data_holder[0]) if game_round else ""

                system_prompt = (
                    "Du är en AI-assistent för travanalys integrerad i Trav Agent-dashboarden. "
                    "Du svarar på svenska.\n\n"
                    f"Här är data för den aktuella omgången:\n\n{context}\n\n"
                    f"{bl_context}\n\n"
                    "Du kan svara på frågor om:\n"
                    "- Hästar, kuskar, tränare i omgången\n"
                    "- Modellpoäng, rekommendationer och skrällrisker\n"
                    "- Historisk statistik: ROI per strategi, spelform och bana\n"
                    "- Senaste resultat och trender\n"
                    "- Strategianalys och spelförslag\n\n"
                    "Svara kortfattat och informativt. Använd data från omgången och backlog-statistiken."
                )

                try:
                    api_data = _json.dumps({
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 2048,
                        "system": system_prompt,
                        "messages": messages,
                    }).encode("utf-8")

                    req = urllib.request.Request(
                        "https://api.anthropic.com/v1/messages",
                        data=api_data,
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01",
                        },
                        method="POST",
                    )

                    with urllib.request.urlopen(req, timeout=60) as api_resp:
                        result = _json.loads(api_resp.read().decode("utf-8"))
                        ai_text = result["content"][0]["text"]

                    resp = _json.dumps({"response": ai_text}).encode()
                    self.send_response(200)
                except Exception as e:
                    error_msg = str(e)
                    # Extrahera HTTP-felmeddelande om möjligt
                    if hasattr(e, "read"):
                        try:
                            error_body = _json.loads(e.read().decode("utf-8"))
                            error_msg = error_body.get("error", {}).get("message", error_msg)
                        except Exception:
                            pass
                    resp = _json.dumps({"error": error_msg}).encode()
                    self.send_response(500)

                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            path = self.path.strip("/").split("?")[0].split("#")[0]

            if not path:
                self.send_response(302)
                self.send_header("Location", f"/{_get_sorted_keys()[0]}")
                self.end_headers()
                return

            # Manuell refresh: /refresh/V75/2026-02-22
            if path.startswith("refresh/"):
                key = path[8:]  # "V75/2026-02-22"
                if key in live_rounds:
                    async def _manual_refresh():
                        from trav_agent.betting.live_backlog import generate_live_entries
                        client = ATGClient()
                        gr = live_rounds[key]
                        n_odds, n_finished, has_divs = await client.refresh_results(gr)

                        if n_odds > 0 or n_finished > 0:
                            gt, d_str = key.split("/", 1)
                            bd = backlog_data_holder[0]
                            if bd:
                                new_live = generate_live_entries(gr)
                                bd["entries"] = [
                                    e for e in bd["entries"]
                                    if not (e.get("live") and e.get("date") == d_str
                                            and e.get("game_type") == gt)
                                ]
                                bd["entries"] = new_live + bd["entries"]

                            available_rounds = [
                                (k, k.split("/")[0], k.split("/")[1], k not in live_rounds,
                                 (all_game_rounds[k].track_name or "") if k in all_game_rounds else "")
                                for k in sorted(html_pages.keys(), reverse=True)
                            ]
                            available_dates = [
                                (k.split("/")[1], k not in live_rounds)
                                for k in sorted(html_pages.keys(), reverse=True)
                            ]
                            new_html = generate_dashboard_html(
                                gr,
                                available_dates=available_dates,
                                backlog_data=backlog_data_holder[0],
                                available_rounds=available_rounds,
                            )
                            html_pages[key] = new_html.encode("utf-8")
                        return (n_odds, n_finished)
                    try:
                        n_odds, n_finished = asyncio.run(_manual_refresh())
                        click.echo(
                            f"  ↻ Manuell refresh: {key} "
                            f"({n_odds} odds, {n_finished} lopp avslutade)"
                        )
                    except Exception as e:
                        click.echo(f"  ⚠ Refresh-fel: {e}")

                self.send_response(302)
                self.send_header("Location", f"/{key}")
                self.end_headers()
                return

            if path == "landing":
                from .output.dashboard import generate_landing_html
                content = generate_landing_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

            if path in html_pages:
                content = html_pages[path]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                # 404 med lista av tillgängliga omgångar
                links = "".join(
                    f'<li><a href="/{k}">{k}</a></li>' for k in _get_sorted_keys()
                )
                body = (
                    f'<html><body style="font-family:sans-serif;padding:2rem">'
                    f'<h1>404 — Sidan hittades inte</h1>'
                    f'<p>Tillgängliga omgångar:</p><ul>{links}</ul>'
                    f'</body></html>'
                ).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # Tyst loggning

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    click.echo(f"Dashboard: {url}  (Ctrl+C för att stänga)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nDashboard stängd.")
        server.shutdown()


# ── DB-kommandon ─────────────────────────────────────────────────────────────


@cli.group()
def db():
    """Databaskommandon — Supabase-integration."""
    pass


@db.command("status")
def db_status():
    """Visa Supabase-anslutningsstatus och radantal."""
    from .config import SUPABASE_ENABLED, SUPABASE_URL
    from .database.client import is_configured, get_table_counts

    if not is_configured():
        click.echo("✗ Supabase ej konfigurerad")
        click.echo("  Skapa .env med SUPABASE_URL och SUPABASE_KEY")
        return

    click.echo(f"✓ Supabase ansluten: {SUPABASE_URL}")
    click.echo()

    try:
        counts = get_table_counts()
        total = 0
        click.echo("Tabell                  Rader")
        click.echo("─" * 40)
        for table, count in counts.items():
            count_str = str(count) if count >= 0 else "FEL"
            click.echo(f"  {table:<22} {count_str:>8}")
            if count > 0:
                total += count
        click.echo("─" * 40)
        click.echo(f"  {'TOTALT':<22} {total:>8}")
    except Exception as e:
        click.echo(f"✗ Fel vid anslutning: {e}")


@db.command("sync")
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.argument("day", type=str)
def db_sync(game_type: str, day: str):
    """Hämta, analysera och synka en omgång till Supabase."""
    from .database.client import get_client
    from .database.sync import sync_game_round, sync_analysis_results

    async def _run():
        client_atg = ATGClient()
        analyzer = CompositeAnalyzer()

        d = parse_date(day)
        click.echo(f"Hämtar {game_type} {d}...")

        game_round = await client_atg.fetch_full_round(game_type.upper(), d)
        if not game_round:
            click.echo(f"  ✗ Ingen {game_type} hittad för {d}")
            return

        # Berika med scrapead data
        scraped_path = CACHE_DIR / f"scraped_{game_type.lower()}_{d}.json"
        if scraped_path.exists():
            enrich_game_round(game_round, scraped_path)

        # Analysera
        analyzer.analyze_round(game_round)

        # Synka till Supabase
        db_client = get_client()
        click.echo("Synkar till Supabase...")

        counts = sync_game_round(game_round, client=db_client)
        click.echo(f"  ✓ Game round: {counts['races']} lopp, "
                    f"{counts['horses']} hästar, {counts['past_starts']} starter")

        updated = sync_analysis_results(game_round, client=db_client)
        click.echo(f"  ✓ Analysresultat: {updated} entries uppdaterade")

    asyncio.run(_run())


@db.command("import-cache")
@click.argument("game_type", type=click.Choice(GAME_TYPES, case_sensitive=False))
@click.option("--limit", "-n", type=int, default=0,
              help="Max antal omgångar att importera (0 = alla)")
def db_import_cache(game_type: str, limit: int):
    """Importera alla cachade omgångar till Supabase."""
    from .database.client import get_client
    from .database.sync import sync_game_round, sync_analysis_results

    async def _run():
        client_atg = ATGClient()
        analyzer = CompositeAnalyzer()
        db_client = get_client()

        # Hitta alla cachade datum
        cached_dates = _discover_cached_dates(game_type)
        if not cached_dates:
            click.echo(f"✗ Inga cachade {game_type}-omgångar hittades")
            return

        if limit > 0:
            cached_dates = cached_dates[:limit]

        click.echo(f"Importerar {len(cached_dates)} {game_type}-omgångar till Supabase...")
        click.echo()

        total_counts = {
            "rounds": 0, "races": 0, "horses": 0,
            "entries": 0, "past_starts": 0, "errors": 0,
        }

        for i, day_str in enumerate(cached_dates, 1):
            d = parse_date(day_str)
            click.echo(f"[{i}/{len(cached_dates)}] {game_type} {d}...", nl=False)

            try:
                game_round = await client_atg.fetch_full_round(game_type.upper(), d)
                if not game_round:
                    click.echo(" ✗ ej hittad")
                    continue

                # Berika
                scraped_path = CACHE_DIR / f"scraped_{game_type.lower()}_{d}.json"
                if scraped_path.exists():
                    enrich_game_round(game_round, scraped_path)

                # Analysera
                analyzer.analyze_round(game_round)

                # Synka
                counts = sync_game_round(game_round, client=db_client)
                sync_analysis_results(game_round, client=db_client)

                total_counts["rounds"] += 1
                total_counts["races"] += counts["races"]
                total_counts["horses"] += counts["horses"]
                total_counts["entries"] += counts["race_entries"]
                total_counts["past_starts"] += counts["past_starts"]

                click.echo(f" ✓ {counts['races']} lopp, {counts['horses']} hästar")

            except Exception as e:
                click.echo(f" ✗ Fel: {e}")
                total_counts["errors"] += 1
                logger.exception(f"Import error for {day_str}")

        click.echo()
        click.echo("═" * 50)
        click.echo(f"Import klar!")
        click.echo(f"  Omgångar:  {total_counts['rounds']}")
        click.echo(f"  Lopp:      {total_counts['races']}")
        click.echo(f"  Hästar:    {total_counts['horses']}")
        click.echo(f"  Entries:   {total_counts['entries']}")
        click.echo(f"  Starter:   {total_counts['past_starts']}")
        if total_counts["errors"]:
            click.echo(f"  Fel:       {total_counts['errors']}")

    asyncio.run(_run())


@db.command("import-backlog")
@click.option("--file", "-f", "file_path", type=click.Path(exists=True),
              default=None, help="Sökväg till backlog.json (default: backlog.json)")
def db_import_backlog(file_path: str):
    """Importera backlog.json till Supabase (backlog_rounds + backlog_race_picks)."""
    import json as _json
    from .database.backlog_sync import sync_all_backlog
    from .database.client import get_client

    if file_path is None:
        from .config import PROJECT_ROOT
        file_path = str(PROJECT_ROOT / "backlog.json")

    click.echo(f"Läser {file_path}...")
    with open(file_path) as f:
        backlog_data = _json.load(f)

    entries = backlog_data.get("entries", [])
    click.echo(f"Hittade {len(entries)} entries att importera")

    if not entries:
        click.echo("✗ Inga entries hittades")
        return

    db_client = get_client()

    def progress(i, total):
        click.echo(f"  [{i}/{total}] importerade...")

    click.echo("Importerar till Supabase...")
    stats = sync_all_backlog(backlog_data, client=db_client, progress_callback=progress)

    click.echo()
    click.echo("═" * 50)
    click.echo(f"Import klar!")
    click.echo(f"  Totalt:    {stats['total']}")
    click.echo(f"  Synkade:   {stats['synced']}")
    if stats['errors']:
        click.echo(f"  Fel:       {stats['errors']}")


if __name__ == "__main__":
    cli()
