"""Dashboard — genererar en interaktiv HTML-dashboard för analysresultat.

Inkluderar expanderbara startlistor per häst med senaste 5 starter,
karriärstatistik, och färgkodade placeringar.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from ..data.models import GameRound, Race, RaceEntry, RaceStatus, StartMethod


def _esc(s: str) -> str:
    return html.escape(str(s))


# A/B/C/D ranking display — maps internal recommendation to tier label
RANK_LABEL = {
    "spik": "A",
    "2-val": "B",
    "3-val": "B",
    "gardering": "C",
    "strykning": "D",
}


def _rank_label(rec: str) -> str:
    """Map recommendation to A/B/C/D tier label for display."""
    return RANK_LABEL.get(rec, "D")


def _rec_color(rec: str) -> str:
    return {
        "spik": "#15803d",       # A = green
        "2-val": "#1e40af",      # B = blue
        "3-val": "#1e40af",      # B = blue
        "gardering": "#b45309",  # C = amber
        "strykning": "#991b1b",  # D = red
    }.get(rec, "#64748b")


def _rec_bg(rec: str) -> str:
    return {
        "spik": "#dcfce7",       # A = green bg
        "2-val": "#dbeafe",      # B = blue bg
        "3-val": "#dbeafe",      # B = blue bg
        "gardering": "#fef3c7",  # C = amber bg
        "strykning": "#fee2e2",  # D = red bg
    }.get(rec, "rgba(0,0,0,0.04)")


def _plac_color(plac) -> str:
    """Färgkod för placering: 1=guld, 2-3=silver/brons, disk=röd."""
    if plac is None:
        return "#ef4444"  # Röd = disk/utgången
    if plac == 1:
        return "#fbbf24"  # Guld
    if plac == 2:
        return "#9ca3af"  # Silver
    if plac == 3:
        return "#cd7f32"  # Brons
    return "#64748b"  # Grå


FACTOR_LABELS = {
    "time_analysis": "Tid",
    "form_curve": "Form",
    "prize_index": "Pris",
    "track_profile": "Bana",
    "category_profile": "Kat",
    "driver_trainer": "Kusk",
    "post_position": "Spar",
}


def _calculate_spelvarde(game_round: GameRound) -> dict | None:
    """Beräkna spelvärde (0-100) — Expected Value Score.

    Baserat på validering mot 12,078 historiska omgångar.
    Mäter: "Ser denna omgång ut som ett historiskt lönsamt läge?"

    Historisk korrelation (alla spelformer):
      85+  → +4,195 kr/omg  |  70-84 → +937 kr/omg
      55-69 → +1,923 kr/omg |  40-54 → +1,031 kr/omg

    Komponenter:
      A) Streck-profil — favoriter i sweet spot 25-40% (inte tunga, inte lätta)
      B) Value-mix — andel picks på <20% streck (30-65% = bäst EV)
      C) Upset-balans — rätt antal skräll-lopp per spelform
      D) Spelform — historisk netto/omg som bas
    """
    import statistics as _stats

    try:
        from ..betting.system_generator import SystemGenerator
    except ImportError:
        return None

    gen = SystemGenerator(
        budget=2500, strategy="I_streck_1st", selective_filter="avg_upset_lt_40",
    )
    system = gen.generate(game_round)

    if system.skip_round:
        return {
            "score": 0, "color": "#ef4444", "bg": "rgba(239,68,68,0.15)",
            "text": "Skippa", "advice": system.skip_reason,
            "details": {"streck": 0, "value": 0, "upset": 0, "bas": 0},
        }

    gt = game_round.game_type or "V75"

    # ── Extrahera features från systemet ──
    top_strecks: list[float] = []
    low_streck_count = 0
    total_picks = 0
    upset_values: list[float] = []

    for rp in system.race_picks:
        if rp.pick_streck:
            # pick_streck är i 0-1 range (0.5 = 50%), konvertera till %
            pct = [s * 100 if s < 1 else s for s in rp.pick_streck]
            top_strecks.append(pct[0])
            for s in pct:
                if s < 20:
                    low_streck_count += 1
                total_picks += 1
        upset_values.append(rp.upset_risk)

    avg_top_streck = sum(top_strecks) / len(top_strecks) if top_strecks else 30
    pct_value = low_streck_count / total_picks * 100 if total_picks > 0 else 50
    upset_count = sum(1 for u in upset_values if u >= 20)
    max_upset = max(upset_values) if upset_values else 0
    upset_std = _stats.stdev(upset_values) if len(upset_values) >= 2 else 0

    # ── Beräkna score ──
    if gt == "GS75":
        # GS75 har helt annan karaktär — streckvärden irrelevanta
        # Prediktor 1: Max upset (40%+ = +445% ROI historiskt)
        if max_upset >= 40:
            streck_score = 35
        elif max_upset >= 30:
            streck_score = 25
        elif max_upset >= 20:
            streck_score = 18
        else:
            streck_score = 22  # <20% faktiskt ok (+240% ROI)

        # Prediktor 2: Spridning i upset (hög stddev = stora vinster)
        if upset_std >= 15:
            value_score = 30  # +775% ROI, +15,869/omg
        elif upset_std < 5:
            value_score = 25  # <5 bra (+603% ROI)
        elif upset_std >= 10:
            value_score = 20
        else:
            value_score = 15  # 5-9 sämst

        upset_score = 0  # ingår i ovan
        gt_base = 15
    else:
        # V75 / V86 / V64 / V85

        # A) Streck sweet spot (30p max)
        if 25 <= avg_top_streck <= 40:
            streck_score = 30
        elif 20 <= avg_top_streck < 25:
            streck_score = 22
        elif 40 < avg_top_streck <= 50:
            streck_score = 18
        else:
            streck_score = 10

        # B) Value-mix (25p max) — 30-65% picks på <20% streck
        if 30 <= pct_value <= 65:
            value_score = 25
        elif 20 <= pct_value < 30 or 65 < pct_value <= 75:
            value_score = 18
        else:
            value_score = 10

        # C) Upset-balans (25p max) — spelformsspecifik sweet spot
        if gt in ("V75", "V85"):
            # V75: 3 upset-lopp = +25,601/omg historiskt
            upset_map = {3: 25, 2: 20, 1: 15, 4: 15, 0: 12}
            upset_score = upset_map.get(upset_count, 8)
        elif gt == "V86":
            # V86: 1 upset-lopp = +5,971/omg, 3 också bra
            upset_map = {1: 25, 3: 22, 0: 15, 4: 15, 2: 12}
            upset_score = upset_map.get(upset_count, 8)
        else:
            # V64: 2 upset-lopp = +1,236/omg
            upset_map = {2: 25, 1: 20, 3: 20, 0: 15}
            upset_score = upset_map.get(upset_count, 10)

        # D) Spelform-bonus (historisk netto/omg)
        gt_base = {"V85": 20, "V75": 18, "V86": 16, "V64": 12, "V65": 12}.get(gt, 12)

    score = max(0, min(100, streck_score + value_score + upset_score + gt_base))

    # ── Färg och text baserat på historiska nivåer ──
    if score >= 85:
        color, bg = "#fbbf24", "rgba(251,191,36,0.15)"
        text = "Utmärkt"
        advice = "Bra läge — överväg fler system/dubbletter"
    elif score >= 70:
        color, bg = "#22c55e", "rgba(34,197,94,0.15)"
        text = "Bra"
        advice = "Normalspel rekommenderas"
    elif score >= 55:
        color, bg = "#f59e0b", "rgba(245,158,11,0.12)"
        text = "Medel"
        advice = "Liten insats — osäkert läge"
    else:
        color, bg = "#ef4444", "rgba(239,68,68,0.12)"
        text = "Lågt"
        advice = "Överväg att skippa omgången"

    return {
        "score": round(score), "color": color, "bg": bg,
        "text": text, "advice": advice,
        "details": {
            "streck": round(streck_score),
            "value": round(value_score),
            "upset": round(upset_score),
            "bas": round(gt_base),
        },
    }


def _format_km_time(km_time, start_method=None) -> str:
    """Formatera km-tid: 73.6 → '1.13,6a' eller '1.13,6v'."""
    if not km_time or km_time <= 0:
        return "-"
    minutes = int(km_time // 60)
    seconds = km_time - minutes * 60
    suffix = ""
    if start_method == StartMethod.AUTO:
        suffix = "a"
    elif start_method == StartMethod.VOLT:
        suffix = "v"
    return f"{minutes}.{seconds:04.1f}{suffix}".replace(".", ",", 1)


def _trend_html(trend) -> str:
    """Formatera trend som färgkodad ↑/↓ text."""
    if trend is None:
        return '<span class="trend neutral">-</span>'
    if trend > 0:
        return f'<span class="trend trend-up">↑{trend:+.1f}</span>'
    elif trend < 0:
        return f'<span class="trend trend-down">↓{trend:+.1f}</span>'
    return f'<span class="trend neutral">{trend:.1f}</span>'


def _mini_sparkline(past_starts: list) -> str:
    """Render last 5 placements as a tiny inline SVG sparkline."""
    recent = past_starts[-5:] if len(past_starts) >= 5 else past_starts
    if len(recent) < 2:
        return ""
    points = []
    for i, start in enumerate(recent):
        plac = start.get("placering", 0) if isinstance(start, dict) else getattr(start, "placement", 0)
        if not plac or plac == 0:
            y = 18
        else:
            y = {1: 2, 2: 7, 3: 12}.get(plac, 16) if plac <= 5 else 18
        x = i * 12
        points.append(f"{x},{y}")
    polyline = " ".join(points)
    return f'<svg class="sparkline" viewBox="0 0 {(len(recent)-1)*12} 20"><polyline points="{polyline}" fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-linecap="round"/></svg>'


def _smart_time_interval(entry: RaceEntry, race: Race) -> dict | None:
    """Beräkna smart tidsintervall — max 1 sekund brett, viktat mot senaste form.

    Dennis's metod: titta på alla tider, vikta nyare tyngre, filtrera outliers,
    och ge ett intervall som beskriver vad hästen troligen springer JUST NU.

    Returns:
        dict med low, high, confidence, num_starts, is_estimated, source_distances
        eller None om ingen data.
    """
    SECS_PER_STEP = 1.7       # Dennis's 1.7s per 500m-regel
    EXACT_MARGIN = 100        # ±100m räknas som exakt distansmatch
    BROAD_MARGIN = 500        # ±500m för kors-distansestimering
    DECAY_FACTOR = 0.7        # Exponentiell viktförfall per start
    OUTLIER_THRESHOLD = 2.0   # Ta bort tider >2 stdav från medel

    dist = race.distance
    all_starts = entry.horse.past_starts
    if not all_starts:
        return None

    # Steg 1: Samla alla tidsatta starter, normalisera till loppets distans
    # Sortera efter datum (nyast först)
    sorted_starts = sorted(all_starts, key=lambda s: s.start_date, reverse=True)

    data_points = []  # (normalized_km_time, is_exact, original_distance)
    for s in sorted_starts:
        if not s.km_time or s.km_time <= 0:
            continue
        if s.disqualified or s.galloped:
            continue  # Disk/galopp = outlier-känslig data

        dist_diff = abs(dist - s.distance)

        if dist_diff <= EXACT_MARGIN:
            # Exakt distansmatch — använd rå km-tid
            data_points.append((s.km_time, True, s.distance))
        elif dist_diff <= BROAD_MARGIN:
            # Kors-distans: justera med 1.7s per 500m
            steps = (s.distance - dist) / 500  # positiv = längre originaldistans
            adjusted = s.km_time - steps * SECS_PER_STEP
            data_points.append((adjusted, False, s.distance))

    if not data_points:
        return None

    # Steg 2: Tilldela recency-vikter (exponentiellt avtagande)
    # Mest nyliga starten får vikt 1.0, nästa 0.7, sedan 0.49, etc.
    weighted_points = []
    for idx, (km_time, is_exact, orig_dist) in enumerate(data_points):
        weight = DECAY_FACTOR ** idx
        # Exakt-distansdata får 1.5x viktmultiplikator
        if is_exact:
            weight *= 1.5
        weighted_points.append((km_time, weight, is_exact, orig_dist))

    # Steg 3: Viktat medelvärde och vägd standardavvikelse
    total_weight = sum(w for _, w, _, _ in weighted_points)
    w_mean = sum(t * w for t, w, _, _ in weighted_points) / total_weight

    w_variance = sum(w * (t - w_mean) ** 2 for t, w, _, _ in weighted_points) / total_weight
    w_stdev = w_variance ** 0.5

    # Steg 4: Ta bort outliers (>2 viktade stdav från medel)
    if w_stdev > 0 and len(weighted_points) > 2:
        filtered = [
            (t, w, ex, d) for t, w, ex, d in weighted_points
            if abs(t - w_mean) <= OUTLIER_THRESHOLD * w_stdev
        ]
        if len(filtered) >= 2:
            weighted_points = filtered

    # Steg 5: Beräkna nytt viktat medel efter outlier-filtrering
    total_weight = sum(w for _, w, _, _ in weighted_points)
    center = sum(t * w for t, w, _, _ in weighted_points) / total_weight

    # Steg 6: Intervall = center ± 0.5 (alltid exakt 1 sekund brett)
    low = center - 0.5
    high = center + 0.5

    # Steg 7: Konfidensberäkning
    # Baserat på: (a) andel datapunkter inom intervallet, (b) exakt-distansandel, (c) datavolym
    times_in_range = sum(1 for t, _, _, _ in weighted_points if low <= t <= high)
    total_points = len(weighted_points)

    hit_rate = times_in_range / total_points if total_points > 0 else 0

    exact_count = sum(1 for _, _, ex, _ in weighted_points if ex)
    exact_ratio = exact_count / total_points if total_points > 0 else 0

    # Datavolym-faktor: mer data = mer konfidens, diminishing returns
    volume_factor = min(1.0, total_points / 8)

    confidence = (
        hit_rate * 0.50 +          # Hur många tider faller i 1s-fönstret
        exact_ratio * 0.25 +       # Hur mycket är exakt-distansdata
        volume_factor * 0.25       # Hur mycket data vi har
    )
    confidence = min(0.95, max(0.10, confidence))

    # Steg 8: Bestäm om estimerat
    is_estimated = exact_count == 0
    source_distances = set(d for _, _, _, d in weighted_points)

    return {
        "center": round(center, 1),
        "low": round(low, 1),
        "high": round(high, 1),
        "confidence": round(confidence, 2),
        "num_starts": total_points,
        "is_estimated": is_estimated,
        "exact_count": exact_count,
        "source_distances": source_distances,
    }


def _time_range_html(entry: RaceEntry, race: Race) -> str:
    """Rendera smart tidsintervall — 1 sekund brett, viktat mot senaste form."""
    result = _smart_time_interval(entry, race)
    if result is None:
        return ""

    low_str = _format_km_time(result["low"], race.start_method)
    high_str = _format_km_time(result["high"], race.start_method)
    conf_pct = int(result["confidence"] * 100)
    n = result["num_starts"]
    dist = race.distance

    star = "*" if result["is_estimated"] else ""
    css_class = " estimated" if result["is_estimated"] else ""
    hl_class = "time-estimate" if result["is_estimated"] else "time-highlight"

    range_str = (
        f'<strong class="{hl_class}">{low_str}{star}</strong>'
        f' — '
        f'<strong class="{hl_class}">{high_str}{star}</strong>'
    )

    # Metadata-sträng
    if result["is_estimated"]:
        src_dists = sorted(result["source_distances"])
        src_str = "/".join(f"{d}m" for d in src_dists)
        meta = f'{conf_pct}% konf, est. från {src_str}'
    else:
        meta = f'{conf_pct}% konf, {n} starter'

    return (
        f'<div class="time-range-box{css_class}">'
        f'<span class="time-label">{dist}m:</span> {range_str}'
        f' <span class="time-meta">({meta})</span>'
        f'</div>'
    )


def _horse_detail_html(entry: RaceEntry, race: Race) -> str:
    """Generera expanderbar detaljvy med senaste 10 starter + karriärstatistik + tidsintervall."""
    past = entry.horse.recent_starts(10)
    career = entry.horse.career

    # Karriärstatistik
    career_html = ""
    if career.total_starts > 0:
        # Basinfo
        kr_start_str = f'{career.prize_per_start:,}kr' if career.prize_per_start else "-"
        snittodds_str = f'{career.avg_odds:.1f}' if career.avg_odds else "-"

        career_html = (
            f'<div class="career-stats">'
            f'<span class="career-item">Livs: <strong>{career.total_starts}</strong> '
            f'{career.wins}-{career.seconds}-{career.thirds}</span>'
            f'<span class="career-item">Vinst%: <strong>{career.win_rate:.0%}</strong></span>'
            f'<span class="career-item">Top3%: <strong>{career.top3_rate:.0%}</strong></span>'
            f'<span class="career-item">Pengar: <strong>{career.total_prize_money:,}kr</strong></span>'
            f'<span class="career-item">Kr/start: <strong>{kr_start_str}</strong></span>'
            f'<span class="career-item">Snittodds: <strong>{snittodds_str}</strong></span>'
            f'</div>'
        )

        # Tidsintervall vid loppets distans
        career_html += _time_range_html(entry, race)

    if not past:
        return (
            f'<tr class="detail-row hidden" data-horse="{entry.post_position}">'
            f'<td colspan="14"><div class="detail-content">'
            f'{career_html}'
            f'<p class="no-starts">Ingen starthistorik tillgänglig</p>'
            f'</div></td></tr>'
        )

    # Startlistatabell
    start_rows = []
    for s in past:
        plac_str = str(s.placement) if s.placement else ("d" if s.disqualified else "u")
        plac_color = _plac_color(s.placement)

        km_str = _format_km_time(s.km_time, s.start_method)

        odds_str = f"{s.odds:.2f}" if s.odds else "-"
        prize_str = f"{s.prize_money // 1000}'" if s.prize_money > 0 else "-"
        purse_str = f"{s.race_purse // 1000}'" if s.race_purse > 0 else "-"

        start_rows.append(
            f'<tr>'
            f'<td class="start-date">{s.start_date}</td>'
            f'<td>{_esc(s.track_name)}</td>'
            f'<td>{s.distance}m</td>'
            f'<td>{s.start_method.value[0].upper()}</td>'
            f'<td>{s.post_position}</td>'
            f'<td><span class="plac-badge" style="color:{plac_color};font-weight:700">{plac_str}</span></td>'
            f'<td class="km-time">{km_str}</td>'
            f'<td>{odds_str}</td>'
            f'<td>{prize_str}</td>'
            f'<td>{purse_str}</td>'
            f'</tr>'
        )

    return (
        f'<tr class="detail-row hidden" data-horse="{entry.post_position}">'
        f'<td colspan="14"><div class="detail-content">'
        f'{career_html}'
        f'<table class="starts-table">'
        f'<thead><tr>'
        f'<th>Datum</th><th>Bana</th><th>Dist</th><th>Start</th>'
        f'<th>Spar</th><th>Plac</th><th>Km-tid</th><th>Odds</th>'
        f'<th>Pris</th><th>Pott</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(start_rows)}</tbody>'
        f'</table>'
        f'</div></td></tr>'
    )


def _race_table_html(race: Race, proffs_horses: dict[int, dict] | None = None) -> str:
    sorted_entries = sorted(
        race.active_entries,
        key=lambda e: e.super_score,
        reverse=True,
    )

    # Build actual placement lookup for finished races
    is_finished = race.status == RaceStatus.FINISHED and len(race.result_order) > 0
    actual_placement: dict[int, int] = {}
    if is_finished:
        for pos, num in enumerate(race.result_order, 1):
            actual_placement[num] = pos

    value_picks = [
        e for e in sorted_entries
        if e.bet_percentage and 0.05 <= e.bet_percentage <= 0.15
        and e.super_score >= 50
    ]

    rows = []
    for rank_idx, e in enumerate(sorted_entries):
        model_rank = rank_idx + 1  # 1-indexed
        fs = e.factor_scores
        bet_str = f"{e.bet_percentage:.1%}" if e.bet_percentage else "-"
        rec = e.recommendation
        color = _rec_color(rec)
        bg = _rec_bg(rec)

        factor_cells = ""
        for key in FACTOR_LABELS:
            val = fs.get(key, 0)
            bar_w = max(0, min(100, val))
            factor_cells += (
                f'<td class="factor-cell">'
                f'<div class="factor-bar" style="width:{bar_w}%"></div>'
                f'<span class="factor-val">{val:.0f}</span>'
                f'</td>'
            )

        is_value = e in value_picks
        value_badge = ' <span class="value-badge">VALUE</span>' if is_value else ""

        # Chansspik vinnarspel indicator: model rank ≤2, streck 5-20%
        # Historical ROI: +47.8% on flat bet (walk-forward validated)
        is_chansspik = (
            model_rank <= 2
            and e.bet_percentage is not None
            and 0.05 <= e.bet_percentage <= 0.20
        )
        winbet_badge = ' <span class="winbet-badge">VINNARSPEL</span>' if is_chansspik else ""

        has_starts = len(e.horse.past_starts) > 0
        toggle_class = " clickable" if has_starts else ""
        toggle_icon = '<span class="toggle-icon">&#9654;</span> ' if has_starts else ""

        trend_cell = _trend_html(e.trend)
        sparkline = _mini_sparkline(e.horse.past_starts) if has_starts else ""

        # Result cell for finished races
        result_cell = ""
        if is_finished:
            plac = actual_placement.get(e.post_position)
            if plac:
                plac_color = {1: "#fbbf24", 2: "#9ca3af", 3: "#cd7f32"}.get(plac, "#64748b")
                result_cell = f'<td class="result-cell"><span class="result-plac" style="color:{plac_color}">{plac}</span></td>'
            else:
                result_cell = '<td class="result-cell" style="color:#6b7280">-</td>'

        # Proffs cell
        proffs_cell = ""
        if proffs_horses:
            ph = proffs_horses.get(e.post_position)
            if ph and ph.get("proffs_weighted_pct", 0) > 0:
                pp = ph["proffs_weighted_pct"]
                edge = ph.get("edge_pp", 0)
                edge_color = "#22c55e" if edge > 10 else "#f59e0b" if edge > 0 else "#ef4444"
                proffs_cell = (
                    f'<td class="proffs-cell">'
                    f'{pp:.0f}%'
                    f'<span style="color:{edge_color};font-size:0.7em;margin-left:3px">'
                    f'{edge:+.0f}'
                    f'</span>'
                    f'</td>'
                )
            else:
                proffs_cell = '<td class="proffs-cell" style="color:#6b7280">-</td>'

        rows.append(
            f'<tr class="horse-row{toggle_class}" data-horse="{e.post_position}">'
            f'<td class="pos">{e.post_position}</td>'
            f'<td class="horse-name">{toggle_icon}{_esc(e.horse.name)}{value_badge}{winbet_badge}</td>'
            f'<td class="score"><strong>{e.super_score:.0f}</strong></td>'
            f'<td class="bet">{bet_str}</td>'
            f'{proffs_cell}'
            f'<td><span class="rec-badge" style="background:{bg};color:{color}">{_rank_label(rec)}</span></td>'
            f'{result_cell}'
            f'<td class="driver">{_esc(e.driver_name)}</td>'
            f'<td class="trend-cell">{trend_cell}{sparkline}</td>'
            f'{factor_cells}'
            f'</tr>'
        )
        # Add expandable detail row
        rows.append(_horse_detail_html(e, race))

    factor_headers = "".join(
        f"<th>{lbl}</th>" for lbl in FACTOR_LABELS.values()
    )

    result_header = "<th>Res</th>" if is_finished else ""

    vp_html = ""
    if value_picks:
        vp_items = ", ".join(
            f"<strong>{e.post_position} {_esc(e.horse.name)}</strong> "
            f"({e.super_score:.0f}p / {e.bet_percentage:.0%})"
            for e in value_picks
        )
        vp_html = f'<div class="value-picks">Spelvarda: {vp_items}</div>'

    stair_badge = ' <span class="stair-badge">🔄 Spårtrappa</span>' if race.is_stair_draw else ""
    race_name_html = f'<div class="race-name-label">{_esc(race.race_name)}</div>' if race.race_name else ""

    # Skrällrisk-badge + garderingsrekommendation
    upset_badge = ""
    upset_advice = ""
    race_card_class = ""
    if race.upset_risk >= 50:
        upset_badge = f' <span class="upset-badge high pulse">⚡ Skrällrisk {race.upset_risk:.0f}%</span>'
        upset_advice = '<div class="upset-advice high">🔴 Gardera brett — minst 4-5 val rekommenderas</div>'
        race_card_class = " upset-high"
    elif race.upset_risk >= 25:
        upset_badge = f' <span class="upset-badge medium">⚡ Skrällrisk {race.upset_risk:.0f}%</span>'
        upset_advice = '<div class="upset-advice medium">🟡 Gardera — 3-4 val rekommenderas</div>'
        race_card_class = " upset-medium"
    else:
        upset_advice = '<div class="upset-advice low">🟢 Spiklopp — starkt förstaval</div>'

    # Skrällkandidater
    upset_cand_html = ""
    if race.upset_candidates:
        cand_names = []
        for num in race.upset_candidates:
            for e in race.active_entries:
                if e.post_position == num:
                    cand_names.append(f"{num} {_esc(e.horse.name[:15])}")
                    break
        if cand_names:
            upset_cand_html = (
                f'<div class="upset-candidates">'
                f'🎯 Skrällkandidater: {", ".join(cand_names)} '
                f'<span class="upset-desc">(stark banaprofil, underskattad)</span>'
                f'</div>'
            )

    # Race classification
    if race.upset_risk >= 50:
        class_badge = '<span class="race-class-badge skrallopp">Skrällopp</span>'
    elif race.upset_risk >= 25:
        class_badge = '<span class="race-class-badge oppet">Öppet lopp</span>'
    else:
        class_badge = '<span class="race-class-badge spiklopp">Spiklopp</span>'

    return (
        f'<div class="race-card{race_card_class}" id="race-{race.race_number}">'
        f'<div class="race-header">'
        f'<h2>Avd {race.race_number}{stair_badge}{class_badge}{upset_badge}</h2>'
        f'{race_name_html}'
        f'<div class="race-meta">'
        f'{race.distance}m {race.start_method.value}start &middot; '
        f'{race.purse:,} kr &middot; '
        f'{race.num_starters} startande &middot; '
        f'{race.breed.value}'
        f'</div>'
        f'{upset_advice}'
        f'{upset_cand_html}'
        f'</div>'
        f'{vp_html}'
        f'<div class="table-wrap">'
        f'<table>'
        f'<thead><tr>'
        f'<th>#</th><th>Hast</th><th>Poang</th><th>Streck</th>'
        f'{"<th>Proffs</th>" if proffs_horses else ""}'
        f'<th>Rank</th>{result_header}<th>Kusk</th><th>Trend</th>{factor_headers}'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'</div>'
        f'</div>'
    )


def _summary_html(game_round: GameRound) -> str:
    """Visual race-card grid for overview, with key info per race."""
    is_round_finished = game_round.is_finished
    gt = _esc(game_round.game_type)
    cards = []
    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        if not sorted_entries:
            continue

        top = sorted_entries[0]
        gardering = [
            e for e in sorted_entries[1:]
            if e.recommendation in ("2-val", "3-val", "gardering")
        ]
        gard_str = ", ".join(
            f"{e.post_position} {_esc(e.horse.name[:10])}"
            for e in gardering[:3]
        )
        bet_pct = top.bet_percentage * 100 if top.bet_percentage else 0
        bet_str = f"{bet_pct:.0f}%" if top.bet_percentage else "-"
        bg = _rec_bg(top.recommendation)
        color = _rec_color(top.recommendation)

        # Risk
        risk_pct = race.upset_risk
        if risk_pct >= 50:
            risk_color = "#ef4444"
            risk_bg = "rgba(239,68,68,0.08)"
            risk_cls = "high"
        elif risk_pct >= 25:
            risk_color = "#eab308"
            risk_bg = "rgba(234,179,8,0.06)"
            risk_cls = "medium"
        else:
            risk_color = "#22c55e"
            risk_bg = "rgba(34,197,94,0.06)"
            risk_cls = "low"

        # Driver name
        driver = _esc(top.driver_name[:18]) if top.driver_name else ""

        # Result badge for finished races
        result_badge = ""
        if is_round_finished and race.result_order:
            winner_num = race.result_order[0]
            winner_entry = next(
                (e for e in sorted_entries if e.post_position == winner_num), None
            )
            if winner_entry:
                rec_w = winner_entry.recommendation
                if rec_w == "spik" or (top.recommendation == "spik" and top.post_position == winner_num):
                    result_badge = '<span class="hit-badge" style="font-size:.7rem">&#10003;</span>'  # A-rank hit
                elif rec_w in ("2-val", "3-val", "gardering"):
                    result_badge = '<span class="partial-badge" style="font-size:.7rem">~</span>'  # B/C-rank
                else:
                    result_badge = '<span class="miss-badge" style="font-size:.7rem">&#10007;</span>'

        # Chansspik vinnarspel candidates in this race
        winbet_candidates = []
        for rank_idx, entry in enumerate(sorted_entries):
            if rank_idx >= 2:
                break
            if (entry.bet_percentage is not None
                    and 0.05 <= entry.bet_percentage <= 0.20):
                winbet_candidates.append(entry)
        winbet_orc_badge = ""
        if winbet_candidates:
            names = ", ".join(f"{e.post_position} {_esc(e.horse.name[:10])}" for e in winbet_candidates)
            winbet_orc_badge = f'<div class="orc-winbet">🎰 {names}</div>'

        # Race info line
        dist_str = f"{race.distance}m"
        method_str = race.start_method.value[0].upper()
        breed_str = race.breed.value

        cards.append(
            f'<div class="overview-race-card" onclick="showDivision({race.race_number})">'
            f'<div class="orc-top">'
            f'<span class="orc-num">{race.race_number}</span>'
            f'<span class="orc-label">{gt}-{race.race_number}</span>'
            f'<span class="orc-risk" style="color:{risk_color};background:{risk_bg}">{risk_pct:.0f}%</span>'
            f'</div>'
            f'<div class="orc-meta">{dist_str} {method_str} &middot; {race.num_starters} st &middot; {breed_str}</div>'
            f'<div class="orc-pick">'
            f'<div class="orc-pick-main">'
            f'<strong>{top.post_position} {_esc(top.horse.name[:16])}</strong>'
            f' <span class="rec-badge" style="background:{bg};color:{color};font-size:.65rem;padding:.15rem .5rem">{_rank_label(top.recommendation)}</span>'
            f'{result_badge}'
            f'</div>'
            f'<div class="orc-driver">{driver}</div>'
            f'</div>'
            f'<div class="orc-stats">'
            f'<div class="orc-stat">'
            f'<span class="orc-stat-val">{top.super_score:.0f}</span>'
            f'<span class="orc-stat-lbl">Poang</span>'
            f'</div>'
            f'<div class="orc-stat">'
            f'<span class="orc-stat-val">{bet_str}</span>'
            f'<span class="orc-stat-lbl">Streck</span>'
            f'</div>'
            f'</div>'
            f'<div class="orc-gard">{gard_str if gard_str else "&mdash;"}</div>'
            f'{winbet_orc_badge}'
            f'</div>'
        )

    return f'<div class="overview-race-grid">{"".join(cards)}</div>'


def _vinnarspel_summary_html(game_round: GameRound) -> str:
    """Render a summary card showing all chansspik vinnarspel candidates.

    Criteria: model rank ≤ 2, streck 5-20%.
    Historical ROI: +47.8% on flat bet (walk-forward validated across all periods).
    """
    candidates = []
    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        for rank_idx, e in enumerate(sorted_entries):
            model_rank = rank_idx + 1
            if model_rank > 2:
                break
            if (
                e.bet_percentage is not None
                and 0.05 <= e.bet_percentage <= 0.20
            ):
                odds_est = round(1 / e.bet_percentage, 1) if e.bet_percentage > 0 else 0
                candidates.append({
                    "race_num": race.race_number,
                    "name": e.horse.name,
                    "post": e.post_position,
                    "score": e.super_score,
                    "streck": e.bet_percentage,
                    "rank": model_rank,
                    "odds_est": odds_est,
                    "driver": e.driver_name or "",
                })

    if not candidates:
        return ""

    rows = []
    for c in candidates:
        streck_pct = c["streck"] * 100
        rank_badge = "🥇" if c["rank"] == 1 else "🥈"
        rows.append(
            f'<tr class="winbet-row" onclick="showDivision({c["race_num"]})" style="cursor:pointer">'
            f'<td><strong>Avd {c["race_num"]}</strong></td>'
            f'<td>{rank_badge} {c["post"]} {_esc(c["name"][:20])}</td>'
            f'<td><strong>{c["score"]:.0f}</strong></td>'
            f'<td>{streck_pct:.0f}%</td>'
            f'<td>~{c["odds_est"]:.1f}x</td>'
            f'<td class="winbet-driver">{_esc(c["driver"][:15])}</td>'
            f'</tr>'
        )

    count = len(candidates)
    roi_text = "+14% ROI (bas) · +44% ROI (sharp)"

    return (
        f'<div class="winbet-summary-card">'
        f'<div class="winbet-header">'
        f'<span class="winbet-title">🎰 Vinnarspel — Chansspik</span>'
        f'<span class="winbet-roi">{roi_text}</span>'
        f'</div>'
        f'<div class="winbet-desc">'
        f'{count} kandidater denna omgång — modell topp-2, streck 5-20%'
        f'</div>'
        f'<table class="winbet-table">'
        f'<thead><tr><th>Lopp</th><th>Häst</th><th>Poäng</th><th>Streck</th><th>Odds</th><th>Kusk</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'<div class="winbet-footnote">Flat bet 500kr/st · Bas: +31 129kr (438 spel) · Sharp (kusk≥100, score≥45): +41 299kr (190 spel). 120 omgångar.</div>'
        f'</div>'
    )


def _risk_summary_bar(game_round: GameRound) -> str:
    """Render heatmap-bar showing upset risk per race as colored segments."""
    high_count = sum(1 for r in game_round.races if r.upset_risk >= 50)
    medium_count = sum(1 for r in game_round.races if 25 <= r.upset_risk < 50)
    total = len(game_round.races)

    risk_count = high_count + medium_count
    if risk_count == 0:
        summary_text = f"Alla {total} lopp ser stabila ut — spikvänligt!"
    else:
        parts = []
        if high_count:
            parts.append(f"{high_count} hög risk")
        if medium_count:
            parts.append(f"{medium_count} medel")
        summary_text = f"{', '.join(parts)} av {total} lopp — gardera dessa!"

    segments = []
    labels = []
    for race in game_round.races:
        risk = race.upset_risk
        # Color: green → yellow → red
        if risk >= 50:
            color = "#ef4444"
        elif risk >= 35:
            color = "#f97316"
        elif risk >= 25:
            color = "#eab308"
        elif risk >= 15:
            color = "#84cc16"
        else:
            color = "#22c55e"
        # Height proportional to risk (min 8px, max 48px)
        h = max(8, min(48, int(risk * 0.48 + 8)))
        segments.append(
            f'<div class="heatmap-seg" style="height:{h}px;background:{color}" '
            f'onclick="showSection(\'race-{race.race_number}\')" title="Avd {race.race_number}: {risk:.0f}%"></div>'
        )
        labels.append(f'<div class="heatmap-lbl">{race.race_number}</div>')

    return (
        f'<div class="heatmap-wrapper">'
        f'<div class="heatmap-title">⚡ Skrällkarta</div>'
        f'<div class="heatmap-bar">{"".join(segments)}</div>'
        f'<div class="heatmap-labels">{"".join(labels)}</div>'
        f'<div class="heatmap-summary">{summary_text}</div>'
        f'</div>'
    )


def _sidebar_html(game_round: GameRound, has_system: bool = False, has_backlog: bool = False, has_stats: bool = False) -> str:
    """Vertical sidebar with brand, race info, nav items, division links, and premium card."""
    # Division links with game type labels (e.g. V85-1) and risk dots
    gt = _esc(game_round.game_type)  # e.g. "V85"
    div_items = []
    for race in game_round.races:
        risk_cls = "high" if race.upset_risk >= 50 else ("medium" if race.upset_risk >= 25 else "low")
        risk_color = "#ef4444" if risk_cls == "high" else ("#eab308" if risk_cls == "medium" else "#22c55e")
        div_items.append(
            f'<button class="div-item" data-div="{race.race_number}" '
            f'onclick="showDivision({race.race_number})">'
            f'<span class="div-num">{race.race_number}</span>'
            f'<span class="sb-text">{gt}-{race.race_number}</span>'
            f'<span class="div-dot sb-text" style="background:{risk_color}"></span>'
            f'</button>'
        )

    # Nav items
    nav_overview = (
        '<button class="nav-item active" data-section="summary" onclick="showSection(\'summary\')">'
        '<span class="nav-icon">'
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<circle cx="12" cy="12" r="10"/><path d="M12 8v8m-4-4h8"/></svg>'
        '</span><span class="sb-text">Översikt</span></button>'
    )
    nav_system = (
        '<button class="nav-item" data-section="system" onclick="openDrawer()">'
        '<span class="nav-icon">'
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/></svg>'
        '</span><span class="sb-text">System</span></button>'
    ) if has_system else ''
    nav_stats = (
        '<button class="nav-item" data-section="stats" onclick="showSection(\'stats\')">'
        '<span class="nav-icon">'
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M18 20V10M12 20V4M6 20v-6"/></svg>'
        '</span><span class="sb-text">Statistik</span></button>'
    ) if has_stats else ''
    nav_backlog = (
        '<button class="nav-item" data-section="backlog" onclick="showSection(\'backlog\')">'
        '<span class="nav-icon">'
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>'
        '</span><span class="sb-text">Backlog</span></button>'
    ) if has_backlog else ''

    track_name = _esc(game_round.track_name or "")
    date_str = str(game_round.round_date) if game_round.round_date else ""

    return (
        '<nav class="sidebar" id="sidebar">'
        # Brand
        '<div class="sb-brand" onclick="toggleSidebar()">'
        '<div class="sb-icon">👑</div>'
        '<div class="sb-text">'
        '<div class="sb-title">Kungens Trav</div>'
        '<div class="sb-sub">AI-driven analys</div>'
        '</div>'
        '</div>'
        # Race card
        f'<div class="sb-race sb-text">'
        f'<div class="sb-race-type">{_esc(game_round.game_type)}</div>'
        f'<div class="sb-race-meta">{date_str} · {track_name}</div>'
        f'</div>'
        # Nav
        '<div class="sb-nav">'
        f'{nav_overview}'
        f'{nav_system}'
        f'{nav_stats}'
        f'{nav_backlog}'
        '<div class="nav-divider"></div>'
        '<div class="nav-label sb-text">AVDELNINGAR</div>'
        + "".join(div_items) +
        '</div>'
        # Premium card
        '<div class="sb-premium sb-text">'
        '<div class="sb-premium-card">'
        '<div style="font-size:12px;font-weight:700;color:#f59e0b">⭐ Premium</div>'
        '<div style="font-size:11px;color:#6b7280;margin-top:4px">Lås upp alla avdelningar och system</div>'
        '<button class="sb-premium-btn">Uppgradera</button>'
        '</div>'
        '</div>'
        '</nav>'
    )


def _bet_view_html(game_round: GameRound) -> str:
    """Build the Bet recommendations view with multiple vinnarspel profiles.

    Profiles (120 omgångar, jun 2025 → maj 2026, 438 kandidater):
    1. Bas (rank≤2, 5-20%) — +14% ROI, 438 spel, 17.6% vinst
    2. Pro (rank≤2, kusk≥100 st/år, 5-20%) — +27% ROI, 306 spel, 19.6% vinst
    3. Sharp (rank≤2, score≥45, kusk≥100, 5-20%) — +44% ROI, 190 spel, 24.2% vinst
    4. Sniper (rank≤2, kusk≥100, 3-10%) — +56% ROI, 31 spel, 12.9% vinst
    5. Elite (score≥P90, 5-20%) — +29% ROI, 44 spel, 22.7% vinst
    """
    gt = _esc(game_round.game_type)
    date_str = str(game_round.round_date) if game_round.round_date else ""
    is_finished = game_round.is_finished

    # Collect ALL potential candidates with full metadata
    all_candidates = []
    # Compute P90 threshold across all entries in round
    all_scores = []
    for race in game_round.races:
        for e in race.active_entries:
            if e.bet_percentage is not None and 0.03 <= e.bet_percentage <= 0.25:
                all_scores.append(e.super_score)
    all_scores.sort()
    p90_threshold = all_scores[int(len(all_scores) * 0.90)] if len(all_scores) >= 10 else 999

    for race in game_round.races:
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        for rank_idx, e in enumerate(sorted_entries):
            model_rank = rank_idx + 1
            if model_rank > 3:
                break
            if e.bet_percentage is None:
                continue
            streck = e.bet_percentage
            if not (0.03 <= streck <= 0.25):
                continue

            odds_est = round(1 / streck, 1) if streck > 0 else 0

            # Check result if finished
            won = False
            actual_odds = 0.0
            placement = "-"
            if is_finished and race.result_order:
                plac = 0
                for pos, num in enumerate(race.result_order, 1):
                    if num == e.post_position:
                        plac = pos
                        break
                if plac > 0:
                    placement = str(plac)
                won = plac == 1
                if won:
                    actual_odds = odds_est

            all_candidates.append({
                "race_num": race.race_number,
                "name": e.horse.name,
                "post": e.post_position,
                "score": e.super_score,
                "streck": streck,
                "rank": model_rank,
                "odds_est": odds_est,
                "driver": e.driver_name or "",
                "won": won,
                "placement": placement,
                "actual_odds": actual_odds,
                "is_p90": e.super_score >= p90_threshold,
                "driver_starts": getattr(e, "driver_starts_year", 0),
                "driver_win_pct": getattr(e, "driver_win_pct", 0.0),
            })

    # Define profiles — ordered by selectivity (broadest → most selective)
    # Backtest: 120 omgångar, juni 2025 → maj 2026, 438 kandidater
    PROFILES = [
        {
            "id": "bas",
            "name": "Bas",
            "desc": "Rank A-B + streck 5-20%",
            "badge_roi": "+14%",
            "badge_color": "#6b7280",
            "bt_wins": "77/438",
            "bt_winrate": "17.6%",
            "bt_odds": "7.0x",
            "bt_profit": "+31 129kr",
            "filter": lambda c: c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20,
        },
        {
            "id": "pro",
            "name": "Pro",
            "desc": "Rank A-B + kusk ≥100 st/år + 5-20%",
            "badge_roi": "+27%",
            "badge_color": "#7c3aed",
            "bt_wins": "60/306",
            "bt_winrate": "19.6%",
            "bt_odds": "7.0x",
            "bt_profit": "+41 498kr",
            "filter": lambda c: (c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20
                                 and c["driver_starts"] >= 100),
        },
        {
            "id": "sharp",
            "name": "Sharp",
            "desc": "Score ≥45 + kusk ≥100 st/år + 5-20%",
            "badge_roi": "+44%",
            "badge_color": "#15803d",
            "bt_wins": "46/190",
            "bt_winrate": "24.2%",
            "bt_odds": "6.0x",
            "bt_profit": "+41 299kr",
            "filter": lambda c: (c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20
                                 and c["driver_starts"] >= 100
                                 and c["score"] >= 45),
        },
        {
            "id": "sniper",
            "name": "Sniper",
            "desc": "Rank A-B + kusk ≥100 st/år + 3-10%",
            "badge_roi": "+56%",
            "badge_color": "#b45309",
            "bt_wins": "4/31",
            "bt_winrate": "12.9%",
            "bt_odds": "12.8x",
            "bt_profit": "+8 696kr",
            "filter": lambda c: (c["rank"] <= 2 and 0.03 <= c["streck"] <= 0.10
                                 and c["driver_starts"] >= 100),
        },
        {
            "id": "elite",
            "name": "Elite",
            "desc": "Score topp-10% + streck 5-20%",
            "badge_roi": "+29%",
            "badge_color": "#0369a1",
            "bt_wins": "10/44",
            "bt_winrate": "22.7%",
            "bt_odds": "5.6x",
            "bt_profit": "+6 409kr",
            "filter": lambda c: c["is_p90"] and 0.05 <= c["streck"] <= 0.20,
        },
    ]

    def _build_profile_card(profile: dict, candidates: list) -> str:
        """Build one profile card with candidates and P&L."""
        pid = profile["id"]
        filtered = [c for c in candidates if profile["filter"](c)]
        count = len(filtered)

        # Compute P&L
        total_bet = count * 500
        total_return = sum(500 * c["actual_odds"] for c in filtered if c["won"])
        wins = sum(1 for c in filtered if c["won"])

        # Strategy header (always shown)
        header = (
            f'<div class="bet-profile-header" data-profile="{pid}">'
            f'<div class="bet-profile-left">'
            f'<span class="bet-profile-badge" style="background:{profile["badge_color"]}">'
            f'{profile["badge_roi"]} ROI</span>'
            f'<span class="bet-profile-name">{profile["name"]}</span>'
            f'<span class="bet-profile-desc">{profile["desc"]}</span>'
            f'</div>'
            f'<div class="bet-profile-right">'
            f'<span class="bet-profile-bt">{profile["bt_wins"]} vinster'
            f' &middot; {profile["bt_odds"]} odds'
            f' &middot; {profile["bt_profit"]}</span>'
            f'</div>'
            f'</div>'
        )

        if count == 0:
            return (
                f'<div class="bet-profile-card" id="profile-{pid}">'
                f'{header}'
                f'<div class="bet-profile-empty">Inga kandidater denna omgång</div>'
                f'</div>'
            )

        # Status
        if is_finished:
            pnl = total_return - total_bet
            roi = (pnl / total_bet * 100) if total_bet > 0 else 0
            pnl_color = "#15803d" if pnl >= 0 else "#dc2626"
            pnl_sign = "+" if pnl >= 0 else ""
            status_html = (
                f'<div class="bet-profile-pnl">'
                f'<span>{count} spel</span>'
                f'<span>{wins} vinst</span>'
                f'<span style="color:{pnl_color};font-weight:700">'
                f'{pnl_sign}{pnl:,.0f}kr ({pnl_sign}{roi:.0f}%)</span>'
                f'</div>'
            )
        else:
            status_html = (
                f'<div class="bet-profile-pnl">'
                f'<span>{count} spel &middot; Insats: {total_bet:,}kr</span>'
                f'</div>'
            )

        # Candidate rows
        rows = []
        for c in filtered:
            rank_badge = "\U0001f947" if c["rank"] == 1 else "\U0001f948"
            streck_pct = c["streck"] * 100
            driver_warn = ""
            if c.get("driver_starts", 0) < 100:
                driver_warn = ' <span class="bet-driver-warn" title="Under 100 starter/år">⚠</span>'

            if is_finished:
                if c["won"]:
                    win_amt = 500 * c["actual_odds"]
                    result_cell = f'<td class="bet-result bet-win">✅ +{win_amt - 500:,.0f}kr</td>'
                    row_class = " bet-row-win"
                else:
                    result_cell = f'<td class="bet-result bet-loss">❌ {c["placement"]}</td>'
                    row_class = " bet-row-loss"
            else:
                result_cell = '<td class="bet-result bet-pending">⏳</td>'
                row_class = ""

            rows.append(
                f'<tr class="bet-candidate-row{row_class}" onclick="showDivision({c["race_num"]})" style="cursor:pointer">'
                f'<td class="bet-race">Avd {c["race_num"]}</td>'
                f'<td class="bet-horse">{rank_badge} {c["post"]} {_esc(c["name"][:22])}</td>'
                f'<td class="bet-score">{c["score"]:.0f}</td>'
                f'<td class="bet-streck">{streck_pct:.0f}%</td>'
                f'<td class="bet-odds">~{c["odds_est"]:.1f}x</td>'
                f'<td class="bet-driver">{_esc(c["driver"][:15])}{driver_warn}</td>'
                f'{result_cell}'
                f'</tr>'
            )

        return (
            f'<div class="bet-profile-card" id="profile-{pid}">'
            f'{header}'
            f'{status_html}'
            f'<div class="table-wrap">'
            f'<table class="bet-table">'
            f'<thead><tr>'
            f'<th>Lopp</th><th>Häst</th><th>Poäng</th><th>Streck</th>'
            f'<th>Odds</th><th>Kusk</th><th>Resultat</th>'
            f'</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            f'</table>'
            f'</div>'
            f'</div>'
        )

    # Build all profile cards
    profile_cards = "\n".join(_build_profile_card(p, all_candidates) for p in PROFILES)

    # Status banner
    if is_finished:
        status_icon = "✅"
        status_text = "Avslutad"
    else:
        status_icon = "\U0001f534"
        status_text = "Live — spela innan start!"

    round_header = (
        f'<div class="bet-round-header-top">'
        f'<span>{status_icon}</span>'
        f'<h2>{gt} — {date_str}</h2>'
        f'<span class="bet-status-badge {"finished" if is_finished else "live"}">{status_text}</span>'
        f'</div>'
    )

    # Historical tracking section (loaded via JS from /api/bets)
    history_section = (
        '<div class="bet-history-section" id="bet-history">'
        '<div class="bet-history-header">'
        '<h3>Historisk P&L</h3>'
        '<div class="bet-period-tabs">'
        '<button class="bet-period-btn active" onclick="setBetPeriod(\'month\')">Månad</button>'
        '<button class="bet-period-btn" onclick="setBetPeriod(\'week\')">Vecka</button>'
        '<button class="bet-period-btn" onclick="setBetPeriod(\'day\')">Dag</button>'
        '</div>'
        '</div>'
        '<div id="bet-history-content">'
        '<div class="bet-loading">Laddar historik...</div>'
        '</div>'
        '</div>'
    )

    return f'{round_header}\n{profile_cards}\n{history_section}'


def _round_dropdown_html(
    current_key: str,
    available_rounds: list[tuple[str, str, str, bool, str]] | None = None,
) -> str:
    """Render round dropdown <select> for top-bar.

    available_rounds: list of (key, game_type, date_str, is_finished, track_name)
    """
    if not available_rounds or len(available_rounds) < 2:
        return ""
    options = []
    for key, gt, d_str, is_finished, track in sorted(available_rounds, key=lambda r: r[2], reverse=True):
        sel = " selected" if key == current_key else ""
        status = "\u2713" if is_finished else "\u23F3"
        track_str = f" {track}" if track else ""
        options.append(f'<option value="{key}"{sel}>{gt} \u2014 {d_str}{track_str} {status}</option>')
    return '<select class="round-select" onchange="changeRound(this)">' + "".join(options) + '</select>'


def _accuracy_html(game_round: GameRound) -> str:
    """Render model accuracy card for finished rounds."""
    if not game_round.is_finished:
        return ""

    total_races = 0
    spik_attempts = 0
    spik_wins = 0
    top1_in_top3 = 0
    covered_winners = 0
    top3_overlaps = []
    surprises = []

    for race in game_round.races:
        if not race.result_order:
            continue
        total_races += 1
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        if not sorted_entries:
            continue

        winner_num = race.result_order[0]
        actual_top3 = set(race.result_order[:3])
        our_top3_nums = {e.post_position for e in sorted_entries[:3]}

        # A-rank check (former "spik")
        spik_entries = [e for e in sorted_entries if e.recommendation == "spik"]
        if spik_entries:
            spik_attempts += 1
            if spik_entries[0].post_position == winner_num:
                spik_wins += 1

        # Top 1 in actual top 3
        if sorted_entries[0].post_position in actual_top3:
            top1_in_top3 += 1

        # Coverage: winner not recommended as strykning
        winner_entry = next(
            (e for e in sorted_entries if e.post_position == winner_num), None
        )
        if winner_entry and winner_entry.recommendation != "strykning":  # Not D-rank
            covered_winners += 1

        # Top 3 overlap
        overlap = len(our_top3_nums & actual_top3)
        top3_overlaps.append(overlap)

        # Surprise: winner ranked > 3
        if winner_entry and winner_entry.rank and winner_entry.rank > 3:
            surprises.append(
                (race.race_number, winner_num, _esc(winner_entry.horse.name), winner_entry.rank)
            )

    if total_races == 0:
        return ""

    avg_overlap = sum(top3_overlaps) / len(top3_overlaps) if top3_overlaps else 0

    # Build stats
    spik_pct = f"{spik_wins / spik_attempts:.0%}" if spik_attempts else "-"
    top1_pct = f"{top1_in_top3 / total_races:.0%}"
    cover_pct = f"{covered_winners / total_races:.0%}"

    surprise_html = ""
    if surprises:
        items = ", ".join(
            f"Avd {rn} ({num} {name}, rank {rank})"
            for rn, num, name, rank in surprises
        )
        surprise_html = (
            f'<div class="accuracy-surprises">'
            f'<strong>Skrällar:</strong> {items}'
            f"</div>"
        )

    return (
        f'<div class="accuracy-card">'
        f'<h2>Modellprecision — {game_round.round_date}</h2>'
        f'<div class="accuracy-grid">'
        f'<div class="accuracy-stat">'
        f'<div class="big-num">{spik_wins}/{spik_attempts}</div>'
        f'<div class="label">A-rank rätt</div>'
        f'<div class="sub">{spik_pct}</div></div>'
        f'<div class="accuracy-stat">'
        f'<div class="big-num">{top1_in_top3}/{total_races}</div>'
        f'<div class="label">Förstaval topp-3</div>'
        f'<div class="sub">{top1_pct}</div></div>'
        f'<div class="accuracy-stat">'
        f'<div class="big-num">{covered_winners}/{total_races}</div>'
        f'<div class="label">Täckningsgrad</div>'
        f'<div class="sub">{cover_pct}</div></div>'
        f'<div class="accuracy-stat">'
        f'<div class="big-num">{avg_overlap:.1f}/3</div>'
        f'<div class="label">Topp-3 overlap</div>'
        f'<div class="sub">snitt</div></div>'
        f'</div>'
        f'{surprise_html}'
        f'</div>'
    )


def _system_html(game_round: GameRound, proffs_data: dict | None = None) -> str:
    """Generera system-rekommendation baserat på SystemGenerator."""
    try:
        from ..betting.system_generator import SystemGenerator
    except ImportError:
        return ""

    # (strategy, filter, budget, max_spikes, spike_conf, spike_gap, label, desc)
    strategies = [
        ("I_streck_1st", "avg_upset_lt_40", 2500, 0, 0, 0, "I_streck_1st", "Bäst netto — utan spik"),
        ("I_streck_1st", "avg_upset_lt_40", 2500, 1, 75, 12, "I_streck + 1 spik", "Med 1 spik (conf≥75, gap≥12)"),
        ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500, 0, 0, 0, "Q_dom_x_mktgap", "Bäst ROI"),
        ("D_market_gap", "avg_upset_lt_40", 2500, 0, 0, 0, "D_market_gap", "Mest hits"),
    ]

    systems_html = []
    for strategy, filt, budget, max_spikes, spike_conf, spike_gap, label, desc in strategies:
        gen = SystemGenerator(
            budget=budget, strategy=strategy, selective_filter=filt,
            max_spikes=max_spikes, spike_conf_threshold=spike_conf, spike_score_gap=spike_gap,
        )
        system = gen.generate(game_round)

        if system.skip_round:
            systems_html.append(
                f'<div class="system-card skipped">'
                f'<div class="system-header">'
                f'<h3>⏭️ {label}</h3>'
                f'<span class="system-meta">{desc}</span>'
                f'</div>'
                f'<div class="skip-reason">{_esc(system.skip_reason)}</div>'
                f'</div>'
            )
            continue

        # Bygg pick-rader
        pick_rows = []
        for rp in system.race_picks:
            conf_cls = "high" if rp.confidence >= 60 else ("medium" if rp.confidence >= 30 else "low")
            upset_icon = "🟢" if rp.upset_risk < 25 else ("🟡" if rp.upset_risk < 50 else "🔴")

            pick_nums = ", ".join(
                f"<strong>{n}</strong> {name[:12]}"
                for n, name in zip(rp.picks, rp.pick_names)
            )

            spik_badge = ' <span class="spik-badge">🔒 A</span>' if rp.num_picks == 1 else ''

            pick_rows.append(
                f'<tr>'
                f'<td class="race-link">Avd {rp.race_number}{spik_badge}</td>'
                f'<td>{rp.distance}m {rp.start_method}</td>'
                f'<td><span class="conf-badge {conf_cls}">{rp.confidence:.0f}</span></td>'
                f'<td>{upset_icon} {rp.upset_risk:.0f}%</td>'
                f'<td class="system-picks">{pick_nums}</td>'
                f'<td>{rp.num_picks}</td>'
                f'<td>{rp.combined_streck:.0%}</td>'
                f'</tr>'
            )

        rows_str = "".join(pick_rows)
        cost_color = "#22c55e" if system.total_cost <= budget else "#ef4444"

        systems_html.append(
            f'<div class="system-card">'
            f'<div class="system-header">'
            f'<h3>💰 {label.replace("_", " ")}</h3>'
            f'<span class="system-meta">{desc}</span>'
            f'</div>'
            f'<div class="system-stats">'
            f'<div class="sys-stat"><span class="sys-val">{system.total_rows:,}</span><span class="sys-lbl">Rader</span></div>'
            f'<div class="sys-stat"><span class="sys-val" style="color:{cost_color}">{system.total_cost:,.0f} kr</span><span class="sys-lbl">Kostnad</span></div>'
            f'<div class="sys-stat"><span class="sys-val">{system.avg_confidence:.0f}</span><span class="sys-lbl">Konfidens</span></div>'
            f'<div class="sys-stat"><span class="sys-val">{system.avg_upset_risk:.0f}</span><span class="sys-lbl">Skrällrisk</span></div>'
            f'</div>'
            f'<div class="table-wrap">'
            f'<table class="system-table">'
            f'<thead><tr>'
            f'<th>Lopp</th><th>Info</th><th>Konf</th><th>Risk</th><th>Picks</th><th>#</th><th>Täckn</th>'
            f'</tr></thead>'
            f'<tbody>{rows_str}</tbody>'
            f'</table></div>'
            f'</div>'
        )

    # ── Dennis-method system builder ──
    dennis_html = _dennis_system_html(game_round, proffs_data=proffs_data)

    return (
        f'<div id="system" class="summary-card system-section">'
        f'<h2>💰 System — Budget 2,500 kr</h2>'
        f'<p style="color:#6b7280;font-size:.85rem;margin-bottom:1rem">'
        f'4 strategier backtestade på 266 omgångar (5 år). '
        f'Inkl. spik-variant med 1 spik vid hög konfidens.</p>'
        f'{dennis_html}'
        f'{"".join(systems_html)}'
        f'</div>'
    )


def _dennis_system_html(game_round: GameRound, proffs_data: dict | None = None) -> str:
    """System-rekommendationer — Spike Easiest (säker) + Chansspik (25-100k)."""
    try:
        from ..analysis.system_builder import build_system
    except ImportError:
        return ""

    # ── CHANSSPIK-system (Dennis mål: 25-100k utdelning) ──
    chansspik_cards = []
    try:
        from ..analysis.upset_system import build_upset_system, analyze_upset_round

        # Upset analysis
        upset_analysis = analyze_upset_round(game_round)

        for budget in [300, 500]:
            plan = build_system(game_round, budget=budget, strategy="chansspik")

            pick_rows = []
            for leg in sorted(plan.legs, key=lambda l: l.race_number):
                race = game_round.get_race(leg.race_number)
                dist = race.distance if race else 0
                method = (race.start_method.value if race else "?")[:4]

                is_upset_race = "CHANSSPIK" in leg.reasoning
                is_spike_race = "SPIK-lopp" in leg.reasoning

                if is_upset_race:
                    type_badge = '\U0001f3b2 CHANSSPIK'
                    border_style = 'border-left:3px solid #f59e0b'
                elif is_spike_race:
                    type_badge = '\U0001f512 SPIK'
                    border_style = 'border-left:3px solid #22c55e'
                else:
                    type_badge = f'{leg.num_picks}-val'
                    border_style = ''

                picks_str = ", ".join(
                    f"<strong>{p}</strong>"
                    for p in leg.picks[:leg.num_picks]
                )

                upset_color = "#ef4444" if leg.upset_risk >= 50 else ("#f59e0b" if leg.upset_risk >= 30 else "#22c55e")

                pick_rows.append(
                    f'<tr style="{border_style}">'
                    f'<td class="race-link">Avd {leg.race_number} <small>{type_badge}</small></td>'
                    f'<td>{dist}m {method}</td>'
                    f'<td style="color:{upset_color}">{leg.upset_risk:.0f}</td>'
                    f'<td class="system-picks">{picks_str}</td>'
                    f'<td>{leg.num_picks}</td>'
                    f'</tr>'
                )

            rows_str = "".join(pick_rows)
            cost_color = "#22c55e" if plan.total_cost <= budget else "#ef4444"

            chansspik_cards.append(
                f'<div class="system-card" style="border-left:3px solid #f59e0b">'
                f'<div class="system-header">'
                f'<h3>\U0001f3b2 Chansspik ({budget} kr) — Mål: 25-100k</h3>'
                f'<span class="system-meta">{upset_analysis["expected_payout"]}</span>'
                f'</div>'
                f'<div class="system-stats">'
                f'<div class="sys-stat"><span class="sys-val">{plan.total_rows:,}</span><span class="sys-lbl">Rader</span></div>'
                f'<div class="sys-stat"><span class="sys-val" style="color:{cost_color}">{plan.total_cost:,.0f} kr</span><span class="sys-lbl">Kostnad</span></div>'
                f'<div class="sys-stat"><span class="sys-val">{plan.num_spikes}</span><span class="sys-lbl">Spikar</span></div>'
                f'<div class="sys-stat"><span class="sys-val">{upset_analysis["high_upset_races"]}</span><span class="sys-lbl">Skrällopp</span></div>'
                f'</div>'
                f'<div class="table-wrap">'
                f'<table class="system-table">'
                f'<thead><tr>'
                f'<th>Lopp</th><th>Info</th><th>Skräll</th><th>Picks</th><th>#</th>'
                f'</tr></thead>'
                f'<tbody>{rows_str}</tbody>'
                f'</table></div>'
                f'</div>'
            )
    except Exception:
        pass

    # ── SPIKE EASIEST-system (bevisat +112% ROI) ──
    budgets = [300, 500]
    cards = []

    for budget in budgets:
        plan = build_system(game_round, budget=budget, strategy="optimal", proffs_data=proffs_data)

        pick_rows = []
        for leg in sorted(plan.legs, key=lambda l: l.race_number):
            race = game_round.get_race(leg.race_number)
            dist = race.distance if race else 0
            method = (race.start_method.value if race else "?")[:4]
            conf_cls = "high" if leg.confidence >= 60 else ("medium" if leg.confidence >= 30 else "low")
            upset_icon = "\U0001f7e2" if leg.upset_risk < 25 else ("\U0001f7e1" if leg.upset_risk < 50 else "\U0001f534")

            picks_str = ", ".join(
                f"<strong>{p}</strong>"
                for p in leg.picks[:leg.num_picks]
            )

            type_badge = {
                "spik": '\U0001f512 A',
                "kort": 'B',
                "medel": 'C',
                "bred": f'{leg.num_picks}-val',
            }.get(leg.leg_type, leg.leg_type)

            diff_color = "#22c55e" if leg.difficulty < 25 else ("#f59e0b" if leg.difficulty < 45 else "#ef4444")

            pick_rows.append(
                f'<tr>'
                f'<td class="race-link">Avd {leg.race_number} <small>{type_badge}</small></td>'
                f'<td>{dist}m {method}</td>'
                f'<td><span class="conf-badge {conf_cls}">{leg.confidence:.0f}%</span></td>'
                f'<td style="color:{diff_color}">D{leg.difficulty:.0f}</td>'
                f'<td class="system-picks">{picks_str}</td>'
                f'<td>{leg.num_picks}</td>'
                f'</tr>'
            )

        rows_str = "".join(pick_rows)
        cost_color = "#22c55e" if plan.total_cost <= budget else "#ef4444"
        prob_str = f"{plan.predicted_hit_prob:.1%}" if plan.predicted_hit_prob > 0 else "—"
        is_rec = budget == 300

        cards.append(
            f'<div class="system-card" style="border-left:3px solid {"#22c55e" if is_rec else "#f59e0b"}">'
            f'<div class="system-header">'
            f'<h3>\U0001f3af Greedy Optimal ({budget} kr){"  ★ Rekommenderad" if is_rec else ""}</h3>'
            f'<span class="system-meta">Adaptiv bredd: {plan.num_spikes} spikar, '
            f'{plan.num_short} korta, {plan.num_wide} breda | '
            f'P(alla rätt) ≈ {prob_str}</span>'
            f'</div>'
            f'<div class="system-stats">'
            f'<div class="sys-stat"><span class="sys-val">{plan.total_rows:,}</span><span class="sys-lbl">Rader</span></div>'
            f'<div class="sys-stat"><span class="sys-val" style="color:{cost_color}">{plan.total_cost:,.0f} kr</span><span class="sys-lbl">Kostnad</span></div>'
            f'<div class="sys-stat"><span class="sys-val">{plan.num_spikes}</span><span class="sys-lbl">Spikar</span></div>'
            f'<div class="sys-stat"><span class="sys-val">{prob_str}</span><span class="sys-lbl">P(hit)</span></div>'
            f'</div>'
            f'<div class="table-wrap">'
            f'<table class="system-table">'
            f'<thead><tr>'
            f'<th>Lopp</th><th>Info</th><th>Täckn</th><th>Diff</th><th>Picks</th><th>#</th>'
            f'</tr></thead>'
            f'<tbody>{rows_str}</tbody>'
            f'</table></div>'
            f'</div>'
        )

    return "".join(chansspik_cards) + "".join(cards)


def _stats_html(backlog_data: dict | None = None) -> str:
    """Generera statistik-sektion med strategi-kort, CSS-bar-chart och smart gruppering."""
    if not backlog_data or "strategies" not in backlog_data:
        return ""

    strategies = backlog_data["strategies"]
    strat_names = list(strategies.keys())

    strat_short = {
        "I_streck_1st": "I_streck", "Q_dom_x_mktgap": "Q_dom",
        "D_market_gap": "D_market", "I_streck_spik1": "I_spik1",
        "I_streck_spik2": "I_spik2", "I_streck_spik3": "I_spik3",
    }
    STRAT_COLORS = {
        "I_streck_1st": ("#f59e0b", "rgba(245,166,35,0.10)"),
        "Q_dom_x_mktgap": ("#a78bfa", "rgba(167,139,250,0.10)"),
        "D_market_gap": ("#fb923c", "rgba(251,146,60,0.10)"),
        "I_streck_spik1": ("#fbbf24", "rgba(251,191,36,0.10)"),
        "I_streck_spik2": ("#34d399", "rgba(52,211,153,0.10)"),
        "I_streck_spik3": ("#f472b6", "rgba(244,114,182,0.10)"),
    }
    GT_COLORS_STATS = {
        "V75": "#f59e0b", "V85": "#f59e0b", "GS75": "#a78bfa",
        "V86": "#fb923c", "V64": "#34d399", "V65": "#34d399",
    }

    # Group strategies: main vs variants
    MAIN_STRATS = {"I_streck_1st", "Q_dom_x_mktgap", "D_market_gap"}
    main_names = [s for s in strat_names if s in MAIN_STRATS]
    variant_names = [s for s in strat_names if s not in MAIN_STRATS]

    # ── Build strategy cards ──
    def _strat_card(s: str) -> str:
        ss = strategies[s]
        short = strat_short.get(s, s[:10])
        roi = ss.get("roi", 0)
        netto = ss.get("netto", 0)
        rounds_played = ss.get("rounds_played", 0)
        full_hits = ss.get("full_hits", 0)
        hit_rate = (full_hits / rounds_played * 100) if rounds_played > 0 else 0
        roi_color = "#22c55e" if roi >= 0 else "#ef4444"
        netto_color = "#22c55e" if netto >= 0 else "#ef4444"
        accent, bg = STRAT_COLORS.get(s, ("#64748b", "rgba(100,116,139,0.10)"))
        return (
            f'<div class="sc-card" style="border-top:3px solid {accent}">'
            f'<div class="sc-header">'
            f'<span class="sc-name" style="color:{accent}">{short}</span>'
            f'<span class="sc-rounds">{rounds_played} omg</span>'
            f'</div>'
            f'<div class="sc-roi" style="color:{roi_color}">{roi:+.1f}%</div>'
            f'<div class="sc-label">ROI</div>'
            f'<div class="sc-metrics">'
            f'<div class="sc-metric"><span class="sc-metric-val">{full_hits}</span>'
            f'<span class="sc-metric-lbl">Alla ratt</span></div>'
            f'<div class="sc-metric"><span class="sc-metric-val">{hit_rate:.0f}%</span>'
            f'<span class="sc-metric-lbl">Traffprocent</span></div>'
            f'<div class="sc-metric"><span class="sc-metric-val" style="color:{netto_color}">{netto:+,.0f}</span>'
            f'<span class="sc-metric-lbl">Netto (kr)</span></div>'
            f'</div>'
            f'</div>'
        )

    main_cards = "".join(_strat_card(s) for s in main_names)
    variant_cards = "".join(_strat_card(s) for s in variant_names)

    strategy_cards_html = (
        f'<div class="sc-section">'
        f'<h3 class="sc-group-title">Huvudstrategier</h3>'
        f'<div class="sc-grid">{main_cards}</div>'
    )
    if variant_cards:
        strategy_cards_html += (
            f'<div class="sc-variant-toggle">'
            f'<button class="sc-toggle-btn" data-action="toggle-variants">Visa varianter ({len(variant_names)})</button>'
            f'</div>'
            f'<div class="sc-variants-wrap" style="display:none">'
            f'<h3 class="sc-group-title">Varianter</h3>'
            f'<div class="sc-grid">{variant_cards}</div>'
            f'</div>'
        )
    strategy_cards_html += '</div>'

    # ── Equity curve (Chart.js — kept) ──
    import json as _json
    equity_data = []
    entries = backlog_data.get("entries", [])
    cum_netto = 0
    for entry in sorted(entries, key=lambda e: e.get("date", "")):
        netto_val = entry.get("netto", 0)
        if netto_val:
            cum_netto += netto_val
            equity_data.append({"x": entry.get("date", ""), "y": round(cum_netto)})

    chart_html = ""
    if equity_data:
        chart_html = (
            f'<div class="stats-block">'
            f'<h3>Equity Curve</h3>'
            f'<canvas id="equity-chart" height="180"></canvas>'
            f'<script>'
            f'(function(){{'
            f'const data = {_json.dumps(equity_data)};'
            f'new Chart(document.getElementById("equity-chart"),{{'
            f'type:"line",'
            f'data:{{datasets:[{{data:data,borderColor:"#f59e0b",backgroundColor:"rgba(245,166,35,0.08)",'
            f'fill:true,tension:0.3,pointRadius:0,borderWidth:2}}]}},'
            f'options:{{responsive:true,plugins:{{legend:{{display:false}}}},'
            f'scales:{{x:{{type:"category",ticks:{{color:"#94a3b8",maxTicksLimit:8,font:{{size:10}}}},'
            f'grid:{{display:false}}}},'
            f'y:{{ticks:{{color:"#94a3b8",callback:function(v){{return (v/1000).toFixed(0)+"k"}},'
            f'font:{{size:10}}}},grid:{{color:"rgba(0,0,0,0.04)"}}}}}}}}}}'
            f');}})();'
            f'</script>'
            f'</div>'
        )

    # ── Monthly ROI CSS bar chart ──
    MONTH_NAMES_SV = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "Maj", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Okt", "11": "Nov", "12": "Dec",
    }
    monthly_bar_html = ""
    entries_for_monthly = backlog_data.get("entries", [])
    if entries_for_monthly:
        from collections import defaultdict as _defaultdict
        month_agg = _defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "hits": 0})
        for entry in entries_for_monthly:
            d = entry.get("date", "")
            if len(d) >= 7:
                ym = d[:7]
                ma = month_agg[ym]
                ma["cost"] += entry.get("cost", 0)
                ma["payout"] += entry.get("payout", 0)
                ma["rounds"] += 1
                if entry.get("hit"):
                    ma["hits"] += 1

        if month_agg:
            sorted_months = sorted(month_agg.keys(), reverse=True)[:12]
            sorted_months.reverse()  # chronological order for chart

            roi_values = []
            for ym in sorted_months:
                ma = month_agg[ym]
                roi_val = ((ma["payout"] - ma["cost"]) / ma["cost"] * 100) if ma["cost"] > 0 else 0
                roi_values.append(roi_val)
            max_abs_roi = max(abs(v) for v in roi_values) if roi_values else 1
            if max_abs_roi == 0:
                max_abs_roi = 1

            bar_items = []
            for i, ym in enumerate(sorted_months):
                ma = month_agg[ym]
                roi = roi_values[i]
                netto = ma["payout"] - ma["cost"]
                win_rate = (ma["hits"] / ma["rounds"] * 100) if ma["rounds"] > 0 else 0
                bar_pct = abs(roi) / max_abs_roi * 100
                bar_color = "#22c55e" if roi >= 0 else "#ef4444"
                bar_bg = "rgba(34,197,94,0.20)" if roi >= 0 else "rgba(239,68,68,0.20)"
                month_num = ym[5:7]
                year_short = ym[2:4]
                month_label = f"{MONTH_NAMES_SV.get(month_num, month_num)} '{year_short}"
                netto_color = "#22c55e" if netto >= 0 else "#ef4444"
                bar_items.append(
                    f'<div class="mbar-row">'
                    f'<span class="mbar-label">{month_label}</span>'
                    f'<div class="mbar-track">'
                    f'<div class="mbar-fill" style="width:{max(bar_pct, 2):.0f}%;background:{bar_bg};border-left:3px solid {bar_color}"></div>'
                    f'</div>'
                    f'<span class="mbar-val" style="color:{bar_color}">{roi:+.1f}%</span>'
                    f'<span class="mbar-netto" style="color:{netto_color}">{netto:+,.0f} kr</span>'
                    f'<span class="mbar-meta">{ma["rounds"]} omg &middot; {win_rate:.0f}% vinst</span>'
                    f'</div>'
                )

            monthly_bar_html = (
                f'<div class="stats-block">'
                f'<h3>Manadsvis ROI</h3>'
                f'<div class="mbar-chart">{"".join(bar_items)}</div>'
                f'</div>'
            )

    # ── Speltyp-filter + detail tables (kept, with compact filter) ──
    all_game_types = sorted(set(
        gt for s in strategies.values() for gt in s.get("game_types", {}).keys()
    ))
    gt_buttons = ['<button class="gt-btn active" data-action="filter-stats-gt" data-gt="all">Alla</button>']
    for gt in all_game_types:
        color = GT_COLORS_STATS.get(gt, "#64748b")
        gt_buttons.append(
            f'<button class="gt-btn" data-action="filter-stats-gt" data-gt="{gt}" '
            f'style="--gt-color:{color}">{gt}</button>'
        )

    strat_buttons = ['<button class="strat-btn active" data-action="filter-stats" data-strat="all">Alla</button>']
    for s in strat_names:
        short = strat_short.get(s, s[:10])
        strat_buttons.append(
            f'<button class="strat-btn" data-action="filter-stats" data-strat="{s}">{short}</button>'
        )

    # ── Detail tables per strategy (speltyp, year, track) ──
    detail_sections = []

    for s in strat_names:
        short = strat_short.get(s, s[:10])

        # Game type table
        game_types = strategies[s].get("game_types", {})
        if game_types:
            gt_rows = []
            for gt in sorted(game_types.keys()):
                g = game_types[gt]
                roi = g.get("roi", 0)
                roi_cls = "#22c55e" if roi > 0 else "#ef4444"
                netto = g.get("netto", 0)
                netto_cls = "#22c55e" if netto > 0 else "#ef4444"
                gt_color = GT_COLORS_STATS.get(gt, "#64748b")
                gt_rows.append(
                    f'<tr data-gt="{gt}">'
                    f'<td><strong style="color:{gt_color}">{gt}</strong></td>'
                    f'<td>{g.get("rounds", 0)}</td><td>{g.get("full", 0)}</td>'
                    f'<td>{g.get("cost", 0):,.0f} kr</td><td>{g.get("payout", 0):,.0f} kr</td>'
                    f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                    f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td></tr>'
                )
            detail_sections.append(
                f'<div class="stats-block strat-block" data-strat="{s}">'
                f'<h3>Speltyp &mdash; {short}</h3>'
                f'<div class="table-wrap"><table class="stats-table">'
                f'<thead><tr><th>Typ</th><th>Omg</th><th>Full</th>'
                f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
                f'<tbody>{"".join(gt_rows)}</tbody></table></div></div>'
            )

        # Yearly table
        yearly = strategies[s].get("yearly", {})
        if yearly:
            yr_rows = []
            for year in sorted(yearly.keys()):
                y = yearly[year]
                roi = y.get("roi", 0)
                roi_cls = "#22c55e" if roi > 0 else "#ef4444"
                netto = y.get("netto", 0)
                netto_cls = "#22c55e" if netto > 0 else "#ef4444"
                yr_rows.append(
                    f'<tr><td><strong>{year}</strong></td>'
                    f'<td>{y.get("rounds", 0)}</td><td>{y.get("full", 0)}</td>'
                    f'<td>{y.get("cost", 0):,.0f} kr</td><td>{y.get("payout", 0):,.0f} kr</td>'
                    f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                    f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td></tr>'
                )
            detail_sections.append(
                f'<div class="stats-block strat-block" data-strat="{s}">'
                f'<h3>Per ar &mdash; {short}</h3>'
                f'<div class="table-wrap"><table class="stats-table">'
                f'<thead><tr><th>Ar</th><th>Omg</th><th>Full</th>'
                f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
                f'<tbody>{"".join(yr_rows)}</tbody></table></div></div>'
            )

        # Track table
        tracks = strategies[s].get("tracks", {})
        if tracks:
            trk_rows = []
            for track in sorted(tracks.keys(), key=lambda t: tracks[t].get("rounds", 0), reverse=True)[:10]:
                t = tracks[track]
                roi = t.get("roi", 0)
                roi_cls = "#22c55e" if roi > 0 else "#ef4444"
                netto = t.get("netto", 0)
                netto_cls = "#22c55e" if netto > 0 else "#ef4444"
                trk_rows.append(
                    f'<tr><td><strong>{_esc(track)}</strong></td>'
                    f'<td>{t.get("rounds", 0)}</td><td>{t.get("full", 0)}</td>'
                    f'<td>{t.get("cost", 0):,.0f} kr</td><td>{t.get("payout", 0):,.0f} kr</td>'
                    f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                    f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td></tr>'
                )
            detail_sections.append(
                f'<div class="stats-block strat-block" data-strat="{s}">'
                f'<h3>Per bana &mdash; {short}</h3>'
                f'<div class="table-wrap"><table class="stats-table">'
                f'<thead><tr><th>Bana</th><th>Omg</th><th>Full</th>'
                f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
                f'<tbody>{"".join(trk_rows)}</tbody></table></div></div>'
            )

        # Yearly by game type
        yearly_gt = strategies[s].get("yearly_by_game_type", {})
        if yearly_gt:
            for gt in sorted(yearly_gt.keys()):
                gt_color = GT_COLORS_STATS.get(gt, "#64748b")
                yr_data = yearly_gt[gt]
                yr_rows = []
                for year in sorted(yr_data.keys()):
                    y = yr_data[year]
                    roi = y.get("roi", 0)
                    roi_cls = "#22c55e" if roi > 0 else "#ef4444"
                    netto = y.get("netto", 0)
                    netto_cls = "#22c55e" if netto > 0 else "#ef4444"
                    yr_rows.append(
                        f'<tr><td><strong>{year}</strong></td>'
                        f'<td>{y.get("rounds", 0)}</td><td>{y.get("full", 0)}</td>'
                        f'<td>{y.get("cost", 0):,.0f} kr</td><td>{y.get("payout", 0):,.0f} kr</td>'
                        f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                        f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td></tr>'
                    )
                detail_sections.append(
                    f'<div class="stats-block strat-block gt-block" data-strat="{s}" data-gt="{gt}">'
                    f'<h3 style="color:{gt_color}">{gt} per ar &mdash; {short}</h3>'
                    f'<div class="table-wrap"><table class="stats-table">'
                    f'<thead><tr><th>Ar</th><th>Omg</th><th>Full</th>'
                    f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
                    f'<tbody>{"".join(yr_rows)}</tbody></table></div></div>'
                )

    # ── Monthly per-strategy tables ──
    for s in strat_names:
        monthly = strategies[s].get("monthly", {})
        if not monthly:
            continue
        short = strat_short.get(s, s[:10])
        m_rows = []
        for month in sorted(monthly.keys()):
            m = monthly[month]
            roi = m.get("roi", 0)
            roi_cls = "#22c55e" if roi > 0 else "#ef4444"
            netto = m.get("netto", 0)
            netto_cls = "#22c55e" if netto > 0 else "#ef4444"
            m_rows.append(
                f'<tr><td><strong>{m.get("name", month)}</strong></td>'
                f'<td>{m.get("rounds", 0)}</td><td>{m.get("full", 0)}</td>'
                f'<td>{m.get("cost", 0):,.0f} kr</td><td>{m.get("payout", 0):,.0f} kr</td>'
                f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td></tr>'
            )
        detail_sections.append(
            f'<div class="stats-block strat-block" data-strat="{s}">'
            f'<h3>Per manad &mdash; {short}</h3>'
            f'<div class="table-wrap"><table class="stats-table">'
            f'<thead><tr><th>Manad</th><th>Omg</th><th>Full</th>'
            f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
            f'<tbody>{"".join(m_rows)}</tbody></table></div></div>'
        )

    # ── Assemble ──
    filter_html = (
        f'<div class="stats-filter-bar">'
        f'<div class="strat-filter">{"".join(strat_buttons)}</div>'
        f'<div class="strat-filter gt-filter-row">{"".join(gt_buttons)}</div>'
        f'</div>'
    )

    return (
        f'<div id="stats" class="summary-card stats-section">'
        f'<h2>Statistik</h2>'
        f'{strategy_cards_html}'
        f'{chart_html}'
        f'{monthly_bar_html}'
        f'{filter_html}'
        f'{"".join(detail_sections)}'
        f'</div>'
    )


def _backlog_html(
    game_round: GameRound,
    backlog_data: dict | None = None,
) -> str:
    """Generera backlog-sektion med aggregerade sammanfattningskort och filtrerbar tabell."""
    if not backlog_data or "entries" not in backlog_data:
        return ""

    entries = backlog_data["entries"]
    cutoff = str(date.today() - timedelta(days=30))
    strat_short = {
        "I_streck_1st": "I_streck", "Q_dom_x_mktgap": "Q_dom",
        "D_market_gap": "D_market", "I_streck_spik1": "I_spik1",
        "I_streck_spik2": "I_spik2", "I_streck_spik3": "I_spik3",
    }

    GT_COLORS_BL = {
        "V75": "#f59e0b", "V85": "#f59e0b", "GS75": "#a78bfa",
        "V86": "#fb923c", "V64": "#34d399", "V65": "#34d399",
    }

    # ── Aggregate stats per strategy for summary cards ──
    from collections import defaultdict as _defaultdict
    strat_agg = _defaultdict(lambda: {
        "cost": 0, "payout": 0, "rounds": 0, "full": 0, "partial": 0,
    })
    for entry in entries:
        if entry.get("live"):
            continue
        s = entry.get("strategy", "")
        sa = strat_agg[s]
        sa["rounds"] += 1
        sa["cost"] += entry.get("cost", 0)
        sa["payout"] += entry.get("payout", 0)
        if entry.get("hit"):
            sa["full"] += 1
        elif entry.get("payout", 0) > 0:
            sa["partial"] += 1

    # ── Summary metric cards (top-level aggregation) ──
    strat_colors = {
        "I_streck_1st": ("#f59e0b", "rgba(245,166,35,0.15)"),
        "Q_dom_x_mktgap": ("#a78bfa", "rgba(167,139,250,0.15)"),
        "D_market_gap": ("#fb923c", "rgba(251,146,60,0.15)"),
        "I_streck_spik1": ("#fbbf24", "rgba(251,191,36,0.15)"),
        "I_streck_spik2": ("#34d399", "rgba(52,211,153,0.15)"),
        "I_streck_spik3": ("#f472b6", "rgba(244,114,182,0.15)"),
    }

    agg_cards = []
    for s in sorted(strat_agg.keys()):
        sa = strat_agg[s]
        short = strat_short.get(s, s[:10])
        netto = sa["payout"] - sa["cost"]
        roi = (netto / sa["cost"] * 100) if sa["cost"] > 0 else 0
        hit_rate = (sa["full"] / sa["rounds"] * 100) if sa["rounds"] > 0 else 0
        accent, bg = strat_colors.get(s, ("#64748b", "rgba(100,116,139,0.15)"))
        roi_color = "#22c55e" if roi >= 0 else "#ef4444"
        netto_color = "#22c55e" if netto >= 0 else "#ef4444"
        agg_cards.append(
            f'<div class="hist-agg-card" style="border-top:3px solid {accent}">'
            f'<div class="hist-agg-name" style="color:{accent}">{short}</div>'
            f'<div class="hist-agg-grid">'
            f'<div class="hist-agg-item"><span class="hist-agg-val">{sa["rounds"]}</span><span class="hist-agg-lbl">Omgangar</span></div>'
            f'<div class="hist-agg-item"><span class="hist-agg-val" style="color:{roi_color}">{roi:+.1f}%</span><span class="hist-agg-lbl">ROI</span></div>'
            f'<div class="hist-agg-item"><span class="hist-agg-val">{hit_rate:.0f}%</span><span class="hist-agg-lbl">Traffprocent</span></div>'
            f'<div class="hist-agg-item"><span class="hist-agg-val" style="color:{netto_color}">{netto:+,.0f}</span><span class="hist-agg-lbl">Netto (kr)</span></div>'
            f'</div></div>'
        )

    agg_section = (
        f'<div class="hist-agg-wrap">'
        f'<h3 class="hist-agg-title">Per strategi</h3>'
        f'<div class="hist-agg-grid-outer">{"".join(agg_cards)}</div>'
        f'</div>'
    )

    # ── Strategy filter (compact, event delegation) ──
    all_strategies = sorted(set(e.get("strategy", "") for e in entries))
    filter_btns = ['<button class="strat-btn active" data-action="filter-backlog" data-strat="all">Alla</button>']
    for s in all_strategies:
        short = strat_short.get(s, s[:10])
        filter_btns.append(
            f'<button class="strat-btn" data-action="filter-backlog" data-strat="{s}">{short}</button>'
        )

    # Speltyp filter (compact)
    all_game_types = sorted(set(e.get("game_type", "") for e in entries if e.get("game_type")))
    gt_filter_btns = ['<button class="gt-btn active" data-action="filter-backlog-gt" data-gt="all">Alla</button>']
    for gt in all_game_types:
        color = GT_COLORS_BL.get(gt, "#64748b")
        gt_filter_btns.append(
            f'<button class="gt-btn" data-action="filter-backlog-gt" data-gt="{gt}" '
            f'style="--gt-color:{color}">{gt}</button>'
        )
    gt_filter_btns.append(
        '<button class="gt-btn" data-action="filter-backlog-recent" data-months="12" '
        'style="--gt-color:#f59e0b">12 man</button>'
    )

    rows = []
    total_cost = 0
    total_payout = 0
    full_hits = 0
    partial_hits = 0
    total_rounds = 0
    hidden_count = 0

    for idx, entry in enumerate(entries):
        total_rounds += 1
        cost = entry.get("cost", 0)
        payout = entry.get("payout", 0)
        hit = entry.get("hit", False)
        num_correct = entry.get("num_correct", 0)
        num_races = entry.get("num_races", 7)
        strategy = entry.get("strategy", "")
        track = entry.get("track", "?")
        is_live = entry.get("live", False)
        races_finished = entry.get("races_finished", num_races)
        races_total = entry.get("races_total", num_races)

        if not is_live:
            total_cost += cost
            total_payout += payout
            if hit:
                full_hits += 1
            elif payout > 0:
                partial_hits += 1

        # Resultat-badge
        if is_live:
            races_remaining = races_total - races_finished
            if races_finished == 0:
                hit_badge = f'<span class="live-result-badge">0/{races_total} ({races_remaining} kvar)</span>'
            elif num_correct == races_finished:
                hit_badge = f'<span class="live-result-badge live-allright">{num_correct}/{races_finished} ({races_remaining} kvar)</span>'
            else:
                hit_badge = f'<span class="live-result-badge">{num_correct}/{races_finished} ({races_remaining} kvar)</span>'
        elif hit:
            hit_badge = f'<span class="hit-badge">{num_correct}/{num_races}</span>'
        elif payout > 0:
            hit_badge = f'<span class="partial-badge">{num_correct}/{num_races}</span>'
        else:
            hit_badge = f'<span class="miss-badge">{num_correct}/{num_races}</span>'

        if is_live:
            payout_str = "—"
            netto = 0
            netto_str = "—"
            netto_cls = "color:#6b7280"
        else:
            payout_str = f"{payout:,.0f} kr" if payout > 0 else "-"
            netto = payout - cost
            netto_str = f"{netto:+,.0f} kr"
            netto_cls = "color:#22c55e" if netto > 0 else "color:#ef4444"

        short_strat = strat_short.get(strategy, strategy[:10])
        sc, sbg = strat_colors.get(strategy, ("#64748b", "rgba(100,116,139,0.15)"))
        strat_badge = f'<span class="strat-tag" style="color:{sc};background:{sbg}">{short_strat}</span>'

        ppr = entry.get("payout_per_row", 0)
        wr = entry.get("winning_rows", 0)
        detail = f"{ppr:,.0f}/rad" if ppr > 0 else ""

        live_badge = ' <span class="live-badge">LIVE</span>' if is_live else ""

        has_races = bool(entry.get("races"))
        toggle_attr = f'data-action="toggle-bl-detail" data-detail="bl-detail-{idx}"' if has_races else ''
        expand_icon = '<span class="bl-expand-icon">&#9656;</span> ' if has_races else ''

        entry_gt = entry.get("game_type", "")
        gt_c = GT_COLORS_BL.get(entry_gt, "#64748b")
        gt_bg_map = {
            "V75": "rgba(245,166,35,0.08)", "V85": "rgba(245,166,35,0.08)",
            "GS75": "rgba(167,139,250,0.15)", "V86": "rgba(251,146,60,0.15)",
            "V64": "rgba(52,211,153,0.15)", "V65": "rgba(52,211,153,0.15)",
        }
        gt_bg = gt_bg_map.get(entry_gt, "rgba(100,116,139,0.15)")
        gt_badge = f'<span class="gt-tag" style="color:{gt_c};background:{gt_bg}">{entry_gt}</span>'

        entry_date = entry.get("date", "")
        is_older = entry_date < cutoff and not is_live
        if is_older:
            hidden_count += 1
            continue

        cls_attr = f' class="bl-expandable"' if has_races else ""
        rows.append(
            f'<tr{cls_attr} {toggle_attr} data-strategy="{strategy}" data-gt="{entry_gt}">'
            f'<td>{expand_icon}{entry.get("date", "")}{live_badge}</td>'
            f'<td>{gt_badge}</td>'
            f'<td>{_esc(track[:12])}</td>'
            f'<td>{strat_badge}</td>'
            f'<td>{cost:,.0f} kr</td>'
            f'<td>{hit_badge}</td>'
            f'<td>{payout_str}</td>'
            f'<td style="{netto_cls};font-weight:600">{netto_str}</td>'
            f'<td style="color:#6b7280;font-size:.75rem">{detail}</td>'
            f'</tr>'
        )

        if has_races:
            race_rows = []
            for rd in entry["races"]:
                picks_str = ", ".join(
                    f'{p["nr"]} {p["namn"]} ({p["score"]}p/{p["streck"]}%)'
                    for p in rd.get("pick_list", [])
                )
                race_status = rd.get("status", "finished")
                ratt = rd.get("ratt")
                if race_status == "pending" or ratt is None:
                    ratt_icon = '⏳'
                    ratt_cls = 'color:#6b7280'
                elif ratt:
                    ratt_icon = '✅'
                    ratt_cls = 'color:#22c55e'
                else:
                    ratt_icon = '❌'
                    ratt_cls = 'color:#ef4444'

                vinnare = rd.get("vinnare", "")
                vinnare_namn = rd.get("vinnare_namn", "")
                vinnare_streck = rd.get("vinnare_streck", 0)
                vinnare_str = f'Vann: {vinnare} {vinnare_namn} ({vinnare_streck}%)' if vinnare else '<span style="color:#6b7280">Vantar...</span>'

                race_rows.append(
                    f'<tr class="bl-race-row">'
                    f'<td style="padding-left:2rem;color:#6b7280">Avd {rd.get("avd", "")}</td>'
                    f'<td style="font-size:.75rem">{rd.get("dist","")}m {rd.get("metod","")}</td>'
                    f'<td style="font-size:.75rem">{rd.get("picks","")} val</td>'
                    f'<td style="{ratt_cls};font-weight:600">{ratt_icon}</td>'
                    f'<td colspan="3" style="font-size:.75rem;color:#6b7280">{picks_str}</td>'
                    f'<td style="font-size:.75rem;color:#6b7280">{vinnare_str}</td>'
                    f'<td></td></tr>'
                )

            rows.append(
                f'<tr id="bl-detail-{idx}" class="bl-detail-container" style="display:none" data-strategy="{strategy}" data-gt="{entry_gt}">'
                f'<td colspan="9" style="padding:0">'
                f'<table class="bl-detail-table">{"".join(race_rows)}</table>'
                f'</td></tr>'
            )

    total_netto = total_payout - total_cost
    roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
    roi_cls = "#22c55e" if roi > 0 else "#ef4444"
    total_wins = full_hits + partial_hits
    win_rate = total_wins / total_rounds * 100 if total_rounds > 0 else 0

    show_all_note = ""
    if hidden_count > 0:
        show_all_note = (
            f'<div class="bl-date-note">'
            f'Visar senaste 30 dagar ({total_rounds - hidden_count} av {total_rounds})'
            f'</div>'
        )

    # Streak indicator (last 5 non-live rounds)
    recent_results = []
    for entry in sorted(entries, key=lambda e: e.get("date", ""), reverse=True):
        if entry.get("live", False):
            continue
        if entry.get("hit", False):
            recent_results.append('<span class="streak-dot streak-hit"></span>')
        elif entry.get("payout", 0) > 0:
            recent_results.append('<span class="streak-dot streak-partial"></span>')
        else:
            recent_results.append('<span class="streak-dot streak-miss"></span>')
        if len(recent_results) >= 10:
            break
    streak_html = f'<div class="streak-bar">{"".join(recent_results)}</div>' if recent_results else ""

    return (
        f'<div id="backlog" class="summary-card backlog-section">'
        f'<h2>Historik</h2>'
        f'{agg_section}'
        f'{streak_html}'
        f'<div class="bl-filter-bar">'
        f'<div class="strat-filter bl-filter">{"".join(filter_btns)}</div>'
        f'<div class="strat-filter gt-filter-row bl-gt-filter">{"".join(gt_filter_btns)}</div>'
        f'</div>'
        f'{show_all_note}'
        f'<div class="backlog-summary" id="bl-summary">'
        f'<div class="bl-stat"><span class="bl-val" id="bl-rounds">{total_rounds}</span><span class="bl-lbl">Omgangar</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-full">{full_hits}</span><span class="bl-lbl">Alla ratt</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-partial">{partial_hits}</span><span class="bl-lbl">Delvinster</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-winrate">{win_rate:.0f}%</span><span class="bl-lbl">Vinstfrekvens</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-cost">{total_cost:,.0f} kr</span><span class="bl-lbl">Insats</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-payout">{total_payout:,.0f} kr</span><span class="bl-lbl">Utdelning</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-roi" style="color:{roi_cls}">{roi:+.1f}%</span><span class="bl-lbl">ROI</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-netto" style="color:{roi_cls}">{total_netto:+,.0f} kr</span><span class="bl-lbl">Netto</span></div>'
        f'</div>'
        f'<div class="table-wrap">'
        f'<table class="backlog-table">'
        f'<thead><tr>'
        f'<th>Datum</th><th>Typ</th><th>Bana</th><th>Strategi</th><th>Insats</th>'
        f'<th>Resultat</th><th>Utdelning</th><th>Netto</th><th>Detalj</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table></div>'
        f'</div>'
    )


def _ranking_html(game_round: GameRound) -> str:
    """Create compact ranking table grouping horses by tier (A/B/C/D) per race."""
    import json as _json

    TIER_MAP = {
        "spik": "A",
        "2-val": "B",
        "3-val": "B",
        "gardering": "C",
        "strykning": "D",
    }
    TIER_ORDER = ["A", "B", "C", "D"]
    gt = _esc(game_round.game_type)

    rows = []
    ranking_data = []  # For embedding in chat context

    for race in game_round.races:
        tiers: dict[str, list[int]] = {t: [] for t in TIER_ORDER}
        for entry in sorted(race.active_entries, key=lambda e: e.super_score, reverse=True):
            tier = TIER_MAP.get(entry.recommendation, "D")
            tiers[tier].append(entry.post_position)

        # Build row cells
        cells = []
        for tier in TIER_ORDER:
            nums = tiers[tier]
            if nums:
                pills = "".join(
                    f'<span class="rank-pill rank-{tier.lower()}">{n}</span>'
                    for n in nums
                )
                cells.append(f'<td class="rank-cell">{pills}</td>')
            else:
                cells.append('<td class="rank-cell rank-empty">&mdash;</td>')

        rows.append(
            f'<tr>'
            f'<td class="rank-race">{gt}-{race.race_number}</td>'
            f'{"".join(cells)}'
            f'</tr>'
        )

        # Collect data for chat context
        race_ranking = {
            "race": f"{gt}-{race.race_number}",
            "A": tiers["A"],
            "B": tiers["B"],
            "C": tiers["C"],
            "D": tiers["D"],
        }
        ranking_data.append(race_ranking)

    ranking_json = _json.dumps(ranking_data, ensure_ascii=False)

    return (
        f'<div class="ranking-card">'
        f'<div class="ranking-header">Ranking</div>'
        f'<div class="table-wrap">'
        f'<table class="ranking-table">'
        f'<thead><tr>'
        f'<th class="rank-th-race">Lopp</th>'
        f'<th class="rank-th rank-th-a">A</th>'
        f'<th class="rank-th rank-th-b">B</th>'
        f'<th class="rank-th rank-th-c">C</th>'
        f'<th class="rank-th rank-th-d">D</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'</div>'
        f'<div id="ranking-data" style="display:none" data-ranking=\'{ranking_json}\'></div>'
        f'</div>'
    )


def _consensus_ranking_html(game_round: GameRound, tips_raw: dict | None = None) -> str:
    """Build consensus ranking table from model + all tipster sources."""
    import json as _json
    import re as _re
    from collections import defaultdict

    if not tips_raw:
        return ""

    sources = tips_raw.get("sources", {})
    spetstrid = tips_raw.get("spetstrid", {})
    if not sources:
        return ""

    TIER_POINTS = {"A": 4, "B": 3, "C": 2, "D": 0}
    SOURCE_WEIGHTS = {
        "model": 3.0, "sharps_berglund": 2.5, "sharps_jensa": 2.0,
        "expressen_edholm": 2.0, "travcash": 1.5,
        "aftonbladet_mario": 1.0, "aftonbladet_robert": 1.0,
        "aftonbladet_kim": 1.0, "aftonbladet_quist": 1.0,
        "aftonbladet_nils": 1.0,
    }
    # Dynamic weights for user-added sources (default 1.5 — same tier as travcash)
    for src_key in sources:
        if src_key not in SOURCE_WEIGHTS and src_key != "expressen_tranarsnack":
            SOURCE_WEIGHTS[src_key] = 1.5
    TIER_ORDER = ["A", "B", "C", "D"]
    gt = _esc(game_round.game_type)

    # -- Weight legend computation --
    _known_keys = {"model", "travcash"}
    _known_keys |= {k for k in SOURCE_WEIGHTS if k.startswith("sharps_")}
    _known_keys |= {k for k in SOURCE_WEIGHTS if k.startswith("aftonbladet_")}
    _known_keys.add("expressen_edholm")
    _w_model = SOURCE_WEIGHTS["model"]
    _w_sharps = sum(v for k, v in SOURCE_WEIGHTS.items() if k.startswith("sharps_"))
    _w_media = (
        sum(v for k, v in SOURCE_WEIGHTS.items() if k.startswith("aftonbladet_"))
        + SOURCE_WEIGHTS.get("expressen_edholm", 0)
    )
    _w_travcash = SOURCE_WEIGHTS.get("travcash", 0)
    _w_custom = sum(v for k, v in SOURCE_WEIGHTS.items() if k not in _known_keys)
    _custom_names = [k for k in SOURCE_WEIGHTS if k not in _known_keys]
    _w_total = _w_model + _w_sharps + _w_media + _w_travcash + _w_custom

    rows = []
    for race in game_round.races:
        race_key = f"{gt}-{race.race_number}"
        horse_scores: dict[str, float] = defaultdict(float)
        horse_weights: dict[str, float] = defaultdict(float)

        # -- Track model A-tier picks for agreement indicator --
        model_a_picks: set[str] = set()

        sorted_entries = sorted(
            race.active_entries, key=lambda e: e.super_score, reverse=True,
        )
        model_w = SOURCE_WEIGHTS["model"]
        total_e = len(sorted_entries)
        for i, entry in enumerate(sorted_entries):
            num = str(entry.post_position)
            pct = i / total_e if total_e else 0
            if pct < 0.15:
                tier = "A"
                model_a_picks.add(num)
            elif pct < 0.40:
                tier = "B"
            elif pct < 0.70:
                tier = "C"
            else:
                tier = "D"
            horse_scores[num] += TIER_POINTS[tier] * model_w
            horse_weights[num] += model_w

        # -- Track expert A-tier votes (weighted) for agreement indicator --
        expert_a_votes: dict[str, float] = defaultdict(float)
        expert_total_weight = 0.0

        for src_key, src_data in sources.items():
            weight = SOURCE_WEIGHTS.get(src_key, 0.8)
            rankings = src_data.get("rankings", {})
            race_ranking = rankings.get(race_key)
            if race_ranking:
                tier_map: dict[str, str] = {}
                if isinstance(race_ranking, dict):
                    for tier, horses_str in race_ranking.items():
                        for num in _re.findall(r"\d+", str(horses_str)):
                            tier_map[num] = tier
                elif isinstance(race_ranking, str) and race_ranking != "Alla brett":
                    nums = [n.strip() for n in race_ranking.split("-") if n.strip()]
                    tot = len(nums)
                    for j, num in enumerate(nums):
                        p = j / tot if tot else 0
                        if p < 0.15:
                            tier_map[num] = "A"
                        elif p < 0.40:
                            tier_map[num] = "B"
                        elif p < 0.70:
                            tier_map[num] = "C"
                        else:
                            tier_map[num] = "D"
                for num, tier in tier_map.items():
                    pts = TIER_POINTS.get(tier, 1)
                    horse_scores[num] += pts * weight
                    horse_weights[num] += weight
                    if tier == "A":
                        expert_a_votes[num] += weight
                expert_total_weight += weight

            picks = src_data.get("picks", {})
            race_picks = picks.get(race_key)
            if race_picks and not rankings.get(race_key):
                pick_str = str(race_picks)
                if "SPIK" in pick_str.upper():
                    nums = _re.findall(r"\d+", pick_str.split("(")[0])
                    for num in nums[:1]:
                        horse_scores[num] += TIER_POINTS["A"] * weight
                        horse_weights[num] += weight
                        expert_a_votes[num] += weight
                    expert_total_weight += weight
                elif "Skräll" in pick_str:
                    nums = _re.findall(r"\d+", pick_str.split("(")[0])
                    for num in nums[:1]:
                        horse_scores[num] += TIER_POINTS["B"] * weight
                        horse_weights[num] += weight
                    expert_total_weight += weight

        if not horse_scores:
            continue

        avg = {n: horse_scores[n] / horse_weights[n] if horse_weights[n] > 0 else 0
               for n in horse_scores}
        sorted_h = sorted(avg.items(), key=lambda x: -x[1])
        tot_h = len(sorted_h)

        tiers: dict[str, list[str]] = {t: [] for t in TIER_ORDER}
        for j, (num, _score) in enumerate(sorted_h):
            p = j / tot_h if tot_h else 0
            if p < 0.15:
                tiers["A"].append(num)
            elif p < 0.40:
                tiers["B"].append(num)
            elif p < 0.70:
                tiers["C"].append(num)
            else:
                tiers["D"].append(num)

        cells = []
        for tier in TIER_ORDER:
            nums = tiers[tier]
            if nums:
                pills = "".join(
                    f'<span class="rank-pill rank-{tier.lower()}">{n}</span>'
                    for n in nums
                )
                cells.append(f'<td class="rank-cell">{pills}</td>')
            else:
                cells.append('<td class="rank-cell rank-empty">&mdash;</td>')

        # -- Agreement indicator: compare model A-tier vs expert A-tier --
        expert_a_picks: set[str] = set()
        if expert_total_weight > 0:
            for num, vote_w in expert_a_votes.items():
                if vote_w / expert_total_weight >= 0.30:
                    expert_a_picks.add(num)

        if model_a_picks and expert_a_picks:
            overlap = model_a_picks & expert_a_picks
            union = model_a_picks | expert_a_picks
            jaccard = len(overlap) / len(union) if union else 0
            if jaccard >= 0.5:
                agree_icon = '<span class="rank-agree" title="Modell och experter enas">&#10003;</span>'
            else:
                agree_icon = '<span class="rank-disagree" title="Modell och experter oeniga">&#9888;</span>'
        elif not model_a_picks and not expert_a_picks:
            agree_icon = '<span class="rank-agree" title="Inga tydliga A-val">&#8211;</span>'
        else:
            agree_icon = '<span class="rank-disagree" title="Modell och experter oeniga">&#9888;</span>'

        rows.append(
            f'<tr>'
            f'<td class="rank-race">{race_key}</td>'
            f'{"".join(cells)}'
            f'<td class="rank-cell rank-agree-cell">{agree_icon}</td>'
            f'</tr>'
        )

    if not rows:
        return ""

    num_sources = len([s for s in sources if s != "expressen_tranarsnack"])

    # -- Weight legend pills --
    def _wpill(label: str, weight: float, color: str, bg: str) -> str:
        pct = round(weight / _w_total * 100)
        return (
            f'<span class="wt-pill" style="color:{color};background:{bg}">'
            f'{label} <b>{pct}%</b></span>'
        )

    _custom_pill = ""
    if _w_custom > 0:
        _clbl = ", ".join(n.replace("_", " ").title() for n in _custom_names)
        _custom_pill = _wpill(_clbl, _w_custom, "#b45309", "rgba(254,215,170,0.6)")

    legend = (
        f'<div class="wt-legend">'
        + _wpill("Modell", _w_model, "#92400e", "rgba(254,243,199,0.6)")
        + _wpill("Sharps", _w_sharps, "#1e40af", "rgba(219,234,254,0.6)")
        + _wpill("Media", _w_media, "#6b21a8", "rgba(243,232,255,0.6)")
        + _wpill("Travcash", _w_travcash, "#065f46", "rgba(209,250,229,0.6)")
        + _custom_pill
        + f'</div>'
    )

    # -- List user-added custom sources for display --
    custom_src_badges = ""
    if _custom_names:
        badges = "".join(
            f'<span class="custom-src-badge" data-src="{_esc(n)}">'
            f'{_esc(n.replace("_", " ").title())}'
            f'<button class="custom-src-rm" onclick="removeCustomSource(\'{_esc(n)}\')" '
            f'title="Ta bort">&times;</button></span>'
            for n in _custom_names
        )
        custom_src_badges = f'<div class="custom-src-list">{badges}</div>'

    return (
        f'<div class="ranking-card">'
        f'<div class="ranking-header" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        f'Konsensus-ranking '
        f'<span style="font-weight:400;font-size:.75rem;color:#64748b">'
        f'(modell + {num_sources} experter)</span>'
        f'<button class="add-source-btn" onclick="openTipsUpload()" '
        f'title="Lägg till tipskälla via bild">+ Källa</button>'
        f'</div>'
        f'{legend}'
        f'{custom_src_badges}'
        f'<div class="table-wrap">'
        f'<table class="ranking-table">'
        f'<thead><tr>'
        f'<th class="rank-th-race">Lopp</th>'
        f'<th class="rank-th rank-th-a">A</th>'
        f'<th class="rank-th rank-th-b">B</th>'
        f'<th class="rank-th rank-th-c">C</th>'
        f'<th class="rank-th rank-th-d">D</th>'
        f'<th class="rank-th rank-th-agree" title="Modell vs experter: enas eller oeniga om A-tier">'
        f'<span style="font-size:.7rem">M/E</span></th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'</div>'
        f'</div>'
    )


def generate_dashboard_html(
    game_round: GameRound,
    available_dates: list[tuple[str, bool]] | None = None,
    backlog_data: dict | list[dict] | None = None,
    available_rounds: list[tuple[str, str, str, bool]] | list[tuple[str, str, str, bool, str]] | None = None,
    premium: bool = True,
    tips_raw: dict | None = None,
    proffs_data: dict | None = None,
) -> str:
    """Generera en komplett HTML-dashboard for en analyserad spelomgang.

    available_rounds: list of (key, game_type, date_str, is_finished[, track_name])
    """
    total_starters = sum(r.num_starters for r in game_round.races)

    # Stöd för både gammalt (list) och nytt (dict) format
    if isinstance(backlog_data, list):
        backlog_data = {"entries": backlog_data, "strategies": {}}

    # Normalize available_rounds to 5-tuples
    norm_rounds = None
    if available_rounds:
        norm_rounds = []
        for r in available_rounds:
            if len(r) == 5:
                norm_rounds.append(r)
            else:
                norm_rounds.append((r[0], r[1], r[2], r[3], ""))

    # Build proffs lookup per race
    proffs_by_race: dict[int, dict[int, dict]] = {}
    if proffs_data and "races" in proffs_data:
        for pr in proffs_data["races"]:
            rn = pr.get("race_number", 0)
            proffs_by_race[rn] = {
                h["number"]: h for h in pr.get("horses", [])
            }

    # Generera sektioner
    race_htmls = {r.race_number: _race_table_html(r, proffs_by_race.get(r.race_number)) for r in game_round.races}
    summary = _summary_html(game_round)
    ranking = _ranking_html(game_round)
    consensus_ranking = _consensus_ranking_html(game_round, tips_raw)
    system_section = _system_html(game_round, proffs_data=proffs_data)
    stats_section = _stats_html(backlog_data)
    backlog_section = _backlog_html(game_round, backlog_data)
    accuracy = _accuracy_html(game_round)
    risk_bar = _risk_summary_bar(game_round)
    winbet_summary = _vinnarspel_summary_html(game_round)
    bet_view = _bet_view_html(game_round)

    current_key = f"{game_round.game_type}/{game_round.round_date}"

    # Round dropdown for top navbar
    round_dropdown = ""
    if norm_rounds and len(norm_rounds) >= 2:
        round_options = []
        for key, gt, d_str, is_finished, track in sorted(norm_rounds, key=lambda r: r[2], reverse=True):
            sel = " selected" if key == current_key else ""
            status = "✓" if is_finished else "⏳"
            track_str = f" {track}" if track else ""
            round_options.append(f'<option value="{key}"{sel}>{gt} — {d_str}{track_str} {status}</option>')
        round_dropdown = '<select class="round-select" onchange="changeRound(this)">' + "".join(round_options) + '</select>'

    # Prepare date_str and track_name early — needed by round_navigator
    date_str = str(game_round.round_date) if game_round.round_date else ""
    track_name = _esc(game_round.track_name or "")

    # Round navigator for sidebar — always show current round label
    prev_key = ""
    next_key = ""
    if norm_rounds and len(norm_rounds) >= 2:
        sorted_rounds = sorted(norm_rounds, key=lambda r: r[2])
        current_idx = -1
        for i, (key, *_rest) in enumerate(sorted_rounds):
            if key == current_key:
                current_idx = i
                break
        if current_idx < 0:
            current_idx = len(sorted_rounds) - 1
        prev_key = sorted_rounds[current_idx - 1][0] if current_idx > 0 else ""
        next_key = sorted_rounds[current_idx + 1][0] if current_idx < len(sorted_rounds) - 1 else ""
    prev_disabled = " disabled" if not prev_key else ""
    next_disabled = " disabled" if not next_key else ""
    prev_href = f' data-key="{prev_key}"' if prev_key else ""
    next_href = f' data-key="{next_key}"' if next_key else ""
    # Build sidebar round select (always visible on desktop in sidebar)
    sidebar_round_select = ""
    if norm_rounds and len(norm_rounds) >= 2:
        sb_options = []
        for key, gt, d_str, is_finished, track in sorted(norm_rounds, key=lambda r: r[2], reverse=True):
            sel = " selected" if key == current_key else ""
            status = "✓" if is_finished else "⏳"
            track_str = f" {track}" if track else ""
            sb_options.append(f'<option value="{key}"{sel}>{gt} — {d_str}{track_str} {status}</option>')
        sidebar_round_select = (
            '<select class="sidebar-round-select" onchange="changeRound(this)">'
            + "".join(sb_options)
            + '</select>'
        )

    round_navigator = (
        f'<div class="round-nav">'
        f'<button class="round-nav-btn"{prev_disabled}{prev_href} data-action="nav-round">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>'
        f'</button>'
        f'<span class="round-nav-label">{_esc(game_round.game_type)} {date_str}</span>'
        f'<button class="round-nav-btn"{next_disabled}{next_href} data-action="nav-round">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>'
        f'</button>'
        f'</div>'
        f'{sidebar_round_select}'
    )

    # Race sections (all visible — no premium gating)
    race_sections_parts = []
    for n, h in race_htmls.items():
        race_sections_parts.append(
            f'<section id="s-race-{n}" class="dashboard-section">{h}</section>'
        )
    race_sections = "".join(race_sections_parts)

    # Spelvärde — Hero section with SVG ring (grid layout)
    sv = _calculate_spelvarde(game_round)
    if sv:
        score = sv["score"]
        circumference = 377.0  # 2 * pi * 60
        offset = circumference * (1 - score / 100)
        sv_bar = (
            f'<div class="hero-grid">'
            f'<div class="hero-score-card">'
            f'<svg width="140" height="140" viewBox="0 0 140 140">'
            f'<defs><linearGradient id="scoreGrad" x1="0%" y1="0%" x2="100%" y2="100%">'
            f'<stop offset="0%" stop-color="#4ade80"/><stop offset="100%" stop-color="#22c55e"/>'
            f'</linearGradient></defs>'
            f'<circle cx="70" cy="70" r="60" fill="none" stroke="#e5e7eb" stroke-width="8"/>'
            f'<circle cx="70" cy="70" r="60" fill="none" stroke="url(#scoreGrad)" stroke-width="8" '
            f'stroke-dasharray="{circumference:.1f}" stroke-dashoffset="{offset:.1f}" '
            f'stroke-linecap="round" transform="rotate(-90 70 70)"/>'
            f'<text x="70" y="65" text-anchor="middle" font-size="36" font-weight="800" '
            f'fill="#1e293b" font-family="\'JetBrains Mono\',monospace">{score}</text>'
            f'<text x="70" y="85" text-anchor="middle" font-size="10" font-weight="600" '
            f'fill="#6b7280" font-family="\'Inter\',sans-serif" letter-spacing="0.08em">SPELVÄRDE</text>'
            f'</svg>'
            f'<div class="hero-score-text" style="color:{sv["color"]}">{_esc(sv["text"])}</div>'
            f'<div class="hero-score-advice">{_esc(sv["advice"])}</div>'
            f'</div>'
            f'<div class="hero-right">'
            f'<div class="hero-kpis">'
            f'<div class="hero-kpi blue"><div class="hero-kpi-val">{sv["details"]["streck"]}</div><div class="hero-kpi-lbl">Streck</div></div>'
            f'<div class="hero-kpi green"><div class="hero-kpi-val">{sv["details"]["value"]}</div><div class="hero-kpi-lbl">Value</div></div>'
            f'<div class="hero-kpi yellow"><div class="hero-kpi-val">{sv["details"]["upset"]}</div><div class="hero-kpi-lbl">Upset</div></div>'
            f'<div class="hero-kpi purple"><div class="hero-kpi-val">{sv["details"]["bas"]}</div><div class="hero-kpi-lbl">Spelform</div></div>'
            f'</div>'
            f'</div>'
            f'</div>'
        )
    else:
        sv_bar = ""

    # Sidebar nav items for dashboard view
    gt = _esc(game_round.game_type)
    div_items_html = []
    for race in game_round.races:
        risk_cls = "high" if race.upset_risk >= 50 else ("medium" if race.upset_risk >= 25 else "low")
        risk_color = "#ef4444" if risk_cls == "high" else ("#eab308" if risk_cls == "medium" else "#22c55e")
        div_items_html.append(
            f'<button class="div-item" data-div="{race.race_number}" '
            f'onclick="showDivision({race.race_number})">'
            f'<span class="div-num">{race.race_number}</span>'
            f'<span>{gt}-{race.race_number}</span>'
            f'<span class="div-dot" style="background:{risk_color}"></span>'
            f'</button>'
        )
    sidebar_div_items = "".join(div_items_html)

    system_drawer_btn = ""
    if system_section:
        system_drawer_btn = (
            '<button class="nav-item" data-section="system" onclick="openDrawer()">'
            '<span class="nav-icon">'
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/></svg>'
            '</span><span>System</span></button>'
        )

    # track_name and date_str already assigned above (before round_navigator)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kungens Trav — {_esc(game_round.game_type)} {game_round.round_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
::selection{{background:rgba(59,130,246,0.2)}}
body{{font-family:'Inter',system-ui,-apple-system,sans-serif;
background:#f5f6f8;color:#1e293b;line-height:1.6;-webkit-font-smoothing:antialiased;font-size:14px}}

/* ── Top Navbar ── */
.top-navbar{{height:56px;background:#ffffff;border-bottom:1px solid #e5e7eb;
display:flex;align-items:center;justify-content:space-between;padding:0 24px;
position:fixed;top:0;left:0;right:0;z-index:200}}
.nav-brand{{font-size:17px;font-weight:800;color:#1e293b;letter-spacing:-0.03em;
display:flex;align-items:center;gap:8px;flex-shrink:0}}
.nav-brand span{{color:#f59e0b}}
.nav-tabs{{display:flex;gap:0;position:relative;min-width:0}}
.nav-tab{{padding:8px 20px;border:none;background:transparent;
cursor:pointer;font-size:13px;font-weight:500;color:#64748b;
font-family:inherit;transition:all 0.2s;white-space:nowrap;
position:relative;border-radius:0}}
.nav-tab:hover{{color:#1e293b}}
.nav-tab.active{{color:#1e293b;font-weight:600}}
.nav-tab.active::after{{content:'';position:absolute;bottom:-1px;left:8px;right:8px;
height:2px;background:#f59e0b;border-radius:2px}}
.nav-right{{display:flex;align-items:center;gap:14px;flex-shrink:0}}
.nav-clock{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#94a3b8;font-weight:400}}
.round-select{{padding:7px 14px;border-radius:10px;border:1px solid #e5e7eb;
background:#ffffff;color:#1e293b;font-size:13px;cursor:pointer;outline:none;
font-family:inherit;transition:border-color .2s,box-shadow .2s;flex-shrink:0;min-width:0}}
.round-select:hover{{border-color:#cbd5e1;box-shadow:0 1px 4px rgba(0,0,0,0.06)}}

/* ── App layout ── */
.app-layout{{display:flex;padding-top:56px;min-height:100vh}}

/* ── Left Sidebar ── */
.sidebar{{width:220px;background:#f9fafb;border-right:1px solid #e5e7eb;
position:fixed;left:0;top:56px;bottom:0;z-index:100;
display:flex;flex-direction:column;overflow-y:auto}}
.sb-info{{padding:16px;border-bottom:1px solid #e5e7eb;background:#ffffff}}
.sb-info-type{{font-size:13px;font-weight:700;color:#1e293b}}
.sb-info-meta{{font-size:11px;color:#6b7280;margin-top:2px}}
.sb-nav{{padding:8px 0;flex:1;overflow-y:auto}}
.nav-item{{width:100%;display:flex;align-items:center;gap:10px;
padding:9px 16px;background:transparent;border:none;border-left:3px solid transparent;border-radius:0;
cursor:pointer;color:#64748b;font-size:13px;font-weight:500;
font-family:inherit;transition:all 0.15s;text-align:left}}
.nav-item:hover{{background:rgba(0,0,0,0.02);color:#1e293b}}
.nav-item.active{{background:rgba(245,158,11,0.04);color:#1e293b;font-weight:600;border-left-color:#f59e0b}}
.nav-icon{{width:18px;text-align:center;flex-shrink:0}}
.nav-divider{{height:1px;background:#e5e7eb;margin:10px 12px}}
.nav-label{{font-size:10px;font-weight:600;color:#94a3b8;
letter-spacing:0.08em;padding:6px 16px;text-transform:uppercase}}
.div-item{{width:100%;display:flex;align-items:center;gap:8px;
padding:7px 16px;background:transparent;border:none;border-left:3px solid transparent;border-radius:0;
cursor:pointer;color:#64748b;font-size:12px;font-weight:400;
font-family:inherit;transition:all 0.15s;text-align:left;margin-bottom:1px}}
.div-item:hover{{background:rgba(0,0,0,0.02)}}
.div-item.active{{background:rgba(245,158,11,0.04);color:#1e293b;border-left-color:#f59e0b}}
.div-num{{width:22px;height:22px;border-radius:6px;font-size:11px;font-weight:700;
display:inline-flex;align-items:center;justify-content:center;
background:#eef0f3;color:#64748b;flex-shrink:0;font-family:'JetBrains Mono',monospace}}
.div-item.active .div-num{{background:rgba(245,158,11,0.12);color:#b45309}}
.div-dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-left:auto}}

/* Agent sidebar */
.agent-sidebar{{display:none}}
.agent-sidebar .agent-new-btn{{width:calc(100% - 24px);margin:12px 12px 8px;padding:9px;
border-radius:8px;border:1px solid #e5e7eb;background:#ffffff;color:#1e293b;
font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s}}
.agent-sidebar .agent-new-btn:hover{{background:#f8fafc;border-color:#cbd5e1}}
.agent-sidebar .agent-chips{{padding:8px 12px;display:flex;flex-direction:column;gap:4px}}
.agent-sidebar .agent-chip{{padding:8px 12px;border-radius:8px;border:none;
background:#f8fafc;color:#64748b;font-size:12px;cursor:pointer;
font-family:inherit;text-align:left;transition:all .15s}}
.agent-sidebar .agent-chip:hover{{background:#f1f5f9;color:#1e293b}}
.agent-sessions{{padding:4px 8px;display:flex;flex-direction:column;gap:2px;
max-height:300px;overflow-y:auto}}
.session-item{{display:flex;flex-direction:column;padding:8px 10px;border-radius:8px;
background:transparent;color:#64748b;font-size:12px;cursor:pointer;
text-align:left;transition:all .15s;gap:2px;border:none;font-family:inherit;width:100%}}
.session-item:hover{{background:#f1f5f9;color:#1e293b}}
.session-item.active{{background:#fffbeb;color:#92400e;border-left:3px solid #f59e0b}}
.session-item-top{{display:flex;justify-content:space-between;align-items:center}}
.session-item-label{{font-weight:600;font-size:11px;color:#1e293b}}
.session-item.active .session-item-label{{color:#92400e}}
.session-item-count{{font-weight:400;font-size:10px;color:#94a3b8;
background:#f1f5f9;padding:1px 5px;border-radius:4px}}
.session-item-preview{{font-size:10.5px;color:#94a3b8;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis;max-width:170px}}
.session-item.active .session-item-preview{{color:#b45309}}
.session-item-del{{display:none;background:none;border:none;cursor:pointer;color:#cbd5e1;
font-size:14px;padding:0 2px;font-family:inherit;line-height:1}}
.session-item:hover .session-item-del{{display:block}}
.session-item-del:hover{{color:#ef4444}}
.session-empty{{font-size:11px;color:#94a3b8;padding:12px 4px;text-align:center;font-style:italic}}

/* ── Main content area ── */
.main-area{{flex:1;margin-left:220px;display:flex;flex-direction:column;min-height:calc(100vh - 56px)}}
.content{{overflow-y:auto;flex:1;padding:24px 28px;background:#f5f6f8}}

/* ── Views ── */
.view{{display:none;max-width:1100px;margin:0 auto;width:100%}}
.view.active{{display:block}}

/* Backtest sub-tabs */
.backtest-tabs{{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid #e5e7eb}}
.bt-tab{{padding:10px 20px;border:none;background:transparent;color:#64748b;font-size:.85rem;
font-weight:500;cursor:pointer;position:relative;font-family:inherit;transition:color .15s}}
.bt-tab:hover{{color:#1e293b}}
.bt-tab.active{{color:#1e293b;font-weight:600}}
.bt-tab.active::after{{content:'';position:absolute;bottom:-1px;left:8px;right:8px;
height:2px;background:#f59e0b;border-radius:1px}}
.bt-panel{{display:none}}
.bt-panel.active{{display:block}}

/* Dashboard sections within dashboard view */
.dashboard-section{{display:none;max-width:1100px;margin:0 auto}}
.dashboard-section.active{{display:block}}

/* System drawer - fullscreen modal */
.system-drawer{{position:fixed;inset:0;background:#f5f6f8;z-index:300;
transform:translateY(100%);transition:transform .35s ease;overflow-y:auto;padding:2rem}}
.system-drawer.open{{transform:translateY(0)}}
.drawer-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:299;display:none;
backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}}
.drawer-overlay.open{{display:block}}
.drawer-close{{position:fixed;top:1.2rem;right:1.5rem;background:#ffffff;border:1px solid #e5e7eb;
font-size:1.3rem;width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;
cursor:pointer;color:#64748b;z-index:301;transition:all .2s;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.drawer-close:hover{{color:#1e293b;background:#f8fafc}}

/* ── Cards ── */
.summary-card,.race-card{{background:#ffffff;border-radius:12px;padding:24px;
margin-bottom:24px;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04);
transition:all .2s ease}}
.race-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,0.08),0 2px 4px rgba(0,0,0,0.04)}}
.race-header{{display:flex;justify-content:space-between;align-items:baseline;
flex-wrap:wrap;gap:.5rem;margin-bottom:1rem}}
.race-header h2{{font-size:1.15rem;color:#1e293b;font-weight:700;letter-spacing:-0.02em}}
.race-meta{{color:#6b7280;font-size:.82rem}}
.summary-card h2{{font-size:1.15rem;color:#1e293b;margin-bottom:1rem;font-weight:700;letter-spacing:-0.02em}}
.table-wrap{{overflow-x:auto;border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:.84rem}}
thead{{z-index:5}}
th{{background:#f1f5f9;color:#64748b;text-transform:uppercase;font-size:.65rem;font-weight:600;
letter-spacing:.07em;padding:12px 16px;text-align:left;white-space:nowrap;border-bottom:1px solid #e5e7eb}}
td{{padding:12px 16px;border-bottom:1px solid #f1f5f9;color:#1e293b}}
tbody tr:nth-child(even) td{{background:#f9fafb}}
tbody tr:hover td{{background:#f1f5f9}}
.pos{{font-weight:800;color:#f59e0b;width:2rem;text-align:center;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.horse-name{{font-weight:600;white-space:nowrap;color:#1e293b}}
.score{{font-size:1rem;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.score strong{{color:#f59e0b}}
.bet{{color:#6b7280;font-family:'JetBrains Mono',monospace;font-size:.82rem}}
.driver{{color:#6b7280;font-size:.8rem;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rec-badge{{padding:.2rem .65rem;border-radius:20px;font-size:.72rem;font-weight:600;white-space:nowrap;
letter-spacing:.01em}}
.value-badge{{background:linear-gradient(135deg,#f59e0b,#d97706);color:#ffffff;padding:.15rem .5rem;border-radius:20px;
font-size:.62rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
text-transform:uppercase;letter-spacing:.04em}}
.winbet-badge{{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#ffffff;padding:.15rem .5rem;border-radius:20px;
font-size:.62rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
text-transform:uppercase;letter-spacing:.04em;animation:winbet-pulse 2.5s ease-in-out infinite}}
@keyframes winbet-pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(139,92,246,0.4)}}50%{{box-shadow:0 0 8px 3px rgba(139,92,246,0.2)}}}}

/* Vinnarspel summary card */
.winbet-summary-card{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;
padding:20px 24px;margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08);
border-left:4px solid #8b5cf6}}
.winbet-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.winbet-title{{font-weight:800;font-size:1.05rem;color:#1e293b}}
.winbet-roi{{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff;padding:.25rem .8rem;
border-radius:20px;font-size:.78rem;font-weight:700;letter-spacing:.02em}}
.winbet-desc{{font-size:.85rem;color:#6b7280;margin-bottom:12px}}
.winbet-table{{width:100%;border-collapse:collapse;font-size:.85rem}}
.winbet-table thead th{{text-align:left;font-weight:600;color:#6b7280;font-size:.75rem;
text-transform:uppercase;letter-spacing:.04em;padding:6px 10px;border-bottom:2px solid #e5e7eb}}
.winbet-table tbody td{{padding:8px 10px;border-bottom:1px solid #f3f4f6}}
.winbet-row:hover{{background:rgba(139,92,246,0.04)}}
.winbet-driver{{color:#6b7280;font-size:.8rem}}
.winbet-footnote{{margin-top:10px;font-size:.75rem;color:#94a3b8;font-style:italic}}
.orc-winbet{{background:rgba(139,92,246,0.08);color:#7c3aed;font-size:.72rem;font-weight:600;
padding:4px 8px;border-radius:6px;margin-top:6px;border:1px solid rgba(139,92,246,0.15)}}

/* ═══ Bet View ═══ */
.bet-view-container{{max-width:900px;margin:0 auto;padding:0 8px}}
.bet-strategy-card{{background:#fff;border-radius:12px;border:1px solid #e5e7eb;
padding:20px 24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.bet-strategy-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.bet-strategy-title{{font-weight:800;font-size:1.1rem;color:#1e293b}}
.bet-strategy-badge{{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff;
padding:.3rem 1rem;border-radius:20px;font-size:.82rem;font-weight:700}}
.bet-strategy-desc{{font-size:.85rem;color:#6b7280;margin-bottom:14px}}
.bet-strategy-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.bet-strategy-stat{{text-align:center;background:#f8f9fb;border-radius:8px;padding:10px 6px}}
.bet-strategy-num{{display:block;font-weight:800;font-size:1.05rem;color:#1e293b;
font-family:'JetBrains Mono',monospace}}
.bet-strategy-lbl{{display:block;font-size:.7rem;color:#6b7280;margin-top:2px;text-transform:uppercase;
letter-spacing:.04em}}

.bet-round-card{{background:#fff;border-radius:12px;border:1px solid #e5e7eb;
padding:20px 24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.bet-round-header{{margin-bottom:16px}}
.bet-round-title{{display:flex;align-items:center;gap:10px}}
.bet-round-title h3{{font-size:1.1rem;font-weight:700;color:#1e293b;margin:0}}
.bet-round-icon{{font-size:1.2rem}}
.bet-round-icon.live{{animation:pulse-glow 2s ease-in-out infinite}}
.bet-status-badge{{padding:.2rem .7rem;border-radius:20px;font-size:.72rem;font-weight:600}}
.bet-status-badge.live{{background:#fef2f2;color:#dc2626;border:1px solid rgba(239,68,68,0.2)}}
.bet-status-badge.finished{{background:#f0fdf4;color:#15803d;border:1px solid rgba(34,197,94,0.2)}}
.bet-round-summary{{font-size:.85rem;color:#6b7280;margin-top:6px}}

.bet-pnl{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;
padding:14px;background:#f8f9fb;border-radius:10px}}
.bet-pnl-item{{text-align:center}}
.bet-pnl-label{{display:block;font-size:.7rem;color:#6b7280;text-transform:uppercase;
letter-spacing:.04em;margin-bottom:2px}}
.bet-pnl-val{{display:block;font-weight:700;font-size:.95rem;color:#1e293b;
font-family:'JetBrains Mono',monospace}}

.bet-table{{width:100%;border-collapse:collapse;font-size:.85rem}}
.bet-table thead th{{text-align:left;font-weight:600;color:#6b7280;font-size:.72rem;
text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;border-bottom:2px solid #e5e7eb}}
.bet-table tbody td{{padding:10px;border-bottom:1px solid #f3f4f6}}
.bet-candidate-row:hover{{background:#f8f9fb}}
.bet-row-win{{background:rgba(34,197,94,0.04)}}
.bet-row-loss{{background:rgba(239,68,68,0.02)}}
.bet-result{{font-weight:600;white-space:nowrap}}
.bet-win{{color:#15803d}}
.bet-loss{{color:#94a3b8}}
.bet-pending{{color:#6b7280}}
.bet-horse{{font-weight:500}}
.bet-score{{font-weight:700;font-family:'JetBrains Mono',monospace}}
.bet-streck{{color:#6b7280}}
.bet-odds{{font-family:'JetBrains Mono',monospace;font-weight:600;color:#7c3aed}}
.bet-driver{{color:#6b7280;font-size:.8rem}}
.bet-driver-warn{{color:#f59e0b;font-size:.7rem;cursor:help}}
.bet-empty{{text-align:center;padding:60px 20px;color:#94a3b8}}
.bet-empty h3{{color:#1e293b;margin-bottom:8px}}
.bet-empty p{{font-size:.85rem;max-width:400px;margin:0 auto}}

/* ═══ Multi-profile cards ═══ */
.bet-round-header-top{{display:flex;align-items:center;gap:12px;margin-bottom:20px}}
.bet-round-header-top h2{{margin:0;font-size:1.3rem;font-weight:800;color:#1e293b}}
.bet-profile-card{{background:#fff;border-radius:12px;border:1px solid #e5e7eb;
margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.06);overflow:hidden}}
.bet-profile-header{{display:flex;align-items:center;justify-content:space-between;
padding:16px 20px;cursor:pointer;transition:background .15s;gap:12px;flex-wrap:wrap}}
.bet-profile-header:hover{{background:#f9fafb}}
.bet-profile-left{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.bet-profile-right{{display:flex;align-items:center}}
.bet-profile-badge{{color:#fff;padding:.25rem .75rem;border-radius:20px;font-size:.78rem;
font-weight:700;white-space:nowrap}}
.bet-profile-name{{font-weight:700;font-size:1rem;color:#1e293b}}
.bet-profile-desc{{font-size:.8rem;color:#6b7280}}
.bet-profile-bt{{font-size:.78rem;color:#94a3b8;font-family:'JetBrains Mono',monospace}}
.bet-profile-pnl{{display:flex;align-items:center;gap:16px;padding:8px 20px;
background:#f8f9fb;border-top:1px solid #f3f4f6;font-size:.85rem;color:#6b7280}}
.bet-profile-empty{{padding:20px;text-align:center;color:#94a3b8;font-size:.84rem;
border-top:1px solid #f3f4f6}}
.bet-profile-card .table-wrap{{padding:0 12px 12px}}
.bet-profile-card .bet-table{{margin-top:0}}

@media(max-width:768px){{
  .bet-profile-header{{flex-direction:column;align-items:flex-start;gap:8px}}
  .bet-profile-right{{width:100%}}
  .bet-round-header-top{{flex-wrap:wrap}}
}}

.bet-history-section{{margin-top:24px;background:#fff;border-radius:12px;border:1px solid #e5e7eb;
padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.bet-history-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}}
.bet-history-header h3{{margin:0;font-size:1rem;color:#1e293b}}
.bet-period-tabs{{display:flex;gap:4px;background:#f3f4f6;border-radius:8px;padding:3px}}
.bet-period-btn{{border:none;background:transparent;padding:5px 14px;font-size:.78rem;
font-weight:600;color:#6b7280;border-radius:6px;cursor:pointer;transition:all .15s}}
.bet-period-btn.active{{background:#fff;color:#1e293b;box-shadow:0 1px 2px rgba(0,0,0,0.08)}}
.bet-loading{{text-align:center;padding:40px;color:#94a3b8;font-size:.85rem}}
.bet-history-grid{{display:grid;gap:12px}}
.bet-hist-row{{display:grid;grid-template-columns:120px 1fr 80px 80px 90px;align-items:center;
padding:10px 12px;border-radius:8px;font-size:.84rem;border:1px solid #f3f4f6}}
.bet-hist-row:hover{{background:#f9fafb}}
.bet-hist-period{{font-weight:600;color:#1e293b}}
.bet-hist-bar{{height:6px;background:#f3f4f6;border-radius:3px;overflow:hidden}}
.bet-hist-bar-fill{{height:100%;border-radius:3px;transition:width .4s ease}}
.bet-hist-bar-pos{{background:linear-gradient(90deg,#22c55e,#16a34a)}}
.bet-hist-bar-neg{{background:linear-gradient(90deg,#ef4444,#dc2626)}}
.bet-hist-bets{{color:#6b7280;text-align:center;font-size:.78rem}}
.bet-hist-winrate{{color:#6b7280;text-align:center;font-size:.78rem}}
.bet-hist-pnl{{font-weight:700;text-align:right;font-family:'JetBrains Mono',monospace;font-size:.84rem}}
.bet-hist-pnl.pos{{color:#15803d}}
.bet-hist-pnl.neg{{color:#dc2626}}
.bet-cum-summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;
padding:16px;background:#f8fafc;border-radius:10px;border:1px solid #e5e7eb}}
.bet-cum-item{{text-align:center}}
.bet-cum-label{{display:block;font-size:.68rem;color:#6b7280;text-transform:uppercase;
letter-spacing:.5px;margin-bottom:2px}}
.bet-cum-val{{display:block;font-weight:700;font-size:1rem;color:#1e293b}}

@media(max-width:768px){{
  .bet-strategy-stats{{grid-template-columns:repeat(2,1fr)}}
  .bet-pnl{{grid-template-columns:repeat(2,1fr)}}
  .bet-cum-summary{{grid-template-columns:repeat(2,1fr)}}
  .bet-hist-row{{grid-template-columns:90px 1fr 60px 70px}}
  .bet-hist-winrate{{display:none}}
  .bet-period-tabs{{flex-wrap:wrap}}
}}
.value-picks{{background:rgba(245,166,35,0.04);border:1px solid rgba(245,166,35,0.12);border-radius:10px;
padding:.6rem 1rem;margin-bottom:1rem;font-size:.85rem;color:#b45309}}
.proffs-cell{{font-size:.8rem;font-family:'JetBrains Mono',monospace;white-space:nowrap;padding:4px 6px !important}}
.factor-cell{{position:relative;width:44px;min-width:44px;padding:12px 4px !important}}
.factor-bar{{position:absolute;left:4px;bottom:6px;height:3px;background:#f59e0b;opacity:.25;
border-radius:2px;top:auto;transition:width .3s ease}}
.factor-val{{position:relative;z-index:1;font-size:.75rem;color:#64748b;
font-family:'JetBrains Mono',monospace;text-align:center;display:block}}
.race-link{{color:#f59e0b;text-decoration:none;font-weight:600}}
.race-link:hover{{color:#d4911e;text-decoration:underline}}

/* Expandable rows */
.horse-row.clickable{{cursor:pointer}}
.horse-row.clickable:hover td{{background:#f1f5f9;transition:background .15s}}
.toggle-icon{{display:inline-block;font-size:.6rem;transition:transform .2s;color:#94a3b8;margin-right:.3rem}}
.horse-row.expanded .toggle-icon{{transform:rotate(90deg);color:#f59e0b}}
.detail-row{{background:#fafbfc}}
.detail-row.hidden{{display:none}}
.detail-row td{{padding:0}}
.detail-content{{padding:1rem 1.5rem 1.5rem 2.5rem;border-left:3px solid #f59e0b}}
.career-stats{{display:flex;gap:1.2rem;flex-wrap:wrap;margin-bottom:.6rem;font-size:.82rem}}
.career-item{{color:#6b7280}}
.career-item strong{{color:#1e293b;font-family:'JetBrains Mono',monospace;font-size:.82rem}}
.starts-table{{width:100%;font-size:.8rem}}
.starts-table th{{background:#f1f5f9;font-size:.62rem;padding:8px 10px;letter-spacing:.05em}}
.starts-table td{{padding:6px 10px;border-bottom:1px solid #f1f5f9}}
.starts-table tr:hover td{{background:#eef0f3}}
.start-date{{color:#6b7280;white-space:nowrap;font-size:.75rem}}
.km-time{{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:.82rem}}
.plac-badge{{font-weight:700}}
.no-starts{{color:#94a3b8;font-style:italic;font-size:.85rem}}

/* Trend */
.trend-cell{{white-space:nowrap;min-width:50px}}
.trend{{font-weight:600;font-size:.82rem}}
.trend-up{{color:#22c55e}}
.trend-down{{color:#ef4444}}
.trend.neutral{{color:#94a3b8}}

/* Sparkline */
.sparkline{{width:48px;height:20px;vertical-align:middle;margin-left:.3rem}}

/* Spårtrappa badge */
.stair-badge{{background:rgba(124,58,237,0.1);color:#7c3aed;padding:.15rem .6rem;border-radius:10px;
font-size:.75rem;font-weight:600;margin-left:.5rem;vertical-align:middle;border:1px solid rgba(124,58,237,0.15)}}
.race-name-label{{color:#6b7280;font-size:.78rem;margin-top:.2rem;font-style:italic}}

/* Race classification */
.race-class-badge{{padding:.2rem .6rem;border-radius:8px;font-size:.72rem;font-weight:700;margin-left:.5rem}}
.race-class-badge.spiklopp{{background:rgba(34,197,94,0.1);color:#16a34a}}
.race-class-badge.oppet{{background:rgba(234,179,8,0.1);color:#ca8a04}}
.race-class-badge.skrallopp{{background:rgba(239,68,68,0.1);color:#dc2626}}

/* Time highlights */
.time-highlight{{color:#b45309;font-family:monospace}}
.time-estimate{{color:#d97706;font-family:monospace;font-style:italic}}

/* Time range box */
.time-range-box{{background:rgba(245,166,35,0.06);border-radius:8px;padding:.4rem .8rem;margin-top:.4rem;
display:inline-block;font-size:.82rem;border-left:3px solid #f59e0b}}
.time-range-box.estimated{{border-left-color:#d97706}}
.time-label{{color:#6b7280;margin-right:.3rem}}
.time-meta{{color:#94a3b8;font-size:.72rem;font-style:italic;margin-left:.3rem}}

/* GT tag */
.gt-tag{{padding:.1rem .45rem;border-radius:8px;font-size:.7rem;font-weight:700;
letter-spacing:.04em;white-space:nowrap}}

/* Result column */
.result-cell{{text-align:center;min-width:45px}}
.result-plac{{font-weight:700;font-size:.95rem;font-variant-numeric:tabular-nums}}
.result-icon{{margin-left:.2rem;font-size:.75rem}}

/* Accuracy card */
.accuracy-card{{background:#ffffff;border-radius:12px;padding:24px;
margin-bottom:24px;border-left:4px solid #22c55e;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.accuracy-card h2{{color:#1e293b;margin-bottom:1rem;font-size:1.15rem;font-weight:700;letter-spacing:-0.01em}}
.accuracy-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}
.accuracy-stat{{text-align:center;background:#f9fafb;border-radius:10px;padding:1.2rem;border:1px solid #e5e7eb}}
.accuracy-stat .big-num{{font-size:1.8rem;font-weight:800;color:#f59e0b;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.accuracy-stat .label{{font-size:.68rem;text-transform:uppercase;color:#6b7280;
letter-spacing:.05em;margin-top:.3rem;font-weight:600}}
.accuracy-stat .sub{{font-size:.68rem;color:#94a3b8;margin-top:.2rem;
font-family:'JetBrains Mono',monospace}}
.accuracy-surprises{{margin-top:1rem;padding:.8rem 1rem;background:rgba(245,166,35,0.04);border-radius:10px;
font-size:.85rem;color:#b45309;border:1px solid rgba(245,166,35,0.1)}}

/* Hit/miss badges */
.hit-badge{{background:rgba(34,197,94,0.1);color:#16a34a;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(34,197,94,0.15)}}
.miss-badge{{background:rgba(239,68,68,0.1);color:#dc2626;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(239,68,68,0.15)}}
.partial-badge{{background:rgba(234,179,8,0.1);color:#ca8a04;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(234,179,8,0.15)}}

/* Skrällrisk */
.upset-badge{{padding:.25rem .7rem;border-radius:10px;font-size:.8rem;font-weight:700;white-space:nowrap}}
.upset-badge.high{{background:rgba(239,68,68,0.1);color:#dc2626;border:1px solid rgba(239,68,68,0.15)}}
.upset-badge.medium{{background:rgba(234,179,8,0.1);color:#ca8a04;border:1px solid rgba(234,179,8,0.15)}}
.upset-badge.pulse{{animation:pulse-glow 2s ease-in-out infinite}}
@keyframes pulse-glow{{0%,100%{{box-shadow:0 0 0 0 rgba(239,68,68,0.3)}}50%{{box-shadow:0 0 12px 4px rgba(239,68,68,0.15)}}}}
.upset-low{{color:#22c55e;font-size:.8rem;font-weight:600}}

/* Race card risk borders */
.race-card.upset-high{{border-left:4px solid #ef4444;box-shadow:0 1px 3px rgba(0,0,0,0.06),0 0 8px rgba(239,68,68,0.06)}}
.race-card.upset-medium{{border-left:4px solid #eab308;box-shadow:0 1px 3px rgba(0,0,0,0.06),0 0 8px rgba(234,179,8,0.05)}}

/* Garderingsrekommendation */
.upset-advice{{font-size:.82rem;font-weight:600;margin-top:.4rem;padding:.3rem .6rem;border-radius:6px}}
.upset-advice.high{{color:#dc2626;background:rgba(239,68,68,0.08)}}
.upset-advice.medium{{color:#ca8a04;background:rgba(234,179,8,0.06)}}
.upset-advice.low{{color:#16a34a;background:rgba(34,197,94,0.06)}}

/* Skrällkandidater */
.upset-candidates{{background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.12);
border-radius:8px;padding:.5rem .8rem;margin-top:.4rem;font-size:.82rem;color:#b45309;font-weight:500}}
.upset-desc{{color:#94a3b8;font-size:.75rem;font-weight:400}}

/* Heatmap risk bar */
.heatmap-wrapper{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;
padding:16px 20px;margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.heatmap-title{{font-weight:700;color:#1e293b;font-size:.95rem;margin-bottom:.8rem}}
.heatmap-bar{{display:flex;gap:4px;height:48px;align-items:flex-end}}
.heatmap-seg{{flex:1;border-radius:6px 6px 0 0;min-height:8px;cursor:pointer;
transition:all .2s;position:relative}}
.heatmap-seg:hover{{opacity:.8;transform:scaleY(1.08)}}
.heatmap-labels{{display:flex;gap:4px;margin-top:6px}}
.heatmap-lbl{{flex:1;text-align:center;font-size:.65rem;color:#6b7280;font-weight:600}}
.heatmap-summary{{color:#6b7280;font-size:.82rem;margin-top:.5rem}}

/* Hero section (spelvärde) — grid layout */
.hero-grid{{display:grid;grid-template-columns:260px 1fr;gap:24px;margin-bottom:24px}}
.hero-score-card{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;
padding:2rem;display:flex;flex-direction:column;align-items:center;justify-content:center;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.hero-score-label{{font-size:.7rem;text-transform:uppercase;color:#6b7280;letter-spacing:.06em;
font-weight:600;margin-top:.8rem}}
.hero-score-text{{font-size:1rem;font-weight:700;margin-top:.3rem}}
.hero-score-advice{{font-size:.82rem;color:#6b7280;margin-top:.2rem;text-align:center}}
.hero-right{{display:flex;flex-direction:column;gap:1rem}}
.hero-kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:.8rem}}
.hero-kpi{{text-align:center;border-radius:12px;padding:.9rem .5rem}}
.hero-kpi.blue{{background:rgba(96,165,250,0.08);border:1px solid rgba(96,165,250,0.12)}}
.hero-kpi.green{{background:rgba(74,222,128,0.08);border:1px solid rgba(74,222,128,0.12)}}
.hero-kpi.yellow{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.12)}}
.hero-kpi.purple{{background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.12)}}
.hero-kpi-val{{font-size:1.3rem;font-weight:800;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.hero-kpi.blue .hero-kpi-val{{color:#3b82f6}}
.hero-kpi.green .hero-kpi-val{{color:#22c55e}}
.hero-kpi.yellow .hero-kpi-val{{color:#d97706}}
.hero-kpi.purple .hero-kpi-val{{color:#7c3aed}}
.hero-kpi-lbl{{font-size:.62rem;text-transform:uppercase;color:#6b7280;letter-spacing:.05em;
font-weight:600;margin-top:.2rem}}

/* Overview race grid (card-based) */
.overview-race-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;margin-bottom:24px}}
.overview-race-card{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;
padding:20px;cursor:pointer;transition:all .2s;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.overview-race-card:hover{{border-color:#cbd5e1;box-shadow:0 4px 16px rgba(0,0,0,0.08);transform:translateY(-2px)}}
.orc-top{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.orc-num{{font-family:'JetBrains Mono',monospace;font-size:1.4rem;font-weight:800;color:#f59e0b;min-width:28px}}
.orc-label{{font-size:.72rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em}}
.orc-risk{{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:.72rem;font-weight:700;
padding:2px 8px;border-radius:20px}}
.orc-meta{{font-size:.75rem;color:#6b7280;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #f1f5f9}}
.orc-pick{{margin-bottom:10px}}
.orc-pick-main{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.orc-pick-main strong{{font-size:.88rem;color:#1e293b}}
.orc-driver{{font-size:.75rem;color:#94a3b8;margin-top:2px}}
.orc-stats{{display:flex;gap:16px;margin-bottom:8px}}
.orc-stat{{text-align:center}}
.orc-stat-val{{display:block;font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:700;color:#f59e0b}}
.orc-stat-lbl{{display:block;font-size:.6rem;text-transform:uppercase;color:#94a3b8;letter-spacing:.05em;font-weight:600}}
.orc-gard{{font-size:.75rem;color:#6b7280;padding-top:8px;border-top:1px solid #f1f5f9}}

/* Ranking table */
.ranking-card{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;padding:20px;
margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.ranking-header{{font-size:1rem;font-weight:700;color:#1e293b;margin-bottom:12px;letter-spacing:-0.01em}}
.ranking-table{{width:100%;border-collapse:collapse}}
.ranking-table th{{padding:8px 12px;font-size:.62rem;font-weight:700;text-transform:uppercase;
letter-spacing:.07em;text-align:center;border-bottom:2px solid #e5e7eb}}
.rank-th-race{{text-align:left !important;color:#64748b}}
.rank-th-a{{color:#92400e;background:rgba(254,243,199,0.5)}}
.rank-th-b{{color:#1e40af;background:rgba(219,234,254,0.5)}}
/* removed BC tier */
.rank-th-c{{color:#475569;background:rgba(241,245,249,0.5)}}
.rank-th-d{{color:#991b1b;background:rgba(254,226,226,0.4)}}
.ranking-table td{{padding:8px 12px;border-bottom:1px solid #f1f5f9;text-align:center;vertical-align:middle}}
.ranking-table tbody tr:nth-child(even) td{{background:#f9fafb}}
.rank-race{{font-weight:600;color:#1e293b;text-align:left !important;font-size:.82rem;
font-family:'JetBrains Mono',monospace;white-space:nowrap}}
.rank-cell{{min-width:60px}}
.rank-empty{{color:#cbd5e1}}
.rank-pill{{display:inline-flex;align-items:center;justify-content:center;
min-width:24px;height:22px;padding:0 6px;border-radius:20px;font-size:.72rem;
font-weight:600;margin:1px 2px;font-family:'JetBrains Mono',monospace}}
.rank-pill.rank-a{{background:#dcfce7;color:#15803d}}
.rank-pill.rank-b{{background:#dbeafe;color:#1e40af}}
.rank-pill.rank-c{{background:#fef3c7;color:#b45309}}
.rank-pill.rank-d{{background:#fee2e2;color:#991b1b}}

/* Weight legend & agreement indicator */
.wt-legend{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}}
.wt-pill{{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:12px;
font-size:.65rem;font-weight:500;letter-spacing:.01em}}
.wt-pill b{{font-weight:700}}
.rank-th-agree{{color:#64748b;background:rgba(241,245,249,0.5);width:36px;text-align:center !important}}
.rank-agree-cell{{width:36px;text-align:center !important}}
.rank-agree{{color:#16a34a;font-size:.85rem;font-weight:700}}
.rank-disagree{{color:#d97706;font-size:.85rem}}

/* Add source button & custom source badges */
.add-source-btn{{display:inline-flex;align-items:center;gap:3px;padding:3px 10px;border-radius:8px;
border:1px dashed #cbd5e1;background:#f8fafc;color:#64748b;font-size:.7rem;font-weight:600;
cursor:pointer;transition:all .2s;font-family:inherit}}
.add-source-btn:hover{{border-color:#f59e0b;color:#f59e0b;background:#fffbeb}}
.custom-src-list{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}}
.custom-src-badge{{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;
font-size:.65rem;font-weight:500;background:rgba(254,215,170,0.5);color:#b45309;border:1px solid rgba(180,83,9,0.2)}}
.custom-src-rm{{background:none;border:none;color:#b45309;cursor:pointer;font-size:.8rem;
padding:0 2px;opacity:.6;font-family:inherit}}
.custom-src-rm:hover{{opacity:1}}

/* Tips upload modal */
.tips-modal-overlay{{display:none;position:fixed;inset:0;z-index:500;background:rgba(0,0,0,0.5);
backdrop-filter:blur(4px);align-items:center;justify-content:center}}
.tips-modal-overlay.open{{display:flex}}
.tips-modal{{background:#fff;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,0.2);
width:min(520px,90vw);max-height:85vh;overflow-y:auto;animation:modalIn .25s ease}}
@keyframes modalIn{{from{{opacity:0;transform:scale(0.95)}}to{{opacity:1;transform:scale(1)}}}}
.tips-modal-header{{display:flex;justify-content:space-between;align-items:center;
padding:18px 24px;border-bottom:1px solid #e5e7eb}}
.tips-modal-header h3{{font-size:1rem;font-weight:700;color:#1e293b}}
.tips-modal-close{{background:none;border:none;font-size:1.3rem;color:#94a3b8;cursor:pointer;
padding:4px 8px;border-radius:6px;font-family:inherit}}
.tips-modal-close:hover{{background:#f1f5f9;color:#475569}}
.tips-modal-body{{padding:20px 24px}}
.tips-drop-zone{{border:2px dashed #cbd5e1;border-radius:12px;padding:32px 20px;text-align:center;
cursor:pointer;transition:all .25s;background:#fafbfc;position:relative}}
.tips-drop-zone:hover,.tips-drop-zone.dragover{{border-color:#f59e0b;background:#fffbeb}}
.tips-drop-zone input{{position:absolute;inset:0;opacity:0;cursor:pointer}}
.tips-drop-icon{{font-size:2rem;margin-bottom:8px}}
.tips-drop-text{{font-size:.85rem;color:#64748b}}
.tips-drop-text strong{{color:#1e293b}}
.tips-preview-img{{max-width:100%;max-height:200px;border-radius:8px;margin:12px 0;
box-shadow:0 2px 8px rgba(0,0,0,0.1)}}
.tips-source-input{{width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;
font-size:.85rem;font-family:inherit;margin:12px 0;outline:none;transition:border-color .2s}}
.tips-source-input:focus{{border-color:#f59e0b}}
.tips-parsing{{text-align:center;padding:20px;color:#64748b}}
.tips-parsing .spinner{{display:inline-block;width:28px;height:28px;border:3px solid #e5e7eb;
border-top-color:#f59e0b;border-radius:50%;animation:spin 0.8s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.tips-preview-data{{background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;
padding:14px;margin:12px 0;max-height:300px;overflow-y:auto}}
.tips-preview-data h4{{font-size:.8rem;font-weight:700;color:#1e293b;margin-bottom:8px}}
.tips-preview-table{{width:100%;font-size:.75rem;border-collapse:collapse}}
.tips-preview-table th{{text-align:left;padding:4px 8px;color:#6b7280;font-weight:600;
border-bottom:1px solid #e5e7eb}}
.tips-preview-table td{{padding:4px 8px;color:#334155;border-bottom:1px solid #f1f5f9}}
.tips-modal-footer{{display:flex;gap:8px;justify-content:flex-end;padding:16px 24px;
border-top:1px solid #e5e7eb}}
.tips-btn{{padding:8px 18px;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;
font-family:inherit;border:none;transition:all .2s}}
.tips-btn-secondary{{background:#f1f5f9;color:#475569}}
.tips-btn-secondary:hover{{background:#e2e8f0}}
.tips-btn-primary{{background:#f59e0b;color:#fff}}
.tips-btn-primary:hover{{background:#d97706}}
.tips-btn-primary:disabled{{background:#e5e7eb;color:#94a3b8;cursor:not-allowed}}
.tips-hot-info{{margin-top:8px;font-size:.72rem;color:#64748b;line-height:1.5}}
.tips-hot-info strong{{color:#1e293b}}

/* Track record banner */
.track-banner{{background:#ffffff;border-radius:12px;border:1px solid #e5e7eb;
padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;justify-content:space-between;
flex-wrap:wrap;gap:1rem;box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.track-stat{{text-align:center;min-width:80px}}
.track-stat-val{{font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:700;color:#22c55e}}
.track-stat-lbl{{font-size:.65rem;text-transform:uppercase;color:#6b7280;letter-spacing:.04em;font-weight:600}}

/* Legacy summary card (kept for compatibility) */
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem}}
.race-summary-card{{background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;
padding:1.2rem;cursor:pointer;transition:all .25s;position:relative;overflow:hidden;
box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.race-summary-card:hover{{border-color:#cbd5e1;box-shadow:0 4px 12px rgba(0,0,0,0.08);transform:translateY(-2px)}}
.rsc-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.6rem}}
.rsc-num{{font-size:1.5rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}}
.rsc-risk{{width:8px;height:8px;border-radius:50%}}
.rsc-risk.high{{background:#ef4444;box-shadow:0 0 6px rgba(239,68,68,0.3)}}
.rsc-risk.medium{{background:#eab308;box-shadow:0 0 6px rgba(234,179,8,0.2)}}
.rsc-risk.low{{background:#22c55e}}
.rsc-pick{{margin-bottom:.5rem}}
.rsc-pick strong{{color:#1e293b;font-size:.9rem}}
.rsc-score-row{{display:flex;align-items:center;gap:.6rem;margin-bottom:.4rem}}
.rsc-score-num{{font-size:1.1rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}}
.streck-bar{{flex:1;height:4px;background:#e5e7eb;border-radius:2px;overflow:hidden}}
.streck-bar div{{height:100%;background:#f59e0b;border-radius:2px;transition:width .5s ease}}
.streck-pct{{font-size:.72rem;color:#6b7280;font-weight:600;min-width:32px;text-align:right}}
.rsc-gard{{font-size:.78rem;color:#6b7280;margin-bottom:.4rem}}
.rsc-result{{display:flex;align-items:center;gap:.5rem;margin-top:.4rem;padding-top:.4rem;
border-top:1px solid #e5e7eb;font-size:.82rem}}
.rsc-winner{{color:#6b7280}}

/* Spelvärde bar (kept for compatibility) */
.spelvarde-bar{{background:#ffffff;border-radius:14px;border:1px solid #e5e7eb;
padding:1rem 1.5rem;margin-bottom:1.5rem;display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap;
border-left:4px solid var(--sv-color,#f59e0b);box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.sv-score-box{{display:flex;align-items:center;gap:.8rem}}
.sv-score{{font-size:2.2rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}}
.sv-label{{font-size:.72rem;text-transform:uppercase;color:#6b7280;letter-spacing:.05em;font-weight:600}}
.sv-text{{font-size:1rem;font-weight:700}}
.sv-advice{{font-size:.88rem;margin-top:.2rem;color:#6b7280}}
.sv-meta{{display:flex;gap:1.2rem;margin-left:auto;flex-wrap:wrap}}
.sv-meta-item{{text-align:center;min-width:60px}}
.sv-meta-val{{display:block;font-size:1.1rem;font-weight:700;color:#1e293b;font-variant-numeric:tabular-nums}}
.sv-meta-lbl{{display:block;font-size:.65rem;text-transform:uppercase;color:#6b7280;letter-spacing:.04em;font-weight:600}}

/* System section */
.system-section h2{{margin-bottom:1rem;color:#1e293b}}
.system-card{{background:#ffffff;border-radius:12px;padding:20px 24px;margin-bottom:16px;
border-left:4px solid #f59e0b;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.system-card.skipped{{border-left-color:#cbd5e1;opacity:.5}}
.system-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem;flex-wrap:wrap;gap:.5rem}}
.system-header h3{{font-size:1rem;color:#1e293b;margin:0;font-weight:700}}
.system-meta{{color:#94a3b8;font-size:.78rem;font-style:italic}}
.system-stats{{display:flex;gap:1.5rem;margin-bottom:.8rem;flex-wrap:wrap}}
.sys-stat{{text-align:center}}
.sys-val{{display:block;font-size:1.2rem;font-weight:700;color:#f59e0b;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.sys-lbl{{display:block;font-size:.68rem;text-transform:uppercase;color:#6b7280;letter-spacing:.04em;font-weight:600}}
.system-table th{{font-size:.68rem;padding:.4rem .5rem}}
.system-table td{{padding:.4rem .5rem;font-size:.82rem}}
.system-picks strong{{color:#f59e0b}}
.conf-badge{{padding:.15rem .5rem;border-radius:10px;font-size:.75rem;font-weight:600}}
.conf-badge.high{{background:rgba(34,197,94,0.1);color:#16a34a}}
.conf-badge.medium{{background:rgba(234,179,8,0.08);color:#ca8a04}}
.conf-badge.low{{background:rgba(239,68,68,0.08);color:#dc2626}}
.skip-reason{{color:#94a3b8;font-style:italic;font-size:.85rem}}
.spik-badge{{background:#f59e0b;color:#0f1117;padding:.1rem .45rem;border-radius:8px;
font-size:.65rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
box-shadow:0 1px 4px rgba(245,166,35,0.3)}}
.system-pill{{background:#f59e0b !important;color:#0f1117 !important;font-weight:700}}

/* LIVE backlog badges */
.live-badge{{background:#ef4444;color:white;padding:.1rem .4rem;border-radius:8px;
font-size:.62rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
animation:pulse-live 2s ease-in-out infinite;box-shadow:0 0 8px rgba(239,68,68,0.4)}}
@keyframes pulse-live{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
.live-result-badge{{background:rgba(234,179,8,0.08);color:#ca8a04;padding:.15rem .5rem;
border-radius:8px;font-size:.75rem;font-weight:600;white-space:nowrap}}
.live-result-badge.live-allright{{background:rgba(34,197,94,0.08);color:#16a34a;
animation:pulse-live 2s ease-in-out infinite}}

/* Backlog section */
.backlog-section h2{{margin-bottom:1rem;color:#1e293b}}
.backlog-summary{{display:flex;gap:1.2rem;flex-wrap:wrap;margin-bottom:1rem;
background:#ffffff;border-radius:12px;padding:1.2rem;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.bl-stat{{text-align:center;min-width:80px}}
.bl-val{{display:block;font-size:1.3rem;font-weight:700;color:#f59e0b;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.bl-lbl{{display:block;font-size:.68rem;text-transform:uppercase;color:#6b7280;letter-spacing:.04em;font-weight:600}}
.backlog-table th{{font-size:.68rem;padding:.4rem .5rem}}
.backlog-table td{{padding:.4rem .5rem;font-size:.82rem}}
.bl-expandable{{cursor:pointer}}
.bl-expandable:hover{{background:#f8fafc}}
.bl-expand-icon{{display:inline-block;transition:transform .2s;color:#94a3b8}}
.bl-expand-icon.open{{transform:rotate(90deg)}}
.bl-detail-table{{width:100%;border-collapse:collapse;background:#f8fafc}}
.bl-race-row td{{padding:.25rem .5rem;border-top:1px solid #f1f5f9;font-size:.78rem}}
.bl-detail-container td{{background:#f8fafc}}

/* Strategi-filter knappar */
.strat-filter{{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}}
.strat-btn{{padding:.4rem 1rem;border-radius:10px;background:#ffffff;color:#64748b;
border:1px solid #e5e7eb;cursor:pointer;font-size:.8rem;font-weight:600;transition:all .2s;font-family:inherit}}
.strat-btn:hover{{background:#f8fafc;color:#1e293b}}
.strat-btn.active{{background:#f59e0b;color:#0f1117;border-color:#f59e0b}}
.strat-tag{{padding:.15rem .5rem;border-radius:10px;font-size:.72rem;font-weight:700;white-space:nowrap}}

/* Speltyp-filter knappar */
.gt-filter-row{{margin-bottom:.8rem}}
.gt-btn{{padding:.4rem 1rem;border-radius:10px;background:#ffffff;color:#64748b;
border:1px solid #e5e7eb;cursor:pointer;font-size:.8rem;font-weight:600;transition:all .2s;font-family:inherit}}
.gt-btn:hover{{background:#f8fafc;color:#1e293b}}
.gt-btn.active{{background:var(--gt-color,#f59e0b);color:#0f1117;border-color:var(--gt-color,#f59e0b)}}

/* Statistik-sektion */
.stats-section h2{{margin-bottom:1rem;color:#1e293b}}
.stats-block{{background:#ffffff;border-radius:12px;padding:20px 24px;margin-bottom:16px;
border-left:4px solid #f59e0b;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04)}}
.stats-block h3{{font-size:.95rem;color:#1e293b;margin-bottom:.8rem;font-weight:700}}
.stats-table{{width:100%;border-collapse:collapse;font-size:.82rem}}
.stats-table th{{background:#f1f5f9;color:#64748b;text-transform:uppercase;font-size:.65rem;
letter-spacing:.06em;padding:10px 12px;text-align:left;white-space:nowrap;font-weight:600;border-bottom:1px solid #e5e7eb}}
.stats-table td{{padding:10px 12px;border-bottom:1px solid #f1f5f9}}
.stats-table tbody tr:nth-child(even) td{{background:#f9fafb}}
.stats-table tr:hover td{{background:#f1f5f9}}

/* ── Agent Chat View ── */
.agent-layout{{display:flex;flex-direction:column;height:calc(100vh - 56px - 48px);max-width:800px;margin:0 auto}}
.agent-header{{padding:2rem 0 1.2rem;text-align:center}}
.agent-header h2{{font-size:1.5rem;font-weight:800;color:#1e293b;margin-bottom:.4rem;letter-spacing:-0.02em}}
.agent-header p{{font-size:.88rem;color:#6b7280}}
.chat-suggestions{{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-bottom:1.2rem}}
.chat-suggest-btn{{padding:8px 16px;border-radius:20px;border:1px solid #e5e7eb;
background:#ffffff;color:#64748b;font-size:.82rem;cursor:pointer;transition:all .2s;
font-family:inherit;box-shadow:0 1px 2px rgba(0,0,0,0.04)}}
.chat-suggest-btn:hover{{border-color:#f59e0b;color:#b45309;background:#fffbeb;
box-shadow:0 2px 8px rgba(245,166,35,0.1)}}
.chat-messages{{flex:1;overflow-y:auto;padding:.5rem 0;margin-bottom:1rem;display:flex;flex-direction:column;gap:12px}}
.chat-msg{{padding:12px 16px;border-radius:16px;max-width:82%;
line-height:1.7;font-size:.88rem;word-wrap:break-word;position:relative}}
.chat-msg .chat-ts{{display:block;font-size:.62rem;color:#94a3b8;margin-top:6px;font-weight:400}}
.chat-msg.user{{background:#3b82f6;color:#ffffff;margin-left:auto;
border-bottom-right-radius:4px;box-shadow:0 1px 4px rgba(59,130,246,0.2)}}
.chat-msg.user .chat-ts{{color:rgba(255,255,255,0.6)}}
.chat-msg.ai{{background:#ffffff;color:#1e293b;border:1px solid #e5e7eb;
border-bottom-left-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.chat-msg.ai strong{{color:#b45309}}
.chat-msg.ai code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:.82rem}}
.chat-msg.ai pre{{background:#1e293b;color:#e2e8f0;padding:12px;border-radius:8px;overflow-x:auto;
font-family:'JetBrains Mono',monospace;font-size:.78rem;margin:8px 0;line-height:1.5}}
.chat-msg.ai ul,.chat-msg.ai ol{{padding-left:1.2rem;margin:6px 0}}
.chat-msg.ai li{{margin-bottom:4px}}
.chat-table{{width:100%;border-collapse:collapse;font-size:.82rem;margin:10px 0;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb}}
.chat-table th{{background:#f1f5f9;color:#64748b;text-transform:uppercase;font-size:.68rem;
letter-spacing:.06em;padding:8px 10px;text-align:left;font-weight:600;border-bottom:1px solid #e5e7eb}}
.chat-table td{{padding:8px 10px;border-bottom:1px solid #f1f5f9}}
.chat-table tbody tr:nth-child(even) td{{background:#f9fafb}}
.chat-table tbody tr:hover td{{background:#f1f5f9}}
.chat-msg.streaming::after{{content:'';display:inline-block;width:2px;height:14px;background:#f59e0b;
margin-left:2px;vertical-align:text-bottom;animation:blink-cursor .8s infinite}}
@keyframes blink-cursor{{0%,100%{{opacity:1}}50%{{opacity:0}}}}
.typing-indicator{{display:flex;gap:4px;padding:12px 16px;align-items:center}}
.typing-dot{{width:6px;height:6px;border-radius:50%;background:#94a3b8;
animation:typing-bounce 1.4s ease-in-out infinite}}
.typing-dot:nth-child(2){{animation-delay:.2s}}
.typing-dot:nth-child(3){{animation-delay:.4s}}
@keyframes typing-bounce{{0%,60%,100%{{transform:translateY(0)}}30%{{transform:translateY(-6px)}}}}
.chat-input-bar{{display:flex;gap:8px;padding:14px 0;border-top:1px solid #e5e7eb}}
.chat-input-bar input{{flex:1;padding:12px 16px;border-radius:24px;border:1px solid #e5e7eb;
background:#ffffff;color:#1e293b;font-size:.88rem;outline:none;font-family:inherit;
transition:border-color .2s,box-shadow .2s}}
.chat-input-bar input:focus{{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,166,35,0.1)}}
.chat-input-bar input::placeholder{{color:#94a3b8}}
.chat-input-bar button{{width:44px;height:44px;border-radius:50%;border:none;
background:#f59e0b;color:#ffffff;font-weight:700;cursor:pointer;font-size:1.1rem;
transition:all .2s;font-family:inherit;display:flex;align-items:center;justify-content:center;
flex-shrink:0}}
.chat-input-bar button:hover{{background:#d97706;box-shadow:0 2px 8px rgba(245,166,35,0.3);transform:scale(1.05)}}
.chat-input-bar button:disabled{{opacity:.4;cursor:not-allowed;transform:none}}

/* Backlog lazy-load */
.bl-older{{display:none}}
.bl-show-all .bl-older{{display:table-row}}

/* ── Round navigator ── */
.round-nav{{display:flex;align-items:center;gap:6px;justify-content:center}}
.round-nav-btn{{width:28px;height:28px;border-radius:8px;border:1px solid #e5e7eb;
background:#ffffff;color:#64748b;cursor:pointer;display:flex;align-items:center;
justify-content:center;transition:all .15s;flex-shrink:0;padding:0}}
.round-nav-btn:hover:not([disabled]){{background:#f8fafc;border-color:#cbd5e1;color:#1e293b}}
.round-nav-btn[disabled]{{opacity:.3;cursor:not-allowed}}
.round-nav-label{{font-size:12px;font-weight:700;color:#1e293b;white-space:nowrap;
letter-spacing:-0.01em;text-align:center;flex:1}}
.sidebar-round-select{{width:100%;margin-top:8px;padding:6px 10px;border-radius:8px;
border:1px solid #e5e7eb;background:#ffffff;color:#1e293b;font-size:12px;
cursor:pointer;outline:none;font-family:inherit;transition:border-color .2s,box-shadow .2s}}
.sidebar-round-select:hover{{border-color:#cbd5e1;box-shadow:0 1px 4px rgba(0,0,0,0.06)}}

/* ── Strategy cards (ROI tab) ── */
.sc-section{{margin-bottom:24px}}
.sc-group-title{{font-size:.8rem;font-weight:700;color:#94a3b8;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:12px}}
.sc-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-bottom:16px}}
.sc-card{{background:#ffffff;border-radius:12px;padding:18px;border:1px solid #e5e7eb;
box-shadow:0 1px 3px rgba(0,0,0,0.06);transition:all .2s}}
.sc-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,0.08);transform:translateY(-1px)}}
.sc-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.sc-name{{font-size:.85rem;font-weight:700}}
.sc-rounds{{font-size:.7rem;color:#94a3b8;font-weight:500}}
.sc-roi{{font-size:1.6rem;font-weight:800;font-family:'JetBrains Mono',monospace;
font-variant-numeric:tabular-nums;letter-spacing:-0.02em}}
.sc-label{{font-size:.6rem;text-transform:uppercase;color:#94a3b8;letter-spacing:.06em;
font-weight:600;margin-bottom:10px}}
.sc-metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
padding-top:10px;border-top:1px solid #f1f5f9}}
.sc-metric{{text-align:center}}
.sc-metric-val{{display:block;font-size:.82rem;font-weight:700;color:#1e293b;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.sc-metric-lbl{{display:block;font-size:.55rem;text-transform:uppercase;color:#94a3b8;
letter-spacing:.04em;font-weight:600;margin-top:2px}}
.sc-variant-toggle{{text-align:center;margin:4px 0 16px}}
.sc-toggle-btn{{padding:6px 18px;border-radius:20px;border:1px solid #e5e7eb;
background:#ffffff;color:#64748b;font-size:.78rem;font-weight:600;cursor:pointer;
font-family:inherit;transition:all .2s}}
.sc-toggle-btn:hover{{border-color:#f59e0b;color:#b45309;background:#fffbeb}}

/* ── Monthly bar chart (CSS-only) ── */
.mbar-chart{{display:flex;flex-direction:column;gap:6px}}
.mbar-row{{display:grid;grid-template-columns:70px 1fr 60px 80px 1fr;gap:8px;align-items:center;
padding:6px 0;border-bottom:1px solid #f8f9fb}}
.mbar-row:last-child{{border-bottom:none}}
.mbar-label{{font-size:.78rem;font-weight:600;color:#1e293b;white-space:nowrap}}
.mbar-track{{height:20px;background:#f8f9fb;border-radius:4px;overflow:hidden}}
.mbar-fill{{height:100%;border-radius:2px;transition:width .5s ease}}
.mbar-val{{font-size:.78rem;font-weight:700;text-align:right;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.mbar-netto{{font-size:.72rem;font-weight:600;text-align:right;
font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}}
.mbar-meta{{font-size:.65rem;color:#94a3b8;white-space:nowrap}}

/* ── Stats filter bar ── */
.stats-filter-bar{{margin-bottom:16px;padding:14px 16px;background:#f9fafb;
border-radius:10px;border:1px solid #e5e7eb}}

/* ── Historik aggregated cards ── */
.hist-agg-wrap{{margin-bottom:20px}}
.hist-agg-title{{font-size:.8rem;font-weight:700;color:#94a3b8;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:12px}}
.hist-agg-grid-outer{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.hist-agg-card{{background:#ffffff;border-radius:10px;padding:14px 16px;border:1px solid #e5e7eb;
box-shadow:0 1px 2px rgba(0,0,0,0.04)}}
.hist-agg-name{{font-size:.82rem;font-weight:700;margin-bottom:8px}}
.hist-agg-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 12px}}
.hist-agg-item{{display:flex;flex-direction:column}}
.hist-agg-val{{font-size:.95rem;font-weight:700;font-family:'JetBrains Mono',monospace;
font-variant-numeric:tabular-nums;color:#1e293b}}
.hist-agg-lbl{{font-size:.55rem;text-transform:uppercase;color:#94a3b8;letter-spacing:.04em;font-weight:600}}

/* ── Streak bar ── */
.streak-bar{{display:flex;gap:4px;margin-bottom:16px;padding:10px 14px;
background:#ffffff;border-radius:10px;border:1px solid #e5e7eb}}
.streak-dot{{width:18px;height:18px;border-radius:6px;flex-shrink:0}}
.streak-hit{{background:rgba(34,197,94,0.18)}}
.streak-partial{{background:rgba(234,179,8,0.18)}}
.streak-miss{{background:rgba(239,68,68,0.18)}}

/* ── Backlog filter bar ── */
.bl-filter-bar{{margin-bottom:12px;padding:12px 14px;background:#f9fafb;
border-radius:10px;border:1px solid #e5e7eb}}
.bl-date-note{{font-size:.75rem;color:#94a3b8;margin-bottom:10px;font-style:italic}}

/* Hamburger menu button */
.hamburger-btn{{display:none;background:none;border:none;cursor:pointer;padding:6px;color:#1e293b}}
.hamburger-btn svg{{display:block}}
.sidebar-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.3);z-index:99;
backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px)}}

/* ── Mobile bottom nav ── */
.mobile-bottom-nav{{display:none;position:fixed;bottom:0;left:0;right:0;z-index:200;
background:#ffffff;border-top:1px solid #e5e7eb;padding:6px 0 env(safe-area-inset-bottom,6px);
box-shadow:0 -2px 10px rgba(0,0,0,0.06)}}
.mobile-bottom-nav button{{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;
background:none;border:none;padding:6px 0;color:#94a3b8;font-size:.65rem;font-weight:600;
font-family:inherit;cursor:pointer;transition:color .2s}}
.mobile-bottom-nav button.active{{color:#f59e0b}}
.mobile-bottom-nav button svg{{width:20px;height:20px}}

/* ── Responsive ── */
@media(max-width:768px){{
  .top-navbar{{padding:0 12px}}
  .hamburger-btn{{display:block}}
  .nav-tabs{{display:none}}
  .mobile-bottom-nav{{display:flex}}
  .sidebar{{transform:translateX(-100%);transition:transform .3s ease;z-index:150;
  background:#f9fafb;box-shadow:4px 0 20px rgba(0,0,0,0.1)}}
  .sidebar.mobile-open{{transform:translateX(0)}}
  .sidebar-overlay.open{{display:block}}
  .main-area{{margin-left:0 !important;padding-bottom:60px}}
  .content{{padding:16px}}
  .system-drawer{{padding:1rem}}
  .round-select{{max-width:160px;font-size:.78rem}}
  .summary-card,.race-card{{padding:16px;border-radius:12px}}
  .hero-grid{{grid-template-columns:1fr}}
  .hero-kpis{{grid-template-columns:repeat(2,1fr)}}
  .overview-race-grid{{grid-template-columns:1fr}}
  .ranking-table{{font-size:.75rem}}
  .rank-pill{{min-width:20px;height:20px;font-size:.65rem;padding:0 4px}}
  .wt-legend{{gap:4px}}
  .wt-pill{{font-size:.58rem;padding:1px 6px}}
  .rank-agree,.rank-disagree{{font-size:.72rem}}
  table{{font-size:.75rem}}
  th,td{{padding:8px 8px}}
  .driver{{display:none}}
  .detail-content{{padding-left:1rem}}
  .factor-cell{{display:none}}
  .heatmap-wrapper{{overflow-x:auto}}
  .agent-layout{{height:auto;min-height:calc(100vh - 56px - 48px)}}
  .chat-msg{{max-width:90%}}
  .nav-clock{{display:none}}
  .sc-grid{{grid-template-columns:1fr}}
  .hist-agg-grid-outer{{grid-template-columns:1fr}}
  .mbar-row{{grid-template-columns:55px 1fr 50px;gap:4px}}
  .mbar-netto,.mbar-meta{{display:none}}
  .bl-filter-bar,.stats-filter-bar{{padding:10px}}
  .strat-filter{{gap:.3rem}}
  .strat-btn,.gt-btn{{padding:.3rem .6rem;font-size:.72rem}}
}}
</style>
</head>
<body>

<!-- ── Top Navbar ── -->
<nav class="top-navbar">
  <button class="hamburger-btn" onclick="toggleMobileSidebar()">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M3 12h18M3 6h18M3 18h18"/>
    </svg>
  </button>
  <div class="nav-brand">Kungens <span>Trav</span></div>
  <div class="nav-tabs">
    <button class="nav-tab active" data-view="dashboard" onclick="showView('dashboard')">Dashboard</button>
    <button class="nav-tab" data-view="bet" onclick="showView('bet')">Bet</button>
    <button class="nav-tab" data-view="agent" onclick="showView('agent')">Agent</button>
    <button class="nav-tab" data-view="backtest" onclick="showView('backtest')">Backtest</button>
  </div>
  <div class="nav-right">
    <span class="nav-clock" id="nav-clock"></span>
    {round_dropdown}
  </div>
</nav>
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleMobileSidebar()"></div>

<div class="app-layout">

<!-- ── Left Sidebar ── -->
<aside class="sidebar" id="sidebar">
  <!-- Dashboard sidebar content -->
  <div id="sidebar-dashboard">
    <div class="sb-info">
      {round_navigator}
      <div class="sb-info-meta" style="margin-top:4px">{track_name}</div>
    </div>
    <div class="sb-nav">
      <button class="nav-item active" data-section="summary" onclick="showSection('summary')">
        <span class="nav-icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><path d="M12 8v8m-4-4h8"/></svg>
        </span><span>Översikt</span>
      </button>
      {system_drawer_btn}
      <div class="nav-divider"></div>
      <div class="nav-label">LOPP</div>
      {sidebar_div_items}
    </div>
  </div>
  <!-- Agent sidebar content -->
  <div id="sidebar-agent" class="agent-sidebar">
    <button class="agent-new-btn" onclick="newChatSession()">+ Ny session</button>
    <div class="nav-divider"></div>
    <div class="nav-label">SESSIONER</div>
    <div class="agent-sessions" id="agent-sessions"></div>
    <div class="nav-divider"></div>
    <div class="nav-label">SNABBFRÅGOR</div>
    <div class="agent-chips">
      <button class="agent-chip" onclick="askSuggestion('Sammanfatta omgången')">Sammanfatta omgången</button>
      <button class="agent-chip" onclick="askSuggestion('Vilka lopp har högst skällrisk?')">Skällrisker</button>
      <button class="agent-chip" onclick="askSuggestion('Ge mig dina bästa spikar')">Spikar</button>
      <button class="agent-chip" onclick="askSuggestion('Finns det value-hästar?')">Value-hästar</button>
    </div>
  </div>
</aside>

<div class="main-area" id="main-area">
<main class="content">

<!-- ═══ Dashboard View ═══ -->
<div class="view active" id="view-dashboard">
  <section id="s-summary" class="dashboard-section active">
    {risk_bar}
    {winbet_summary}
    {sv_bar}
    {ranking}
    {consensus_ranking}
    {accuracy}
    {summary}
  </section>
  {race_sections}
</div>

<!-- ═══ Bet View ═══ -->
<div class="view" id="view-bet">
  <div class="bet-view-container">
    {bet_view}
  </div>
</div>

<!-- ═══ Agent View ═══ -->
<div class="view" id="view-agent">
  <div class="agent-layout">
    <div class="agent-header">
      <h2>AI Analys</h2>
      <p>Fråga om omgången, strategier eller historisk statistik</p>
    </div>
    <div class="chat-suggestions" id="agent-suggestions">
      <button class="chat-suggest-btn" onclick="askSuggestion('Sammanfatta omgången')">Sammanfatta</button>
      <button class="chat-suggest-btn" onclick="askSuggestion('Vilka lopp har högst skällrisk?')">Skällrisker</button>
      <button class="chat-suggest-btn" onclick="askSuggestion('Ge mig dina bästa spikar')">Spikar</button>
      <button class="chat-suggest-btn" onclick="askSuggestion('Finns det value-hästar?')">Value</button>
    </div>
    <div class="chat-messages" id="chat-messages"></div>
    <div class="chat-input-bar">
      <input id="chat-input" placeholder="Ställ en fråga om omgången...">
      <button id="chat-send" onclick="sendChat()" title="Skicka">&#8593;</button>
    </div>
  </div>
</div>

<!-- ═══ Backtest View (ROI + historik) ═══ -->
<div class="view" id="view-backtest">
  <div class="backtest-tabs">
    <button class="bt-tab active" data-bt="roi" onclick="showBacktestTab('roi')">ROI &amp; Statistik</button>
    <button class="bt-tab" data-bt="history" onclick="showBacktestTab('history')">Historik</button>
  </div>
  <div class="bt-panel active" id="bt-roi">
    {stats_section if stats_section else '<div style="text-align:center;padding:60px 20px;color:#94a3b8"><div style="font-size:2.5rem;margin-bottom:12px">📊</div><h3 style="color:#1e293b;margin-bottom:8px">Ingen backtest-data tillgänglig</h3><p style="max-width:400px;margin:0 auto;font-size:.85rem">Backlog-filen (backlog.json) behöver laddas upp till servern eller synkas via Supabase för att visa ROI-statistik.</p></div>'}
  </div>
  <div class="bt-panel" id="bt-history">
    {backlog_section if backlog_section else '<div style="text-align:center;padding:60px 20px;color:#94a3b8"><div style="font-size:2.5rem;margin-bottom:12px">📋</div><h3 style="color:#1e293b;margin-bottom:8px">Ingen historik tillgänglig</h3><p style="max-width:400px;margin:0 auto;font-size:.85rem">Ladda upp backlog.json för att se alla historiska baktester.</p></div>'}
  </div>
</div>

</main>
</div><!-- /main-area -->

<!-- System drawer (modal) -->
<div class="system-drawer" id="system-drawer">
  <button class="drawer-close" onclick="closeDrawer()">&times;</button>
  {system_section}
</div>
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>

</div><!-- /app-layout -->

<script>
// ── View switching (top navbar tabs) ──
function showView(name){{
  // Toggle views
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  const target=document.getElementById('view-'+name);
  if(target) target.classList.add('active');
  // Toggle nav tabs (desktop + mobile)
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  const tab=document.querySelector('.nav-tab[data-view="'+name+'"]');
  if(tab) tab.classList.add('active');
  document.querySelectorAll('.mobile-bottom-nav button').forEach(b=>b.classList.remove('active'));
  const mBtn=document.querySelector('.mobile-bottom-nav button[data-view="'+name+'"]');
  if(mBtn) mBtn.classList.add('active');
  // Toggle sidebar content
  const sbDash=document.getElementById('sidebar-dashboard');
  const sbAgent=document.getElementById('sidebar-agent');
  if(sbDash) sbDash.style.display=(name==='dashboard')?'':'none';
  if(sbAgent) sbAgent.style.display=(name==='agent')?'block':'none';
  // Focus chat input when switching to agent
  if(name==='agent'){{
    const ci=document.getElementById('chat-input');
    if(ci) setTimeout(()=>ci.focus(),100);
  }}
}}

// ── Section switching within dashboard view ──
function showSection(id){{
  document.querySelectorAll('#view-dashboard .dashboard-section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('#sidebar-dashboard .nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('#sidebar-dashboard .div-item').forEach(b=>b.classList.remove('active'));
  const target=document.getElementById('s-'+id);
  if(target) target.classList.add('active');
  const nav=document.querySelector('#sidebar-dashboard .nav-item[data-section="'+id+'"]');
  if(nav) nav.classList.add('active');
  document.querySelector('.content').scrollTop=0;
}}

// ── Division switching ──
function showDivision(num){{
  // Ensure we are in dashboard view
  showView('dashboard');
  showSection('race-'+num);
  document.querySelectorAll('#sidebar-dashboard .div-item').forEach(b=>b.classList.remove('active'));
  const di=document.querySelector('#sidebar-dashboard .div-item[data-div="'+num+'"]');
  if(di) di.classList.add('active');
}}

// ── Backtest sub-tabs ──
function showBacktestTab(tab){{
  document.querySelectorAll('.bt-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.bt-panel').forEach(p=>p.classList.remove('active'));
  const tabBtn=document.querySelector('.bt-tab[data-bt="'+tab+'"]');
  const panel=document.getElementById('bt-'+tab);
  if(tabBtn) tabBtn.classList.add('active');
  if(panel) panel.classList.add('active');
}}

// ── Drawer ──
function openDrawer(){{
  document.getElementById('system-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  document.body.style.overflow='hidden';
}}
function closeDrawer(){{
  document.getElementById('system-drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
  document.body.style.overflow='';
}}

// ── Round dropdown ──
function changeRound(sel){{
  window.location.href='/dashboard/'+sel.value;
}}

// ── Backlog helpers ──
function toggleBlDetail(id){{
  const el=document.getElementById(id);
  if(!el)return;
  const isOpen=el.style.display!=='none';
  el.style.display=isOpen?'none':'table-row';
  const prevRow=el.previousElementSibling;
  if(prevRow){{
    const icon=prevRow.querySelector('.bl-expand-icon');
    if(icon)icon.classList.toggle('open',!isOpen);
  }}
}}

// ── Dubbelfiltrering: strategi x speltyp (event delegation) ──
let activeStatsStrat='all', activeStatsGT='all';
let activeBacklogStrat='all', activeBacklogGT='all';

function applyStatsFilter(){{
  document.querySelectorAll('.strat-block').forEach(block=>{{
    const matchS=activeStatsStrat==='all'||block.dataset.strat===activeStatsStrat;
    const matchG=activeStatsGT==='all'||!block.dataset.gt||block.dataset.gt===activeStatsGT;
    block.style.display=(matchS&&matchG)?'':'none';
  }});
  document.querySelectorAll('.stats-table tr[data-gt]').forEach(row=>{{
    const matchG=activeStatsGT==='all'||row.dataset.gt===activeStatsGT;
    row.style.display=matchG?'':'none';
  }});
}}
function applyBacklogFilter(){{
  let rounds=0,full=0,partial=0,cost=0,payout=0;
  document.querySelectorAll('.backlog-table tbody tr').forEach(row=>{{
    if(!row.dataset.strategy)return;
    const matchS=activeBacklogStrat==='all'||row.dataset.strategy===activeBacklogStrat;
    const matchG=activeBacklogGT==='all'||row.dataset.gt===activeBacklogGT;
    const match=matchS&&matchG;
    row.style.display=match?'':'none';
    if(match&&!row.classList.contains('bl-detail-container')){{
      rounds++;
      const cells=row.querySelectorAll('td');
      if(cells[4])cost+=parseFloat(cells[4].textContent.replace(/[^0-9.-]/g,''))||0;
      if(cells[6]&&cells[6].textContent!=='—')payout+=parseFloat(cells[6].textContent.replace(/[^0-9.-]/g,''))||0;
      if(cells[5]){{
        const badge=cells[5].querySelector('.hit-badge,.partial-badge,.miss-badge');
        if(badge){{
          if(badge.classList.contains('hit-badge'))full++;
          else if(badge.classList.contains('partial-badge'))partial++;
        }}
      }}
    }}
  }});
  updateBlSummary(rounds,full,partial,cost,payout);
}}
function applyBacklogRecent(months){{
  const cutoff=new Date();
  cutoff.setMonth(cutoff.getMonth()-months);
  const cutoffStr=cutoff.toISOString().slice(0,10);
  activeBacklogGT='recent_'+months;
  let rounds=0,full=0,partial=0,cost=0,payout=0;
  document.querySelectorAll('.backlog-table tbody tr').forEach(row=>{{
    if(!row.dataset.strategy)return;
    const matchS=activeBacklogStrat==='all'||row.dataset.strategy===activeBacklogStrat;
    const dateCell=row.querySelector('td');
    const dateText=dateCell?dateCell.textContent.trim().slice(0,10):'';
    const matchDate=dateText>=cutoffStr;
    const match=matchS&&matchDate;
    row.style.display=match?'':'none';
    if(match&&!row.classList.contains('bl-detail-container')){{
      rounds++;
      const cells=row.querySelectorAll('td');
      if(cells[4])cost+=parseFloat(cells[4].textContent.replace(/[^0-9.-]/g,''))||0;
      if(cells[6]&&cells[6].textContent!=='—')payout+=parseFloat(cells[6].textContent.replace(/[^0-9.-]/g,''))||0;
      if(cells[5]){{
        const badge=cells[5].querySelector('.hit-badge,.partial-badge,.miss-badge');
        if(badge){{
          if(badge.classList.contains('hit-badge'))full++;
          else if(badge.classList.contains('partial-badge'))partial++;
        }}
      }}
    }}
  }});
  updateBlSummary(rounds,full,partial,cost,payout);
}}
function updateBlSummary(rounds,full,partial,cost,payout){{
  const netto=payout-cost;
  const roi=cost>0?(netto/cost*100):0;
  const winRate=rounds>0?((full+partial)/rounds*100):0;
  const clr=roi>=0?'#22c55e':'#ef4444';
  const fmt=n=>n.toLocaleString('sv-SE',{{maximumFractionDigits:0}});
  const el=id=>document.getElementById(id);
  if(el('bl-rounds'))el('bl-rounds').textContent=rounds;
  if(el('bl-full'))el('bl-full').textContent=full;
  if(el('bl-partial'))el('bl-partial').textContent=partial;
  if(el('bl-winrate'))el('bl-winrate').textContent=winRate.toFixed(0)+'%';
  if(el('bl-cost'))el('bl-cost').textContent=fmt(cost)+' kr';
  if(el('bl-payout'))el('bl-payout').textContent=fmt(payout)+' kr';
  if(el('bl-roi')){{el('bl-roi').textContent=(roi>=0?'+':'')+roi.toFixed(1)+'%';el('bl-roi').style.color=clr;}}
  if(el('bl-netto')){{el('bl-netto').textContent=(netto>=0?'+':'')+fmt(netto)+' kr';el('bl-netto').style.color=clr;}}
}}

// ── Event delegation for data-action buttons ──
document.addEventListener('click',function(ev){{
  const btn=ev.target.closest('[data-action]');
  if(!btn)return;
  const action=btn.dataset.action;

  if(action==='nav-round'){{
    const key=btn.dataset.key;
    if(key)window.location.href='/dashboard/'+key;
    return;
  }}
  if(action==='toggle-variants'){{
    const wrap=btn.closest('.sc-section').querySelector('.sc-variants-wrap');
    if(wrap){{
      const shown=wrap.style.display!=='none';
      wrap.style.display=shown?'none':'block';
      btn.textContent=shown?btn.textContent.replace('Dolj','Visa'):btn.textContent.replace('Visa','Dolj');
    }}
    return;
  }}
  if(action==='toggle-bl-detail'){{
    toggleBlDetail(btn.dataset.detail);
    return;
  }}
  if(action==='filter-stats'){{
    activeStatsStrat=btn.dataset.strat;
    btn.closest('.strat-filter').querySelectorAll('.strat-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyStatsFilter();
    return;
  }}
  if(action==='filter-stats-gt'){{
    activeStatsGT=btn.dataset.gt;
    btn.closest('.strat-filter').querySelectorAll('.gt-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyStatsFilter();
    return;
  }}
  if(action==='filter-backlog'){{
    activeBacklogStrat=btn.dataset.strat;
    btn.closest('.strat-filter').querySelectorAll('.strat-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyBacklogFilter();
    return;
  }}
  if(action==='filter-backlog-gt'){{
    activeBacklogGT=btn.dataset.gt;
    btn.closest('.strat-filter').querySelectorAll('.gt-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyBacklogFilter();
    return;
  }}
  if(action==='filter-backlog-recent'){{
    btn.closest('.strat-filter').querySelectorAll('.gt-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyBacklogRecent(parseInt(btn.dataset.months)||12);
    return;
  }}
}});

// ── Mobile sidebar ──
function toggleMobileSidebar(){{
  const sb=document.getElementById('sidebar');
  const ov=document.getElementById('sidebar-overlay');
  if(sb)sb.classList.toggle('mobile-open');
  if(ov)ov.classList.toggle('open');
}}

// ── Clock ──
(function(){{
  const clockEl=document.getElementById('nav-clock');
  if(!clockEl)return;
  function tick(){{
    const now=new Date();
    clockEl.textContent=now.toLocaleTimeString('sv-SE',{{hour:'2-digit',minute:'2-digit'}});
  }}
  tick();
  setInterval(tick,30000);
}})();

// ── AI Chat (SSE streaming) ──
let chatMsgs=[];
let chatAbort=null;
let activeSessionKey='';

function getCurrentRoundKey(){{
  return window.location.pathname.replace(/^\\/dashboard\\//,'').replace(/^\\/+/,'');
}}

// ── Session storage (localStorage, per device) ──
function allSessionKeys(){{
  const keys=[];
  for(let i=0;i<localStorage.length;i++){{
    const k=localStorage.key(i);
    if(k&&k.startsWith('trav_chat_'))keys.push(k.slice(10));
  }}
  return keys;
}}
function saveSession(key,msgs){{
  if(!key||!msgs.length)return;
  const meta={{
    key:key,
    count:msgs.length,
    preview:(msgs.find(m=>m.role==='user')||{{}}).content||'',
    ts:Date.now()
  }};
  try{{
    localStorage.setItem('trav_chat_'+key,JSON.stringify({{meta:meta,messages:msgs}}));
  }}catch(e){{}}
}}
function loadSessionMsgs(key){{
  try{{
    const raw=localStorage.getItem('trav_chat_'+key);
    if(raw){{const d=JSON.parse(raw);return d.messages||[];}}
  }}catch(e){{}}
  return [];
}}
function deleteSessionLocal(key){{
  try{{localStorage.removeItem('trav_chat_'+key);}}catch(e){{}}
}}
function getSessionMeta(key){{
  try{{
    const raw=localStorage.getItem('trav_chat_'+key);
    if(raw){{const d=JSON.parse(raw);return d.meta||null;}}
  }}catch(e){{}}
  return null;
}}

// ── Session list rendering (event delegation, no inline onclick) ──
function renderSessionList(){{
  const container=document.getElementById('agent-sessions');
  if(!container)return;
  const keys=allSessionKeys().sort((a,b)=>{{
    const ma=getSessionMeta(a),mb=getSessionMeta(b);
    return((mb&&mb.ts)||0)-((ma&&ma.ts)||0);
  }});
  if(!keys.length){{
    container.innerHTML='<div class="session-empty">Inga sparade sessioner</div>';
    return;
  }}
  container.innerHTML=keys.map(k=>{{
    const m=getSessionMeta(k);
    const parts=k.split('/');
    const label=parts[0]+' '+parts[1];
    const preview=m&&m.preview?m.preview.slice(0,50):'';
    const count=m?m.count:0;
    const isActive=k===activeSessionKey;
    return '<button class="session-item'+(isActive?' active':'')+'" data-rk="'+k+'">'
      +'<div class="session-item-top">'
      +'<span class="session-item-label">'+label+'</span>'
      +'<span class="session-item-count">'+Math.floor(count/2)+' frågor</span>'
      +'</div>'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<span class="session-item-preview">'+preview.replace(/</g,'&lt;')+'</span>'
      +'<span class="session-item-del" data-del="'+k+'">&times;</span>'
      +'</div></button>';
  }}).join('');
}}

// Event delegation for session clicks
document.addEventListener('click',function(e){{
  // Delete button
  const delBtn=e.target.closest('.session-item-del');
  if(delBtn){{
    e.stopPropagation();
    const rk=delBtn.getAttribute('data-del');
    if(rk){{
      deleteSessionLocal(rk);
      if(activeSessionKey===rk){{
        activeSessionKey=getCurrentRoundKey();
        chatMsgs=loadSessionMsgs(activeSessionKey);
        renderChat();
        const sug=document.getElementById('agent-suggestions');
        if(sug)sug.style.display=chatMsgs.length?'none':'flex';
      }}
      renderSessionList();
      try{{fetch('/api/chat/session/'+encodeURIComponent(rk),{{method:'DELETE'}});}}catch(ex){{}}
    }}
    return;
  }}
  // Session item click
  const item=e.target.closest('.session-item');
  if(item){{
    const rk=item.getAttribute('data-rk');
    if(rk)switchToSession(rk);
  }}
}});

function switchToSession(rk){{
  if(chatAbort){{chatAbort.abort();chatAbort=null;}}
  activeSessionKey=rk;
  chatMsgs=loadSessionMsgs(rk);
  renderChat();
  const sug=document.getElementById('agent-suggestions');
  if(sug)sug.style.display=chatMsgs.length?'none':'flex';
  renderSessionList();
  showView('agent');
  const ci=document.getElementById('chat-input');
  if(ci)setTimeout(()=>ci.focus(),100);
}}

function newChatSession(){{
  if(chatAbort){{chatAbort.abort();chatAbort=null;}}
  // Save current if it has messages
  if(chatMsgs.length&&activeSessionKey){{
    saveSession(activeSessionKey,chatMsgs);
  }}
  // Start fresh for current round
  activeSessionKey=getCurrentRoundKey()+'_'+Date.now();
  chatMsgs=[];
  renderChat();
  const sug=document.getElementById('agent-suggestions');
  if(sug)sug.style.display='flex';
  renderSessionList();
  const ci=document.getElementById('chat-input');
  if(ci)setTimeout(()=>ci.focus(),100);
}}

function resetChat(){{
  if(chatAbort){{chatAbort.abort();chatAbort=null;}}
  deleteSessionLocal(activeSessionKey);
  activeSessionKey=getCurrentRoundKey();
  chatMsgs=[];
  renderChat();
  const sug=document.getElementById('agent-suggestions');
  if(sug)sug.style.display='flex';
  renderSessionList();
  try{{fetch('/api/chat/session/'+encodeURIComponent(getCurrentRoundKey()),{{method:'DELETE'}});}}catch(e){{}}
}}

function askSuggestion(text){{
  document.getElementById('chat-input').value=text;
  sendChat();
}}

function saveChatLocal(){{
  if(activeSessionKey&&chatMsgs.length){{
    saveSession(activeSessionKey,chatMsgs);
    renderSessionList();
  }}
}}

function loadChatSession(){{
  activeSessionKey=getCurrentRoundKey();
  // Load from localStorage
  chatMsgs=loadSessionMsgs(activeSessionKey);
  if(chatMsgs.length){{
    renderChat();
    const sug=document.getElementById('agent-suggestions');
    if(sug)sug.style.display='none';
    renderSessionList();
    return;
  }}
  // Fallback: try server
  fetch('/api/chat/session/'+encodeURIComponent(activeSessionKey))
    .then(r=>r.json())
    .then(data=>{{
      if(data.messages&&data.messages.length>0){{
        chatMsgs=data.messages;
        renderChat();
        const sug=document.getElementById('agent-suggestions');
        if(sug)sug.style.display='none';
        saveChatLocal();
      }}
    }}).catch(()=>{{}});
  renderSessionList();
}}

document.addEventListener('DOMContentLoaded',()=>{{
  const ci=document.getElementById('chat-input');
  if(ci)ci.addEventListener('keydown',e=>{{
    if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();sendChat();}}
  }});
  const sbAgent=document.getElementById('sidebar-agent');
  if(sbAgent) sbAgent.style.display='none';
  loadChatSession();
}});
async function sendChat(){{
  const input=document.getElementById('chat-input');
  const msg=input.value.trim();
  if(!msg)return;
  input.value='';
  const sug=document.getElementById('agent-suggestions');
  if(sug) sug.style.display='none';
  const now=new Date();
  const ts=now.toLocaleTimeString('sv-SE',{{hour:'2-digit',minute:'2-digit'}});
  chatMsgs.push({{role:'user',content:msg,ts:ts}});
  renderChat();
  const btn=document.getElementById('chat-send');
  btn.disabled=true;
  const chatContainer=document.getElementById('chat-messages');

  // Typing indicator
  const typingDiv=document.createElement('div');
  typingDiv.className='chat-msg ai';
  typingDiv.id='chat-typing';
  typingDiv.innerHTML='<div class="typing-indicator"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>';
  chatContainer.appendChild(typingDiv);
  chatContainer.scrollTop=chatContainer.scrollHeight;

  // Collect ranking data for context
  let rankingCtx='';
  const rankEl=document.getElementById('ranking-data');
  if(rankEl)rankingCtx=rankEl.getAttribute('data-ranking')||'';

  try{{
    const rk=getCurrentRoundKey();
    const resp=await fetch('/api/chat',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{messages:chatMsgs.filter(m=>m.role),round_key:rk,ranking:rankingCtx}})
    }});

    // Remove typing indicator
    const ti=document.getElementById('chat-typing');
    if(ti)ti.remove();

    const contentType=resp.headers.get('content-type')||'';

    if(contentType.includes('text/event-stream')){{
      // SSE streaming response
      const aiDiv=document.createElement('div');
      aiDiv.className='chat-msg ai streaming';
      aiDiv.id='chat-stream';
      chatContainer.appendChild(aiDiv);
      let fullText='';

      const reader=resp.body.getReader();
      const decoder=new TextDecoder();
      let buffer='';

      while(true){{
        const {{done,value}}=await reader.read();
        if(done)break;
        buffer+=decoder.decode(value,{{stream:true}});
        const lines=buffer.split('\\n');
        buffer=lines.pop()||'';
        for(const line of lines){{
          if(line.startsWith('data: ')){{
            const payload=line.slice(6).trim();
            if(payload==='[DONE]')continue;
            try{{
              const obj=JSON.parse(payload);
              if(obj.delta){{
                fullText+=obj.delta;
                aiDiv.innerHTML=formatChatMsg(fullText);
                chatContainer.scrollTop=chatContainer.scrollHeight;
              }}
              if(obj.error){{
                fullText+='\\nFel: '+obj.error;
                aiDiv.innerHTML=formatChatMsg(fullText);
              }}
            }}catch(pe){{}}
          }}
        }}
      }}
      aiDiv.classList.remove('streaming');
      const rts=new Date().toLocaleTimeString('sv-SE',{{hour:'2-digit',minute:'2-digit'}});
      aiDiv.innerHTML=formatChatMsg(fullText)+'<span class="chat-ts">'+rts+'</span>';
      chatMsgs.push({{role:'assistant',content:fullText,ts:rts}});
    }}else{{
      // Fallback: JSON response
      const data=await resp.json();
      const rts=new Date().toLocaleTimeString('sv-SE',{{hour:'2-digit',minute:'2-digit'}});
      if(data.error){{
        chatMsgs.push({{role:'assistant',content:'Fel: '+data.error,ts:rts}});
      }}else{{
        chatMsgs.push({{role:'assistant',content:data.response,ts:rts}});
      }}
      renderChat();
    }}
  }}catch(e){{
    const ti2=document.getElementById('chat-typing');
    if(ti2)ti2.remove();
    const rts=new Date().toLocaleTimeString('sv-SE',{{hour:'2-digit',minute:'2-digit'}});
    chatMsgs.push({{role:'assistant',content:'Kunde inte nå AI-tjänsten: '+e.message,ts:rts}});
    renderChat();
  }}
  btn.disabled=false;
  chatContainer.scrollTop=chatContainer.scrollHeight;
  saveChatLocal();
}}
function renderChat(){{
  const c=document.getElementById('chat-messages');
  c.innerHTML=chatMsgs.map(m=>{{
    const cls=m.role==='user'?'user':'ai';
    const tsHtml=m.ts?'<span class="chat-ts">'+m.ts+'</span>':'';
    return '<div class="chat-msg '+cls+'">'+formatChatMsg(m.content)+tsHtml+'</div>';
  }}).join('');
  c.scrollTop=c.scrollHeight;
}}
function formatChatMsg(text){{
  let s=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Code blocks
  s=s.replace(/```([\\s\\S]*?)```/g,function(m,code){{return '<pre>'+code.trim()+'</pre>';}});
  // Inline code
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  // Markdown tables
  s=s.replace(/(^\\|.+\\|\\n?)+/gm,function(block){{
    const rows=block.trim().split('\\n').filter(r=>r.trim());
    if(rows.length<2)return block;
    let html='<table class="chat-table"><thead><tr>';
    const hCells=rows[0].split('|').filter(c=>c.trim());
    hCells.forEach(c=>{{html+='<th>'+c.trim()+'</th>';}});
    html+='</tr></thead><tbody>';
    const startRow=rows[1]&&/^[\\s|:-]+$/.test(rows[1])?2:1;
    for(let i=startRow;i<rows.length;i++){{
      const cells=rows[i].split('|').filter(c=>c.trim());
      html+='<tr>';
      cells.forEach(c=>{{
        let cv=c.trim();
        const tierMatch=cv.match(/^([A-D])$/);
        if(tierMatch){{cv='<span class="rank-pill rank-'+tierMatch[1].toLowerCase()+'">'+cv+'</span>';}}
        html+='<td>'+cv+'</td>';
      }});
      html+='</tr>';
    }}
    html+='</tbody></table>';
    return html;
  }});
  // Headers
  s=s.replace(/^#### (.+)$/gm,'<h5 style="margin:.6em 0 .2em;font-size:.82rem;font-weight:700;color:#1e293b">$1</h5>');
  s=s.replace(/^### (.+)$/gm,'<h4 style="margin:.8em 0 .3em;font-size:.9rem;font-weight:700;color:#1e293b">$1</h4>');
  s=s.replace(/^## (.+)$/gm,'<h3 style="margin:1em 0 .4em;font-size:1rem;font-weight:700;color:#1e293b">$1</h3>');
  // Horizontal rule
  s=s.replace(/^---+$/gm,'<hr style="border:none;border-top:1px solid #e5e7eb;margin:.8em 0">');
  // Bold
  s=s.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>');
  // Italic
  s=s.replace(/\\*(.+?)\\*/g,'<em>$1</em>');
  // Unordered lists
  s=s.replace(/^[\\-\\*] (.+)$/gm,'<li>$1</li>');
  s=s.replace(/(<li>.*<\\/li>)/gs,function(m){{return '<ul style="margin:.3em 0;padding-left:1.2em">'+m+'</ul>';}});
  // Ordered lists
  s=s.replace(/^\\d+\\. (.+)$/gm,'<li>$1</li>');
  // Line breaks
  s=s.replace(/\\n/g,'<br>');
  return s;
}}

// ── Toggle expandable detail rows ──
document.querySelectorAll('.horse-row.clickable').forEach(row=>{{
  row.addEventListener('click',()=>{{
    const horseNum=row.getAttribute('data-horse');
    const card=row.closest('.race-card');
    const detailRow=card.querySelector('.detail-row[data-horse="'+horseNum+'"]');
    if(detailRow){{
      detailRow.classList.toggle('hidden');
      row.classList.toggle('expanded');
    }}
  }});
}});

// ── Auto-refresh for live rounds ──
(function(){{
  const isLive = {'true' if not game_round.is_finished else 'false'};
  if(!isLive) return;
  const roundKey = '{game_round.game_type}/{game_round.round_date}';
  const navRight = document.querySelector('.nav-right');
  if(!navRight) return;
  const timer = document.createElement('span');
  timer.style.cssText = 'font-family:"JetBrains Mono",monospace;font-size:0.75rem;color:#94a3b8;margin-right:8px';
  navRight.insertBefore(timer, navRight.firstChild);
  const refreshBtn = document.createElement('button');
  refreshBtn.textContent = '↻ Uppdatera';
  refreshBtn.style.cssText = 'padding:5px 12px;border-radius:8px;border:1px solid #e5e7eb;background:#fff;color:#1e293b;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit';
  navRight.appendChild(refreshBtn);
  refreshBtn.addEventListener('click',()=>{{
    refreshBtn.textContent = '⏳ Uppdaterar...';
    fetch('/refresh/'+roundKey).then(()=>location.reload());
  }});
  let countdown = 600;
  setInterval(()=>{{
    countdown--;
    const m = Math.floor(countdown/60);
    const s = countdown%60;
    timer.textContent = m+':'+String(s).padStart(2,'0');
    if(countdown<=0){{
      timer.textContent = '⏳';
      fetch('/refresh/'+roundKey).then(()=>location.reload());
    }}
  }},1000);
}})();

// ── Tips Upload (image → AI parse → add source) ──
let tipsImageB64=null;
let tipsParsedData=null;
let tipsMediaType='image/png';

function openTipsUpload(){{
  tipsImageB64=null;tipsParsedData=null;
  document.getElementById('tips-step-upload').style.display='';
  document.getElementById('tips-step-parsing').style.display='none';
  document.getElementById('tips-step-preview').style.display='none';
  document.getElementById('tips-step-done').style.display='none';
  document.getElementById('tips-confirm-btn').disabled=true;
  document.getElementById('tips-confirm-btn').style.display='';
  document.getElementById('tips-modal-footer').style.display='flex';
  document.getElementById('tips-source-name').value='';
  document.getElementById('tips-file-input').value='';
  document.getElementById('tips-modal').classList.add('open');
}}
function closeTipsUpload(){{
  document.getElementById('tips-modal').classList.remove('open');
}}

function handleTipsFile(e){{
  const file=e.target.files[0];
  if(!file)return;
  tipsMediaType=file.type||'image/png';
  const reader=new FileReader();
  reader.onload=function(ev){{
    const dataUrl=ev.target.result;
    tipsImageB64=dataUrl.split(',')[1];
    // Show parsing step
    document.getElementById('tips-step-upload').style.display='none';
    document.getElementById('tips-step-parsing').style.display='';
    document.getElementById('tips-preview-image').src=dataUrl;
    // Start parsing
    parseTipsImage(dataUrl);
  }};
  reader.readAsDataURL(file);
}}

// Drag & drop
document.addEventListener('DOMContentLoaded',()=>{{
  const dz=document.getElementById('tips-drop-zone');
  if(!dz)return;
  dz.addEventListener('dragover',e=>{{e.preventDefault();dz.classList.add('dragover');}});
  dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
  dz.addEventListener('drop',e=>{{
    e.preventDefault();dz.classList.remove('dragover');
    const file=e.dataTransfer.files[0];
    if(file&&file.type.startsWith('image/')){{
      const input=document.getElementById('tips-file-input');
      const dt=new DataTransfer();dt.items.add(file);input.files=dt.files;
      handleTipsFile({{target:input}});
    }}
  }});
}});

async function parseTipsImage(dataUrl){{
  const pathParts=window.location.pathname.replace(/^\\/dashboard\\//,'').split('/');
  const gameType=pathParts[0]||'V85';
  const sourceName=document.getElementById('tips-source-name').value.trim()||'';
  try{{
    const resp=await fetch('/api/tips/parse-image',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        image:tipsImageB64,
        game_type:gameType,
        source_name:sourceName,
        media_type:tipsMediaType
      }})
    }});
    const data=await resp.json();
    if(data.error){{
      alert('Parsningsfel: '+data.error);
      document.getElementById('tips-step-parsing').style.display='none';
      document.getElementById('tips-step-upload').style.display='';
      return;
    }}
    tipsParsedData=data.parsed;
    // Show preview
    document.getElementById('tips-step-parsing').style.display='none';
    document.getElementById('tips-step-preview').style.display='';
    document.getElementById('tips-preview-image2').src=dataUrl;
    // Set source name
    const sn=data.source_name||tipsParsedData.source_name||'custom_tipster';
    if(!document.getElementById('tips-source-name').value){{
      document.getElementById('tips-source-name').value=sn;
    }}
    // Render preview table
    renderTipsPreview(tipsParsedData);
    document.getElementById('tips-confirm-btn').disabled=false;
  }}catch(e){{
    alert('Nätverksfel: '+e.message);
    document.getElementById('tips-step-parsing').style.display='none';
    document.getElementById('tips-step-upload').style.display='';
  }}
}}

function renderTipsPreview(data){{
  const container=document.getElementById('tips-preview-data');
  let html='<h4>Rankings</h4>';
  if(data.rankings){{
    html+='<table class="tips-preview-table"><thead><tr><th>Lopp</th><th>A</th><th>B</th><th>C</th><th>D</th></tr></thead><tbody>';
    for(const[race,tiers] of Object.entries(data.rankings)){{
      html+='<tr><td><strong>'+race+'</strong></td>';
      html+='<td>'+(tiers.A||'—')+'</td>';
      html+='<td>'+(tiers.B||'—')+'</td>';
      /* BC tier removed */
      html+='<td>'+(tiers.C||'—')+'</td>';
      html+='<td>'+(tiers.D||'—')+'</td></tr>';
    }}
    html+='</tbody></table>';
  }}
  if(data.hot_info){{
    html+='<div class="tips-hot-info"><strong>Het info:</strong><br>';
    for(const[race,info] of Object.entries(data.hot_info)){{
      html+=race+': '+info+'<br>';
    }}
    html+='</div>';
  }}
  container.innerHTML=html;
}}

async function confirmTipsSource(){{
  if(!tipsParsedData)return;
  const pathParts=window.location.pathname.replace(/^\\/dashboard\\//,'').split('/');
  const gameType=pathParts[0]||'V85';
  const roundDate=pathParts[1]||'';
  let sourceName=document.getElementById('tips-source-name').value.trim();
  if(!sourceName)sourceName=tipsParsedData.source_name||'custom_tipster';
  // Normalize to snake_case key
  const sourceKey=sourceName.toLowerCase().replace(/[^a-z0-9åäö]+/g,'_').replace(/^_|_$/g,'');

  const btn=document.getElementById('tips-confirm-btn');
  btn.disabled=true;btn.textContent='Sparar...';

  try{{
    const resp=await fetch('/api/tips/add-source',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        game_type:gameType,
        round_date:roundDate,
        source_name:sourceKey,
        source_data:tipsParsedData
      }})
    }});
    const data=await resp.json();
    if(data.error){{
      alert('Fel: '+data.error);
      btn.disabled=false;btn.textContent='Lägg till';
      return;
    }}
    // Success
    document.getElementById('tips-step-preview').style.display='none';
    document.getElementById('tips-step-done').style.display='';
    document.getElementById('tips-modal-footer').style.display='none';
    // Reload after short delay
    setTimeout(()=>location.reload(),1500);
  }}catch(e){{
    alert('Nätverksfel: '+e.message);
    btn.disabled=false;btn.textContent='Lägg till';
  }}
}}

function removeCustomSource(sourceKey){{
  if(!confirm('Ta bort källan "'+sourceKey.replace(/_/g,' ')+'" från konsensus?'))return;
  const pathParts=window.location.pathname.replace(/^\\/dashboard\\//,'').split('/');
  const gameType=pathParts[0]||'V85';
  const roundDate=pathParts[1]||'';
  fetch('/api/tips/source/'+gameType+'/'+roundDate+'/'+encodeURIComponent(sourceKey),{{method:'DELETE'}})
    .then(r=>r.json())
    .then(()=>location.reload())
    .catch(e=>alert('Fel: '+e.message));
}}

// ── Bet History ──
let _betData=null;
let _betPeriod='month';

function setBetPeriod(p){{
  _betPeriod=p;
  document.querySelectorAll('.bet-period-btn').forEach(b=>b.classList.remove('active'));
  const btn=document.querySelector('.bet-period-btn[onclick*="'+p+'"]');
  if(btn)btn.classList.add('active');
  if(_betData)renderBetHistory(_betData);
}}

async function fetchBetHistory(){{
  const el=document.getElementById('bet-history-content');
  if(!el)return;
  try{{
    const resp=await fetch('/api/bets?days=180');
    if(!resp.ok){{
      el.innerHTML='<div class="bet-loading">Historik inte tillgänglig</div>';
      return;
    }}
    _betData=await resp.json();
    renderBetHistory(_betData);
  }}catch(e){{
    el.innerHTML='<div class="bet-loading">Kunde inte ladda historik</div>';
  }}
}}

function renderBetHistory(data){{
  const el=document.getElementById('bet-history-content');
  if(!el||!data)return;
  const sum=data.summary||{{}};
  const periods=data.by_period||{{}};
  const periodData=periods[_betPeriod]||{{}};
  const keys=Object.keys(periodData);

  if(sum.total_bets===0){{
    el.innerHTML='<div class="bet-loading">Inga resultat sparade ännu. Resultat sparas automatiskt när omgångar analyseras.</div>';
    return;
  }}

  // Summary cards
  const pnlColor=sum.total_pnl>=0?'#15803d':'#dc2626';
  let html='<div class="bet-cum-summary">';
  html+='<div class="bet-cum-item"><span class="bet-cum-label">Spel</span><span class="bet-cum-val">'+sum.finished_count+'</span></div>';
  html+='<div class="bet-cum-item"><span class="bet-cum-label">Vinster</span><span class="bet-cum-val">'+sum.wins+' ('+sum.win_rate.toFixed(0)+'%)</span></div>';
  html+='<div class="bet-cum-item"><span class="bet-cum-label">ROI</span><span class="bet-cum-val" style="color:'+pnlColor+'">'+(sum.roi_pct>=0?'+':'')+sum.roi_pct.toFixed(0)+'%</span></div>';
  html+='<div class="bet-cum-item"><span class="bet-cum-label">Resultat</span><span class="bet-cum-val" style="color:'+pnlColor+'">'+(sum.total_pnl>=0?'+':'')+Math.round(sum.total_pnl).toLocaleString('sv-SE')+'kr</span></div>';
  html+='</div>';

  // Period rows
  if(keys.length===0){{
    html+='<div class="bet-loading">Ingen data för vald period</div>';
  }}else{{
    html+='<div class="bet-history-grid">';
    // Find max absolute PNL for scaling bars
    let maxAbs=1;
    keys.forEach(k=>{{const a=Math.abs(periodData[k].pnl||0);if(a>maxAbs)maxAbs=a;}});

    keys.forEach(k=>{{
      const d=periodData[k];
      const pnl=d.pnl||0;
      const roi=d.roi_pct||0;
      const winRate=d.bets>0?(d.wins/d.bets*100):0;
      const barW=Math.round(Math.abs(pnl)/maxAbs*100);
      const barClass=pnl>=0?'bet-hist-bar-pos':'bet-hist-bar-neg';
      const pnlClass=pnl>=0?'pos':'neg';
      const sign=pnl>=0?'+':'';

      html+='<div class="bet-hist-row">';
      html+='<div class="bet-hist-period">'+k+'</div>';
      html+='<div class="bet-hist-bar"><div class="bet-hist-bar-fill '+barClass+'" style="width:'+barW+'%"></div></div>';
      html+='<div class="bet-hist-bets">'+d.bets+' spel</div>';
      html+='<div class="bet-hist-winrate">'+winRate.toFixed(0)+'% vinst</div>';
      html+='<div class="bet-hist-pnl '+pnlClass+'">'+sign+Math.round(pnl).toLocaleString('sv-SE')+'kr</div>';
      html+='</div>';
    }});
    html+='</div>';
  }}

  el.innerHTML=html;
}}

// Load bet history when switching to bet view
const _origShowView=showView;
showView=function(name){{
  _origShowView(name);
  if(name==='bet'&&!_betData)fetchBetHistory();
}};
</script>

<!-- ═══ Tips Upload Modal ═══ -->
<div class="tips-modal-overlay" id="tips-modal">
  <div class="tips-modal">
    <div class="tips-modal-header">
      <h3>Lägg till tipskälla</h3>
      <button class="tips-modal-close" onclick="closeTipsUpload()">&times;</button>
    </div>
    <div class="tips-modal-body">
      <div id="tips-step-upload">
        <div class="tips-drop-zone" id="tips-drop-zone">
          <input type="file" accept="image/*" id="tips-file-input" onchange="handleTipsFile(event)">
          <div class="tips-drop-icon">📸</div>
          <div class="tips-drop-text"><strong>Klicka eller dra en bild hit</strong><br>Screenshot från tipstjänst</div>
        </div>
        <input class="tips-source-input" id="tips-source-name" placeholder="Källans namn (t.ex. SpV Gävle)" />
      </div>
      <div id="tips-step-parsing" style="display:none">
        <img id="tips-preview-image" class="tips-preview-img" />
        <div class="tips-parsing">
          <div class="spinner"></div>
          <div style="margin-top:8px">Analyserar bild med AI...</div>
        </div>
      </div>
      <div id="tips-step-preview" style="display:none">
        <img id="tips-preview-image2" class="tips-preview-img" />
        <div class="tips-preview-data" id="tips-preview-data"></div>
      </div>
      <div id="tips-step-done" style="display:none">
        <div style="text-align:center;padding:24px">
          <div style="font-size:2rem;margin-bottom:8px">&#10003;</div>
          <div style="font-weight:700;color:#16a34a">Källa tillagd!</div>
          <div style="font-size:.82rem;color:#64748b;margin-top:4px">Konsensus-rankingen uppdateras...</div>
        </div>
      </div>
    </div>
    <div class="tips-modal-footer" id="tips-modal-footer">
      <button class="tips-btn tips-btn-secondary" onclick="closeTipsUpload()">Avbryt</button>
      <button class="tips-btn tips-btn-primary" id="tips-confirm-btn" disabled onclick="confirmTipsSource()">Lägg till</button>
    </div>
  </div>
</div>

<!-- Mobile bottom nav -->
<nav class="mobile-bottom-nav" id="mobile-bottom-nav">
  <button class="active" data-view="dashboard" onclick="showView('dashboard');updateMobileNav(this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
    Dashboard
  </button>
  <button data-view="bet" onclick="showView('bet');updateMobileNav(this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v12m-4-8h8"/></svg>
    Bet
  </button>
  <button data-view="agent" onclick="showView('agent');updateMobileNav(this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2v10z"/></svg>
    Agent
  </button>
  <button data-view="backtest" onclick="showView('backtest');updateMobileNav(this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 4-6"/></svg>
    Backtest
  </button>
</nav>
<script>
function updateMobileNav(btn){{
  document.querySelectorAll('.mobile-bottom-nav button').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
}}
</script>

</body>
</html>"""


