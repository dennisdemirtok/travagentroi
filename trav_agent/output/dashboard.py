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


def _rec_color(rec: str) -> str:
    return {
        "spik": "#1a1600",
        "2-val": "#0c1a30",
        "3-val": "#1e0c30",
        "gardering": "#1e2028",
        "strykning": "#2a0a0a",
    }.get(rec, "#e2e8f0")


def _rec_bg(rec: str) -> str:
    return {
        "spik": "#f59e0b",
        "2-val": "#3b82f6",
        "3-val": "#a855f7",
        "gardering": "#64748b",
        "strykning": "#ef4444",
    }.get(rec, "rgba(255,255,255,0.06)")


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


def _race_table_html(race: Race) -> str:
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
    for e in sorted_entries:
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

        rows.append(
            f'<tr class="horse-row{toggle_class}" data-horse="{e.post_position}">'
            f'<td class="pos">{e.post_position}</td>'
            f'<td class="horse-name">{toggle_icon}{_esc(e.horse.name)}{value_badge}</td>'
            f'<td class="score"><strong>{e.super_score:.0f}</strong></td>'
            f'<td class="bet">{bet_str}</td>'
            f'<td><span class="rec-badge" style="background:{bg};color:{color}">{_esc(rec)}</span></td>'
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
        f'<th>Rek</th>{result_header}<th>Kusk</th><th>Trend</th>{factor_headers}'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'</div>'
        f'</div>'
    )


