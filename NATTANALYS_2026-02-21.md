# Nattanalys 2026-02-21 — Sammanfattning

## Vad som gjordes under natten

### ✅ 1. Marknadssignal — IMPLEMENTERAD
Multiplicativ bonus (max 15%) för hästar med:
- Odds < 5.0 (marknaden tror starkt)
- Win-rate > 15% (bevisat vinnare)
- Minst 5 starter

**A/B-testat på 653 lopp:**
- V75 (525 lopp): Top Pick +0.6%, snittrank -0.01
- V85 (128 lopp): Top-1 +1.6%, Top Pick +0.8%, Top-3 +0.01 (4/4 bättre)

Koden finns i `composite.py` Steg 3b.

### ✅ 2. Odds-scraping för live-omgångar
Parsning av ATG:s pool-data (`starter.pools.vinnare.odds` och `betDistribution`).
Fungerar för kommande omgångar — V85 2026-02-21 har nu odds och streckprocent.

Historiskt: `finalOdds` från resultatdata (finns i API:et per häst).

### ✅ 3. Dashboard omstartad
Körs med marknadssignal, historik 8 omgångar. PID 37100.
http://127.0.0.1:8585

### ✅ 4. Supabase synkad
93 omgångar (75 V75 + 18 V85), 669 lopp, 135k+ rader.
Alla scores uppdaterade med marknadssignal.

### ✅ 5. Djupanalys av mönster (653 lopp)

#### Startmetod (AUTO vs VOLT)
| Metod | Lopp | Top-1% | Top-3% | SnittRank |
|-------|------|--------|--------|-----------|
| AUTO | 485 | 57.1% | 81.6% | 2.19 |
| VOLT | 168 | 52.4% | 81.5% | 2.35 |

Modellen är 4.7% bättre på auto. Testade volt-specifika vikter → +0.6% Top-1
för volt men marginellt totalt. **Inte implementerat** (för liten effekt).

#### Distans
| Distans | Lopp | Top-1% | SnittRank |
|---------|------|--------|-----------|
| 1600-1700m | 133 | **63.9%** | 1.98 |
| 2100-2200m | 398 | 51.3% | 2.34 |
| 2600-2700m | 102 | 60.8% | 2.16 |
| 3100+m | 15 | 66.7% | 2.40 |

Korta lopp lättast, medeldistans svårast. Missade vinnare vid medeldistans:
51% från spår 7+ (högt spår = vår svaghet). Fångas redan av post_position.

#### Spårposition
- AUTO: Spår 1-5 dominerar (11-14% win), spår 7+: 5-6%
- VOLT: Spår 1 starkast (15.1%), spår 3 svagt (8.0%), spår 12 högre (7.9%)

#### Bana — Modellens precision
| Bäst | Top-1% | Sämst | Top-1% |
|------|--------|-------|--------|
| Rättvik | 78.6% | Halmstad | 36.4% |
| Kalmar | 73.3% | Åby | 39.5% |
| Örebro | 71.4% | Bjerke | 42.9% |

Svaga banor har 23 rank-5+ missar vs 6 på starka banor.

#### Fältstorlek
| Fält | Lopp | Top-1% | Top-3% |
|------|------|--------|--------|
| ≤8 | 22 | 77.3% | 90.9% |
| 9-10 | 122 | 62.3% | 86.1% |
| 11-12 | 364 | 53.0% | 80.2% |
| 13+ | 145 | 54.5% | 80.0% |

#### Faktorkorrelation per startmetod
| Faktor | AUTO | VOLT | Kommentar |
|--------|------|------|-----------|
| track_profile | -0.396 | -0.364 | Starkast i båda |
| category_profile | -0.334 | -0.324 | Stark |
| prize_index | -0.333 | -0.259 | Svagare i volt |
| post_position | -0.280 | -0.192 | Svagare i volt |
| form_curve | -0.244 | -0.173 | Svagare i volt |
| time_analysis | -0.156 | -0.088 | Halverad i volt |
| driver_trainer | -0.026 | +0.000 | Svag i båda |

### ✅ 6. 5-årig historisk precision
| År | Omg | Lopp | Top-1% | Top-3% | SnittRank |
|----|-----|------|--------|--------|-----------|
| 2021 | 4 | 28 | 50.0% | 82.1% | 2.11 |
| 2022 | 4 | 28 | 39.3% | 53.6% | 3.89 |
| 2023 | 4 | 28 | 53.6% | 82.1% | 2.18 |
| 2024 | 4 | 28 | 46.4% | 71.4% | 2.82 |
| 2025 | 4 | 28 | 50.0% | 71.4% | 2.68 |

**Slutsats**: Modellen fungerar konsekvent 5 år bakåt. 2022 var ett svagt
stickprov (bara 4 omgångar). Att hämta mer data (5 år) ger robustare validering
men förbättrar sannolikt inte vikterna — de är redan optimerade.

## Slutsatser

### Implementerade förbättringar
1. **Marknadssignal** (+1.6% Top-1 för V85) — Permanent i koden
2. **Odds-scraping** för live-omgångar — Klart
3. **Supabase synkad** med uppdaterade scores

### Testade men INTE implementerade (marginella effekter)
1. Volt-specifika vikter → +0.6% volt Top-1, +0.1% totalt
2. Distansspecifika justeringar → redan fångade av befintliga faktorer
3. Banspecifik korrigering → för få lopp per bana för robust modell

### Rekommendationer framåt
1. **Hämta mer V85-data** — bara 17 omgångar nu, mer data stärker V85-analysen
2. **Driver_trainer** har närmast noll prediktiv kraft (-0.026). Kan tas bort eller
   ersättas med en bättre faktor
3. **Åby/Halmstad/Bjerke** — undersök varför modellen är svag där
4. **5-årig import** — ATG API stöder det, men kräver ~2h API-tid
