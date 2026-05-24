"""Utrustningsfaktor — barfot, sulkybyte (Am/Va), skobyte.

Dennis's insikter:
- "Amerikansk vagn brukar ha en effekt ocksa vart att kolla upp nar man
  gar fran vanlig vagn till amerikansk sa kan man hitta skrallar"
- "Samma sak med nar man gar fran skor till barfota brukar vara
  sadana indikationer"
- "Sa man maste tanka lite annorlunda kring skrallar for att hitta dom.
  Hitta edge pa olika satt."

Signaldata (14 292 lopp):
- Barfot: 9.7% vs 7.7% = 1.13x lift
- Sulkybyte (Am→Va el tvartomt): 9.5% vs 8.5% = 1.11x lift
- Skobyte (ej barfot): 8.1% vs 8.7% = 0.95x

Dennis framhaller att dessa ar SKRALL-signaler:
- Forandring = tranaren provar nagot nytt
- Gar ofta under radarn hos vanliga spelare
- Framfor allt i kombination med andra faktorer
"""

from __future__ import annotations

from ..data.models import Race, RaceEntry
from .base import AnalysisFactor


class Equipment(AnalysisFactor):
    """Analyserar utrustningsforandringar: barfot, sulkybyte Am/Va, skobyte.

    Dennis: "hitta edge pa olika satt" — utrustningsbyten ar
    skrall-indikatorer som spelarna ofta missar.
    """

    name = "equipment"

    def score(self, entry: RaceEntry, race: Race) -> float:
        """Poang baserad pa utrustningssignaler.

        Logik:
        - Bas = 50
        - Barfot (fram eller bak): +18 (Dennis: "fran skor till barfota")
        - Sulkybyte (Am→Va eller Va→Am): +12 (Dennis: "amerikansk vagn")
        - Skobyte utan barfot: +5 (taktisk forandring)
        - Kombination barfot + sulkybyte: extra +8
        - Om EJ betrodd (hoga odds / lagt streck) + forandring: extra +5
        """
        shoe_front_off: bool = getattr(entry, "shoe_front_off", False)
        shoe_back_off: bool = getattr(entry, "shoe_back_off", False)
        shoe_changed: bool = getattr(entry, "shoe_changed", False)
        sulky_changed: bool = getattr(entry, "sulky_changed", False)

        score = 50.0

        barefoot = shoe_front_off or shoe_back_off
        has_change = barefoot or sulky_changed or shoe_changed

        # Barfot = starkaste signalen
        # Dennis: "fran skor till barfota brukar vara indikationer"
        if barefoot:
            score += 18.0

        # Sulkybyte = taktisk forandring (Am↔Va)
        # Dennis: "nar man gar fran vanlig vagn till amerikansk
        # sa kan man hitta skrallar"
        if sulky_changed:
            score += 12.0

        # Skobyte utan barfot = mild taktisk signal
        if shoe_changed and not barefoot:
            score += 5.0

        # Kombo-bonus: barfot + sulkybyte = tranaren satsar pa forsta sida
        if barefoot and sulky_changed:
            score += 8.0

        # Skrall-potential: forandring pa en icke-favorit
        # Dennis: "hitta skrallar" — utrustningsbyte pa outsider
        if has_change:
            bet_pct = entry.bet_percentage or 0
            if 0 < bet_pct < 0.08:
                score += 5.0  # Outsider med forandring
            elif 0 < bet_pct < 0.15:
                score += 3.0

        return min(100.0, max(0.0, score))