def _summary_html(game_round: GameRound) -> str:
    """Row-based division list for overview."""
    is_round_finished = game_round.is_finished
    rows = []
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
            if e.recommendation in ("2-val", "3-val")
        ]
        gard_str = ", ".join(
            f"{e.post_position} {_esc(e.horse.name[:8])}"
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
        elif risk_pct >= 25:
            risk_color = "#eab308"
        else:
            risk_color = "#22c55e"

        # Driver name
        driver = ""
        if top.driver_name:
            driver = _esc(top.driver_name[:15])

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
                    result_badge = '<span class="hit-badge" style="font-size:.7rem">&#10003;</span>'
                elif rec_w in ("2-val", "3-val", "gardering"):
                    result_badge = '<span class="partial-badge" style="font-size:.7rem">~</span>'
                else:
                    result_badge = '<span class="miss-badge" style="font-size:.7rem">&#10007;</span>'

        rows.append(
            f'<div class="division-row" onclick="showDivision({race.race_number})">'
            f'<span class="div-num-lg">{race.race_number}</span>'
            f'<div class="pick-info">'
            f'<strong>{top.post_position} {_esc(top.horse.name[:15])}</strong> '
            f'<span class="rec-badge" style="background:{bg};color:{color};font-size:.65rem">{_esc(top.recommendation)}</span>'
            f'{result_badge}'
            f'<div class="driver-name">{driver}</div>'
            f'</div>'
            f'<span class="score-val">{top.super_score:.0f}</span>'
            f'<div style="display:flex;align-items:center;gap:6px">'
            f'<div class="streck-bar"><div style="width:{bet_pct:.0f}%"></div></div>'
            f'<span style="font-size:.7rem;color:#4b5563;font-family:\'DM Mono\',monospace">{bet_str}</span>'
            f'</div>'
            f'<span class="gard-text">{gard_str if gard_str else "—"}</span>'
            f'<span class="risk-pct" style="color:{risk_color}">{risk_pct:.0f}%</span>'
            f'<span class="arrow-icon">→</span>'
            f'</div>'
        )

    return f'<div class="division-list">{"".join(rows)}</div>'


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

        # Spik check
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
        if winner_entry and winner_entry.recommendation != "strykning":
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
        f'<div class="label">Spikar rätt</div>'
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


def _system_html(game_round: GameRound) -> str:
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

            spik_badge = ' <span class="spik-badge">🔒 SPIK</span>' if rp.num_picks == 1 else ''

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

    return (
        f'<div id="system" class="summary-card system-section">'
        f'<h2>💰 System — Budget 2,500 kr</h2>'
        f'<p style="color:#6b7280;font-size:.85rem;margin-bottom:1rem">'
        f'4 strategier backtestade på 266 omgångar (5 år). '
        f'Inkl. spik-variant med 1 spik vid hög konfidens.</p>'
        f'{"".join(systems_html)}'
        f'</div>'
    )


def _stats_html(backlog_data: dict | None = None) -> str:
    """Generera statistik-sektion med ROI per år och per bana, per strategi."""
    if not backlog_data or "strategies" not in backlog_data:
        return ""

    strategies = backlog_data["strategies"]
    strat_names = list(strategies.keys())

    # Strategi-knappar
    strat_buttons = ['<button class="strat-btn active" onclick="filterStats(\'all\')">Alla strategier</button>']
    strat_short = {"I_streck_1st": "I_streck", "Q_dom_x_mktgap": "Q_dom", "D_market_gap": "D_market", "I_streck_spik1": "I_spik1", "I_streck_spik2": "I_spik2", "I_streck_spik3": "I_spik3"}
    for s in strat_names:
        short = strat_short.get(s, s[:10])
        strat_buttons.append(
            f'<button class="strat-btn" onclick="filterStats(\'{s}\')">{short}</button>'
        )

    # Speltyp-knappar
    GT_COLORS_BTN = {
        "V75": "#f59e0b", "V85": "#f59e0b", "GS75": "#a78bfa",
        "V86": "#fb923c", "V64": "#34d399", "V65": "#34d399",
    }
    all_game_types = sorted(set(
        gt
        for s in strategies.values()
        for gt in s.get("game_types", {}).keys()
    ))
    gt_buttons = ['<button class="gt-btn active" onclick="filterStatsGT(\'all\')">Alla speltyper</button>']
    for gt in all_game_types:
        color = GT_COLORS_BTN.get(gt, "#64748b")
        gt_buttons.append(
            f'<button class="gt-btn" onclick="filterStatsGT(\'{gt}\')" '
            f'style="--gt-color:{color}">{gt}</button>'
        )

    # ── Strategi-översikt ──
    overview_rows = []
    for s in strat_names:
        ss = strategies[s]
        short = strat_short.get(s, s[:10])
        roi = ss.get("roi", 0)
        roi_cls = "#22c55e" if roi > 0 else "#ef4444"
        netto = ss.get("netto", 0)
        netto_cls = "#22c55e" if netto > 0 else "#ef4444"
        overview_rows.append(
            f'<tr>'
            f'<td><strong>{short}</strong></td>'
            f'<td>{ss.get("rounds_played", 0)}</td>'
            f'<td>{ss.get("full_hits", 0)}</td>'
            f'<td>{ss.get("partial_hits", 0)}</td>'
            f'<td>{ss.get("total_cost", 0):,.0f} kr</td>'
            f'<td>{ss.get("total_payout", 0):,.0f} kr</td>'
            f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
            f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
            f'</tr>'
        )

    overview_html = (
        f'<div class="stats-block">'
        f'<h3>Strategi-översikt</h3>'
        f'<div class="table-wrap"><table class="stats-table">'
        f'<thead><tr><th>Strategi</th><th>Omg</th><th>Full</th><th>Del</th>'
        f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
        f'<tbody>{"".join(overview_rows)}</tbody>'
        f'</table></div></div>'
    )

    # ── ROI per speltyp — en tabell per strategi (dold via JS) ──
    game_type_sections = []
    GT_COLORS_STATS = {
        "V75": "#f59e0b", "V85": "#f59e0b", "GS75": "#a78bfa",
        "V86": "#fb923c", "V64": "#34d399", "V65": "#34d399",
    }
    for s in strat_names:
        game_types = strategies[s].get("game_types", {})
        if not game_types:
            continue
        short = strat_short.get(s, s[:10])
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
                f'<td>{g.get("rounds", 0)}</td>'
                f'<td>{g.get("full", 0)}</td>'
                f'<td>{g.get("wins", 0)}</td>'
                f'<td>{g.get("cost", 0):,.0f} kr</td>'
                f'<td>{g.get("payout", 0):,.0f} kr</td>'
                f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
                f'</tr>'
            )

        game_type_sections.append(
            f'<div class="stats-block strat-block" data-strat="{s}">'
            f'<h3>🎰 ROI per speltyp — {short}</h3>'
            f'<div class="table-wrap"><table class="stats-table">'
            f'<thead><tr><th>Speltyp</th><th>Omg</th><th>Full</th><th>Vinster</th>'
            f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
            f'<tbody>{"".join(gt_rows)}</tbody>'
            f'</table></div></div>'
        )

    # ── Backtest vs Live-jämförelse ──
    bt_vs_live_html = ""
    entries = backlog_data.get("entries", [])
    if entries:
        from collections import defaultdict
        live_by_strat = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "full": 0, "partial": 0})
        for entry in entries:
            if entry.get("live") and entry.get("races_finished", 0) == entry.get("races_total", 0) and entry.get("races_finished", 0) > 0:
                # Avslutad live-omgång (alla lopp klara)
                s = entry.get("strategy", "")
                ls = live_by_strat[s]
                ls["rounds"] += 1
                ls["cost"] += entry.get("cost", 0)
                ls["payout"] += entry.get("payout", 0)
                if entry.get("hit"):
                    ls["full"] += 1
                elif entry.get("payout", 0) > 0:
                    ls["partial"] += 1
            elif not entry.get("live"):
                pass  # Backtest-stats redan i strategies dict

        # Bygg rader
        bt_live_rows = []
        for s in strat_names:
            short = strat_short.get(s, s[:10])
            # Backtest ROI från strategies
            bt_roi = strategies[s].get("roi", 0)
            bt_roi_cls = "#22c55e" if bt_roi > 0 else "#ef4444"
            bt_rounds = strategies[s].get("rounds_played", 0)

            # Live stats
            ls = live_by_strat.get(s)
            if ls and ls["rounds"] >= 1:
                l_roi = (ls["payout"] - ls["cost"]) / ls["cost"] * 100 if ls["cost"] > 0 else 0
                l_roi_cls = "#22c55e" if l_roi > 0 else "#ef4444"
                l_netto = ls["payout"] - ls["cost"]
                l_netto_cls = "#22c55e" if l_netto > 0 else "#ef4444"
                if ls["rounds"] >= 5:
                    status = '<span style="color:#22c55e">Tracking</span>'
                    roi_str = f'<span style="color:{l_roi_cls};font-weight:700">{l_roi:+.1f}%</span>'
                    netto_str = f'<span style="color:{l_netto_cls};font-weight:700">{l_netto:+,.0f} kr</span>'
                else:
                    status = f'<span style="color:#6b7280">Samlar data ({ls["rounds"]}/5)</span>'
                    roi_str = f'<span style="color:{l_roi_cls}">{l_roi:+.1f}%</span>'
                    netto_str = f'<span style="color:{l_netto_cls}">{l_netto:+,.0f} kr</span>'
            else:
                status = '<span style="color:#6b7280">Ingen data</span>'
                roi_str = '<span style="color:#6b7280">—</span>'
                netto_str = '<span style="color:#6b7280">—</span>'
                ls = {"rounds": 0}

            bt_live_rows.append(
                f'<tr>'
                f'<td><strong>{short}</strong></td>'
                f'<td style="color:{bt_roi_cls};font-weight:700">{bt_roi:+.1f}%</td>'
                f'<td>{bt_rounds}</td>'
                f'<td>{roi_str}</td>'
                f'<td>{netto_str}</td>'
                f'<td>{ls["rounds"]}</td>'
                f'<td>{status}</td>'
                f'</tr>'
            )

        if bt_live_rows:
            bt_vs_live_html = (
                f'<div class="stats-block">'
                f'<h3>🔄 Backtest vs Live</h3>'
                f'<div class="table-wrap"><table class="stats-table">'
                f'<thead><tr><th>Strategi</th><th>Backtest ROI</th><th>BT omg</th>'
                f'<th>Live ROI</th><th>Live netto</th><th>Live omg</th><th>Status</th></tr></thead>'
                f'<tbody>{"".join(bt_live_rows)}</tbody>'
                f'</table></div></div>'
            )

    # ── ROI per år — en tabell per strategi (dold via JS) ──
    yearly_sections = []
    for s in strat_names:
        yearly = strategies[s].get("yearly", {})
        if not yearly:
            continue
        short = strat_short.get(s, s[:10])
        yr_rows = []
        for year in sorted(yearly.keys()):
            y = yearly[year]
            roi = y.get("roi", 0)
            roi_cls = "#22c55e" if roi > 0 else "#ef4444"
            netto = y.get("netto", 0)
            netto_cls = "#22c55e" if netto > 0 else "#ef4444"
            yr_rows.append(
                f'<tr>'
                f'<td><strong>{year}</strong></td>'
                f'<td>{y.get("rounds", 0)}</td>'
                f'<td>{y.get("full", 0)}</td>'
                f'<td>{y.get("wins", 0)}</td>'
                f'<td>{y.get("cost", 0):,.0f} kr</td>'
                f'<td>{y.get("payout", 0):,.0f} kr</td>'
                f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
                f'</tr>'
            )

        yearly_sections.append(
            f'<div class="stats-block strat-block" data-strat="{s}">'
            f'<h3>📅 ROI per år — {short}</h3>'
            f'<div class="table-wrap"><table class="stats-table">'
            f'<thead><tr><th>År</th><th>Omg</th><th>Full</th><th>Vinster</th>'
            f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
            f'<tbody>{"".join(yr_rows)}</tbody>'
            f'</table></div></div>'
        )

    # ── ROI per bana — en tabell per strategi ──
    track_sections = []
    for s in strat_names:
        tracks = strategies[s].get("tracks", {})
        if not tracks:
            continue
        short = strat_short.get(s, s[:10])
        trk_rows = []
        # Sortera efter omgångar (mest först)
        for track in sorted(tracks.keys(), key=lambda t: tracks[t].get("rounds", 0), reverse=True):
            t = tracks[track]
            roi = t.get("roi", 0)
            roi_cls = "#22c55e" if roi > 0 else "#ef4444"
            netto = t.get("netto", 0)
            netto_cls = "#22c55e" if netto > 0 else "#ef4444"
            trk_rows.append(
                f'<tr>'
                f'<td><strong>{_esc(track)}</strong></td>'
                f'<td>{t.get("rounds", 0)}</td>'
                f'<td>{t.get("full", 0)}</td>'
                f'<td>{t.get("wins", 0)}</td>'
                f'<td>{t.get("cost", 0):,.0f} kr</td>'
                f'<td>{t.get("payout", 0):,.0f} kr</td>'
                f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
                f'</tr>'
            )

        track_sections.append(
            f'<div class="stats-block strat-block" data-strat="{s}">'
            f'<h3>🏇 ROI per bana — {short}</h3>'
            f'<div class="table-wrap"><table class="stats-table">'
            f'<thead><tr><th>Bana</th><th>Omg</th><th>Full</th><th>Vinster</th>'
            f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
            f'<tbody>{"".join(trk_rows)}</tbody>'
            f'</table></div></div>'
        )

    # ── ROI per månad — en tabell per strategi ──
    monthly_sections = []
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
                f'<tr>'
                f'<td><strong>{m.get("name", month)}</strong></td>'
                f'<td>{m.get("rounds", 0)}</td>'
                f'<td>{m.get("full", 0)}</td>'
                f'<td>{m.get("wins", 0)}</td>'
                f'<td>{m.get("cost", 0):,.0f} kr</td>'
                f'<td>{m.get("payout", 0):,.0f} kr</td>'
                f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
                f'</tr>'
            )

        monthly_sections.append(
            f'<div class="stats-block strat-block" data-strat="{s}">'
            f'<h3>📆 ROI per månad — {short}</h3>'
            f'<div class="table-wrap"><table class="stats-table">'
            f'<thead><tr><th>Månad</th><th>Omg</th><th>Full</th><th>Vinster</th>'
            f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
            f'<tbody>{"".join(m_rows)}</tbody>'
            f'</table></div></div>'
        )

    # ── ROI per speltyp per år — en tabell per strategi × speltyp ──
    yearly_gt_sections = []
    for s in strat_names:
        yearly_gt = strategies[s].get("yearly_by_game_type", {})
        if not yearly_gt:
            continue
        short = strat_short.get(s, s[:10])
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
                    f'<tr>'
                    f'<td><strong>{year}</strong></td>'
                    f'<td>{y.get("rounds", 0)}</td>'
                    f'<td>{y.get("full", 0)}</td>'
                    f'<td>{y.get("wins", 0)}</td>'
                    f'<td>{y.get("cost", 0):,.0f} kr</td>'
                    f'<td>{y.get("payout", 0):,.0f} kr</td>'
                    f'<td style="color:{roi_cls};font-weight:700">{roi:+.1f}%</td>'
                    f'<td style="color:{netto_cls};font-weight:700">{netto:+,.0f} kr</td>'
                    f'</tr>'
                )
            yearly_gt_sections.append(
                f'<div class="stats-block strat-block gt-block" data-strat="{s}" data-gt="{gt}">'
                f'<h3 style="color:{gt_color}">📅 {gt} ROI per år — {short}</h3>'
                f'<div class="table-wrap"><table class="stats-table">'
                f'<thead><tr><th>År</th><th>Omg</th><th>Full</th><th>Vinster</th>'
                f'<th>Insats</th><th>Utdeln</th><th>ROI</th><th>Netto</th></tr></thead>'
                f'<tbody>{"".join(yr_rows)}</tbody>'
                f'</table></div></div>'
            )

    # Equity curve data from backlog entries
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
            f'<h3>📊 Equity Curve — Kumulativ avkastning</h3>'
            f'<canvas id="equity-chart" height="200"></canvas>'
            f'<script>'
            f'(function(){{'
            f'const data = {_json.dumps(equity_data)};'
            f'new Chart(document.getElementById("equity-chart"),{{'
            f'type:"line",'
            f'data:{{datasets:[{{data:data,borderColor:"#f59e0b",backgroundColor:"rgba(245,166,35,0.08)",'
            f'fill:true,tension:0.3,pointRadius:0,borderWidth:2}}]}},'
            f'options:{{responsive:true,plugins:{{legend:{{display:false}}}},'
            f'scales:{{x:{{type:"category",ticks:{{color:"#64748b",maxTicksLimit:8,font:{{size:10}}}},'
            f'grid:{{color:"rgba(255,255,255,0.04)"}}}},'
            f'y:{{ticks:{{color:"#64748b",callback:function(v){{return (v/1000).toFixed(0)+"k"}},'
            f'font:{{size:10}}}},grid:{{color:"rgba(255,255,255,0.04)"}}}}}}}}}}'
            f');}})();'
            f'</script>'
            f'</div>'
        )

    return (
        f'<div id="stats" class="summary-card stats-section">'
        f'<h2>📈 Statistik — ROI per år, månad, speltyp & bana</h2>'
        f'{chart_html}'
        f'<div class="strat-filter">{"".join(strat_buttons)}</div>'
        f'<div class="strat-filter gt-filter-row">{"".join(gt_buttons)}</div>'
        f'{overview_html}'
        f'{bt_vs_live_html}'
        f'{"".join(game_type_sections)}'
        f'{"".join(yearly_sections)}'
        f'{"".join(monthly_sections)}'
        f'{"".join(yearly_gt_sections)}'
        f'{"".join(track_sections)}'
        f'</div>'
    )


