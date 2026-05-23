# Trav Agent — AI-driven travanalys

Ett Python-baserat analysverktyg för svensk travsport som hämtar data från ATG:s API,
analyserar hästar med multipla faktorer och backtesterar strategier mot historiska resultat.

## Snabbstart

```bash
# Installera dependencies
pip install -r requirements.txt

# Hämta en omgång och analysera
python -m trav_agent.cli fetch V85 2026-02-21
python -m trav_agent.cli analyze V85 2026-02-21
python -m trav_agent.cli report V85 2026-02-21

# Backtesta mot historiska omgångar
python -m trav_agent.cli backtest V85 --from 2025-01-01 --to 2025-12-31
```

## Arkitektur

```
trav_agent/
├── data/
│   ├── atg_client.py       # ATG API-klient (racinginfo v1)
│   ├── models.py           # Datamodeller (Häst, Lopp, Start, Omgång)
│   └── cache.py            # Lokal cache för API-svar
├── analysis/
│   ├── base.py             # Baseklass för analysfaktorer
│   ├── time_analysis.py    # Tidanalys per bana/distans/underlag
│   ├── prize_index.py      # Prispengaindex & motståndsranking
│   ├── form_curve.py       # Formkurva senaste N starter
│   ├── track_profile.py    # Banprofil & hemmabanefördel
│   ├── category_profile.py # Auto/volt, distans, ston, ålder
│   ├── driver_trainer.py   # Kusk & tränare statistik
│   ├── post_position.py    # Spåranalys per bana/startmetod
│   └── composite.py        # Sammanvägd poäng med viktning
├── backtest/
│   ├── runner.py           # Kör modellen på historiska omgångar
│   └── evaluator.py        # Mät precision, ROI, Brier score
├── output/
│   └── report.py           # Generera analysrapport
├── cli.py                  # Kommandorads-interface
└── config.py               # Konfiguration & vikter
```

## ATG API-endpoints

Dessa endpoints används (semi-öppna, kräver ej auth):

- `GET /services/racinginfo/v1/api/calendar/day/{YYYY-MM-DD}` — Alla lopp en dag
- `GET /services/racinginfo/v1/api/games/{game_id}` — Speldata med startlistor
- `GET /services/racinginfo/v1/api/products/{product}` — Nästkommande omgång
- `GET /services/racinginfo/v1/api/races/{race_id}` — Enskilt lopp med resultat

## Analysfaktorer

| Faktor | Beskrivning | Vikt (default) |
|--------|------------|----------------|
| Tid | Normaliserad tid per km, bana, underlag | 0.20 |
| Prispengar | Motståndsindex baserat på prispengar i fältet | 0.15 |
| Form | Trend senaste 5-10 starter (placering + tid) | 0.20 |
| Bana | Prestation på aktuell bana | 0.10 |
| Kategori | Auto/volt, distans, kön, ålder | 0.10 |
| Kusk/Tränare | Vinstprocent, formkurva, kusk+häst-kombo | 0.10 |
| Spår | Spårposition relativt bana/startmetod | 0.05 |
| Distansbyte | Bonus vid distansbyte med bra tid/dåligt resultat | 0.05 |
| Disk-historik | Malus för galoppbenägna hästar | 0.05 |

## Backtesting

Systemet kan testa strategier på historiska omgångar:

```bash
# Testa alla faktorer
python -m trav_agent.cli backtest V75 --from 2025-06-01 --to 2025-12-31

# Testa enskild faktor isolerat
python -m trav_agent.cli backtest V75 --factor time_analysis --from 2025-06-01 --to 2025-12-31

# Optimera vikter
python -m trav_agent.cli optimize V75 --from 2025-01-01 --to 2025-12-31
```
