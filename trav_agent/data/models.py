"""Datamodeller för trav — Häst, Lopp, Start, Omgång.

Alla modeller bygger på Pydantic för validering och serialisering.
Strukturen mappar mot ATG:s API-svar men normaliserar till ett konsekvent format.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────


class StartMethod(str, Enum):
    AUTO = "auto"
    VOLT = "volt"


class Breed(str, Enum):
    WARM = "varmblod"  # Standardbred
    COLD = "kallblod"  # Coldblood trotter
    THOROUGHBRED = "fullblod"  # Gallopp


class Sex(str, Enum):
    STALLION = "hingst"
    MARE = "sto"
    GELDING = "valack"


class RaceStatus(str, Enum):
    UPCOMING = "upcoming"
    STARTED = "started"
    FINISHED = "finished"
    CANCELLED = "cancelled"


# ── Historisk start ───────────────────────────────────────────────────────────


class PastStart(BaseModel):
    """En historisk start för en häst."""

    start_date: date
    track_name: str = ""
    track_id: Optional[int] = None
    distance: int = 0  # meter
    start_method: StartMethod = StartMethod.AUTO
    post_position: int = 0  # spår
    placement: Optional[int] = None  # None = disk/utgången
    disqualified: bool = False
    galloped: bool = False
    finish_time: Optional[float] = None  # sekunder totaltid
    km_time: Optional[float] = None  # tid per km i sekunder (t.ex. 73.5 = 1.13.5)
    prize_money: int = 0  # SEK vunna i loppet
    driver_name: str = ""
    trainer_name: str = ""
    num_starters: int = 0
    odds: Optional[float] = None  # vinnarodds
    race_purse: int = 0  # total prispott
    field_avg_prize: int = 0  # genomsnittlig karriärprispengar i fältet
    comment: str = ""  # loppkommentar

    @property
    def won(self) -> bool:
        return self.placement == 1

    @property
    def top3(self) -> bool:
        return self.placement is not None and self.placement <= 3

    @property
    def dnf(self) -> bool:
        """Did not finish — disk, galopp, utgången."""
        return self.placement is None or self.disqualified


class CareerStats(BaseModel):
    """Sammanfattad karriärstatistik."""

    total_starts: int = 0
    wins: int = 0
    seconds: int = 0
    thirds: int = 0
    total_prize_money: int = 0
    disqualifications: int = 0
    win_rate: float = 0.0  # wins / total_starts
    top3_rate: float = 0.0
    avg_km_time: Optional[float] = None
    prize_per_start: int = 0  # snitt kr/start (krStart från scrape)
    avg_odds: Optional[float] = None  # snittodds från scrape


# ── Häst ──────────────────────────────────────────────────────────────────────


class Horse(BaseModel):
    """Fullständig hästprofil med historik."""

    id: Optional[int] = None  # ATG horse ID
    name: str
    age: int = 0
    sex: Sex = Sex.GELDING
    breed: Breed = Breed.WARM
    sire: str = ""  # far
    dam: str = ""  # mor
    dam_sire: str = ""  # morfar
    trainer_name: str = ""
    trainer_id: Optional[int] = None
    owner_name: str = ""

    career: CareerStats = Field(default_factory=CareerStats)
    past_starts: list[PastStart] = Field(default_factory=list)

    def recent_starts(self, n: int = 10) -> list[PastStart]:
        """Senaste N starter, sorterade nyast först."""
        sorted_starts = sorted(self.past_starts, key=lambda s: s.start_date, reverse=True)
        return sorted_starts[:n]

    def starts_at_track(self, track_name: str) -> list[PastStart]:
        """Alla starter på en specifik bana."""
        return [s for s in self.past_starts if s.track_name.lower() == track_name.lower()]

    def starts_with_method(self, method: StartMethod) -> list[PastStart]:
        """Alla starter med en specifik startmetod."""
        return [s for s in self.past_starts if s.start_method == method]

    def starts_at_distance_range(self, min_dist: int, max_dist: int) -> list[PastStart]:
        """Starter inom ett distansintervall."""
        return [s for s in self.past_starts if min_dist <= s.distance <= max_dist]

    def recompute_career_from_starts(self) -> None:
        """Omberäkna karriärstatistik från past_starts.

        Används vid backtesting efter temporal filtrering — API:ts
        karriärdata inkluderar framtida resultat, men past_starts
        har filtrerats till bara starter före loppets datum.
        """
        starts = self.past_starts
        total = len(starts)
        if total == 0:
            self.career = CareerStats()
            return

        wins = sum(1 for s in starts if s.won)
        seconds = sum(1 for s in starts if s.placement == 2)
        thirds = sum(1 for s in starts if s.placement == 3)
        total_prize = sum(s.prize_money for s in starts)
        disqs = sum(1 for s in starts if s.disqualified)
        timed = [s.km_time for s in starts if s.km_time and s.km_time > 0]
        odds_list = [s.odds for s in starts if s.odds and s.odds > 0]

        self.career = CareerStats(
            total_starts=total,
            wins=wins,
            seconds=seconds,
            thirds=thirds,
            total_prize_money=total_prize,
            disqualifications=disqs,
            win_rate=wins / total,
            top3_rate=(wins + seconds + thirds) / total,
            avg_km_time=sum(timed) / len(timed) if timed else None,
            prize_per_start=total_prize // total if total > 0 else 0,
            avg_odds=sum(odds_list) / len(odds_list) if odds_list else None,
        )

    @property
    def gallop_rate(self) -> float:
        """Andel starter med galopp."""
        if not self.past_starts:
            return 0.0
        return sum(1 for s in self.past_starts if s.galloped) / len(self.past_starts)

    @property
    def recent_form_string(self) -> str:
        """Formsträngen t.ex. '1-3-2-0-5' (0 = disk/utgången)."""
        recent = self.recent_starts(5)
        placements = [str(s.placement) if s.placement else "0" for s in recent]
        return "-".join(placements)


# ── Starter i ett lopp (häst + lopp-specifik info) ────────────────────────────


class RaceEntry(BaseModel):
    """En häst i startlistan för ett specifikt lopp."""

    horse: Horse
    post_position: int = 0  # spårnummer
    distance: int = 0  # individuell distans (kan variera vid volt)
    driver_name: str = ""
    driver_id: Optional[int] = None
    trainer_name: str = ""
    weight: Optional[float] = None  # galopp
    shoes: str = ""  # skoinfo: "alla", "utan bak" etc.
    scratched: bool = False  # struken

    # Speldata
    odds: Optional[float] = None  # aktuella odds
    bet_percentage: Optional[float] = None  # streckprocent i V-spelet

    # Scrape-data
    trend: Optional[float] = None  # trendvärde t.ex. +1.81 eller -2.73
    points: Optional[int] = None  # ATG-poäng (heuristisk kvalitetsscore)

    # Analysresultat (fylls i av analysmotorn)
    factor_scores: dict[str, float] = Field(default_factory=dict)
    composite_score: float = 0.0  # Ren modellpoäng (7 faktorer)
    super_score: float = 0.0  # Blend av modell + marknad (streckprocent)
    rank: int = 0
    recommendation: str = ""  # "spik", "2-val", "3-val", "gardering", "strykning"


# ── Lopp ──────────────────────────────────────────────────────────────────────


class Race(BaseModel):
    """Ett enskilt lopp med alla deltagare."""

    id: str = ""  # ATG race ID
    race_number: int = 0  # avdelningsnummer (1-8)
    track_name: str = ""
    track_id: Optional[int] = None
    race_date: date = Field(default_factory=date.today)
    post_time: Optional[datetime] = None  # starttid

    distance: int = 0  # huvuddistans i meter
    start_method: StartMethod = StartMethod.AUTO
    purse: int = 0  # prispott
    race_type: str = ""  # "lopp", "stayerlopp", "stolopp" etc.
    race_name: str = ""  # loppnamn från ATG (t.ex. "Solar Sverige - STL Klass I - Spårtrappa")
    conditions: str = ""  # loppvillkor/text
    breed: Breed = Breed.WARM
    is_stair_draw: bool = False  # spårtrappa: spår 1=svagast, spår 12=starkast

    status: RaceStatus = RaceStatus.UPCOMING
    entries: list[RaceEntry] = Field(default_factory=list)

    # Resultat (fylls i efter avklarat lopp)
    result_order: list[int] = Field(default_factory=list)  # hästnummer i resultatordning
    win_odds: Optional[float] = None

    # Skrällrisk (fylls i av analysmotorn)
    upset_risk: float = 0.0  # 0-100, hur stor risk för skräll
    upset_candidates: list[int] = Field(default_factory=list)  # hästnummer med skrällprofil

    @property
    def active_entries(self) -> list[RaceEntry]:
        """Startande (ej strukna) hästar."""
        return [e for e in self.entries if not e.scratched]

    @property
    def num_starters(self) -> int:
        return len(self.active_entries)

    @property
    def is_mares_only(self) -> bool:
        """Bara ston?"""
        return all(e.horse.sex == Sex.MARE for e in self.active_entries)


# ── Spelomgång ────────────────────────────────────────────────────────────────


class GameRound(BaseModel):
    """En hel spelomgång (t.ex. V85 med 8 lopp)."""

    game_id: str = ""  # t.ex. "V85_2026-02-21_42_8"
    game_type: str = ""  # "V85", "V75" etc.
    round_date: date = Field(default_factory=date.today)
    track_name: str = ""
    track_id: Optional[int] = None
    status: str = ""

    races: list[Race] = Field(default_factory=list)

    # Speldata
    turnover: Optional[int] = None
    jackpot: Optional[int] = None

    # Utdelning per tier (t.ex. {7: 130507.0, 6: 214.0, 5: 17.0} kr)
    dividends: dict[int, float] = Field(default_factory=dict)
    # Antal vinnande system per tier
    dividend_systems: dict[int, int] = Field(default_factory=dict)

    def get_race(self, race_number: int) -> Optional[Race]:
        """Hämta ett lopp via avdelningsnummer."""
        for race in self.races:
            if race.race_number == race_number:
                return race
        return None

    @property
    def num_races(self) -> int:
        return len(self.races)

    @property
    def is_finished(self) -> bool:
        return all(r.status == RaceStatus.FINISHED for r in self.races)