def _backlog_html(
    game_round: GameRound,
    backlog_data: dict | None = None,
) -> str:
    """Generera backlog-sektion med historiska system-resultat, alla strategier."""
    if not backlog_data or "entries" not in backlog_data:
        return ""

    entries = backlog_data["entries"]
    cutoff = str(date.today() - timedelta(days=30))
    strat_short = {"I_streck_1st": "I_streck", "Q_dom_x_mktgap": "Q_dom", "D_market_gap": "D_market", "I_streck_spik1": "I_spik1", "I_streck_spik2": "I_spik2", "I_streck_spik3": "I_spik3"}

    # Strategi-filterknappar
    all_strategies = sorted(set(e.get("strategy", "") for e in entries))
    filter_btns = ['<button class="strat-btn active" onclick="filterBacklog(\'all\')">Alla strategier</button>']
    for s in all_strategies:
        short = strat_short.get(s, s[:10])
        filter_btns.append(
            f'<button class="strat-btn" onclick="filterBacklog(\'{s}\')">{short}</button>'
        )

    # Speltyp-filterknappar
    GT_COLORS_BL = {
        "V75": "#f59e0b", "V85": "#f59e0b", "GS75": "#a78bfa",
        "V86": "#fb923c", "V64": "#34d399", "V65": "#34d399",
    }
    all_game_types = sorted(set(e.get("game_type", "") for e in entries if e.get("game_type")))
    gt_filter_btns = ['<button class="gt-btn active" onclick="filterBacklogGT(\'all\')">Alla speltyper</button>']
    for gt in all_game_types:
        color = GT_COLORS_BL.get(gt, "#64748b")
        gt_filter_btns.append(
            f'<button class="gt-btn" onclick="filterBacklogGT(\'{gt}\')" '
            f'style="--gt-color:{color}">{gt}</button>'
        )
    gt_filter_btns.append(
        '<button class="gt-btn" onclick="filterBacklogRecent(12)" '
        'style="--gt-color:#f59e0b">Senaste 12 mån</button>'
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

        # Resultat-badge med antal rätt
        if is_live:
            races_remaining = races_total - races_finished
            if races_finished == 0:
                hit_badge = f'<span class="live-result-badge">⏳ 0/{races_total} ({races_remaining} kvar)</span>'
            elif num_correct == races_finished:
                hit_badge = f'<span class="live-result-badge live-allright">✓ {num_correct}/{races_finished} ({races_remaining} kvar)</span>'
            else:
                hit_badge = f'<span class="live-result-badge">◐ {num_correct}/{races_finished} ({races_remaining} kvar)</span>'
        elif hit:
            hit_badge = f'<span class="hit-badge">✓ {num_correct}/{num_races}</span>'
        elif payout > 0:
            hit_badge = f'<span class="partial-badge">◐ {num_correct}/{num_races}</span>'
        else:
            hit_badge = f'<span class="miss-badge">✗ {num_correct}/{num_races}</span>'

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

        # Strategi-kort
        short_strat = strat_short.get(strategy, strategy[:10])
        strat_colors = {
            "I_streck_1st": ("#f59e0b", "rgba(245,166,35,0.15)"),
            "Q_dom_x_mktgap": ("#a78bfa", "rgba(167,139,250,0.15)"),
            "D_market_gap": ("#fb923c", "rgba(251,146,60,0.15)"),
            "I_streck_spik1": ("#fbbf24", "rgba(251,191,36,0.15)"),
            "I_streck_spik2": ("#34d399", "rgba(52,211,153,0.15)"),
            "I_streck_spik3": ("#f472b6", "rgba(244,114,182,0.15)"),
        }
        sc, sbg = strat_colors.get(strategy, ("#64748b", "rgba(100,116,139,0.15)"))
        strat_badge = f'<span class="strat-tag" style="color:{sc};background:{sbg}">{short_strat}</span>'

        # Extra info
        ppr = entry.get("payout_per_row", 0)
        wr = entry.get("winning_rows", 0)
        detail = f"{ppr:,.0f}/rad×{wr}" if ppr > 0 else ""

        # LIVE-badge
        live_badge = ' <span class="live-badge">🔴 LIVE</span>' if is_live else ""

        # Expanderbar rad med system-detaljer
        has_races = bool(entry.get("races"))
        toggle_attr = f'onclick="toggleBlDetail(\'bl-detail-{idx}\')"' if has_races else ''
        expand_icon = '<span class="bl-expand-icon">▸</span> ' if has_races else ''

        # Speltyp-badge med färg
        entry_gt = entry.get("game_type", "")
        gt_c, gt_bg = GT_COLORS_BL.get(entry_gt, "#64748b"), {
            "V75": "rgba(245,166,35,0.08)", "V85": "rgba(245,166,35,0.08)",
            "GS75": "rgba(167,139,250,0.15)", "V86": "rgba(251,146,60,0.15)",
            "V64": "rgba(52,211,153,0.15)", "V65": "rgba(52,211,153,0.15)",
        }.get(entry_gt, "rgba(100,116,139,0.15)")
        gt_badge = f'<span class="gt-tag" style="color:{gt_c};background:{gt_bg}">{entry_gt}</span>'

        # Hoppa över äldre entries (>30 dagar) — de räknas i summering men renderas ej
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

        # Expanderbar detalj-rad med picks per lopp
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

                if vinnare:
                    vinnare_str = f'Vann: {vinnare} {vinnare_namn} ({vinnare_streck}%)'
                else:
                    vinnare_str = '<span style="color:#6b7280">Väntar...</span>'

                race_rows.append(
                    f'<tr class="bl-race-row">'
                    f'<td style="padding-left:2rem;color:#6b7280">Avd {rd.get("avd", "")}</td>'
                    f'<td style="font-size:.75rem">{rd.get("dist","")}m {rd.get("metod","")}</td>'
                    f'<td style="font-size:.75rem">{rd.get("picks","")} val</td>'
                    f'<td style="{ratt_cls};font-weight:600">{ratt_icon}</td>'
                    f'<td colspan="3" style="font-size:.75rem;color:#6b7280">{picks_str}</td>'
                    f'<td style="font-size:.75rem;color:#6b7280">{vinnare_str}</td>'
                    f'<td></td>'
                    f'</tr>'
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

    show_all_btn = ""
    if hidden_count > 0:
        show_all_btn = (
            f'<span style="color:#6b7280;font-size:.82rem;margin-bottom:.8rem;display:inline-block">'
            f'Visar senaste 30 dagar ({total_rounds - hidden_count} av {total_rounds} omgångar)'
            f'</span>'
        )

    # Streak indicator (last 5 non-live rounds)
    recent_results = []
    for entry in sorted(entries, key=lambda e: e.get("date", ""), reverse=True):
        if entry.get("live", False):
            continue
        if entry.get("hit", False):
            recent_results.append('<span style="color:#22c55e;font-weight:700">✓</span>')
        elif entry.get("payout", 0) > 0:
            recent_results.append('<span style="color:#eab308;font-weight:700">◐</span>')
        else:
            recent_results.append('<span style="color:#ef4444;font-weight:700">✗</span>')
        if len(recent_results) >= 5:
            break
    streak_html = f'<span style="margin-left:.8rem;font-size:1.1rem;letter-spacing:.3rem">{"".join(recent_results)}</span>' if recent_results else ""

    return (
        f'<div id="backlog" class="summary-card backlog-section">'
        f'<h2>📊 Backlog — Historiska resultat{streak_html}</h2>'
        f'<div class="strat-filter bl-filter">{"".join(filter_btns)}</div>'
        f'<div class="strat-filter gt-filter-row bl-gt-filter">{"".join(gt_filter_btns)}</div>'
        f'{show_all_btn}'
        f'<div class="backlog-summary" id="bl-summary">'
        f'<div class="bl-stat"><span class="bl-val" id="bl-rounds">{total_rounds}</span><span class="bl-lbl">Omgångar</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-full">{full_hits}</span><span class="bl-lbl">Alla rätt</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-partial">{partial_hits}</span><span class="bl-lbl">Delvinster</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-winrate">{win_rate:.0f}%</span><span class="bl-lbl">Vinstfrekvens</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-cost">{total_cost:,.0f} kr</span><span class="bl-lbl">Total insats</span></div>'
        f'<div class="bl-stat"><span class="bl-val" id="bl-payout">{total_payout:,.0f} kr</span><span class="bl-lbl">Total utdelning</span></div>'
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


def generate_dashboard_html(
    game_round: GameRound,
    available_dates: list[tuple[str, bool]] | None = None,
    backlog_data: dict | list[dict] | None = None,
    available_rounds: list[tuple[str, str, str, bool]] | list[tuple[str, str, str, bool, str]] | None = None,
    premium: bool = True,
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

    # Generera sektioner
    race_htmls = {r.race_number: _race_table_html(r) for r in game_round.races}
    summary = _summary_html(game_round)
    system_section = _system_html(game_round)
    stats_section = _stats_html(backlog_data)
    backlog_section = _backlog_html(game_round, backlog_data)
    accuracy = _accuracy_html(game_round)
    risk_bar = _risk_summary_bar(game_round)

    current_key = f"{game_round.game_type}/{game_round.round_date}"
    dropdown = _round_dropdown_html(current_key, norm_rounds)
    sidebar = _sidebar_html(
        game_round,
        has_system=bool(system_section),
        has_backlog=bool(backlog_section),
        has_stats=bool(stats_section),
    )

    # Wrap each race in its own section (premium gating on races 3-8)
    _premium_cta = (
        '<div class="premium-cta">'
        '<h3>Premium-analys</h3>'
        '<p>Lås upp detaljerade analyser för alla avdelningar</p>'
        '<button class="premium-cta-btn">Uppgradera</button>'
        '</div>'
    )
    race_sections_parts = []
    for n, h in race_htmls.items():
        if not premium and n >= 3:
            race_sections_parts.append(
                f'<section id="s-race-{n}" class="dashboard-section">'
                f'<div class="premium-locked">{h}{_premium_cta}</div></section>'
            )
        else:
            race_sections_parts.append(
                f'<section id="s-race-{n}" class="dashboard-section">{h}</section>'
            )
    race_sections = "".join(race_sections_parts)

    # AI-chatt sektion
    chat_section = (
        '<div class="chat-section">'
        '<div class="chat-header" onclick="toggleChat()">'
        '<h2>AI Analys</h2>'
        '<button class="chat-toggle" id="chat-toggle-icon">&#9654;</button>'
        '</div>'
        '<div class="chat-body" id="chat-body">'
        '<div class="chat-suggestions">'
        '<button class="chat-suggest-btn" onclick="askSuggestion(\'Sammanfatta omgången och ge dina tankar\')">Sammanfatta</button>'
        '<button class="chat-suggest-btn" onclick="askSuggestion(\'Vilka lopp har högst skrällrisk och varför?\')">Skrällrisker</button>'
        '<button class="chat-suggest-btn" onclick="askSuggestion(\'Ge mig dina bästa spikar med motivering\')">Spikar</button>'
        '<button class="chat-suggest-btn" onclick="askSuggestion(\'Finns det value-hästar som sticker ut?\')">Value</button>'
        '</div>'
        '<div class="chat-messages" id="chat-messages"></div>'
        '<div class="chat-input-row">'
        '<input class="chat-input" id="chat-input" placeholder="Fråga om omgången...">'
        '<button class="chat-send" id="chat-send" onclick="sendChat()">Skicka</button>'
        '</div>'
        '</div>'
        '</div>'
    )

    # Spelvärde — Hero section with SVG ring (grid layout)
    sv = _calculate_spelvarde(game_round)
    if sv:
        score = sv["score"]
        circumference = 377.0  # 2 * pi * 60
        offset = circumference * (1 - score / 100)
        sv_bar = (
            f'<div class="hero-grid">'
            # Left: Score card with ring
            f'<div class="hero-score-card">'
            f'<svg width="140" height="140" viewBox="0 0 140 140">'
            f'<defs><linearGradient id="scoreGrad" x1="0%" y1="0%" x2="100%" y2="100%">'
            f'<stop offset="0%" stop-color="#4ade80"/><stop offset="100%" stop-color="#22c55e"/>'
            f'</linearGradient></defs>'
            f'<circle cx="70" cy="70" r="60" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="8"/>'
            f'<circle cx="70" cy="70" r="60" fill="none" stroke="url(#scoreGrad)" stroke-width="8" '
            f'stroke-dasharray="{circumference:.1f}" stroke-dashoffset="{offset:.1f}" '
            f'stroke-linecap="round" transform="rotate(-90 70 70)"/>'
            f'<text x="70" y="65" text-anchor="middle" font-size="36" font-weight="800" '
            f'fill="#e2e8f0" font-family="\'DM Mono\',monospace">{score}</text>'
            f'<text x="70" y="85" text-anchor="middle" font-size="10" font-weight="600" '
            f'fill="#4b5563" font-family="\'DM Sans\',sans-serif" letter-spacing="0.08em">SPELVÄRDE</text>'
            f'</svg>'
            f'<div class="hero-score-text" style="color:{sv["color"]}">{_esc(sv["text"])}</div>'
            f'<div class="hero-score-advice">{_esc(sv["advice"])}</div>'
            f'</div>'
            # Right: KPIs + heatmap
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

    if system_section and not premium:
        system_btn = '<button onclick="openDrawer()" class="system-btn" style="opacity:.5;cursor:not-allowed" disabled>System 🔒</button>'
    elif system_section:
        system_btn = '<button onclick="openDrawer()" class="system-btn">System</button>'
    else:
        system_btn = ""

    # Division tabs for top-bar
    div_tabs = '<div class="div-tabs">' + "".join(
        f'<button class="div-tab" data-section="race-{r.race_number}" '
        f'onclick="showDivision({r.race_number})">{r.race_number}</button>'
        for r in game_round.races
    ) + '</div>'

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kungens Trav — {_esc(game_round.game_type)} {game_round.round_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600;9..40,700&family=DM+Mono:wght@400;500&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
::selection{{background:rgba(59,130,246,0.3)}}
body{{font-family:'DM Sans',system-ui,-apple-system,sans-serif;
background:#0c0e14;color:#c9d1d9;line-height:1.6;-webkit-font-smoothing:antialiased}}

/* ── Sidebar ── */
.sidebar{{width:220px;min-height:100vh;background:#111318;
border-right:1px solid rgba(255,255,255,0.06);display:flex;flex-direction:column;
position:fixed;left:0;top:0;bottom:0;z-index:100;transition:width 0.25s ease}}
.sidebar.collapsed{{width:64px}}
.sidebar.collapsed .sb-text{{display:none}}
.sb-brand{{padding:20px;border-bottom:1px solid rgba(255,255,255,0.06);
display:flex;align-items:center;gap:10px;cursor:pointer}}
.sb-icon{{width:36px;height:36px;border-radius:10px;
background:linear-gradient(135deg,#f59e0b,#d97706);
display:flex;align-items:center;justify-content:center;flex-shrink:0;
font-size:16px;color:#000;font-weight:700}}
.sb-title{{font-size:15px;font-weight:700;color:#f0f0f0;letter-spacing:-0.01em;line-height:1.2}}
.sb-sub{{font-size:11px;color:#6b7280;font-weight:500}}
.sb-race{{background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.15);
border-radius:10px;padding:12px 14px;margin:16px;cursor:pointer}}
.sb-race-type{{font-size:13px;font-weight:700;color:#60a5fa}}
.sb-race-meta{{font-size:12px;color:#6b7280;margin-top:4px}}
.sb-race-oms{{margin-top:6px;font-size:11px;color:#6b7280}}
.sb-race-oms span{{color:#4ade80;font-weight:600}}
.sb-nav{{padding:0 12px;flex:1;overflow-y:auto}}
.nav-item{{width:100%;display:flex;align-items:center;gap:10px;
padding:10px 12px;background:transparent;border:none;border-radius:8px;
cursor:pointer;color:#6b7280;font-size:13px;font-weight:500;
font-family:inherit;transition:all 0.15s;text-align:left}}
.nav-item:hover{{background:rgba(255,255,255,0.04)}}
.nav-item.active{{background:rgba(59,130,246,0.1);color:#60a5fa;font-weight:600}}
.nav-icon{{width:18px;text-align:center;flex-shrink:0}}
.nav-divider{{height:1px;background:rgba(255,255,255,0.06);margin:12px 0}}
.nav-label{{font-size:10px;font-weight:600;color:#4b5563;
letter-spacing:0.08em;padding:6px 12px;text-transform:uppercase}}
.div-item{{width:100%;display:flex;align-items:center;gap:8px;
padding:7px 12px;background:transparent;border:none;border-radius:6px;
cursor:pointer;color:#6b7280;font-size:12px;font-weight:400;
font-family:inherit;transition:all 0.15s;text-align:left;margin-bottom:1px}}
.div-item:hover{{background:rgba(255,255,255,0.03)}}
.div-item.active{{background:rgba(59,130,246,0.1);color:#e2e8f0}}
.div-num{{width:22px;height:22px;border-radius:6px;font-size:11px;font-weight:700;
display:inline-flex;align-items:center;justify-content:center;
background:rgba(255,255,255,0.04);color:#6b7280;flex-shrink:0}}
.div-item.active .div-num{{background:rgba(59,130,246,0.2);color:#60a5fa}}
.div-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0;opacity:0.8}}
.sb-premium{{padding:16px;border-top:1px solid rgba(255,255,255,0.06)}}
.sb-premium-card{{background:linear-gradient(135deg,rgba(245,158,11,0.1),rgba(245,158,11,0.03));
border:1px solid rgba(245,158,11,0.15);border-radius:10px;padding:12px 14px}}
.sb-premium-btn{{margin-top:10px;width:100%;padding:7px 0;
background:linear-gradient(135deg,#f59e0b,#d97706);
border:none;border-radius:6px;color:#000;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit}}

/* ── App layout ── */
.app-layout{{display:flex;min-height:100vh}}
.main-area{{flex:1;margin-left:220px;transition:margin-left 0.25s ease;
display:flex;flex-direction:column;min-height:100vh}}

/* ── Top bar ── */
.top-bar{{height:52px;background:#111318;
border-bottom:1px solid rgba(255,255,255,0.06);
display:flex;align-items:center;justify-content:space-between;
padding:0 28px;position:sticky;top:0;z-index:50;flex-shrink:0}}
.top-bar-left{{display:flex;align-items:center;gap:16px}}
.top-bar-right{{display:flex;align-items:center;gap:12px}}
.top-stat{{color:#4b5563;font-size:.78rem;font-weight:500;white-space:nowrap;
font-family:'DM Mono',monospace}}
.round-select{{padding:.35rem .8rem;border-radius:8px;border:1px solid rgba(255,255,255,0.06);
background:rgba(255,255,255,.04);color:#c9d1d9;font-size:.82rem;cursor:pointer;
max-width:260px;outline:none;transition:border-color .2s;font-family:inherit}}
.round-select:hover{{border-color:rgba(255,255,255,.15)}}
.round-select option{{background:#151820;color:#c9d1d9}}

/* Division tabs in top bar */
.div-tabs{{display:flex;background:rgba(255,255,255,0.04);border-radius:8px;padding:3px;gap:2px}}
.div-tab{{width:30px;height:28px;display:flex;align-items:center;justify-content:center;
background:transparent;border:none;border-radius:6px;cursor:pointer;color:#6b7280;
font-size:12px;font-weight:600;transition:all 0.15s;font-family:inherit}}
.div-tab:hover{{color:#9ca3af}}
.div-tab.active{{background:#3b82f6;color:#fff}}

.system-btn{{padding:6px 16px;border-radius:6px;border:none;
background:linear-gradient(135deg,#f59e0b,#d97706);
color:#000;font-weight:700;font-size:12px;cursor:pointer;white-space:nowrap;
transition:all .25s;font-family:inherit}}
.system-btn:hover{{filter:brightness(1.1);transform:translateY(-1px)}}

/* ── Content ── */
.content{{overflow-y:auto;flex:1;padding:24px 28px;background:#0c0e14}}
.dashboard-section{{display:none;max-width:1100px;margin:0 auto}}
.dashboard-section.active{{display:block}}

/* System drawer — fullscreen modal */
.system-drawer{{position:fixed;inset:0;background:#0c0e14;z-index:200;
transform:translateY(100%);transition:transform .35s ease;overflow-y:auto;padding:2rem}}
.system-drawer.open{{transform:translateY(0)}}
.drawer-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:199;display:none;
backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}}
.drawer-overlay.open{{display:block}}
.drawer-close{{position:fixed;top:1.2rem;right:1.5rem;background:#151820;border:1px solid rgba(255,255,255,0.06);
font-size:1.3rem;width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;
cursor:pointer;color:#6b7280;z-index:201;transition:all .2s}}
.drawer-close:hover{{color:#e2e8f0;background:#1a1f2b}}

/* Cards */
.summary-card,.race-card{{background:#151820;border-radius:14px;padding:1.8rem;
margin-bottom:1.5rem;border:1px solid rgba(255,255,255,0.06);
transition:all .2s ease}}
.race-card:hover{{background:#1a1f2b}}
.race-header{{display:flex;justify-content:space-between;align-items:baseline;
flex-wrap:wrap;gap:.5rem;margin-bottom:1rem}}
.race-header h2{{font-size:1.25rem;color:#e2e8f0;font-weight:700;letter-spacing:-0.01em}}
.race-meta{{color:#6b7280;font-size:.85rem}}
.summary-card h2{{font-size:1.25rem;color:#e2e8f0;margin-bottom:1rem;font-weight:700;letter-spacing:-0.01em}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.84rem}}
thead{{z-index:5}}
th{{background:#111318;color:#4b5563;text-transform:uppercase;font-size:.68rem;font-weight:600;
letter-spacing:.06em;padding:.7rem .6rem;text-align:left;white-space:nowrap;border-bottom:1px solid rgba(255,255,255,0.06)}}
td{{padding:.65rem .6rem;border-bottom:1px solid rgba(255,255,255,0.04);color:#e2e8f0}}
tr:hover td{{background:rgba(255,255,255,0.02)}}
.pos{{font-weight:800;color:#f59e0b;width:2rem;text-align:center;font-variant-numeric:tabular-nums}}
.horse-name{{font-weight:600;white-space:nowrap;color:#e2e8f0}}
.score{{font-size:1rem;font-variant-numeric:tabular-nums}}
.score strong{{color:#f59e0b}}
.bet{{color:#6b7280}}
.driver{{color:#6b7280;font-size:.8rem;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rec-badge{{padding:.25rem .7rem;border-radius:10px;font-size:.75rem;font-weight:700;white-space:nowrap}}
.value-badge{{background:#f59e0b;color:#0f1117;padding:.1rem .45rem;border-radius:8px;
font-size:.65rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
box-shadow:0 1px 4px rgba(245,166,35,0.3)}}
.value-picks{{background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.15);border-radius:10px;
padding:.6rem 1rem;margin-bottom:1rem;font-size:.85rem;color:#f59e0b}}
.factor-cell{{position:relative;width:55px;min-width:55px}}
.factor-bar{{position:absolute;left:0;top:0;bottom:0;background:#f59e0b;opacity:.12;border-radius:3px}}
.factor-val{{position:relative;z-index:1;font-size:.8rem;color:#6b7280}}
.race-link{{color:#f59e0b;text-decoration:none;font-weight:600}}
.race-link:hover{{color:#d4911e;text-decoration:underline}}

/* Expandable rows */
.horse-row.clickable{{cursor:pointer}}
.horse-row.clickable:hover td{{background:rgba(255,255,255,0.02);transition:background .15s}}
.toggle-icon{{display:inline-block;font-size:.65rem;transition:transform .2s;color:#4b5563;margin-right:.3rem}}
.horse-row.expanded .toggle-icon{{transform:rotate(90deg)}}
.detail-row{{background:#111318}}
.detail-row.hidden{{display:none}}
.detail-row td{{padding:0}}
.detail-content{{padding:.9rem 1.2rem 1.2rem 2.5rem;border-left:3px solid #f59e0b}}
.career-stats{{display:flex;gap:1.2rem;flex-wrap:wrap;margin-bottom:.6rem;font-size:.82rem}}
.career-item{{color:#6b7280}}
.career-item strong{{color:#e2e8f0}}
.starts-table{{width:100%;font-size:.8rem}}
.starts-table th{{background:#111318;font-size:.65rem;padding:.4rem .4rem}}
.starts-table td{{padding:.35rem .4rem;border-bottom:1px solid rgba(255,255,255,0.04)}}
.starts-table tr:hover td{{background:rgba(255,255,255,0.02)}}
.start-date{{color:#6b7280;white-space:nowrap;font-size:.75rem}}
.km-time{{font-family:monospace;font-weight:600}}
.plac-badge{{font-weight:700}}
.no-starts{{color:#4b5563;font-style:italic;font-size:.85rem}}

/* Trend */
.trend-cell{{white-space:nowrap;min-width:50px}}
.trend{{font-weight:600;font-size:.82rem}}
.trend-up{{color:#22c55e}}
.trend-down{{color:#ef4444}}
.trend.neutral{{color:#4b5563}}

/* Sparkline */
.sparkline{{width:48px;height:20px;vertical-align:middle;margin-left:.3rem}}

/* Spårtrappa badge */
.stair-badge{{background:rgba(124,58,237,0.15);color:#a78bfa;padding:.15rem .6rem;border-radius:10px;
font-size:.75rem;font-weight:600;margin-left:.5rem;vertical-align:middle;border:1px solid rgba(124,58,237,0.2)}}
.race-name-label{{color:#6b7280;font-size:.78rem;margin-top:.2rem;font-style:italic}}

/* Race classification */
.race-class-badge{{padding:.2rem .6rem;border-radius:8px;font-size:.72rem;font-weight:700;margin-left:.5rem}}
.race-class-badge.spiklopp{{background:rgba(34,197,94,0.12);color:#22c55e}}
.race-class-badge.oppet{{background:rgba(234,179,8,0.12);color:#eab308}}
.race-class-badge.skrallopp{{background:rgba(239,68,68,0.12);color:#ef4444}}

/* Time highlights */
.time-highlight{{color:#f59e0b;font-family:monospace}}
.time-estimate{{color:#d97706;font-family:monospace;font-style:italic}}

/* Time range box */
.time-range-box{{background:rgba(245,166,35,0.06);border-radius:8px;padding:.4rem .8rem;margin-top:.4rem;
display:inline-block;font-size:.82rem;border-left:3px solid #f59e0b}}
.time-range-box.estimated{{border-left-color:#d97706}}
.time-label{{color:#6b7280;margin-right:.3rem}}
.time-meta{{color:#4b5563;font-size:.72rem;font-style:italic;margin-left:.3rem}}

/* GT tag */
.gt-tag{{padding:.1rem .45rem;border-radius:8px;font-size:.7rem;font-weight:700;
letter-spacing:.04em;white-space:nowrap}}

/* Result column */
.result-cell{{text-align:center;min-width:45px}}
.result-plac{{font-weight:700;font-size:.95rem;font-variant-numeric:tabular-nums}}
.result-icon{{margin-left:.2rem;font-size:.75rem}}

/* Accuracy card */
.accuracy-card{{background:#151820;border-radius:16px;padding:1.8rem;
margin-bottom:1.5rem;border-left:4px solid #22c55e;border:1px solid rgba(255,255,255,0.06);
box-shadow:0 2px 8px rgba(0,0,0,0.3)}}
.accuracy-card h2{{color:#e2e8f0;margin-bottom:1rem;font-size:1.25rem;font-weight:700}}
.accuracy-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem}}
.accuracy-stat{{text-align:center;background:#111318;border-radius:12px;padding:1.2rem;border:1px solid rgba(255,255,255,0.06)}}
.accuracy-stat .big-num{{font-size:2rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}}
.accuracy-stat .label{{font-size:.72rem;text-transform:uppercase;color:#4b5563;
letter-spacing:.05em;margin-top:.3rem;font-weight:600}}
.accuracy-stat .sub{{font-size:.7rem;color:#4b5563;margin-top:.2rem}}
.accuracy-surprises{{margin-top:1rem;padding:.8rem;background:rgba(245,166,35,0.06);border-radius:10px;
font-size:.85rem;color:#f59e0b;border:1px solid rgba(245,166,35,0.12)}}

/* Hit/miss badges */
.hit-badge{{background:rgba(34,197,94,0.15);color:#22c55e;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(34,197,94,0.2)}}
.miss-badge{{background:rgba(239,68,68,0.15);color:#ef4444;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(239,68,68,0.2)}}
.partial-badge{{background:rgba(234,179,8,0.15);color:#eab308;padding:.2rem .5rem;border-radius:8px;
font-size:.75rem;font-weight:600;white-space:nowrap;border:1px solid rgba(234,179,8,0.2)}}

/* Skrällrisk */
.upset-badge{{padding:.25rem .7rem;border-radius:10px;font-size:.8rem;font-weight:700;white-space:nowrap}}
.upset-badge.high{{background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.2)}}
.upset-badge.medium{{background:rgba(234,179,8,0.15);color:#eab308;border:1px solid rgba(234,179,8,0.2)}}
.upset-badge.pulse{{animation:pulse-glow 2s ease-in-out infinite}}
@keyframes pulse-glow{{0%,100%{{box-shadow:0 0 0 0 rgba(239,68,68,0.4)}}50%{{box-shadow:0 0 12px 4px rgba(239,68,68,0.2)}}}}
.upset-low{{color:#22c55e;font-size:.8rem;font-weight:600}}

/* Race card risk borders */
.race-card.upset-high{{border-left:4px solid #ef4444;box-shadow:0 2px 8px rgba(0,0,0,0.3),0 0 12px rgba(239,68,68,0.08)}}
.race-card.upset-medium{{border-left:4px solid #eab308;box-shadow:0 2px 8px rgba(0,0,0,0.3),0 0 12px rgba(234,179,8,0.06)}}

/* Garderingsrekommendation */
.upset-advice{{font-size:.82rem;font-weight:600;margin-top:.4rem;padding:.3rem .6rem;border-radius:6px}}
.upset-advice.high{{color:#ef4444;background:rgba(239,68,68,0.1)}}
.upset-advice.medium{{color:#eab308;background:rgba(234,179,8,0.08)}}
.upset-advice.low{{color:#22c55e;background:rgba(34,197,94,0.08)}}

/* Skrällkandidater */
.upset-candidates{{background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.15);
border-radius:8px;padding:.5rem .8rem;margin-top:.4rem;font-size:.82rem;color:#f59e0b;font-weight:500}}
.upset-desc{{color:#4b5563;font-size:.75rem;font-weight:400}}

/* Heatmap risk bar */
.heatmap-wrapper{{background:#151820;border-radius:14px;border:1px solid rgba(255,255,255,0.06);
padding:1rem 1.5rem;margin-bottom:1.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.3)}}
.heatmap-title{{font-weight:700;color:#e2e8f0;font-size:.95rem;margin-bottom:.8rem}}
.heatmap-bar{{display:flex;gap:4px;height:48px;align-items:flex-end}}
.heatmap-seg{{flex:1;border-radius:6px 6px 0 0;min-height:8px;cursor:pointer;
transition:all .2s;position:relative}}
.heatmap-seg:hover{{opacity:.8;transform:scaleY(1.08)}}
.heatmap-labels{{display:flex;gap:4px;margin-top:6px}}
.heatmap-lbl{{flex:1;text-align:center;font-size:.65rem;color:#4b5563;font-weight:600}}
.heatmap-summary{{color:#6b7280;font-size:.82rem;margin-top:.5rem}}

/* Hero section (spelvärde) — grid layout */
.hero-grid{{display:grid;grid-template-columns:280px 1fr;gap:1.5rem;margin-bottom:1.5rem}}
.hero-score-card{{background:#151820;border-radius:16px;border:1px solid rgba(255,255,255,0.06);
padding:2rem;display:flex;flex-direction:column;align-items:center;justify-content:center;
box-shadow:0 2px 8px rgba(0,0,0,0.3)}}
.hero-score-label{{font-size:.7rem;text-transform:uppercase;color:#4b5563;letter-spacing:.06em;
font-weight:600;margin-top:.8rem}}
.hero-score-text{{font-size:1rem;font-weight:700;margin-top:.3rem}}
.hero-score-advice{{font-size:.82rem;color:#6b7280;margin-top:.2rem;text-align:center}}
.hero-right{{display:flex;flex-direction:column;gap:1rem}}
.hero-kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:.8rem}}
.hero-kpi{{text-align:center;border-radius:12px;padding:.9rem .5rem}}
.hero-kpi.blue{{background:rgba(96,165,250,0.08);border:1px solid rgba(96,165,250,0.15)}}
.hero-kpi.green{{background:rgba(74,222,128,0.08);border:1px solid rgba(74,222,128,0.15)}}
.hero-kpi.yellow{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.15)}}
.hero-kpi.purple{{background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.15)}}
.hero-kpi-val{{font-size:1.3rem;font-weight:800;font-family:'DM Mono',monospace;font-variant-numeric:tabular-nums}}
.hero-kpi.blue .hero-kpi-val{{color:#60a5fa}}
.hero-kpi.green .hero-kpi-val{{color:#4ade80}}
.hero-kpi.yellow .hero-kpi-val{{color:#fbbf24}}
.hero-kpi.purple .hero-kpi-val{{color:#a78bfa}}
.hero-kpi-lbl{{font-size:.62rem;text-transform:uppercase;color:#4b5563;letter-spacing:.05em;
font-weight:600;margin-top:.2rem}}

/* Division list (row-based overview) */
.division-list{{display:flex;flex-direction:column;gap:6px;margin-bottom:1.5rem}}
.division-row{{background:#151820;border-radius:10px;padding:14px 18px;cursor:pointer;display:grid;
grid-template-columns:36px 1fr 80px 100px 1fr 60px 28px;align-items:center;gap:14px;
border-left:3px solid transparent;transition:all .15s;border:1px solid rgba(255,255,255,0.06)}}
.division-row:hover{{background:#1a1f2b}}
.division-row .div-num-lg{{font-family:'DM Mono',monospace;font-size:18px;font-weight:500;color:#4b5563}}
.division-row .pick-info strong{{color:#e2e8f0;font-size:.88rem}}
.division-row .pick-info .driver-name{{color:#4b5563;font-size:.75rem;margin-top:2px}}
.division-row .score-val{{font-family:'DM Mono',monospace;font-size:1.05rem;font-weight:700;color:#f59e0b}}
.division-row .streck-bar{{flex:1;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden}}
.division-row .streck-bar div{{height:100%;background:#f59e0b;border-radius:2px;transition:width .5s ease}}
.division-row .gard-text{{font-size:.78rem;color:#6b7280}}
.division-row .risk-pct{{font-family:'DM Mono',monospace;font-size:.8rem;font-weight:600}}
.division-row .arrow-icon{{color:#4b5563;font-size:.9rem}}

/* Track record banner */
.track-banner{{background:#151820;border-radius:12px;border:1px solid rgba(255,255,255,0.06);
padding:1rem 1.5rem;margin-bottom:1.5rem;display:flex;align-items:center;justify-content:space-between;
flex-wrap:wrap;gap:1rem}}
.track-stat{{text-align:center;min-width:80px}}
.track-stat-val{{font-family:'DM Mono',monospace;font-size:1.1rem;font-weight:700;color:#4ade80}}
.track-stat-lbl{{font-size:.65rem;text-transform:uppercase;color:#4b5563;letter-spacing:.04em;font-weight:600}}

/* Legacy summary card (kept for compatibility) */
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem}}
.race-summary-card{{background:#151820;border:1px solid rgba(255,255,255,0.06);border-radius:14px;
padding:1.2rem;cursor:pointer;transition:all .25s;position:relative;overflow:hidden}}
.race-summary-card:hover{{border-color:#3a3d45;box-shadow:0 4px 16px rgba(0,0,0,0.4);transform:translateY(-2px)}}
.rsc-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.6rem}}
.rsc-num{{font-size:1.5rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}}
.rsc-risk{{width:8px;height:8px;border-radius:50%}}
.rsc-risk.high{{background:#ef4444;box-shadow:0 0 6px rgba(239,68,68,0.4)}}
.rsc-risk.medium{{background:#eab308;box-shadow:0 0 6px rgba(234,179,8,0.3)}}
.rsc-risk.low{{background:#22c55e}}
.rsc-pick{{margin-bottom:.5rem}}
.rsc-pick strong{{color:#e2e8f0;font-size:.9rem}}
.rsc-score-row{{display:flex;align-items:center;gap:.6rem;margin-bottom:.4rem}}
.rsc-score-num{{font-size:1.1rem;font-weight:800;color:#f59e0b;font-variant-numeric:tabular-nums}}
.streck-bar{{flex:1;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden}}
.streck-bar div{{height:100%;background:#f59e0b;border-radius:2px;transition:width .5s ease}}
.streck-pct{{font-size:.72rem;color:#4b5563;font-weight:600;min-width:32px;text-align:right}}
.rsc-gard{{font-size:.78rem;color:#4b5563;margin-bottom:.4rem}}
.rsc-result{{display:flex;align-items:center;gap:.5rem;margin-top:.4rem;padding-top:.4rem;
border-top:1px solid #2e3138;font-size:.82rem}}
.rsc-winner{{color:#6b7280}}

/* Spelvärde bar (kept for compatibility) */
.spelvarde-bar{{background:#151820;border-radius:14px;border:1px solid rgba(255,255,255,0.06);
padding:1rem 1.5rem;margin-bottom:1.5rem;display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap;
border-left:4px solid var(--sv-color,#f59e0b);box-shadow:0 2px 8px rgba(0,0,0,0.3)}}
.sv-score-box{{display:flex;align-items:center;gap:.8rem}}
.sv-score{{font-size:2.2rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}}
.sv-label{{font-size:.72rem;text-transform:uppercase;color:#4b5563;letter-spacing:.05em;font-weight:600}}
.sv-text{{font-size:1rem;font-weight:700}}
.sv-advice{{font-size:.88rem;margin-top:.2rem;color:#6b7280}}
.sv-meta{{display:flex;gap:1.2rem;margin-left:auto;flex-wrap:wrap}}
.sv-meta-item{{text-align:center;min-width:60px}}
.sv-meta-val{{display:block;font-size:1.1rem;font-weight:700;color:#e2e8f0;font-variant-numeric:tabular-nums}}
.sv-meta-lbl{{display:block;font-size:.65rem;text-transform:uppercase;color:#4b5563;letter-spacing:.04em;font-weight:600}}

/* System section */
.system-section h2{{margin-bottom:1rem;color:#e2e8f0}}
.system-card{{background:#151820;border-radius:14px;padding:1.2rem 1.4rem;margin-bottom:1rem;
border-left:4px solid #f59e0b;border:1px solid rgba(255,255,255,0.06)}}
.system-card.skipped{{border-left-color:#3a3d45;opacity:.5}}
.system-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem;flex-wrap:wrap;gap:.5rem}}
.system-header h3{{font-size:1rem;color:#e2e8f0;margin:0;font-weight:700}}
.system-meta{{color:#4b5563;font-size:.78rem;font-style:italic}}
.system-stats{{display:flex;gap:1.5rem;margin-bottom:.8rem;flex-wrap:wrap}}
.sys-stat{{text-align:center}}
.sys-val{{display:block;font-size:1.2rem;font-weight:700;color:#f59e0b;font-variant-numeric:tabular-nums}}
.sys-lbl{{display:block;font-size:.68rem;text-transform:uppercase;color:#4b5563;letter-spacing:.04em;font-weight:600}}
.system-table th{{font-size:.68rem;padding:.4rem .5rem}}
.system-table td{{padding:.4rem .5rem;font-size:.82rem}}
.system-picks strong{{color:#f59e0b}}
.conf-badge{{padding:.15rem .5rem;border-radius:10px;font-size:.75rem;font-weight:600}}
.conf-badge.high{{background:rgba(34,197,94,0.15);color:#22c55e}}
.conf-badge.medium{{background:rgba(234,179,8,0.12);color:#eab308}}
.conf-badge.low{{background:rgba(239,68,68,0.12);color:#ef4444}}
.skip-reason{{color:#4b5563;font-style:italic;font-size:.85rem}}
.spik-badge{{background:#f59e0b;color:#0f1117;padding:.1rem .45rem;border-radius:8px;
font-size:.65rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
box-shadow:0 1px 4px rgba(245,166,35,0.3)}}
.system-pill{{background:#f59e0b !important;color:#0f1117 !important;font-weight:700}}

/* LIVE backlog badges */
.live-badge{{background:#ef4444;color:white;padding:.1rem .4rem;border-radius:8px;
font-size:.62rem;font-weight:700;margin-left:.3rem;vertical-align:middle;
animation:pulse-live 2s ease-in-out infinite;box-shadow:0 0 12px rgba(239,68,68,0.5)}}
@keyframes pulse-live{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
.live-result-badge{{background:rgba(234,179,8,0.12);color:#eab308;padding:.15rem .5rem;
border-radius:8px;font-size:.75rem;font-weight:600;white-space:nowrap}}
.live-result-badge.live-allright{{background:rgba(34,197,94,0.12);color:#22c55e;
animation:pulse-live 2s ease-in-out infinite}}

/* Backlog section */
.backlog-section h2{{margin-bottom:1rem;color:#e2e8f0}}
.backlog-summary{{display:flex;gap:1.2rem;flex-wrap:wrap;margin-bottom:1rem;
background:#151820;border-radius:12px;padding:1.2rem;border:1px solid rgba(255,255,255,0.06)}}
.bl-stat{{text-align:center;min-width:80px}}
.bl-val{{display:block;font-size:1.3rem;font-weight:700;color:#f59e0b;font-variant-numeric:tabular-nums}}
.bl-lbl{{display:block;font-size:.68rem;text-transform:uppercase;color:#4b5563;letter-spacing:.04em;font-weight:600}}
.backlog-table th{{font-size:.68rem;padding:.4rem .5rem}}
.backlog-table td{{padding:.4rem .5rem;font-size:.82rem}}
.bl-expandable{{cursor:pointer}}
.bl-expandable:hover{{background:rgba(255,255,255,0.02)}}
.bl-expand-icon{{display:inline-block;transition:transform .2s;color:#4b5563}}
.bl-expand-icon.open{{transform:rotate(90deg)}}
.bl-detail-table{{width:100%;border-collapse:collapse;background:#111318}}
.bl-race-row td{{padding:.25rem .5rem;border-top:1px solid rgba(255,255,255,0.04);font-size:.78rem}}
.bl-detail-container td{{background:#111318}}

/* Strategi-filter knappar */
.strat-filter{{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}}
.strat-btn{{padding:.4rem 1rem;border-radius:10px;background:#151820;color:#6b7280;
border:1px solid rgba(255,255,255,0.06);cursor:pointer;font-size:.8rem;font-weight:600;transition:all .2s;font-family:inherit}}
.strat-btn:hover{{background:rgba(255,255,255,0.02);color:#e2e8f0}}
.strat-btn.active{{background:#f59e0b;color:#0f1117;border-color:#f59e0b}}
.strat-tag{{padding:.15rem .5rem;border-radius:10px;font-size:.72rem;font-weight:700;white-space:nowrap}}

/* Speltyp-filter knappar */
.gt-filter-row{{margin-bottom:.8rem}}
.gt-btn{{padding:.4rem 1rem;border-radius:10px;background:#151820;color:#6b7280;
border:1px solid rgba(255,255,255,0.06);cursor:pointer;font-size:.8rem;font-weight:600;transition:all .2s;font-family:inherit}}
.gt-btn:hover{{background:rgba(255,255,255,0.02);color:#e2e8f0}}
.gt-btn.active{{background:var(--gt-color,#f59e0b);color:#0f1117;border-color:var(--gt-color,#f59e0b)}}

/* Statistik-sektion */
.stats-section h2{{margin-bottom:1rem;color:#e2e8f0}}
.stats-block{{background:#151820;border-radius:14px;padding:1.2rem 1.4rem;margin-bottom:1rem;
border-left:4px solid #f59e0b;border:1px solid rgba(255,255,255,0.06)}}
.stats-block h3{{font-size:.95rem;color:#e2e8f0;margin-bottom:.8rem;font-weight:700}}
.stats-table{{width:100%;border-collapse:collapse;font-size:.82rem}}
.stats-table th{{background:#111318;color:#4b5563;text-transform:uppercase;font-size:.68rem;
letter-spacing:.05em;padding:.5rem .5rem;text-align:left;white-space:nowrap;font-weight:600}}
.stats-table td{{padding:.45rem .5rem;border-bottom:1px solid rgba(255,255,255,0.04)}}
.stats-table tr:hover td{{background:rgba(255,255,255,0.02)}}

/* AI-chatt */
.chat-section{{background:#151820;border-radius:16px;padding:1.8rem;margin-bottom:1.5rem;
border:1px solid rgba(255,255,255,0.06);box-shadow:0 2px 8px rgba(0,0,0,0.3)}}
.chat-header{{display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none}}
.chat-header h2{{font-size:1.25rem;color:#e2e8f0;margin:0;font-weight:700}}
.chat-toggle{{background:none;border:none;color:#f59e0b;font-size:1.5rem;cursor:pointer;transition:transform .2s}}
.chat-toggle.open{{transform:rotate(90deg)}}
.chat-body{{display:none;margin-top:1rem}}
.chat-body.open{{display:block}}
.chat-messages{{max-height:500px;overflow-y:auto;padding:.5rem 0;margin-bottom:1rem}}
.chat-msg{{padding:.7rem 1rem;border-radius:10px;margin-bottom:.5rem;max-width:90%;
line-height:1.6;font-size:.9rem;white-space:pre-wrap;word-wrap:break-word}}
.chat-msg.user{{background:rgba(245,166,35,0.08);color:#e2e8f0;margin-left:auto}}
.chat-msg.ai{{background:#111318;color:#e2e8f0;border:1px solid rgba(255,255,255,0.06)}}
.chat-msg.ai strong{{color:#f59e0b}}
.chat-input-row{{display:flex;gap:.5rem}}
.chat-input{{flex:1;padding:.7rem 1rem;border-radius:10px;border:1px solid rgba(255,255,255,0.06);
background:#111318;color:#e2e8f0;font-size:.9rem;outline:none;font-family:inherit;transition:border-color .2s,box-shadow .2s}}
.chat-input:focus{{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,166,35,0.15)}}
.chat-send{{padding:.7rem 1.2rem;border-radius:10px;border:none;background:#f59e0b;
color:#0f1117;font-weight:600;cursor:pointer;font-size:.9rem;white-space:nowrap;transition:all .2s}}
.chat-send:hover{{background:#d4911e;box-shadow:0 2px 8px rgba(245,166,35,0.3)}}
.chat-send:disabled{{opacity:.5;cursor:not-allowed}}
.chat-suggestions{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem}}
.chat-suggest-btn{{padding:.3rem .8rem;border-radius:10px;border:1px solid rgba(255,255,255,0.06);
background:transparent;color:#6b7280;font-size:.8rem;cursor:pointer;transition:all .2s}}
.chat-suggest-btn:hover{{border-color:#f59e0b;color:#f59e0b}}
.chat-loading{{display:inline-block;color:#6b7280;font-size:.85rem}}
.chat-loading::after{{content:'';animation:chatdots 1.5s steps(4,end) infinite}}
@keyframes chatdots{{0%{{content:''}}25%{{content:'.'}}50%{{content:'..'}}75%{{content:'...'}}}}

/* Backlog lazy-load */
.bl-older{{display:none}}
.bl-show-all .bl-older{{display:table-row}}

/* Premium gating */
.premium-locked{{position:relative;overflow:hidden}}
.premium-locked>*{{filter:blur(6px);pointer-events:none;user-select:none}}
.premium-locked::after{{content:'';position:absolute;inset:0;background:rgba(15,17,23,0.5);
backdrop-filter:blur(4px);z-index:10}}
.premium-cta{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:11;
text-align:center;padding:2rem;background:#151820;border:1px solid rgba(255,255,255,0.06);border-radius:16px;
box-shadow:0 8px 32px rgba(0,0,0,0.5)}}
.premium-cta h3{{color:#f59e0b;margin-bottom:.5rem;font-size:1.1rem}}
.premium-cta p{{color:#6b7280;font-size:.85rem;margin-bottom:1rem}}
.premium-cta-btn{{background:#f59e0b;color:#0f1117;border:none;padding:.6rem 1.5rem;
border-radius:10px;font-weight:700;cursor:pointer;font-size:.9rem}}

@media(max-width:768px){{
  .sidebar{{display:none}}
  .main-area{{margin-left:0 !important}}
  .top-bar{{padding:0 16px;flex-wrap:wrap;gap:.5rem}}
  .content{{padding:16px}}
  .system-drawer{{padding:1rem}}
  .round-select{{max-width:200px;font-size:.8rem}}
  .summary-card,.race-card{{padding:1.3rem;border-radius:14px}}
  .hero-grid{{grid-template-columns:1fr}}
  .hero-kpis{{grid-template-columns:repeat(2,1fr)}}
  .division-row{{grid-template-columns:28px 1fr 60px 80px 28px}}
  .division-row .gard-text,.division-row .risk-pct{{display:none}}
  table{{font-size:.75rem}}
  .driver{{display:none}}
  .detail-content{{padding-left:1rem}}
  .factor-cell{{display:none}}
  .chat-section{{margin-bottom:0}}
  .heatmap-wrapper{{overflow-x:auto}}
}}
</style>
</head>
<body>
<div class="app-layout">
{sidebar}
<div class="main-area" id="main-area">
<header class="top-bar">
  <div class="top-bar-left">
    {div_tabs}
    {dropdown}
  </div>
  <div class="top-bar-right">
    <span class="top-stat" style="font-family:'DM Mono',monospace;color:#4b5563;font-size:.8rem">
      {game_round.num_races} lopp · {_esc(game_round.track_name or '')}
    </span>
    {system_btn}
  </div>
</header>
<main class="content">
<section id="s-summary" class="dashboard-section active">
{risk_bar}
{sv_bar}
{accuracy}
{summary}
{chat_section}
</section>
{race_sections}
<section id="s-stats" class="dashboard-section">{'<div class="premium-locked">' + stats_section + _premium_cta + '</div>' if not premium and stats_section else stats_section}</section>
<section id="s-backlog" class="dashboard-section">{backlog_section}</section>
</main>
</div><!-- /main-area -->
<div class="system-drawer" id="system-drawer">
<button class="drawer-close" onclick="closeDrawer()">&times;</button>
{system_section}
</div>
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>
</div>
<script>
// ── Section switching ──
function showSection(id){{
  document.querySelectorAll('.dashboard-section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.div-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.div-tab').forEach(b=>b.classList.remove('active'));
  const target=document.getElementById('s-'+id);
  if(target) target.classList.add('active');
  const nav=document.querySelector('.nav-item[data-section="'+id+'"]');
  if(nav) nav.classList.add('active');
  document.querySelector('.content').scrollTop=0;
}}
// ── Division switching (syncs sidebar + top-bar tabs) ──
function showDivision(num){{
  showSection('race-'+num);
  document.querySelectorAll('.div-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.div-tab').forEach(b=>b.classList.remove('active'));
  const di=document.querySelector('.div-item[data-div="'+num+'"]');
  if(di) di.classList.add('active');
  const dt=document.querySelector('.div-tab[data-section="race-'+num+'"]');
  if(dt) dt.classList.add('active');
}}
// ── Sidebar toggle ──
function toggleSidebar(){{
  const sb=document.getElementById('sidebar');
  sb.classList.toggle('collapsed');
  document.getElementById('main-area').style.marginLeft=
    sb.classList.contains('collapsed')?'64px':'220px';
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
  window.location.href='/'+sel.value;
}}
// Toggle visa alla äldre entries i backlog
function toggleShowAll(){{
  const section=document.getElementById('backlog');
  const btn=document.getElementById('bl-show-all-btn');
  if(!section||!btn)return;
  if(!btn.dataset.origText) btn.dataset.origText=btn.textContent;
  section.classList.toggle('bl-show-all');
  btn.textContent=section.classList.contains('bl-show-all')?'Dölj äldre':btn.dataset.origText;
}}
// Toggle backlog detail rows
function toggleBlDetail(id){{
  const el=document.getElementById(id);
  if(!el)return;
  const isOpen=el.style.display!=='none';
  el.style.display=isOpen?'none':'table-row';
  // Toggle expand icon
  const prevRow=el.previousElementSibling;
  if(prevRow){{
    const icon=prevRow.querySelector('.bl-expand-icon');
    if(icon)icon.classList.toggle('open',!isOpen);
  }}
}}
// ── Dubbelfiltrering: strategi × speltyp ──
let activeStatsStrat='all', activeStatsGT='all';
let activeBacklogStrat='all', activeBacklogGT='all';

// Stats: strategi-filter
function filterStats(strat){{
  activeStatsStrat=strat;
  document.querySelectorAll('.stats-section .strat-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  applyStatsFilter();
}}
// Stats: speltyp-filter
function filterStatsGT(gt){{
  activeStatsGT=gt;
  document.querySelectorAll('.stats-section .gt-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  applyStatsFilter();
}}
function applyStatsFilter(){{
  document.querySelectorAll('.strat-block').forEach(block=>{{
    const matchS=activeStatsStrat==='all'||block.dataset.strat===activeStatsStrat;
    const matchG=activeStatsGT==='all'||!block.dataset.gt||block.dataset.gt===activeStatsGT;
    block.style.display=(matchS&&matchG)?'':'none';
  }});
  // Filtrera rader inom speltyp-tabeller
  document.querySelectorAll('.stats-table tr[data-gt]').forEach(row=>{{
    const matchG=activeStatsGT==='all'||row.dataset.gt===activeStatsGT;
    row.style.display=matchG?'':'none';
  }});
}}

// Backlog: strategi-filter
function filterBacklog(strat){{
  activeBacklogStrat=strat;
  document.querySelectorAll('.bl-filter .strat-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  applyBacklogFilter();
}}
// Backlog: speltyp-filter
function filterBacklogGT(gt){{
  activeBacklogGT=gt;
  document.querySelectorAll('.bl-gt-filter .gt-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  applyBacklogFilter();
}}
// Backlog: senaste N månader
function filterBacklogRecent(months){{
  const cutoff=new Date();
  cutoff.setMonth(cutoff.getMonth()-months);
  const cutoffStr=cutoff.toISOString().slice(0,10);
  activeBacklogGT='recent_'+months;
  document.querySelectorAll('.bl-gt-filter .gt-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
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
// ── AI-chatt ──
let chatMsgs=[];
function toggleChat(){{
  const body=document.getElementById('chat-body');
  const icon=document.getElementById('chat-toggle-icon');
  body.classList.toggle('open');
  icon.classList.toggle('open');
}}
function askSuggestion(text){{
  document.getElementById('chat-input').value=text;
  sendChat();
}}
document.addEventListener('DOMContentLoaded',()=>{{
  const ci=document.getElementById('chat-input');
  if(ci)ci.addEventListener('keydown',e=>{{
    if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();sendChat();}}
  }});
}});
async function sendChat(){{
  const input=document.getElementById('chat-input');
  const msg=input.value.trim();
  if(!msg)return;
  input.value='';
  chatMsgs.push({{role:'user',content:msg}});
  renderChat();
  const btn=document.getElementById('chat-send');
  btn.disabled=true;btn.textContent='\\u23F3';
  const loadDiv=document.createElement('div');
  loadDiv.className='chat-msg ai';
  loadDiv.id='chat-loading';
  loadDiv.innerHTML='<span class="chat-loading">Analyserar</span>';
  document.getElementById('chat-messages').appendChild(loadDiv);
  document.getElementById('chat-messages').scrollTop=999999;
  try{{
    const rk=window.location.pathname.replace(/^\\/+/,'');
    const resp=await fetch('/api/chat',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{messages:chatMsgs,round_key:rk}})
    }});
    const data=await resp.json();
    if(data.error){{
      chatMsgs.push({{role:'assistant',content:'Fel: '+data.error}});
    }}else{{
      chatMsgs.push({{role:'assistant',content:data.response}});
    }}
  }}catch(e){{
    chatMsgs.push({{role:'assistant',content:'Kunde inte nå AI-tjänsten: '+e.message}});
  }}
  btn.disabled=false;btn.textContent='Skicka';
  renderChat();
}}
function renderChat(){{
  const c=document.getElementById('chat-messages');
  c.innerHTML=chatMsgs.map(m=>{{
    const cls=m.role==='user'?'user':'ai';
    return '<div class="chat-msg '+cls+'">'+formatChatMsg(m.content)+'</div>';
  }}).join('');
  c.scrollTop=c.scrollHeight;
}}
function formatChatMsg(text){{
  let s=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s=s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/\*(.+?)\*/g,'<em>$1</em>');
  return s;
}}
// Toggle expandable detail rows
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
// Auto-refresh odds var 10 min för live-omgångar — injiceras i top-bar
(function(){{
  const isLive = {'true' if not game_round.is_finished else 'false'};
  if(!isLive) return;
  const roundKey = '{game_round.game_type}/{game_round.round_date}';
  const topRight = document.querySelector('.top-bar-right');
  if(!topRight) return;
  // Timer
  const timer = document.createElement('span');
  timer.style.cssText = 'font-family:"DM Mono",monospace;font-size:0.75rem;color:#4b5563';
  topRight.insertBefore(timer, topRight.firstChild);
  // Refresh button
  const refreshBtn = document.createElement('button');
  refreshBtn.textContent = '↻ Uppdatera odds';
  refreshBtn.className = 'system-btn';
  refreshBtn.style.cssText = 'padding:0.4rem 0.9rem;font-size:0.78rem';
  topRight.appendChild(refreshBtn);
  refreshBtn.addEventListener('click',()=>{{
    refreshBtn.textContent = '⏳ Uppdaterar...';
    fetch('/refresh/'+roundKey).then(()=>location.reload());
  }});
  // Countdown
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
background:#0c0e14;color:#e2e8f0;line-height:1.6;-webkit-font-smoothing:antialiased;
overflow-x:hidden}

/* Nav */
.landing-nav{position:fixed;top:0;left:0;right:0;padding:1rem 2rem;display:flex;
justify-content:space-between;align-items:center;z-index:50;
background:rgba(15,17,23,0.8);backdrop-filter:blur(12px);border-bottom:1px solid rgba(46,49,56,0.5)}
.landing-nav .logo{font-size:1.2rem;font-weight:800;letter-spacing:-0.02em}
.landing-nav .logo span{color:#f59e0b}
.nav-cta{background:#f59e0b;color:#0f1117;border:none;padding:.5rem 1.2rem;border-radius:10px;
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
.hero-cta{display:inline-flex;align-items:center;gap:.5rem;background:#f59e0b;color:#0f1117;
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
.feature-card{background:#151820;border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:2rem;
transition:all .3s}
.feature-card:hover{border-color:#3a3d45;transform:translateY(-2px);
box-shadow:0 8px 24px rgba(0,0,0,0.3)}
.feature-icon{font-size:1.8rem;margin-bottom:1rem}
.feature-card h3{font-size:1.05rem;font-weight:700;margin-bottom:.5rem;color:#e2e8f0}
.feature-card p{color:#6b7280;font-size:.88rem;line-height:1.6}

/* Social proof */
.proof{padding:4rem 2rem;text-align:center;background:#111318;border-top:1px solid #2e3138;
border-bottom:1px solid rgba(255,255,255,0.06)}
.proof h2{font-size:1.8rem;font-weight:800;margin-bottom:1rem}
.proof-stat{font-size:3rem;font-weight:900;color:#22c55e;margin-bottom:.5rem;
font-variant-numeric:tabular-nums}
.proof-label{color:#6b7280;font-size:1rem}

/* Pricing */
.pricing{padding:5rem 2rem;max-width:900px;margin:0 auto}
.pricing h2{text-align:center;font-size:2rem;font-weight:800;margin-bottom:3rem}
.price-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.5rem}
.price-card{background:#151820;border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:2.5rem;
text-align:center;position:relative}
.price-card.featured{border-color:#f59e0b;box-shadow:0 0 30px rgba(245,166,35,0.15)}
.price-card.featured::before{content:'Populärast';position:absolute;top:-12px;left:50%;
transform:translateX(-50%);background:#f59e0b;color:#0f1117;padding:.2rem 1rem;
border-radius:20px;font-size:.72rem;font-weight:700}
.price-name{font-size:1.1rem;font-weight:700;margin-bottom:.5rem}
.price-amount{font-size:2.5rem;font-weight:900;color:#f59e0b;margin-bottom:.3rem}
.price-amount span{font-size:.9rem;color:#6b7280;font-weight:500}
.price-desc{color:#6b7280;font-size:.85rem;margin-bottom:1.5rem}
.price-features{list-style:none;text-align:left;margin-bottom:2rem}
.price-features li{padding:.4rem 0;font-size:.88rem;color:#e2e8f0}
.price-features li::before{content:'\\2713';color:#22c55e;font-weight:700;margin-right:.5rem}
.price-btn{width:100%;padding:.7rem;border-radius:10px;border:none;font-weight:700;
font-size:.9rem;cursor:pointer;transition:all .25s}
.price-btn.primary{background:#f59e0b;color:#0f1117}
.price-btn.primary:hover{background:#d4911e}
.price-btn.secondary{background:rgba(255,255,255,0.02);color:#e2e8f0;border:1px solid rgba(255,255,255,0.06)}
.price-btn.secondary:hover{background:rgba(255,255,255,0.06)}

/* Footer */
.landing-footer{padding:2rem;text-align:center;color:#4b5563;font-size:.8rem;
border-top:1px solid #2e3138}

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

<footer class="landing-footer">
  &copy; 2026 Kungens Trav. AI-driven travanalys.
</footer>

</body>
</html>"""
