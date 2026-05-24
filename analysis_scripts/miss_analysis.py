import asyncio, re
from datetime import date
from pathlib import Path
from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.backtest.runner import BacktestRunner
from trav_agent.betting.build_system import build_ram, select_horses

async def main():
    client = ATGClient()
    cache_dir = Path('cache')

    pattern = re.compile(r'^(V85|V75)_(\d{4}-\d{2}-\d{2})_')
    dates = []
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m and date.fromisoformat(m.group(2)) >= date(2026, 1, 1):
            dates.append((m.group(1), m.group(2)))
    dates = sorted(set(dates))

    print(f"Found date/gametype combos: {dates}")

    analyzer = CompositeAnalyzer()
    rounds = []
    for gt, gd in dates:
        try:
            gr = await client.fetch_full_round(gt, date.fromisoformat(gd))
            if not gr or not gr.races or not gr.is_finished:
                print(f"  Skipping {gt} {gd}: not finished or no races")
                continue
            BacktestRunner._filter_future_starts(gr)
            for race in gr.races:
                for entry in race.entries:
                    entry.horse.recompute_career_from_starts()
            analyzer.analyze_round(gr)
            rounds.append(gr)
            print(f"  Loaded {gt} {gd}: {len(gr.races)} races")
        except Exception as e:
            print(f"  Error loading {gt} {gd}: {e}")
            import traceback; traceback.print_exc()
            continue

    print(f"\nLoaded {len(rounds)} finished rounds")

    total_misses_by_type = {}
    total_races_by_type = {}
    miss_details = []

    for gr in rounds:
        legs = build_ram(gr)
        select_horses(legs, 1000, 0.50)

        print(f"\n{'='*80}")
        print(f"  {gr.game_type} {gr.round_date} -- {gr.dividends.get(8, 'ingen 8/8')} kr/rad for 8/8")
        print(f"{'='*80}")

        correct = 0
        misses = []

        for leg, race in zip(legs, gr.races):
            winner = race.result_order[0] if race.result_order else None
            selected_set = set(leg.selected)
            hit = winner in selected_set if winner else False

            if hit:
                correct += 1

            winner_horse = None
            for h in leg.horses:
                if h.post == winner:
                    winner_horse = h
                    break

            if winner_horse:
                status = 'HIT' if hit else 'MISS'
                marker = ''
                if not hit:
                    misses.append({
                        'date': str(gr.round_date),
                        'leg': leg.leg_num,
                        'typ': leg.typ,
                        'security': leg.security,
                        'winner_post': winner,
                        'winner_name': winner_horse.name,
                        'winner_model_rank': winner_horse.model_rank,
                        'winner_market_rank': winner_horse.market_rank,
                        'winner_streck': winner_horse.streck,
                        'winner_poang': winner_horse.poang,
                        'n_picks': len(leg.selected),
                        'n_starters': leg.num_starters,
                        'picks': leg.selected,
                    })
                    marker = f" <- MISS! Vinnare rank M{winner_horse.model_rank}/K{winner_horse.market_rank} streck={winner_horse.streck:.1%}"

                typ_key = leg.typ
                total_races_by_type[typ_key] = total_races_by_type.get(typ_key, 0) + 1
                if not hit:
                    total_misses_by_type[typ_key] = total_misses_by_type.get(typ_key, 0) + 1

                print(f"  Avd {leg.leg_num} [{leg.typ:>6} s={leg.security:>3.0f}] "
                      f"{len(leg.selected)} val: {status} "
                      f"vinnare #{winner} {winner_horse.name} "
                      f"M{winner_horse.model_rank}/K{winner_horse.market_rank} "
                      f"{winner_horse.streck:.0%}{marker}")

        print(f"  -> {correct}/8 ratt")
        miss_details.extend(misses)

    print(f"\n{'='*80}")
    print(f"  SAMMANFATTNING: VAR SPRICKER SYSTEMEN?")
    print(f"{'='*80}")

    print(f"\n  Totalt {len(miss_details)} missar i {len(rounds)} omgangar")

    print(f"\n  Miss-rate per loppkaraktar:")
    for typ in sorted(total_races_by_type.keys()):
        total = total_races_by_type.get(typ, 0)
        m_count = total_misses_by_type.get(typ, 0)
        if total > 0:
            print(f"    {typ:>6}: {m_count}/{total} missar = {m_count/total:.0%}")

    if miss_details:
        print(f"\n  Vinnarens ranking vid miss:")
        for label, key in [('Modell-rank', 'winner_model_rank'), ('Marknad-rank', 'winner_market_rank')]:
            ranks = [m[key] for m in miss_details]
            print(f"    {label}: snitt={sum(ranks)/len(ranks):.1f} | "
                  f"rank 1-2: {sum(1 for r in ranks if r <= 2)}/{len(ranks)} | "
                  f"rank 3-4: {sum(1 for r in ranks if 3 <= r <= 4)}/{len(ranks)} | "
                  f"rank 5+: {sum(1 for r in ranks if r >= 5)}/{len(ranks)}")

        print(f"\n  Vinnarens streckprocent vid miss:")
        strecks = [m['winner_streck'] for m in miss_details]
        print(f"    Snitt: {sum(strecks)/len(strecks):.1%}")
        print(f"    >20%: {sum(1 for s in strecks if s > 0.20)}/{len(strecks)} -- FAVORITER vi missar!")
        print(f"    10-20%: {sum(1 for s in strecks if 0.10 <= s <= 0.20)}/{len(strecks)}")
        print(f"    5-10%: {sum(1 for s in strecks if 0.05 <= s <= 0.10)}/{len(strecks)}")
        print(f"    <5%: {sum(1 for s in strecks if s < 0.05)}/{len(strecks)} -- outsiders (ej att fanga)")

        print(f"\n  Antal val i missade lopp:")
        for n in [1, 2, 3, 4, 5, 6]:
            count = sum(1 for m in miss_details if m['n_picks'] == n)
            if count > 0:
                print(f"    {n} val: {count} missar")

        print(f"\n  Hade vi fangat vinnaren med fler val?")
        for threshold in [3, 4, 5]:
            caught = sum(1 for m in miss_details
                        if min(m['winner_model_rank'], m['winner_market_rank']) <= threshold)
            print(f"    Top-{threshold} (modell ELLER marknad): fangar {caught}/{len(miss_details)} = {caught/len(miss_details):.0%}")

    print(f"\n  Omvand spik-analys: vad om vi spikade RATT?")
    for gr in rounds:
        legs = build_ram(gr)
        select_horses(legs, 1000, 0.50)

        safe_spikes = []
        for leg, race in zip(legs, gr.races):
            winner = race.result_order[0] if race.result_order else None
            if winner and leg.horses and leg.horses[0].post == winner:
                safe_spikes.append(leg.leg_num)

        print(f"    {gr.round_date}: {len(safe_spikes)} sakra spikar mojliga (avd {safe_spikes})")

    print(f"\n  Kan vi FORUTSAGA vilka lopp som ar sakra att spika?")

    trygg_total = 0
    trygg_top1_wins = 0
    for gr in rounds:
        legs = build_ram(gr)
        for leg, race in zip(legs, gr.races):
            if leg.typ == 'TRYGG':
                trygg_total += 1
                winner = race.result_order[0] if race.result_order else None
                if winner and leg.horses and leg.horses[0].post == winner:
                    trygg_top1_wins += 1

    if trygg_total:
        print(f"    TRYGG: top-1 vinner {trygg_top1_wins}/{trygg_total} = {trygg_top1_wins/trygg_total:.0%}")
        print(f"    -> {'JA, spik i TRYGG funkar!' if trygg_top1_wins/trygg_total >= 0.70 else 'NEJ, inte sakert nog'}")
    else:
        print('    Inga TRYGG-lopp')

    trygg_top2_wins = 0
    for gr in rounds:
        legs = build_ram(gr)
        for leg, race in zip(legs, gr.races):
            if leg.typ == 'TRYGG':
                winner = race.result_order[0] if race.result_order else None
                top2 = set(h.post for h in leg.horses[:2])
                if winner in top2:
                    trygg_top2_wins += 1

    if trygg_total:
        print(f"    TRYGG top-2 fangar: {trygg_top2_wins}/{trygg_total} = {trygg_top2_wins/trygg_total:.0%}")

asyncio.run(main())
