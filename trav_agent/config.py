"""Konfiguration & vikter för analysmodellen."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Sökvägar ──────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output_reports"

# ── Supabase ─────────────────────────────────────────────────────────────────

_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except ImportError:
        pass

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_KEY", "")
SUPABASE_ENABLED: bool = bool(SUPABASE_URL and SUPABASE_KEY)

# ── Anthropic (AI-chatt i dashboard) ─────────────────────────────────────────

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY and _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            if _line.startswith("ANTHROPIC_API_KEY="):
                ANTHROPIC_API_KEY = _line.strip().split("=", 1)[1]
                break

# ── ATG API ───────────────────────────────────────────────────────────────────

ATG_BASE_URL = "https://www.atg.se/services/racinginfo/v1/api"
ATG_ZETA_URL = "https://dmh.aws.atg.se/zeta"
REQUEST_DELAY = 1.0  # sekunder mellan anrop (var snäll mot ATG)
REQUEST_TIMEOUT = 15.0

# ── Produktnamn → spelform ────────────────────────────────────────────────────

GAME_TYPES = ["V75", "V86", "V85", "V64", "V65", "V4", "V5", "GS75"]

# ── Spelschema per veckodag (0=Mån ... 6=Sön) ────────────────────────────────

GAME_SCHEDULE: dict[int, list[str]] = {
    0: ["V64"],           # Måndag
    1: ["V64"],           # Tisdag
    2: ["V86"],           # Onsdag
    3: ["V64"],           # Torsdag
    4: ["V64"],           # Fredag
    5: ["V85"],           # Lördag (f.d. V75, numera V85)
    6: ["GS75"],          # Söndag
}

# ── Radpris per speltyp (kr per kombination) ─────────────────────────────────

ROW_PRICES: dict[str, float] = {
    "V75": 0.50,
    "V85": 0.50,
    "GS75": 0.50,
    "V86": 0.25,
    "V64": 1.00,
    "V65": 0.50,
    "V4": 0.50,
    "V5": 0.50,
}


def get_todays_game_types() -> list[str]:
    """Returnera speltyper idag — hämtar från ATG:s products-API.

    Kollar om V85/V86/GS75/V64 har en omgång schemalagd idag.
    Fallback: statiskt veckoschema.
    """
    from datetime import date as _date
    import httpx
    today_str = str(_date.today())
    found = []

    try:
        for gt in ["V85", "V86", "GS75", "V64"]:
            r = httpx.get(
                f"https://www.atg.se/services/racinginfo/v1/api/products/{gt}",
                timeout=5.0,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for upcoming in data.get("upcoming", []):
                start = upcoming.get("startTime", "")
                if start.startswith(today_str):
                    found.append(gt)
                    break
    except Exception:
        pass

    if found:
        # Sortera: V85 > V86 > GS75 > V64
        prio = {"V85": 0, "V86": 1, "GS75": 2, "V64": 3}
        return sorted(found, key=lambda x: prio.get(x, 9))

    # Fallback till statiskt schema
    return GAME_SCHEDULE.get(_date.today().weekday(), [])


@dataclass
class FactorWeights:
    """Vikter for varje analysfaktor. Summerar till 1.0.

    Version 7.1: 13 faktorer — Dennis's metod, omoptimerade.

    Forandringar vs v7.0:
    - time_analysis uppgraderad: 5 primart + 6-10 med decay, tillagg
    - equipment: barfot/sulkybyte (Am/Va) som skrall-signal
    - Vikter omoptimerade efter alla forandringar

    Optimerat med random search + hill climbing pa 424 V75/V85-lopp:
    - top1: 24.3% (vinnare korrekt)
    - top2: 37.7% (topp 2 ratt)
    - top3: 54.0% (topp 3 ratt)
    - spik: 30.7% traffsakerhet (150 spikar)

    Utan data-leakage (temporal filtrering + neutraliserade closing odds).
    """

    # v7.1 optimerade vikter (hill climbing pa 424 V75/V85-lopp)
    post_position: float = 0.157       # Sparprofil
    prize_index: float = 0.156         # Prispengaindex
    age: float = 0.146                 # Aldersfaktor
    time_analysis: float = 0.130       # Dennis's tid (5+10 med decay, tillagg)
    competition_strength: float = 0.102 # Motstandsstyrka (Dennis)
    track_profile: float = 0.074       # Banerfarenhet
    driver_class: float = 0.062        # Kuskens vinstprocent
    category_profile: float = 0.062    # Kategoriprofil
    last_win: float = 0.049            # Seger senast (Dennis)
    form_curve: float = 0.049          # Formkurva
    gallop_risk: float = 0.035          # Galopphistorik + voltspårsinteraktion
    equipment: float = 0.005           # Barfot/sulky (Am/Va)
    driver_trainer: float = 0.005      # Kusk-tranar kombination
    layoff: float = 0.003              # Uppehall

    # Interaktionstermer borttagna
    interaction_track_post: float = 0.0
    interaction_track_category: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Bas-vikter (utan interaktioner)."""
        return {
            "time_analysis": self.time_analysis,
            "last_win": self.last_win,
            "competition_strength": self.competition_strength,
            "layoff": self.layoff,
            "prize_index": self.prize_index,
            "form_curve": self.form_curve,
            "track_profile": self.track_profile,
            "category_profile": self.category_profile,
            "driver_trainer": self.driver_trainer,
            "post_position": self.post_position,
            "driver_class": self.driver_class,
            "equipment": self.equipment,
            "age": self.age,
            "gallop_risk": self.gallop_risk,
        }

    def interactions(self) -> list[tuple[str, str, float]]:
        """Interaktionspar: (faktor1, faktor2, vikt)."""
        result = []
        if self.interaction_track_post > 0:
            result.append(("track_profile", "post_position", self.interaction_track_post))
        if self.interaction_track_category > 0:
            result.append(("track_profile", "category_profile", self.interaction_track_category))
        return result

    def normalized(self) -> dict[str, float]:
        """Returnera vikter normaliserade till summa 1.0."""
        d = self.as_dict()
        total = sum(d.values())
        return {k: v / total for k, v in d.items()}


