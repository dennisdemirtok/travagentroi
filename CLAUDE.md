# CLAUDE.md — Instruktioner för Claude Code

## Projektöversikt

Trav Agent är ett Python-baserat analysverktyg för svensk travsport.
Det hämtar data från ATG:s API, analyserar hästar med multipla faktorer,
och backtesterar strategier mot historiska resultat.

## Kom igång

```bash
cd trav-agent
pip install -r requirements.txt
```

## ATG API

ATG har semi-öppna API:er som inte kräver autentisering:

- **Bas-URL:** `https://www.atg.se/services/racinginfo/v1/api/`
- **Kalender:** `/calendar/day/YYYY-MM-DD`
- **Speldata:** `/games/{game_id}` — t.ex. `V85_2026-02-21_42_8`
- **Loppdata:** `/races/{race_id}`
- **Produkter:** `/products/V85`
- **Analyser (zeta):** `https://dmh.aws.atg.se/zeta/{track_id}/YYYY-MM-DD`

### OBS om API-anrop
- Håll minst 1 sekund mellan anrop
- Zeta-endpoints: minst 3 sekunder
- Alla svar är JSON
- API:ernas exakta struktur kan ändras — validera alltid parsers

## Arkitektur

```
trav_agent/
├── data/           # ATG-klient, datamodeller, cache
├── analysis/       # Analysfaktorer (15 st: tid, form, bana, kusk, etc.)
├── backtest/       # Backtesting-motor
├── database/       # Supabase sync (backlog, bet_results)
├── output/         # Dashboard HTML-generator (~5000 rader)
├── cli.py          # Kommandorad
└── config.py       # Vikter & inställningar (v9 hybrid)

app.py              # FastAPI server (dashboard, AI chat, bet API)
scripts/            # Analysscript (proffs-edge, vinnarspel, viktoptimering)
proffs_cache/       # Proffs-konsensusdata (pre/post-race JSON)
```

## Kommandorads-användning

```bash
python -m trav_agent fetch V85 2026-02-21
python -m trav_agent analyze V85 2026-02-21
python -m trav_agent report V85 2026-02-21
python -m trav_agent backtest V85 --from 2025-01-01 --to 2025-12-31
python -m trav_agent explore V85 2026-02-21 --race 3 --horse 5
```

## Utvecklingsguide

### Lägga till en ny analysfaktor

1. Skapa en ny fil i `trav_agent/analysis/`
2. Ärv från `AnalysisFactor`
3. Implementera `score(entry, race) → float 0-100`
4. Lägg till i `CompositeAnalyzer.__init__` och `FactorWeights`
5. Backtesta för att verifiera prediktiv kraft

### Modifiera vikter

Ändra `FactorWeights` i `config.py` eller kör optimeraren.

### API-parsning

Om ATG ändrar API-strukturen, uppdatera parsers i `atg_client.py`.
Kör `fetch` först för att inspektera rå JSON:

```python
import asyncio, httpx
async def peek():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://www.atg.se/services/racinginfo/v1/api/calendar/day/2026-02-21")
        print(r.json())
asyncio.run(peek())
```

## Modellversion (v9 — Hybrid)

Hybrid-modell: 25% modell + 75% marknad (streckprocent).
Bara 4 faktorer tillför positiv edge till hybriden:

| Faktor | Vikt | Edge |
|--------|------|------|
| post_position | 0.35 | +0.38% |
| age | 0.35 | +0.32% |
| driver_class | 0.15 | +0.13% |
| category_profile | 0.10 | +0.13% |

Alla andra faktorer (tid, form, bana, etc.) behålls med 0-vikt
för dashboard-analys men påverkar inte ranking.

### Vinnarspel — Den enda bevisade edgen (no-leakage validerad)

5 profiler med kuskfilter, backtestade på 169 omgångar (jun 2025 → maj 2026)
med full leakage prevention (temporal filtrering + odds-neutralisering):