def generate_landing_html() -> str:
    """Generera en publik landningssida för Kungens Trav."""
    return """<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kungens Trav — AI-driven travanalys</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600;9..40,700&family=DM+Mono:wght@400;500&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
::selection{background:rgba(59,130,246,0.3)}
body{font-family:'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
background:#f5f6f8;color:#1e293b;line-height:1.6;-webkit-font-smoothing:antialiased;
overflow-x:hidden}

/* Nav */
.landing-nav{position:fixed;top:0;left:0;right:0;padding:1rem 2rem;display:flex;
justify-content:space-between;align-items:center;z-index:50;
background:rgba(255,255,255,0.9);backdrop-filter:blur(12px);border-bottom:1px solid #e5e7eb}
.landing-nav .logo{font-size:1.2rem;font-weight:800;letter-spacing:-0.02em}
.landing-nav .logo span{color:#f59e0b}
.nav-cta{background:#f59e0b;color:#1e293b;border:none;padding:.5rem 1.2rem;border-radius:10px;
font-weight:700;font-size:.85rem;cursor:pointer;transition:all .25s;
box-shadow:0 2px 12px rgba(245,166,35,0.3)}
.nav-cta:hover{background:#d4911e;transform:translateY(-1px)}

/* Hero */
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;text-align:center;padding:6rem 2rem 4rem;position:relative}
.hero::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);
width:600px;height:600px;background:radial-gradient(circle,rgba(245,166,35,0.08) 0%,transparent 70%);
pointer-events:none}
.hero-badge{display:inline-block;background:rgba(245,166,35,0.1);color:#f59e0b;
padding:.35rem 1rem;border-radius:20px;font-size:.78rem;font-weight:600;margin-bottom:1.5rem;
border:1px solid rgba(245,166,35,0.2)}
.hero h1{font-size:3.5rem;font-weight:900;line-height:1.1;letter-spacing:-0.03em;
max-width:700px;margin-bottom:1.5rem}
.hero h1 em{font-style:normal;color:#f59e0b}
.hero p{font-size:1.15rem;color:#6b7280;max-width:500px;margin-bottom:2.5rem}
.hero-cta{display:inline-flex;align-items:center;gap:.5rem;background:#f59e0b;color:#1e293b;
border:none;padding:.8rem 2rem;border-radius:12px;font-weight:700;font-size:1rem;
cursor:pointer;transition:all .3s;box-shadow:0 4px 20px rgba(245,166,35,0.35)}
.hero-cta:hover{background:#d4911e;transform:translateY(-2px);box-shadow:0 8px 30px rgba(245,166,35,0.4)}

/* Stats bar */
.stats-bar{display:flex;gap:3rem;justify-content:center;margin-top:3rem;flex-wrap:wrap}
.stat-item{text-align:center}
.stat-val{font-size:2rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}
.stat-lbl{font-size:.78rem;color:#4b5563;text-transform:uppercase;letter-spacing:.05em;margin-top:.2rem}

/* Features */
.features{padding:5rem 2rem;max-width:1100px;margin:0 auto}
.features h2{text-align:center;font-size:2rem;font-weight:800;margin-bottom:.8rem;letter-spacing:-0.02em}
.features .subtitle{text-align:center;color:#6b7280;margin-bottom:3rem;font-size:1rem}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.5rem}
.feature-card{background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;padding:2rem;
transition:all .3s}
.feature-card:hover{border-color:#cbd5e1;transform:translateY(-2px);
box-shadow:0 8px 24px rgba(0,0,0,0.08)}
.feature-icon{font-size:1.8rem;margin-bottom:1rem}
.feature-card h3{font-size:1.05rem;font-weight:700;margin-bottom:.5rem;color:#1e293b}
.feature-card p{color:#6b7280;font-size:.88rem;line-height:1.6}

/* Social proof */
.proof{padding:4rem 2rem;text-align:center;background:#f1f5f9;border-top:1px solid #e5e7eb;
border-bottom:1px solid #e5e7eb}
.proof h2{font-size:1.8rem;font-weight:800;margin-bottom:1rem}
.proof-stat{font-size:3rem;font-weight:900;color:#22c55e;margin-bottom:.5rem;
font-variant-numeric:tabular-nums}
.proof-label{color:#6b7280;font-size:1rem}

/* Pricing */
.pricing{padding:5rem 2rem;max-width:900px;margin:0 auto}
.pricing h2{text-align:center;font-size:2rem;font-weight:800;margin-bottom:3rem}
.price-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.5rem}
.price-card{background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;padding:2.5rem;
text-align:center;position:relative}
.price-card.featured{border-color:#f59e0b;box-shadow:0 0 30px rgba(245,166,35,0.15)}
.price-card.featured::before{content:'Populärast';position:absolute;top:-12px;left:50%;
transform:translateX(-50%);background:#f59e0b;color:#1e293b;padding:.2rem 1rem;
border-radius:20px;font-size:.72rem;font-weight:700}
.price-name{font-size:1.1rem;font-weight:700;margin-bottom:.5rem}
.price-amount{font-size:2.5rem;font-weight:900;color:#f59e0b;margin-bottom:.3rem}
.price-amount span{font-size:.9rem;color:#6b7280;font-weight:500}
.price-desc{color:#6b7280;font-size:.85rem;margin-bottom:1.5rem}
.price-features{list-style:none;text-align:left;margin-bottom:2rem}
.price-features li{padding:.4rem 0;font-size:.88rem;color:#1e293b}
.price-features li::before{content:'\\2713';color:#22c55e;font-weight:700;margin-right:.5rem}
.price-btn{width:100%;padding:.7rem;border-radius:10px;border:none;font-weight:700;
font-size:.9rem;cursor:pointer;transition:all .25s}
.price-btn.primary{background:#f59e0b;color:#1e293b}
.price-btn.primary:hover{background:#d4911e}
.price-btn.secondary{background:#f9fafb;color:#1e293b;border:1px solid #e5e7eb}
.price-btn.secondary:hover{background:#f1f5f9}

/* Footer */
.landing-footer{padding:2rem;text-align:center;color:#4b5563;font-size:.8rem;
border-top:1px solid #e5e7eb}

@media(max-width:768px){
  .hero h1{font-size:2.2rem}
  .hero p{font-size:1rem}
  .stats-bar{gap:1.5rem}
  .stat-val{font-size:1.5rem}
  .feature-grid{grid-template-columns:1fr}
  .price-grid{grid-template-columns:1fr}
  .landing-nav{padding:.8rem 1rem}
}
</style>
</head>
<body>

<nav class="landing-nav">
  <div class="logo">Kungens <span>Trav</span></div>
  <button class="nav-cta" onclick="window.location='/'">Testa gratis</button>
</nav>

<section class="hero">
  <div class="hero-badge">AI-driven travanalys</div>
  <h1>Smartare spel.<br><em>B&auml;ttre odds.</em></h1>
  <p>Kungens Trav anv&auml;nder AI och avancerad dataanalys f&ouml;r att hitta v&auml;rde i V75, V86 och V85.</p>
  <button class="hero-cta" onclick="window.location='/'">Kom ig&aring;ng &rarr;</button>
  <div class="stats-bar">
    <div class="stat-item"><div class="stat-val">8</div><div class="stat-lbl">Strategier</div></div>
    <div class="stat-item"><div class="stat-val">13K+</div><div class="stat-lbl">Historiska rader</div></div>
    <div class="stat-item"><div class="stat-val">24/7</div><div class="stat-lbl">Realtidsanalys</div></div>
  </div>
</section>

<section class="features">
  <h2>Allt du beh&ouml;ver</h2>
  <p class="subtitle">Fr&aring;n analys till f&auml;rdigt system &mdash; p&aring; sekunder</p>
  <div class="feature-grid">
    <div class="feature-card">
      <div class="feature-icon">&#x1F3AF;</div>
      <h3>AI-rekommendationer</h3>
      <p>Varje h&auml;st betygs&auml;tts p&aring; 8 faktorer. Spik, 2-val, 3-val eller gardering &mdash; automatiskt.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#x26A0;&#xFE0F;</div>
      <h3>Sk&auml;llanalys</h3>
      <p>Se vilka lopp som har h&ouml;gst sk&auml;llrisk med v&aring;r heatmap. Garderar r&auml;tt avdelningar.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#x1F4CA;</div>
      <h3>Systemgenerator</h3>
      <p>Generera kompletta V75/V86-system med &ouml;nskad budget och riskniv&aring;. Klicka och spela.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#x1F4C8;</div>
      <h3>Backlog &amp; Statistik</h3>
      <p>F&ouml;lj alla resultat &ouml;ver tid. Equity curves, ROI per strategi och streak-analys.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#x1F4AC;</div>
      <h3>AI-chatt</h3>
      <p>Fr&aring;ga direkt om omg&aring;ngen. &quot;Vilka h&auml;star har v&auml;rde?&quot; &mdash; f&aring; svar p&aring; sekunder.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">&#x1F4F1;</div>
      <h3>Mobilanpassad</h3>
      <p>Kolla analysen var du &auml;n &auml;r. Optimerad f&ouml;r alla sk&auml;rmstorlekar.</p>
    </div>
  </div>
</section>

<section class="proof">
  <h2>Resultat som talar</h2>
  <div class="proof-stat">+52,9 MSEK</div>
  <div class="proof-label">Kumulativ teoretisk avkastning sedan 2021</div>
</section>

<section class="pricing">
  <h2>V&auml;lj din plan</h2>
  <div class="price-grid">
    <div class="price-card">
      <div class="price-name">Gratis</div>
      <div class="price-amount">0 kr <span>/m&aring;n</span></div>
      <div class="price-desc">Testa grundl&auml;ggande analys</div>
      <ul class="price-features">
        <li>Sammanfattning &amp; spelv&auml;rde</li>
        <li>Avdelning 1&ndash;2 fullst&auml;ndig analys</li>
        <li>Sk&auml;llkarta</li>
      </ul>
      <button class="price-btn secondary" onclick="window.location='/'">B&ouml;rja gratis</button>
    </div>
    <div class="price-card featured">
      <div class="price-name">Premium</div>
      <div class="price-amount">299 kr <span>/m&aring;n</span></div>
      <div class="price-desc">Fullst&auml;ndig tillg&aring;ng</div>
      <ul class="price-features">
        <li>Alla avdelningar</li>
        <li>Systemgenerator</li>
        <li>AI-chatt</li>
        <li>Backlog &amp; equity curves</li>
        <li>Realtids-uppdateringar</li>
      </ul>
      <button class="price-btn primary">Uppgradera</button>
    </div>
  </div>
</section>

<style>
.upcoming{padding:4rem 2rem;max-width:900px;margin:0 auto}
.upcoming h2{text-align:center;font-size:1.8rem;font-weight:800;margin-bottom:.5rem}
.upcoming .sub{text-align:center;color:#6b7280;margin-bottom:2rem}
#upcoming-list{display:flex;flex-direction:column;gap:.75rem;align-items:center}
.game-link{display:flex;align-items:center;gap:1rem;width:100%;max-width:500px;padding:1rem 1.5rem;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;text-decoration:none;color:#1e293b;transition:all .2s;cursor:pointer}
.game-link:hover{border-color:#f59e0b;transform:translateY(-1px)}
.game-link .badge{background:rgba(245,166,35,0.15);color:#f59e0b;padding:.3rem .8rem;border-radius:8px;font-weight:700;font-size:.9rem;min-width:50px;text-align:center}
.game-link .date{flex:1}
.game-link .arrow{color:#4b5563}
</style>

<section class="upcoming">
  <h2>Kommande omg&aring;ngar</h2>
  <p class="sub">Klicka f&ouml;r att se fullst&auml;ndig AI-analys</p>
  <div id="upcoming-list">
    <p style="color:#4b5563">Laddar...</p>
  </div>
</section>

<footer class="landing-footer">
  &copy; 2026 Kungens Trav. AI-driven travanalys.
</footer>

<script>
(async()=>{
  const el=document.getElementById('upcoming-list');
  const keep=new Set(['V75','V85','V86','V64','GS75']);
  try{
    const r=await fetch('/api/upcoming');
    const d=await r.json();
    if(!d.upcoming||d.upcoming.length===0){el.innerHTML='<p style="color:#6b7280">Inga kommande omg\\u00e5ngar hittade.</p>';return;}
    const days=['S\\u00f6n','M\\u00e5n','Tis','Ons','Tor','Fre','L\\u00f6r'];
    let html='';
    for(const u of d.upcoming){
      const dt=new Date(u.date+'T12:00:00');
      const day=days[dt.getDay()];
      const dateStr=dt.toLocaleDateString('sv-SE',{day:'numeric',month:'short'});
      for(const g of u.games){
        if(!keep.has(g.toUpperCase()))continue;
        html+=`<a class="game-link" href="/dashboard/${g}/${u.date}"><span class="badge">${g}</span><span class="date">${day} ${dateStr}</span><span class="arrow">\\u2192</span></a>`;
      }
    }
    el.innerHTML=html||'<p style="color:#6b7280">Inga omg\\u00e5ngar hittade.</p>';
  }catch(e){el.innerHTML='<p style="color:#ef4444">Kunde inte ladda.</p>';}
})();
</script>

</body>
</html>"""
