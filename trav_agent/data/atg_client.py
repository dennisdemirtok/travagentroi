"""ATG API-klient — hämtar data från ATG:s racinginfo API.

Endpoints:
  - calendar/day/{date}     → alla lopp & spel en dag
  - products/{product}      → nästkommande omgång för en spelform
  - games/{game_id}         → komplett speldata med startlistor
  - races/{race_id}         → enskilt lopp med detaljerad info

Alla svar är JSON. Inget auth krävs men vi håller låg trafik (1 req/s).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Optional

import httpx

from ..config import ATG_BASE_URL, ATG_ZETA_URL, REQUEST_DELAY, REQUEST_TIMEOUT
from .cache import FileCache
from .models import (
    Breed,
    CareerStats,
    GameRound,
    Horse,
    PastStart,
    Race,
    RaceEntry,
    RaceStatus,
    Sex,
    StartMethod,
)

logger = logging.getLogger(__name__)


class ATGClient:
    """Asynkron klient för ATG:s racinginfo API."""

    def __init__(self, cache: Optional[FileCache] = None):
        self.base_url = ATG_BASE_URL
        self.zeta_url = ATG_ZETA_URL
        self.delay = REQUEST_DELAY
        self.cache = cache or FileCache()
        self._last_request_time = 0.0

    async def _throttle(self):
        """Håll minst REQUEST_DELAY sekunder mellan anrop."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self.delay:
            await asyncio.sleep(self.delay - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, url: str, *, skip_cache: bool = False) -> dict[str, Any]:
        """HTTP GET med throttle, cache och felhantering."""
        # Kolla cache först (om inte skip_cache)
        if not skip_cache:
            cached = self.cache.get(url)
            if cached is not None:
                logger.debug(f"Cache hit: {url}")
                return cached

        await self._throttle()
        logger.info(f"Fetching: {url}")

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()

        # Spara i cache
        self.cache.set(url, data)
        return data

    # ── Kalender ──────────────────────────────────────────────────────────────

    async def get_calendar(self, day: date, *, skip_cache: bool = False) -> dict[str, Any]:
        """Hämta alla lopp och spel för en dag.

        Args:
            day: Datum
            skip_cache: Hoppa över cache (för aktuella/framtida datum)

        Returns: dict med nycklar som "tracks", "games" etc.
        """
        url = f"{self.base_url}/calendar/day/{day.isoformat()}"
        return await self._get(url, skip_cache=skip_cache)

    async def get_product_info(self, product: str) -> dict[str, Any]:
        """Snabbinfo om nästkommande omgång för en spelform.

        Args:
            product: T.ex. "V75", "V85", "V86"
        """
        url = f"{self.base_url}/products/{product.upper()}"
        return await self._get(url)

    # ── Spel & Lopp ──────────────────────────────────────────────────────────

    async def get_game(self, game_id: str) -> dict[str, Any]:
        """Hämta komplett speldata inklusive startlistor.

        Args:
            game_id: T.ex. "V85_2026-02-21_42_8"
        """
        url = f"{self.base_url}/games/{game_id}"
        return await self._get(url)

    async def get_race(self, race_id: str) -> dict[str, Any]:
        """Hämta detaljerad info om ett enskilt lopp.

        Args:
            race_id: T.ex. race-id från game-svaret
        """
        url = f"{self.base_url}/races/{race_id}"
        return await self._get(url)

    # ── Hästhistorik ─────────────────────────────────────────────────────────

    async def get_horse_results(self, horse_id: int) -> list[dict]:
        """Hämta fullständig starthistorik för en häst.

        Returnerar alla registrerade starter med km-tid, placering,
        distans, startmetod, odds etc.
        """
        url = f"{self.base_url}/horses/{horse_id}/results"
        try:
            data = await self._get(url, skip_cache=True)
            return data.get("records", [])
        except Exception as e:
            logger.warning(f"Kunde inte hämta historik för häst {horse_id}: {e}")
            return []

    def _parse_horse_result(self, data: dict) -> PastStart:
        """Parsa ett record från /horses/{id}/results till PastStart."""
        # Datum
        race_date = date.today()
        date_str = data.get("date", "")
        if date_str:
            try:
                race_date = date.fromisoformat(date_str[:10])
            except (ValueError, TypeError):
                pass

        # Km-tid
        km_time_val = None
        km_time = data.get("kmTime", {})
        if isinstance(km_time, dict):
            minutes = km_time.get("minutes", 0)
            seconds = km_time.get("seconds", 0)
            tenths = km_time.get("tenths", 0)
            if minutes or seconds:
                km_time_val = minutes * 60 + seconds + tenths / 10

        # Placering
        placement = None
        place_str = data.get("place", "")
        if place_str:
            try:
                placement = int(place_str)
            except (ValueError, TypeError):
                placement = None

        # Odds (ATG returnerar i hundradelar: 163 = 1.63)
        odds_raw = data.get("odds")
        odds_val = None
        if odds_raw and isinstance(odds_raw, (int, float)):
            odds_val = odds_raw / 100.0

        # Race-info
        race_info = data.get("race", {})
        start_method_str = race_info.get("startMethod", "auto")
        start_method = StartMethod.VOLT if "volt" in start_method_str.lower() else StartMethod.AUTO
        first_prize = race_info.get("firstPrize", 0)
        # ATG ger firstPrize i ören (3500000 = 35000 kr)
        race_purse = first_prize // 100 if first_prize > 100000 else first_prize

        # Track
        track_info = data.get("track", {})
        track_name = track_info.get("name", "") if isinstance(track_info, dict) else str(track_info)

        # Start-info
        start_info = data.get("start", {})
        distance = start_info.get("distance", 0)
        post_position = start_info.get("postPosition", 0)

        # Kusk
        driver_info = start_info.get("driver", {})
        driver_name = ""
        if isinstance(driver_info, dict):
            fn = driver_info.get("firstName", "")
            ln = driver_info.get("lastName", "")
            driver_name = f"{fn} {ln}".strip()

        # Disk-status
        is_disqualified = data.get("disqualified", False) or place_str in ("d", "dq", "dis")
        is_galloped = bool(data.get("galloped", False))

        return PastStart(
            start_date=race_date,
            track_name=track_name,
            distance=distance,
            start_method=start_method,
            post_position=post_position,
            placement=placement,
            disqualified=is_disqualified,
            galloped=is_galloped,
            km_time=km_time_val,
            prize_money=0,
            race_purse=race_purse,
            driver_name=driver_name,
            odds=odds_val,
        )

    def _merge_missing_starts(self, horse: Horse, records: list[dict]) -> None:
        """Fyll i saknade starter från /horses/{id}/results i hästens past_starts.

        Speldata (pastPerformances) kan vara veckor efter — results-endpointen
        har alltid färsk data. Vi behåller befintliga past_starts (rikare data)
        och prepend:ar bara starter som saknas (baserat på datum + bana).
        """
        if not records:
            return

        # Bygg lookup-set av befintliga starter: (datum, bana) som nyckel
        existing_keys: set[tuple[date, str]] = set()
        for ps in horse.past_starts:
            existing_keys.add((ps.start_date, ps.track_name.lower().strip()))

        # Hitta starter i results som inte finns i befintliga past_starts
        missing: list[PastStart] = []
        for record in records:
            parsed = self._parse_horse_result(record)
            key = (parsed.start_date, parsed.track_name.lower().strip())
            if key not in existing_keys:
                missing.append(parsed)

        if missing:
            # Sortera saknade starter med nyast först (samma ordning som past_starts)
            missing.sort(key=lambda ps: ps.start_date, reverse=True)
            # Prepend saknade starter framför befintliga
            horse.past_starts = missing + horse.past_starts
            logger.debug(
                f"Häst {horse.name} ({horse.id}): "
                f"lade till {len(missing)} saknade starter från results-endpoint"
            )

    # ── Zeta (analyser) ──────────────────────────────────────────────────────

    async def get_zeta_analysis(self, track_id: int, day: date) -> dict[str, Any]:
        """Hämta ATG:s egna loppanalyser (zeta).

        OBS: ATG är känsliga för snabba anrop mot zeta. Använd 3s delay.
        """
        url = f"{self.zeta_url}/{track_id}/{day.isoformat()}"
        # Extra fördröjning för zeta
        await asyncio.sleep(2.0)
        return await self._get(url)

    # ── Hitta game_id ─────────────────────────────────────────────────────────

    async def find_game_id(self, game_type: str, day: date) -> Optional[str]:
        """Hitta game_id för en specifik spelform och dag.

        Args:
            game_type: T.ex. "V85"
            day: Datum

        Returns:
            game_id eller None om inget hittas
        """
        # Skippa cache för dagens datum och framåt — ATG publicerar
        # startlistor löpande, så cachad kalenderdata kan vara inaktuell.
        skip = day >= date.today()
        calendar = await self.get_calendar(day, skip_cache=skip)
        games = calendar.get("games", {})
        game_list = games.get(game_type.upper(), [])

        if not game_list:
            # Testa products-endpointen som fallback
            logger.warning(f"Ingen {game_type} hittad i kalendern för {day}")
            return None

        return game_list[0].get("id")

    # ── Fullständig omgångshämtning ──────────────────────────────────────────

    async def fetch_full_round(
        self,
        game_type: str,
        day: date,
        *,
        skip_horse_history: bool = False,
    ) -> Optional[GameRound]:
        """Hämta en komplett spelomgång med alla lopp och hästar.

        Detta är huvudmetoden — den hämtar allt som behövs för analys.

        Args:
            skip_horse_history: Hoppa över /horses/{id}/results-anropet
                (som alltid går mot nätet). Använd för offline-regenerering
                från cache, t.ex. backlog-bygge, för att inte spamma ATG.
        """
        # 1. Hitta game_id
        game_id = await self.find_game_id(game_type, day)
        if not game_id:
            logger.error(f"Kunde inte hitta {game_type} för {day}")
            return None

        # 2. Hämta speldata
        game_data = await self.get_game(game_id)

        # 3. Bygg GameRound
        game_round = self._parse_game_round(game_data, game_type, day)

        # 4. Hämta detaljerad info per lopp (inkl. hästhistorik)
        for i, race_data in enumerate(game_data.get("races", [])):
            race_id = race_data.get("id", "")
            if race_id:
                detailed = await self.get_race(race_id)
                self._enrich_race(game_round.races[i], detailed)

        # 5. Hämta färsk starthistorik per häst via /horses/{id}/results
        #    och fyll i saknade starter som speldata kan missa (ofta veckor efter).
        #    Hoppas över i offline-läge (cachat speldata har redan past_starts).
        if not skip_horse_history:
            horse_ids_fetched: set[int] = set()
            for race in game_round.races:
                for entry in race.entries:
                    horse_id = entry.horse.id
                    if horse_id and horse_id not in horse_ids_fetched:
                        horse_ids_fetched.add(horse_id)
                        records = await self.get_horse_results(horse_id)
                        if records:
                            self._merge_missing_starts(entry.horse, records)

        logger.info(
            f"Hämtade {game_round.game_type} {game_round.round_date}: "
            f"{game_round.num_races} lopp, "
            f"{sum(r.num_starters for r in game_round.races)} hästar"
        )
        return game_round

    # ── Historisk datahämtning ────────────────────────────────────────────────

    async def fetch_historical_rounds(
        self, game_type: str, start_date: date, end_date: date
    ) -> list[GameRound]:
        """Hämta historiska omgångar inom ett datumintervall.

        Wrapper runt fetch_historical_rounds_iter för bakåtkompatibilitet.
        """
        rounds = []
        async for _day, game_round in self.fetch_historical_rounds_iter(
            game_type, start_date, end_date
        ):
            if game_round:
                rounds.append(game_round)
        logger.info(f"Totalt {len(rounds)} omgångar hämtade")
        return rounds

    async def fetch_historical_rounds_iter(
        self, game_type: str, start_date: date, end_date: date
    ):
        """Yielda (datum, GameRound|None) för varje omgång i intervallet.

        Optimerad: hoppar 6 dagar framåt efter en kalender-miss
        (t.ex. V85 körs typiskt 1x/vecka).
        """
        from datetime import timedelta

        current = start_date
        consecutive_misses = 0

        while current <= end_date:
            try:
                game_round = await self.fetch_full_round(game_type, current)
                if game_round:
                    yield current, game_round
                    consecutive_misses = 0
                    # Hoppa 6 dagar — nästa omgång är troligen om en vecka
                    current += timedelta(days=6)
                else:
                    yield current, None
                    consecutive_misses += 1
                    # Hoppa snabbare efter flera missar i rad
                    if consecutive_misses >= 3:
                        current += timedelta(days=5)
                    else:
                        current += timedelta(days=1)
            except Exception as e:
                logger.warning(f"Kunde inte hämta {game_type} {current}: {e}")
                yield current, None
                consecutive_misses += 1
                current += timedelta(days=1)

            current += timedelta(days=1)

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_game_round(
        self, data: dict, game_type: str, day: date
    ) -> GameRound:
        """Parsa API-svar till GameRound."""
        races = []
        for i, race_data in enumerate(data.get("races", []), start=1):
            race = self._parse_race(race_data, i, day)
            races.append(race)

        # Parsa utdelning per tier från pools.{game_type}.result.payouts
        dividends: dict[int, float] = {}
        dividend_systems: dict[int, int] = {}
        pools = data.get("pools", {})
        pool_data = pools.get(game_type, {})
        result_data = pool_data.get("result", {})
        payouts_data = result_data.get("payouts", {})

        for tier_str, payout_info in payouts_data.items():
            try:
                tier_n = int(tier_str)
                if isinstance(payout_info, dict):
                    if "payout" in payout_info:
                        # Belopp i öre → kronor
                        dividends[tier_n] = payout_info["payout"] / 100.0
                    dividend_systems[tier_n] = payout_info.get("systems", 0)
            except (ValueError, TypeError):
                pass

        return GameRound(
            game_id=data.get("id", ""),
            game_type=game_type,
            round_date=day,
            track_name=data.get("tracks", [{}])[0].get("name", "") if data.get("tracks") else "",
            track_id=data.get("tracks", [{}])[0].get("id") if data.get("tracks") else None,
            status=data.get("status", ""),
            races=races,
            turnover=data.get("turnover"),
            jackpot=data.get("jackpot"),
            dividends=dividends,
            dividend_systems=dividend_systems,
        )

    def _parse_race(self, data: dict, race_number: int, day: date) -> Race:
        """Parsa ett lopp från API-svar."""
        entries = []
        for starter in data.get("starts", []):
            entry = self._parse_entry(starter)
            entries.append(entry)

        start_method = StartMethod.AUTO
        if "volt" in data.get("startMethod", "").lower():
            start_method = StartMethod.VOLT

        breed = Breed.WARM
        breed_str = data.get("breed", "").lower()
        if "kall" in breed_str:
            breed = Breed.COLD
        elif "full" in breed_str or "galopp" in breed_str:
            breed = Breed.THOROUGHBRED

        return Race(
            id=data.get("id", ""),
            race_number=race_number,
            track_name=data.get("track", {}).get("name", ""),
            track_id=data.get("track", {}).get("id"),
            race_date=day,
            distance=data.get("distance", 0),
            start_method=start_method,
            purse=data.get("purse", {}).get("amount", 0) if isinstance(data.get("purse"), dict) else data.get("purse", 0),
            race_type=data.get("raceType", ""),
            race_name=data.get("name", ""),
            conditions=data.get("conditions", ""),
            breed=breed,
            status=self._map_status(data.get("status", "")),
            entries=entries,
        )

    def _parse_entry(self, data: dict) -> RaceEntry:
        """Parsa en starter/häst i ett lopp."""
        horse_data = data.get("horse", {})

        # Kön
        sex = Sex.GELDING
        sex_str = horse_data.get("sex", "").lower()
        if sex_str in ("mare", "sto"):
            sex = Sex.MARE
        elif sex_str in ("stallion", "hingst"):
            sex = Sex.STALLION

        # Karriärstatistik
        stats = horse_data.get("statistics", {})
        life_stats = stats.get("life", {})
        placement = life_stats.get("placement", {})
        career = CareerStats(
            total_starts=life_stats.get("starts", 0),
            wins=placement.get("1", 0),
            seconds=placement.get("2", 0),
            thirds=placement.get("3", 0),
            total_prize_money=life_stats.get("earnings", 0) // 100,  # öre → kr
        )
        if career.total_starts > 0:
            career.win_rate = career.wins / career.total_starts
            career.top3_rate = (career.wins + career.seconds + career.thirds) / career.total_starts

        # Snittodds från senaste 5 starter
        last_five = stats.get("lastFiveStarts", {})
        avg_odds_raw = last_five.get("averageOdds")
        if avg_odds_raw and isinstance(avg_odds_raw, (int, float)):
            career.avg_odds = avg_odds_raw / 100.0

        # Senaste starter
        past_starts = []
        for ps in horse_data.get("pastPerformances", []):
            past_start = self._parse_past_start(ps)
            past_starts.append(past_start)

        # Record (best time) from horse.record
        record_time = None
        record_start_method = ""
        record_data = horse_data.get("record", {})
        if isinstance(record_data, dict) and "time" in record_data:
            rt = record_data["time"]
            if isinstance(rt, dict) and "minutes" in rt:
                minutes = rt.get("minutes", 0) or 0
                seconds = rt.get("seconds", 0) or 0
                tenths = rt.get("tenths", 0) or 0
                if minutes > 0 or seconds > 0:
                    record_time = minutes * 60 + seconds + tenths / 10.0
            record_start_method = record_data.get("startMethod", "")

        horse = Horse(
            id=horse_data.get("id"),
            name=horse_data.get("name", "Okänd"),
            age=horse_data.get("age", 0),
            sex=sex,
            breed=Breed.WARM,  # Uppdateras från lopp-nivå
            sire=horse_data.get("pedigree", {}).get("father", {}).get("name", ""),
            dam=horse_data.get("pedigree", {}).get("mother", {}).get("name", ""),
            dam_sire=horse_data.get("pedigree", {}).get("mother", {}).get("father", {}).get("name", ""),
            trainer_name=self._parse_person_name(horse_data.get("trainer", {})),
            trainer_id=horse_data.get("trainer", {}).get("id") if isinstance(horse_data.get("trainer"), dict) else None,
            trainer_location=horse_data.get("trainer", {}).get("location", "") if isinstance(horse_data.get("trainer"), dict) else "",
            owner_name=horse_data.get("owner", {}).get("name", ""),
            record_time=record_time,
            record_start_method=record_start_method,
            career=career,
            api_career_money=career.total_prize_money,  # Preserved for Dennis signals
            past_starts=past_starts,
        )

        # Odds & streckprocent från pools
        pools = data.get("pools", {})
        odds_val = None
        bet_pct = None

        # Vinnarodds (hundradelar: 374 = 3.74)
        vinnare_pool = pools.get("vinnare", {})
        if isinstance(vinnare_pool, dict) and vinnare_pool.get("odds"):
            odds_val = vinnare_pool["odds"] / 100.0

        # Streckprocent från spelforms-pool (V75, V85, V86, GS75 etc.)
        for pool_name, pool_data in pools.items():
            if pool_name.startswith(("V", "GS")) and isinstance(pool_data, dict):
                bd = pool_data.get("betDistribution")
                if bd is not None:
                    bet_pct = bd / 10000.0  # 1120 → 0.1120 (11.20%)
                    break

        # Driver statistics — parse win percentage, placement, earnings from most recent year
        driver_win_pct = 0.0
        driver_starts_year = 0
        driver_top3_pct = 0.0
        driver_earnings = 0
        driver_home_track = ""
        driver_data = data.get("driver", {})
        if isinstance(driver_data, dict):
            # Home track
            home_track_data = driver_data.get("homeTrack", {})
            if isinstance(home_track_data, dict):
                driver_home_track = home_track_data.get("name", "")

            driver_stats = driver_data.get("statistics", {})
            years_data = driver_stats.get("years", {})
            if years_data:
                # Pick the most recent year
                latest_year = max(years_data.keys())
                year_stats = years_data[latest_year]
                if isinstance(year_stats, dict):
                    driver_starts_year = year_stats.get("starts", 0)
                    driver_earnings = year_stats.get("earnings", 0)
                    # winPercentage is per mille (1540 = 15.40% = 0.154)
                    win_pct_raw = year_stats.get("winPercentage", 0)
                    if win_pct_raw:
                        driver_win_pct = win_pct_raw / 10000.0
                    # Top-3 placement rate from placement data
                    placement = year_stats.get("placement", {})
                    if isinstance(placement, dict) and driver_starts_year > 0:
                        p1 = placement.get("1", 0) or 0
                        p2 = placement.get("2", 0) or 0
                        p3 = placement.get("3", 0) or 0
                        driver_top3_pct = (p1 + p2 + p3) / driver_starts_year

        # Shoes — parse structured shoe data
        shoe_front_off = False
        shoe_back_off = False
        shoe_changed = False
        shoes_data = data.get("shoes")
        shoes_str = ""
        if isinstance(shoes_data, dict):
            front = shoes_data.get("front", {})
            back = shoes_data.get("back", {})
            if isinstance(front, dict):
                shoe_front_off = not front.get("hasShoe", True)
                if front.get("changed", False):
                    shoe_changed = True
            if isinstance(back, dict):
                shoe_back_off = not back.get("hasShoe", True)
                if back.get("changed", False):
                    shoe_changed = True
            # Build a human-readable string for backward compat
            parts = []
            if shoe_front_off and shoe_back_off:
                parts.append("utan alla")
            elif shoe_front_off:
                parts.append("utan fram")
            elif shoe_back_off:
                parts.append("utan bak")
            else:
                parts.append("alla")
            shoes_str = ", ".join(parts)
        elif isinstance(shoes_data, str):
            shoes_str = shoes_data
        else:
            shoes_str = str(shoes_data) if shoes_data else ""

        # Sulky — check if sulky type was changed since last start
        sulky_changed = False
        sulky_data = data.get("sulky", {})
        if isinstance(sulky_data, dict):
            sulky_type = sulky_data.get("type", {})
            if isinstance(sulky_type, dict):
                sulky_changed = sulky_type.get("changed", False)

        return RaceEntry(
            horse=horse,
            post_position=data.get("number", 0),
            distance=data.get("distance", 0),
            driver_name=self._parse_person_name(driver_data) if isinstance(driver_data, dict) else "",
            driver_id=driver_data.get("id") if isinstance(driver_data, dict) else None,
            driver_location=driver_data.get("location", "") if isinstance(driver_data, dict) else "",
            shoes=shoes_str,
            scratched=data.get("scratched", False),
            odds=odds_val,
            bet_percentage=bet_pct,
            driver_win_pct=driver_win_pct,
            driver_starts_year=driver_starts_year,
            driver_top3_pct=driver_top3_pct,
            driver_earnings=driver_earnings,
            driver_home_track=driver_home_track,
            shoe_front_off=shoe_front_off,
            shoe_back_off=shoe_back_off,
            shoe_changed=shoe_changed,
            sulky_changed=sulky_changed,
        )

    @staticmethod
    def _parse_person_name(data: dict) -> str:
        """Bygg fullständigt namn från ATG:s firstName+lastName-format."""
        if not isinstance(data, dict):
            return ""
        first = data.get("firstName", "")
        last = data.get("lastName", "")
        if first and last:
            return f"{first} {last}"
        return first or last or data.get("name", "") or data.get("shortName", "")

    def _parse_past_start(self, data: dict) -> PastStart:
        """Parsa en historisk start."""
        placement = data.get("place")
        if isinstance(placement, str):
            try:
                placement = int(placement)
            except (ValueError, TypeError):
                placement = None

        km_time = data.get("kmTime", {})
        km_time_val = None
        if isinstance(km_time, dict):
            minutes = km_time.get("minutes", 0)
            seconds = km_time.get("seconds", 0)
            tenths = km_time.get("tenths", 0)
            if minutes or seconds:
                km_time_val = minutes * 60 + seconds + tenths / 10
        elif isinstance(km_time, (int, float)):
            km_time_val = float(km_time)

        race_date = date.today()
        date_str = data.get("date", "")
        if date_str:
            try:
                race_date = date.fromisoformat(date_str[:10])
            except (ValueError, TypeError):
                pass

        return PastStart(
            start_date=race_date,
            track_name=data.get("track", {}).get("name", "") if isinstance(data.get("track"), dict) else str(data.get("track", "")),
            distance=data.get("distance", 0),
            start_method=StartMethod.VOLT if "volt" in str(data.get("startMethod", "")).lower() else StartMethod.AUTO,
            post_position=data.get("number", 0),
            placement=placement,
            disqualified=bool(data.get("disqualified", False)),
            galloped=bool(data.get("galloped", False)),
            km_time=km_time_val,
            prize_money=data.get("prizeMoney", 0),
            driver_name=self._parse_person_name(data.get("driver", {})) if isinstance(data.get("driver"), dict) else "",
            num_starters=data.get("starters", 0),
            odds=data.get("odds"),
            comment=data.get("comment", ""),
        )

    def _enrich_race(self, race: Race, detailed_data: dict) -> None:
        """Berika ett lopp med data från det detaljerade race-API:et.

        Detta ger oss mer information om varje häst, inklusive
        fler historiska starter och detaljerade villkor.
        """
        # Uppdatera loppvillkor om vi har mer info
        if "conditions" in detailed_data:
            race.conditions = detailed_data.get("conditions", race.conditions)

        # Uppdatera resultat om loppet är avklarat
        # Resultat finns i starts[].result.place, inte i top-level result
        result_info = detailed_data.get("result", {})
        status_str = detailed_data.get("status", "")
        placements: list[tuple[int, int, float | None]] = []  # (place, number, odds)
        for start_data in detailed_data.get("starts", []):
            sr = start_data.get("result")
            if sr and isinstance(sr, dict) and sr.get("place"):
                placements.append((
                    sr["place"],
                    start_data.get("number", 0),
                    sr.get("finalOdds"),
                ))
        if placements:
            placements.sort(key=lambda x: x[0])
            race.result_order = [num for _, num, _ in placements]
            race.status = RaceStatus.FINISHED
            if placements:
                race.win_odds = placements[0][2]
        elif isinstance(result_info, dict) and result_info.get("victoryMargin"):
            # Race has result info but we couldn't parse placements
            race.status = RaceStatus.FINISHED

        # Spara finalOdds på varje entry (om vi inte redan har pool-odds)
        for start_data in detailed_data.get("starts", []):
            sr = start_data.get("result")
            if sr and isinstance(sr, dict):
                final_odds = sr.get("finalOdds")
                if final_odds is not None:
                    number = start_data.get("number", 0)
                    for entry in race.entries:
                        if entry.post_position == number:
                            # Pool-odds (från _parse_entry) har prioritet
                            if entry.odds is None:
                                entry.odds = final_odds
                            break

            # Parsa pool-data från enriched starts (kan ha uppdaterade odds)
            pools = start_data.get("pools", {})
            if pools:
                number = start_data.get("number", 0)
                for entry in race.entries:
                    if entry.post_position == number:
                        # Vinnarodds
                        vinnare = pools.get("vinnare", {})
                        if isinstance(vinnare, dict) and vinnare.get("odds"):
                            entry.odds = vinnare["odds"] / 100.0

                        # Streckprocent
                        for pn, pd in pools.items():
                            if pn.startswith(("V", "GS")) and isinstance(pd, dict):
                                bd = pd.get("betDistribution")
                                if bd is not None:
                                    entry.bet_percentage = bd / 10000.0
                                break
                        break

        # Berika hästar med mer historikdata
        for start_data in detailed_data.get("starts", []):
            number = start_data.get("number", 0)
            for entry in race.entries:
                if entry.post_position == number:
                    horse_detail = start_data.get("horse", {})
                    extra_pp = horse_detail.get("pastPerformances", [])
                    if extra_pp and len(extra_pp) > len(entry.horse.past_starts):
                        entry.horse.past_starts = [
                            self._parse_past_start(ps) for ps in extra_pp
                        ]
                    break

    async def refresh_odds(self, game_round: GameRound) -> int:
        """Uppdatera odds och streckprocent från ATG utan att hämta om allt.

        Hämtar bara game-data (skip cache) och uppdaterar pool-odds/streck
        på befintliga entries. Returnerar antal uppdaterade hästar.
        """
        game_url = f"{self.base_url}/games/{game_round.game_id}"
        try:
            data = await self._get(game_url, skip_cache=True)
        except Exception as e:
            logger.warning(f"Kunde inte uppdatera odds: {e}")
            return 0

        updated = 0
        races_data = data.get("races", [])

        for race_data in races_data:
            race_id = race_data.get("id", "")
            race = None
            for r in game_round.races:
                if r.id == race_id:
                    race = r
                    break
            if not race:
                continue

            for start_data in race_data.get("starts", []):
                number = start_data.get("number", 0)
                entry = None
                for e in race.entries:
                    if e.post_position == number:
                        entry = e
                        break
                if not entry:
                    continue

                pools = start_data.get("pools", {})
                if not pools:
                    continue

                # Vinnarodds
                vinnare = pools.get("vinnare", {})
                if isinstance(vinnare, dict) and vinnare.get("odds"):
                    entry.odds = vinnare["odds"] / 100.0

                # Streckprocent
                for pn, pd in pools.items():
                    if pn.startswith(("V", "GS")) and isinstance(pd, dict):
                        bd = pd.get("betDistribution")
                        if bd is not None:
                            entry.bet_percentage = bd / 10000.0
                        break

                updated += 1

        logger.info(f"Odds uppdaterade: {updated} hästar")
        return updated

    async def refresh_results(self, game_round: GameRound) -> tuple[int, int, bool]:
        """Uppdatera odds, loppresultat och utdelningar för en live-omgång.

        Utökar refresh_odds() med:
        - Hämta resultat för avslutade lopp (result_order)
        - Parsa utdelningar från pools

        Returns:
            (antal_odds_uppdaterade, antal_lopp_avslutade, har_utdelningar)
        """
        game_url = f"{self.base_url}/games/{game_round.game_id}"
        try:
            data = await self._get(game_url, skip_cache=True)
        except Exception as e:
            logger.warning(f"Kunde inte uppdatera resultat: {e}")
            return (0, 0, False)

        updated = 0
        races_finished = 0
        races_data = data.get("races", [])

        for race_data in races_data:
            race_id = race_data.get("id", "")
            race = None
            for r in game_round.races:
                if r.id == race_id:
                    race = r
                    break
            if not race:
                continue

            # Uppdatera odds/streck (samma som refresh_odds)
            for start_data in race_data.get("starts", []):
                number = start_data.get("number", 0)
                entry = None
                for e in race.entries:
                    if e.post_position == number:
                        entry = e
                        break
                if not entry:
                    continue

                pools = start_data.get("pools", {})
                if not pools:
                    continue

                vinnare = pools.get("vinnare", {})
                if isinstance(vinnare, dict) and vinnare.get("odds"):
                    entry.odds = vinnare["odds"] / 100.0

                for pn, pd in pools.items():
                    if pn.startswith(("V", "GS")) and isinstance(pd, dict):
                        bd = pd.get("betDistribution")
                        if bd is not None:
                            entry.bet_percentage = bd / 10000.0
                        break

                updated += 1

            # Kolla om loppet har avslutats men saknar resultat
            api_status = race_data.get("status", "")
            mapped_status = self._map_status(api_status)
            if mapped_status == RaceStatus.FINISHED and not race.result_order:
                # Hämta detaljerad data med resultat (skip cache!)
                try:
                    race_url = f"{self.base_url}/races/{race_id}"
                    detailed = await self._get(race_url, skip_cache=True)
                    self._enrich_race(race, detailed)
                    races_finished += 1
                    logger.info(
                        f"Lopp {race.race_number} avslutat — "
                        f"vinnare: {race.result_order[0] if race.result_order else '?'}"
                    )
                except Exception as e:
                    logger.warning(f"Kunde inte hämta resultat för lopp {race.race_number}: {e}")

        # Parsa utdelningar från pools
        has_dividends = False
        gt = game_round.game_type
        pools_data = data.get("pools", {})
        pool_data = pools_data.get(gt, {})
        result_data = pool_data.get("result", {})
        payouts_data = result_data.get("payouts", {})

        if payouts_data:
            for tier_str, payout_info in payouts_data.items():
                try:
                    tier_n = int(tier_str)
                    if isinstance(payout_info, dict):
                        if "payout" in payout_info:
                            game_round.dividends[tier_n] = payout_info["payout"] / 100.0
                            has_dividends = True
                        game_round.dividend_systems[tier_n] = payout_info.get("systems", 0)
                except (ValueError, TypeError):
                    pass

        logger.info(
            f"Resultat uppdaterade: {updated} hästar, "
            f"{races_finished} nya lopp avslutade, "
            f"utdelningar: {'ja' if has_dividends else 'nej'}"
        )
        return (updated, races_finished, has_dividends)

    @staticmethod
    def _map_status(status_str: str) -> RaceStatus:
        s = status_str.lower()
        if s in ("upcoming", "scheduled", ""):
            return RaceStatus.UPCOMING
        elif s in ("started", "running"):
            return RaceStatus.STARTED
        elif s in ("finished", "results"):
            return RaceStatus.FINISHED
        elif s in ("cancelled",):
            return RaceStatus.CANCELLED
        return RaceStatus.UPCOMING