@dataclass
class AnalysisConfig:
    """Huvudkonfiguration för analysmotorn."""

    weights: FactorWeights = field(default_factory=FactorWeights)

    # Hur många senaste starter att analysera (Dennis: senaste 5)
    recent_starts_count: int = 5

    # Tröskelvärden
    spike_threshold: float = 0.30  # Max streckprocent för spik (skärpt från 0.40)
    value_sweet_spot: tuple[float, float] = (0.05, 0.15)  # 5-15% streck = sweet spot

    # Super Score: blend av modell + marknad
    # 85% modell + 15% streckprocent — streck ger lite info men
    # ska inte dominera. Tidigare 55/45 var overfit på slutgiltig streck.
    # Streck 1h före start är ganska stabil vs slutvärde, men 45% var
    # för mycket — modellen måste stå på egna ben.
    super_score_model_weight: float = 1.0  # 0.0 = ren marknad, 1.0 = ren modell

    # Klassificeringströsklar (anpassade för mer modell-driven skala)
    spike_min_score: float = 75.0     # Minst poäng för "spik" (rank 1)
    spike_min_gap: float = 10.0      # Minst gap till rank 2 för spik
    choice2_min_score: float = 55.0   # Minst poäng för "2-val" (rank ≤ 2)
    choice3_min_score: float = 42.0   # Minst poäng för "3-val" (rank ≤ 3)
    gardering_min_score: float = 28.0  # Under detta = "strykning"

    # Backtesting
    min_roi_threshold: float = -0.10  # Acceptabel ROI-förlust vid backtesting
    confidence_levels: list[str] = field(
        default_factory=lambda: ["spik", "2-val", "3-val", "gardering", "strykning"]
    )


# ── Singleton config ──────────────────────────────────────────────────────────

DEFAULT_CONFIG = AnalysisConfig()
