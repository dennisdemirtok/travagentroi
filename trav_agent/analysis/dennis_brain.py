"""Dennis Brain — Post-analys som flaggar vinnarspel-picks baserat pa Dennis metod.

Dennis analysmetod i modellform:
1. Formtid: Analysera senaste 5-10 starter, filtrera bort hangtider,
   berakna realistiskt tidsintervall (min-max) for varje hast.
   - Hangtid = snabb tid + dalig placering (5+) + hoga odds (20+)
   - Riktiga tider = vinster/topp-3 + lagre odds + rimlig placering
2. Klassdropp: Karriarpengar vs faltets median
3. Barfota: Positiv utrustningssignal
4. Modellrank 1-5 + streck 5-25%

Koers efter CompositeAnalyzer.analyze_race() och satter:
  - entry.dennis_form_min/max (realistiskt tidsintervall fran senaste form)
  - entry.dennis_time_edge (sekunder snabbare an faltets median formtid)
  - entry.dennis_class_ratio (karriarpengar / faltets median)
  - entry.dennis_pick (True om S4-kriterier uppfylls)
  - entry.dennis_confidence (hur saker tidsbedomningen ar: 0-1)
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Optional

from ..data.models import Race, RaceEntry, PastStart

logger = logging.getLogger(__name__)

# S4 strategy thresholds (from backtest: +45.2% vinnarspel ROI, p<0.0001)
S4_MAX_RANK = 5          # model rank 1-5
S4_MIN_STRECK = 0.05     # 5%
S4_MAX_STRECK = 0.25     # 25%
S4_MIN_TIME_EDGE = 1.0   # 1 second faster than field median
S4_MIN_CLASS_RATIO = 1.5  # 1.5x field median career money


# ── Formtid-analys (Dennis metod) ─────────────────────────────────────────

@dataclass
class FormTimeResult:
    """Resultat av formtidsanalys for en hast."""
    form_min: float = 0.0        # snabbaste realistiska km-tid (t.ex. 71.0 = 1.11.0)
    form_max: float = 0.0        # langsammaste realistiska km-tid
    confidence: float = 0.0      # 0-1 hur saker bedomningen ar
    n_real_times: int = 0        # antal riktiga tider (ej hangtider)
    n_total_starts: int = 0      # antal starter analyserade
    flags: list[str] = None      # riskflaggor: "galopp", "uppehall", "hangtider"

    def __post_init__(self):
        if self.flags is None:
            self.flags = []

    @property
    def form_mid(self) -> float:
        """Mittpunkten av tidsintervallet."""
        if self.form_min and self.form_max:
            return (self.form_min + self.form_max) / 2
        return self.form_min or self.form_max


def _is_similar_distance(start_dist: int, race_dist: int) -> bool:
    """Ar starten pa liknande distans som dagens lopp?

    Tre distansgrupper — tider ar jamborbara INOM grupp, inte mellan:
      Sprint:  1400-1800m (1640m-typ)
      Medel:   1900-2400m (2140m-typ)
      Stayer:  2400-3700m (2640m+)

    En 2640m-tid sager ingenting om hur hasten springer 2140m.
    """
    def bucket(d: int) -> str:
        if d <= 1800:
            return "sprint"
        elif d <= 2400:
            return "medel"
        else:
            return "stayer"

    return bucket(start_dist) == bucket(race_dist)


def _hanging_time_penalty(start: PastStart) -> float:
    """Berakna hangtids-penalty for en start.

    Returnerar 0.0 om det INTE ar en hangtid.
    Returnerar 0.3-1.0 sekunder penalty beroende pa hur tydlig
    hangtiden ar. Tiden INKLUDERAS i analysen men med penalty.

    Dennis princip: man ska inte ta bort hangtider — de visar att
    hasten KAN springa dom tiderna. Men for att VINNA behover
    hasten kanske 0.5-1.0s mer. Bedoms fran fall till fall:

    - Mild (5-6:a, odds 15-25): +0.3s — hangde med men var inte langsamst
    - Medium (5-8:a, odds 25-50): +0.5s — tydlig outsider som bara foljde
    - Grov (7+:a, odds 50+ ELLER 8+:a i dyrlopp): +0.8s — helt overklassad
    """
    if not start.km_time or start.km_time <= 0:
        return 0.0

    placement = start.placement or 99
    odds = start.odds or 0

    # Vinst eller 2:a med rimliga odds = aldrig hangtid
    if placement <= 2 and odds < 30:
        return 0.0

    # 3:a-4:a = normalt inte hangtid, utom vid extrema odds
    if placement <= 4 and odds < 35:
        return 0.0

    # ── Grov hangtid ──
    # Extrema outsider (odds 50+) som inte var nara att vinna
    if odds >= 50 and placement >= 5:
        return 0.8

    # Mycket dalig placering (8+) i starkare falt
    if placement >= 8 and odds >= 10:
        return 0.8

    # ── Medium hangtid ──
    # Stor outsider (odds 25-50) med dalig placering
    if odds >= 25 and placement >= 5:
        return 0.5

    # 7:a+ med medelhoga odds
    if placement >= 7 and odds >= 8:
        return 0.5

    # ── Mild hangtid ──
    # 5-6:a med forhojda odds
    if placement >= 5 and odds >= 15:
        return 0.3

    # Disk med hoga odds
    if start.disqualified and odds >= 15:
        return 0.5

    return 0.0


def _is_lazy_win(start: PastStart) -> bool:
    """Identifiera 'lata vinster' — hasten vann men utan att behova
    springa fort.

    En hast som vinner pa 1.14.9 i ett 35 000kr-lopp med odds 1.7
    behover inte springa snabbare an sa for att vinna. Denna tid sager
    INTE att hasten ar i dalig form — bara att klassen pa motstandet
    var lag.

    Indikator: vinst/2:a + lagt odds + lag prissumma relativt
    hastens normala klass.
    """
    if not start.km_time or start.km_time <= 0:
        return False

    placement = start.placement or 99
    odds = start.odds or 0

    # Hast som vann/tvaa med lagt odds i sma lopp
    # Dessa tider ar UNDRE GRANS for vad hasten kan — inte ovre
    if placement <= 2 and odds <= 3.0 and start.race_purse > 0 and start.race_purse <= 60000:
        return True

    return False


def _analyze_form_times(entry: RaceEntry, race_distance: int = 0) -> FormTimeResult:
    """Analysera senaste 5-10 starter for att berakna realistiskt tidsintervall.

    Dennis metod:
    1. Titta pa senaste 10 starterna
    2. Filtrera pa LIKNANDE DISTANS (kort vs lang)
    3. Filtrera bort hangtider (snabb tid + dalig placering + hoga odds)
    4. Separera 'lata vinster' fran verkliga prestationer
    5. Berakna intervall: fokusera pa vad hasten KAN springa pa riktigt
    6. Satt confidence baserat pa datakvalitet + risk
    """
    result = FormTimeResult()
    recent = entry.horse.recent_starts(10)
    if not recent:
        return result

    result.n_total_starts = len(recent)

    # Kolla galopp/uppehall-risker
    if recent and recent[0].galloped:
        result.flags.append("galopp_senast")

    n_gallops = sum(1 for s in recent[:5] if s.galloped)
    if n_gallops >= 2:
        result.flags.append("galoppbenagen")

    # Kolla uppehall (dagar sedan senaste start)
    from datetime import date as date_type
    if recent:
        days_since = (date_type.today() - recent[0].start_date).days
        if days_since > 120:
            result.flags.append(f"uppehall_{days_since}d")

    # ── Filtrera pa distans ───────────────────────────────────────
    # Separera starter pa liknande distans vs annan distans.
    # Om racedistans ar satt, prefer liknande distanser.
    same_dist_starts: list[PastStart] = []
    other_dist_starts: list[PastStart] = []

    for s in recent:
        if not s.km_time or s.km_time <= 0:
            continue  # galopp/disk utan tid
        if s.km_time < 60 or s.km_time > 100:
            continue  # orimligt varde

        if race_distance > 0 and _is_similar_distance(s.distance, race_distance):
            same_dist_starts.append(s)
        elif race_distance > 0:
            other_dist_starts.append(s)
        else:
            same_dist_starts.append(s)  # ingen distans angiven — anvand alla

    # Om vi har minst 2 starter pa ratt distans, anvand bara dem
    # Annars fyll pa med andra distanser (men flagga)
    if len(same_dist_starts) >= 2:
        analysis_starts = same_dist_starts
    elif same_dist_starts:
        analysis_starts = same_dist_starts + other_dist_starts
        result.flags.append("fa_distansstarter")
    elif other_dist_starts:
        analysis_starts = other_dist_starts
        result.flags.append("annan_distans")
    else:
        return result

    # ── Berakna justerade tider (hangtider med penalty) ──────────
    # Dennis princip: hangtider tas INTE bort. De visar kapacitet.
    # Men de justeras med +0.3 till +0.8s for att reflektera
    # att hasten inte hade vinnarchans vid den tiden.
    adjusted_times: list[float] = []
    competitive_times: list[float] = []  # exklusive lazy wins
    n_hanging = 0
    n_lazy = 0
    n_competitive = 0

    for s in analysis_starts:
        penalty = _hanging_time_penalty(s)
        if penalty > 0:
            # Hangtid — inkludera med penalty
            adjusted_time = s.km_time + penalty
            adjusted_times.append(adjusted_time)
            competitive_times.append(adjusted_time)
            n_hanging += 1
        elif _is_lazy_win(s):
            # Lat vinst — hasten gick halvfart mot svagt motstand.
            # Inkluderas i form_min (kan springa sa fort) men INTE
            # i form_max (den lagre tiden beror inte pa dalig form,
            # utan pa att hasten inte behovde springa fortare).
            # Steady Express ex: 1.14.9 i 35k-lopp = lazy, men
            # 1.11.2 mot starkare falt = riktigt. Spread ska vara
            # baserad pa competitive_times, inte lazy wins.
            n_lazy += 1
            adjusted_times.append(s.km_time)  # paverkar form_min
            # INTE competitive_times — paverkar inte form_max/spread
        else:
            adjusted_times.append(s.km_time)
            competitive_times.append(s.km_time)
            n_competitive += 1

    if n_hanging:
        result.flags.append(f"hangtider_{n_hanging}")
    if n_lazy:
        result.flags.append(f"lazy_wins_{n_lazy}")

    # ── Berakna tidsintervall ─────────────────────────────────────
    if not adjusted_times:
        return result

    result.form_min = min(adjusted_times)
    # form_max baseras pa competitive times — lazy wins EXKLUDERADE
    # Sa att spreaden reflekterar riktig inkonsistens, inte att
    # hasten gick halvfart i svaga lopp.
    if competitive_times:
        result.form_max = max(competitive_times)
    else:
        result.form_max = max(adjusted_times)
    result.n_real_times = n_competitive + n_lazy  # alla utom hangtider

    # ── Confidence-berakning ──────────────────────────────────────
    conf = 1.0

    # Farre riktiga tider = lagre confidence
    if result.n_real_times == 0:
        conf *= 0.2
    elif result.n_real_times == 1:
        conf *= 0.4
    elif result.n_real_times == 2:
        conf *= 0.65
    elif result.n_real_times <= 4:
        conf *= 0.85

    # Stort intervall = lagre confidence (ojamn hast)
    spread = result.form_max - result.form_min
    if spread > 3.0:
        conf *= 0.4
    elif spread > 2.0:
        conf *= 0.6
    elif spread > 1.0:
        conf *= 0.8

    # Galopp senast = lagre confidence
    if "galopp_senast" in result.flags:
        conf *= 0.6

    # Langt uppehall = lagre confidence
    for flag in result.flags:
        if flag.startswith("uppehall_"):
            days = int(flag.split("_")[1].rstrip("d"))
            if days > 300:
                conf *= 0.3
            elif days > 180:
                conf *= 0.5
            elif days > 120:
                conf *= 0.7

    # Annan distans = lagre confidence
    if "annan_distans" in result.flags:
        conf *= 0.4
    elif "fa_distansstarter" in result.flags:
        conf *= 0.7

    # Manga vinster med lagt odds = HOG confidence (bevisad klass)
    wins_low_odds = sum(1 for s in analysis_starts
                        if s.placement == 1 and (s.odds or 99) < 5.0)
    if wins_low_odds >= 3:
        conf = min(conf * 1.3, 0.95)  # boost men cap pa 95%
    elif wins_low_odds >= 2:
        conf = min(conf * 1.15, 0.95)

    # Favorit-historik: hasten har varit betrodd (odds <5) i flera lopp
    # Speciellt viktigt for unga hastar med fa starter — marknadens
    # bedomning ger extra information nar egen data ar tunn
    times_favored = sum(1 for s in analysis_starts
                        if (s.odds or 99) < 5.0)
    if times_favored >= 2 and result.n_total_starts <= 5:
        # Ung hast som varit favorit flera ganger = marknaden tror pa den
        conf = min(conf * 1.3, 0.95)
    elif times_favored >= 3:
        conf = min(conf * 1.2, 0.95)

    result.confidence = round(min(conf, 1.0), 2)
    return result


def _compute_class_drop_vs_field(
    entry: RaceEntry, field_median_purse: float
) -> float:
    """Berakna klassdropp: hastens senaste loppsprispottar vs faltets median.

    Om hasten senast sprang i lopp med hogre prissumma (tuffare motstand)
    an det typiska i detta falt, ar det en POSITIV signal — hasten har
    matt bra hastar och nu moter den svagare.

    Jamfor hastens average senaste 5 prispottar mot faltets median.
    Fungerar aven nar race.purse = 0 (vanligt i ATG API).

    Returnerar ratio >1.0 om klassdropp, <1.0 om klasshopp.
    """
    if field_median_purse <= 0:
        return 1.0

    recent = entry.horse.recent_starts(5)
    purses = [s.race_purse for s in recent if s.race_purse > 0]
    if not purses:
        return 1.0

    avg_purse = sum(purses) / len(purses)
    return round(avg_purse / field_median_purse, 2)


def _driver_quality_bonus(entry: RaceEntry) -> float:
    """Berakna tidsbonus for elit-kusk.

    Elite drivers produce faster times through better race tactics,
    timing, and horse management. A top driver can gain 0.3-0.5s
    compared to an average driver on the same horse.

    Returns seconds to SUBTRACT from effective form (negative = faster).
    """
    win_pct = entry.driver_win_pct or 0
    starts = entry.driver_starts_year or 0
    top3_pct = entry.driver_top3_pct or 0

    # Absolut elit: >15% vinstprocent med manga starter
    # Typiskt: Ulf Ohlsson, Orjan Kihlstrom, Robert Bergh etc.
    if win_pct >= 0.15 and starts >= 200:
        return 0.4

    # Stark proffs: >12% med aktiv stallning
    if win_pct >= 0.12 and starts >= 150:
        return 0.25

    # Bra proffs: >10% med rejal volym ELLER >13% med nagra starter
    if (win_pct >= 0.10 and starts >= 200) or (win_pct >= 0.13 and starts >= 80):
        return 0.15

    # Hog top3-procent utan manga vinster (stallt men sakerplacerad)
    if top3_pct >= 0.30 and starts >= 100:
        return 0.15

    return 0.0


# ── Effective form constants ──────────────────────────────────────
# Max penalty for zero confidence (seconds added to form_min)
UNCERTAINTY_PENALTY = 1.5
# Max class drop bonus (seconds subtracted from form)
MAX_CLASS_DROP_BONUS = 1.0
# Class drop ratio threshold (>1.3 = running below recent level)
CLASS_DROP_THRESHOLD = 1.3
# Max driver bonus (seconds subtracted from form)
MAX_DRIVER_BONUS = 0.4


def compute_dennis_form_signals(race: Race) -> None:
    """Berakna Dennis Brain v2 form-signaler (effective_form, time_edge, class_ratio).

    Kors FORE super_score-berakning sa att effective_form kan
    anvandas i triple-blenden (33% comp + 33% effform + 33% marknad).

    Satter pa varje entry:
    - dennis_form_min/max (realistiskt tidsintervall)
    - dennis_confidence (0-1)
    - dennis_effective_form (confidence-vagd + klass + kusk)
    - dennis_time_edge (sekunder snabbare an faltets median)
    - dennis_class_ratio (karriarpengar / faltets median)
    - dennis_class_drop (senaste prispott vs faltets median)
    """
    active = race.active_entries
    if not active:
        return

    # ── 0. Berakna faltets median senaste prispott (for klassdropp) ──
    # Istallet for race.purse (ofta 0 i ATG API), anvand faltets
    # genomsnittliga senaste prispottar som referens.
    field_purses: list[float] = []
    for entry in active:
        purses = [s.race_purse for s in entry.horse.recent_starts(5)
                  if s.race_purse > 0]
        if purses:
            field_purses.append(sum(purses) / len(purses))
    field_median_purse = statistics.median(field_purses) if field_purses else 0.0

    # ── 1. Formtid-analys per hast ────────────────────────────────
    race_dist = race.distance or 0
    form_results: dict[int, FormTimeResult] = {}
    for entry in active:
        fr = _analyze_form_times(entry, race_distance=race_dist)
        form_results[entry.post_position] = fr
        # Spara ra formtider pa entry
        entry.dennis_form_min = fr.form_min
        entry.dennis_form_max = fr.form_max
        entry.dennis_confidence = fr.confidence

    # ── 1b. Detektera distans-glesa lopp ─────────────────────────
    # I 2640m-lopp har nastan ingen hast sprungit den distansen.
    # Da ar confidence-penalty meningslos (alla straffas lika) och
    # rankingen styrs av brus. Dennis: "starka snarare an snabba" —
    # klass, kusk och formtrend ar viktigare nar tiderna ar osakra.
    #
    # Losning: nar >60% av faltet saknar distansdata, skala ner
    # uncertainty_penalty och skala upp klass/kusk-bonusar.
    n_dist_issues = sum(
        1 for fr in form_results.values()
        if "annan_distans" in fr.flags or "fa_distansstarter" in fr.flags
    )
    n_with_form = sum(1 for fr in form_results.values() if fr.form_min > 0)
    dist_coverage = 1.0 - (n_dist_issues / n_with_form) if n_with_form > 0 else 1.0

    # Scale factors for distance-sparse races
    if dist_coverage < 0.3:
        # >70% saknar distansdata (typiskt 2640m-lopp)
        # Minimera confidence-penalty, maxa klass/kusk-signaler
        race_uncertainty_penalty = UNCERTAINTY_PENALTY * 0.25
        race_class_bonus_scale = 1.5  # klass vager 50% mer
        race_driver_bonus_scale = 1.5  # kusk vager 50% mer
        logger.debug(
            f"  Avd {race.race_number}: distance-sparse race "
            f"({n_dist_issues}/{n_with_form} lack dist data) — "
            f"reducing uncertainty penalty, boosting class/driver"
        )
    elif dist_coverage < 0.5:
        # 50-70% saknar distansdata
        race_uncertainty_penalty = UNCERTAINTY_PENALTY * 0.5
        race_class_bonus_scale = 1.25
        race_driver_bonus_scale = 1.25
    else:
        # Normal race — de flesta har distansdata
        race_uncertainty_penalty = UNCERTAINTY_PENALTY
        race_class_bonus_scale = 1.0
        race_driver_bonus_scale = 1.0

    # ── 2. Effective form: confidence-vagd + klass + kusk-justerad ──
    # Dennis rankar INTE enbart pa snabbaste tid. Han vager:
    # - Kan hasten springa fort? (form_min)
    # - Hur saker ar vi pa det? (confidence)
    # - Gar den ner i klass? (class_drop — tuffare lopp nyligen)
    # - Har den elit-kusk? (driver_quality)
    for entry in active:
        fr = form_results[entry.post_position]
        if fr.form_min <= 0:
            entry.dennis_effective_form = 0.0
            continue

        effective = fr.form_min

        # a) Confidence-penalty: osaker hast = hogre effektiv tid
        #    Skalas ner i distans-glesa lopp dar alla har lag confidence
        effective += (1.0 - fr.confidence) * race_uncertainty_penalty

        # b) Klassdropp-bonus: tuffare lopp nyligen = hasten ar bra
        #    I distans-glesa lopp vager klass MER (scale > 1.0)
        class_drop = _compute_class_drop_vs_field(entry, field_median_purse)
        entry.dennis_class_drop = class_drop
        if class_drop > CLASS_DROP_THRESHOLD:
            bonus = min((class_drop - 1.0) * 0.5, MAX_CLASS_DROP_BONUS)
            bonus *= race_class_bonus_scale
            effective -= bonus
            logger.debug(
                f"  {entry.horse.name}: class_drop {class_drop:.1f}x → "
                f"-{bonus:.1f}s (scale {race_class_bonus_scale:.1f}x)"
            )

        # c) Kusk-bonus: elit-kusk pressar bort 0.15-0.4s
        #    I distans-glesa lopp vager kusken MER (scale > 1.0)
        driver_bonus = _driver_quality_bonus(entry)
        if driver_bonus > 0:
            driver_bonus *= race_driver_bonus_scale
            effective -= driver_bonus
            logger.debug(
                f"  {entry.horse.name}: driver_bonus -{driver_bonus:.1f}s "
                f"(win {entry.driver_win_pct:.0%}, starts {entry.driver_starts_year}, "
                f"scale {race_driver_bonus_scale:.1f}x)"
            )

        entry.dennis_effective_form = round(effective, 2)

    # ── 3. Tid-edge: effective_form vs faltets median ──────────────
    # Anvander effective_form for ranking — inte ra form_min.
    # Sa en hast med snabb form_min men lag confidence rankas lagre
    # an en med medel form_min men hog confidence + class drop.
    eff_forms: list[float] = []
    for entry in active:
        if entry.dennis_effective_form > 0:
            eff_forms.append(entry.dennis_effective_form)

    if len(eff_forms) >= 3:
        field_eff_median = statistics.median(eff_forms)
        for entry in active:
            if entry.dennis_effective_form > 0:
                # Positive = faster (lower eff form = better)
                entry.dennis_time_edge = round(
                    field_eff_median - entry.dennis_effective_form, 2
                )
            else:
                entry.dennis_time_edge = 0.0
    else:
        for entry in active:
            entry.dennis_time_edge = 0.0

    # ── 4. Klass-ratio: karriarpengar vs faltets median ────────────
    # Use api_career_money (preserved from ATG API, not wiped by temporal filtering)
    career_moneys: dict[int, float] = {}
    for entry in active:
        cm = entry.horse.api_career_money or entry.horse.career.total_prize_money
        if cm and cm > 0:
            career_moneys[entry.post_position] = float(cm)

    if len(career_moneys) >= 3:
        money_median = statistics.median(career_moneys.values())
        if money_median > 0:
            for entry in active:
                if entry.post_position in career_moneys:
                    entry.dennis_class_ratio = round(
                        career_moneys[entry.post_position] / money_median, 2
                    )
                else:
                    entry.dennis_class_ratio = 0.0
        else:
            for entry in active:
                entry.dennis_class_ratio = 0.0
    else:
        for entry in active:
            entry.dennis_class_ratio = 0.0



def compute_dennis_picks(race: Race) -> None:
    """S4 vinnarspel-kvalificering. Kors EFTER ranking sa att entry.rank finns.

    Satter entry.dennis_pick = True om S4-kriterier uppfylls.
    """
    active = race.active_entries
    if not active:
        return

    n_picks = 0
    for entry in active:
        is_barefoot = entry.shoe_front_off and entry.shoe_back_off
        streck = entry.bet_percentage or 0

        entry.dennis_pick = (
            entry.rank <= S4_MAX_RANK
            and S4_MIN_STRECK <= streck <= S4_MAX_STRECK
            and is_barefoot
            and entry.dennis_time_edge >= S4_MIN_TIME_EDGE
            and entry.dennis_class_ratio >= S4_MIN_CLASS_RATIO
        )

        if entry.dennis_pick:
            n_picks += 1
            logger.info(
                f"DENNIS PICK: {entry.horse.name} "
                f"(rank {entry.rank}, streck {streck:.1%}, "
                f"tid +{entry.dennis_time_edge:.1f}s, "
                f"klass {entry.dennis_class_ratio:.1f}x, barfota)"
            )

    if n_picks > 0:
        logger.info(
            f"Avd {race.race_number}: {n_picks} Dennis-pick(s) hittade"
        )


# Legacy alias for backward compatibility
def compute_dennis_signals(race: Race) -> None:
    """Legacy wrapper — korer bade form-signaler och picks."""
    compute_dennis_form_signals(race)
    compute_dennis_picks(race)


def _best_km_time_from_starts(entry: RaceEntry) -> Optional[float]:
    """Hamta basta km-tid fran hastens senaste starter (fallback om record saknas)."""
    best = None
    for ps in entry.horse.past_starts[:20]:  # senaste 20
        if ps.km_time and ps.km_time > 0:
            if best is None or ps.km_time < best:
                best = ps.km_time
    return best