| Profil | ROI | Spel | Vinst% | Odds | Beskrivning |
|--------|-----|------|--------|------|-------------|
| Sniper | +79.5% | 55 | 12.7% | 14.1x | Rank A-B + kusk ≥100 st/år + 3-10% |
| Pro | +16.3% | 432 | 16.9% | 6.9x | Rank A-B + kusk ≥100 st/år + 5-20% |
| Sharp | +8.1% | 264 | 18.6% | 5.8x | Score ≥45 + kusk ≥100 st/år + 5-20% |
| Bas | +8.7% | 626 | 16.1% | 6.7x | Rank A-B + 5-20% (ingen kuskfilter) |
| Elite | +0.5% | 429 | 14.9% | 6.7x | Score topp-10% + 5-20% |

Kuskfilter: `driver_starts_year ≥ 100` — kuskar med <100 starter/år förlorar (-15.7% ROI).
⚠-varning visas i dashboarden för kuskar med under 100 starter.

### Systemspel — INTE lönsamt (no-leakage backtest)

21 strategier testade på 165 omgångar (V75+V85+GS75, 2025-2026):
- **INGEN** strategi är lönsam med korrekt leakage prevention
- Bäst: Spike Easiest 300kr = -0.3% ROI (breakeven)
- Alla gamla "+2134% ROI"-siffror berodde på data leakage
- Gamla strateginamn (I_streck_1st, Q_dom_x_mktgap, D_market_gap) var fake — alla körde A_union

### Proffs Rescue (systembygge)
- Om proffs rankar häst topp-3 men modellen har den rank 4-6
  + streck 5-20% + proffs ≥15% + edge ≥10pp → ersätt lägsta pick
- Max 1 rescue per lopp

## Dennis' V85-metod (bakgrund)

- Max 2 spikar per omgång (>40% streck)
- Sweet spot: 5-15% streck med hög analysvärde
- Jaga: dåligt resultat + bra tid, distansbyte, disk-favoriter
- Avd 5 och 7 skrällar mest
- Ett lopp avgör — gå brett i osäkra lopp
- Skrällindex: om 3+ expertsystem gardar 8+ hästar = hög skrällrisk
- V75 = V85 (samma spelform, bara namnbyte + 1 extra lopp)

## Ranking-system

A/B/C/D ranking genom hela systemet:
- **A** = Spik (grön) — rank 1, stark favorit
- **B** = 2-val/3-val (blå) — rank 2-3
- **C** = Gardering (amber) — rank 4-6
- **D** = Strykning (röd) — rank 7+

## Dashboard-flikar

1. **Dashboard** — Översikt, system, lopp-per-lopp analys
2. **Statistik** — ROI per strategi, speltyp, tidsperiod
3. **Backlog** — Historiska systemresultat
4. **Bet** — Vinnarspel-rekommendationer + live P&L tracking
5. **Agent** — AI-chat med omgångskontext

## TODO

- [ ] Kör viktoptimeraren (`scripts/optimize_weights.py`) på senaste data
- [ ] TRAIS API-integration (TR Media) för loppkommentarer
- [ ] Excel-export av rekommendationer
- [ ] Skrällindex-integration (koppla expertkonsensus-data)
- [ ] Mobile-responsiv förbättring (hamburger-meny, swipe)
- [ ] Regenerera backlog.json med D_smart strategier (ersätter gammal A/B/C)

## Klart (tidigare TODO)

- [x] ATG API-parsers verifierade
- [x] Kusk-statistik (driver_class + driver_trainer faktorer)
- [x] Faktor-optimering (grid search script klart)
- [x] Proffs-konsensus pipeline (scraping + launchd cron)
- [x] Supabase backlog + bet results sync
- [x] Dashboard ljust tema + ny layout
- [x] AI chat med omgångskontext
- [x] Chansspik vinnarspel backtest + dashboard-integration
- [x] A/B/C/D ranking-system
- [x] No-leakage backtest: 21 strategier × 165 omgångar (maj 2026)
- [x] Kuskfilter i vinnarspel (5 profiler med driver_starts ≥ 100)
- [x] Backfill bet_results till Supabase (169 omgångar)
- [x] Dashboard cleanup: ta bort fake strategier (I_streck, Q_dom, D_market)
- [x] Vinnarspel no-leakage verifiering (+8-80% ROI, bekräftat)
