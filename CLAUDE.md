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
├── analysis/       # Analysfaktorer (tid, form, bana, etc.)
├── backtest/       # Backtesting-motor
├── output/         # Rapportgenerering
├── cli.py          # Kommandorad
└── config.py       # Vikter & inställningar
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

## Dennis' V85-metod (bakgrund)

- Max 2 spikar per omgång (>40% streck)
- Sweet spot: 5-15% streck med hög analysvärde
- Jaga: dåligt resultat + bra tid, distansbyte, disk-favoriter
- Avd 5 och 7 skrällar mest
- Ett lopp avgör — gå brett i osäkra lopp
- Skrällindex: om 3+ expertsystem gardar 8+ hästar = hög skrällrisk

## Nästa steg / TODO

- [ ] Verifiera ATG API-parsers mot faktisk data
- [ ] Implementera MCP browser-scraping för travsport.se (djupare hästhistorik)
- [ ] Lägg till kusk-statistik från extern källa
- [ ] Implementera faktor-optimering (grid search på vikter)
- [ ] Skrällindex-integration (koppla expertkonsensus-data)
- [ ] Testa modellen på V75 (2025 historik finns)
- [ ] Exportera rekommendationer till Excel (för konsensus-jämförelse)
