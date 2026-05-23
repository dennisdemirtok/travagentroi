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
    """Vikter för varje analysfaktor. Summerar till 1.0.

    Version 5: Optimerade utan data-leakage.
    Beräknade med neutraliserade closing odds och temporalt filtrerade
    karriärstatistik (recompute_career_from_starts). 1902 lopp
    (2024-01 till 2026-04), alla speltyper.

    Resultat vs v4: top2 +1.5%, top3 +2.4%

    Viktprinciper (validerade av optimizer):
    - Tid (km_time) = starkaste enskilda signal (0.32)
    - Kategori = distans/metod-matchning, oväntat stark (0.25)
    - Prize/klass & startspår = jämna bidrag (0.15 var)
    - Form = överraskande låg utan marknadssignal (0.03)
    - Kusk/bana = svaga utan extern data (0.05 var)

    Market weight = 0: Modellen förlitar sig helt på egna faktorer.
    Closing odds (streckprocent) ger ~30% prediktiv kraft men är
    inte tillgänglig i tillräcklig kvalitet pre-race för att vara
    pålitlig i live-drift.

    Interaktionstermer BORTTAGNA — de förstärkte brus.
    """

    time_analysis: float = 0.32
    form_curve: float = 0.03
    prize_index: float = 0.15
    category_profile: float = 0.25
    post_position: float = 0.15
    driver_trainer: float = 0.05
    track_profile: float = 0.05

    # Interaktionstermer borttagna (förstärkte brus)
    interaction_track_post: float = 0.0
    interaction_track_category: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Bas-vikter (utan interaktioner)."""
        return {
            "time_analysis": self.time_analysis,
            "prize_index": self.prize_index,
            "form_curve": self.form_curve,
            "track_profile": self.track_profile,
            "category_profile": self.category_profile,
            "driver_trainer": self.driver_trainer,
            "post_position": self.post_position,
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

    # Hur många senaste starter att analysera
    recent_starts_count: int = 10

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
