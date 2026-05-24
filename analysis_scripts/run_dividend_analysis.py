import asyncio, re, json
from datetime import date
from pathlib import Path
from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.backtest.runner import BacktestRunner
from trav_agent.betting.system_generator import _get_rankings

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

    analyzer = CompositeAnalyzer()

    print("="*90)
    print("  V85 2026 UTDELNINGSANALYS — alla avslutade omgångar")
    print("="*90)

    all_dividends = []
    all_winner_data = []

    for gt, gd in dates:
        try:
            gr = await client.fetch_full_round(gt, date.fromisoformat(gd))
            if not gr or not gr.races or not gr.is_finished:
                print(f"\n--- {gt} {gd} --- SKIPPED (not finished or no races)")
                continue

            BacktestRunner._filter_future_starts(gr)
            for race in gr.races:
                for entry in race.entries:
                    entry.horse.recompute_career_from_starts()
            analyzer.analyze_round(gr)

            print(f"\n--- {gt} {gd} ---")
            print(f"  Dividends: {gr.dividends}")
            print(f"  Dividend systems: {gr.dividend_systems}")

            # Analyze winners
            for race in gr.races:
                if not race.result_order:
                    continue
                winner_post = race.result_order[0]
                _, by_market, model_rank, market_rank = _get_rankings(race)

                winner_entry = None
                for e in race.active_entries:
                    if e.post_position == winner_post:
                        winner_entry = e
                        break

                if winner_entry:
                    mr = model_rank.get(winner_post, 99)
                    kr = market_rank.get(winner_post, 99)
                    streck = winner_entry.bet_percentage or 0
                    print(f"  Avd {race.race_number}: vinnare #{winner_post} {winner_entry.horse.name} "
                          f"model_rank={mr} market_rank={kr} streck={streck:.1%}")
                    all_winner_data.append({
                        'date': gd,
                        'race': race.race_number,
                        'model_rank': mr,
                        'market_rank': kr,
                        'streck': streck,
                    })

            for tier, payout in sorted(gr.dividends.items(), reverse=True):
                systems = gr.dividend_systems.get(tier, 0)
                all_dividends.append({
                    'date': gd,
                    'tier': tier,
                    'payout_per_row': payout,
                    'systems': systems,
                })
        except Exception as e:
            import traceback
            print(f"  ERROR for {gt} {gd}: {e}")
            traceback.print_exc()
            continue

    # Summary
    print(f"\n{'='*90}")
    print("  SAMMANFATTNING")
    print(f"{'='*90}")

    # Dividend ranges per tier
    from collections import defaultdict
    by_tier = defaultdict(list)
    for d in all_dividends:
        by_tier[d['tier']].append(d['payout_per_row'])

    print("\n  Utdelning per tier (kr/rad):")
    for tier in sorted(by_tier.keys(), reverse=True):
        payouts = by_tier[tier]
        print(f"    {tier}/8: {len(payouts)} omg | min={min(payouts):,.0f} | median={sorted(payouts)[len(payouts)//2]:,.0f} | max={max(payouts):,.0f} | snitt={sum(payouts)/len(payouts):,.0f}")

    if not all_winner_data:
        print("\n  Ingen vinnardata hittades.")
        return

    # Winner rank distribution
    print(f"\n  Vinnarnas ranking (alla {len(all_winner_data)} lopp):")
    for label, key in [("Modell", "model_rank"), ("Marknad", "market_rank")]:
        ranks = [w[key] for w in all_winner_data]
        for top_n in [1, 2, 3, 4, 5]:
            count = sum(1 for r in ranks if r <= top_n)
            print(f"    {label} top-{top_n}: {count}/{len(ranks)} = {count/len(ranks):.1%}")
        print()

    # Spik analysis: how often does rank 1 win?
    print(f"  Spik-analys per lopp:")
    for key, label in [("model_rank", "Modell #1"), ("market_rank", "Marknad #1")]:
        wins = sum(1 for w in all_winner_data if w[key] == 1)
        print(f"    {label} vinner: {wins}/{len(all_winner_data)} = {wins/len(all_winner_data):.1%}")

    # How many spikes needed per round?
    from collections import Counter
    print(f"\n  Spikar som sitter per omgång:")
    dates_list = sorted(set(w['date'] for w in all_winner_data))
    for key, label in [("model_rank", "Modell"), ("market_rank", "Marknad")]:
        spike_counts = []
        for d in dates_list:
            winners_d = [w for w in all_winner_data if w['date'] == d]
            spikes = sum(1 for w in winners_d if w[key] == 1)
            spike_counts.append(spikes)
        print(f"    {label} #1 sitter: snitt {sum(spike_counts)/len(spike_counts):.1f}/8 | "
              f"min={min(spike_counts)} | max={max(spike_counts)} | "
              f"fördelning: {dict(Counter(spike_counts))}")

    # Combined union spike
    print(f"\n  Union-spikar (modell #1 ELLER marknad #1):")
    union_counts = []
    for d in dates_list:
        winners_d = [w for w in all_winner_data if w['date'] == d]
        union_spikes = sum(1 for w in winners_d if w['model_rank'] == 1 or w['market_rank'] == 1)
        union_counts.append(union_spikes)
    print(f"    Snitt: {sum(union_counts)/len(union_counts):.1f}/8 | "
          f"min={min(union_counts)} | max={max(union_counts)} | "
          f"fördelning: {dict(Counter(union_counts))}")

    # What's the sweet spot for system cost?
    print(f"\n  Sweet spot analys:")
    print(f"    8/8 utdelning: {sum(by_tier.get(8, [0]))/max(1,len(by_tier.get(8, [])))/1000:.0f}K kr/rad i snitt")
    if 7 in by_tier:
        print(f"    7/8 utdelning: {sum(by_tier[7])/len(by_tier[7]):.0f} kr/rad i snitt")
    if 6 in by_tier:
        print(f"    6/8 utdelning: {sum(by_tier[6])/len(by_tier[6]):.0f} kr/rad i snitt")

    # For a 1000kr system (~2000 rows), how many 7/8 rows do you typically get?
    print(f"\n  Typiskt antal vinstrader vid 7/8 (system med ~3 picks/race):")
    print(f"    Om du missar 1 lopp med 3 picks → 3 vinstrader × 7/8-utdelning")
    if 7 in by_tier:
        avg_7 = sum(by_tier[7])/len(by_tier[7])
        for n_picks in [2, 3, 4, 5]:
            payout = n_picks * avg_7
            print(f"    {n_picks} picks i missade loppet → {payout:,.0f} kr")

asyncio.run(main())
