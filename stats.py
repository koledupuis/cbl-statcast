"""
Derived rate stats and percentile ranking helpers.

The CBL API gives us counting stats plus some precomputed rates. We add a
handful of extra rate stats (K%, BB%, hard-hit%, etc.) so the player page
can build Savant-style percentile bars, and we rank every player against
the full league pool for that category.
"""

BATTING_MIN_PA = 10          # minimum plate appearances to count toward percentile pool
PITCHING_MIN_OUTS = 9        # minimum outs recorded (~3 IP) to count toward percentile pool


def safe_div(n, d):
    return (n / d) if d else 0.0


def batting_rates(p):
    """Return a dict of rate stats for one batting stat-line dict from /stats/batting."""
    pa = p.get("plateAppearances") or 0
    ab = p.get("atBats") or 0
    ev = (p.get("precomputedEventAnalytics") or {}).get("batting", {})
    rates = ev.get("rates", {})
    batted = ev.get("battedBalls", {})
    by_type = batted.get("byType", {})
    bb_total = batted.get("total") or 0

    return {
        "avg": p.get("battingAvg", 0.0),
        "obp": p.get("obp", 0.0),
        "slg": p.get("slg", 0.0),
        "ops": p.get("ops", 0.0),
        "k_pct": safe_div(p.get("strikeouts", 0), pa),
        "bb_pct": safe_div(p.get("walks", 0), pa),
        "whiff_pct": rates.get("whiffRate", 0.0),
        "chase_ball_pct": rates.get("ballRate", 0.0),
        "gb_pct": safe_div(by_type.get("groundBall", 0), bb_total) if bb_total else rates.get("groundBallRate", 0.0),
        "pa": pa,
        "ab": ab,
    }


def pitching_rates(p):
    """Return a dict of rate stats for one pitching stat-line dict from /stats/pitching."""
    adv = p.get("advancedPitching", {})
    outs = adv.get("inningsPitchedOuts") or adv.get("innings_pitched_outs") or 0
    bf = adv.get("battersFaced") or adv.get("batters_faced") or 0
    pitch_count = adv.get("pitchCount") or adv.get("pitch_count") or 0
    strikes_swinging = adv.get("strikesSwinging") or adv.get("strikes_swinging") or 0
    ground_outs = adv.get("groundOuts") or adv.get("ground_outs") or 0
    air_outs = adv.get("airOuts") or adv.get("air_outs") or 0

    return {
        "era": p.get("era", 0.0),
        "whip": p.get("whip", 0.0),
        "k_pct": safe_div(p.get("strikeoutsPitching", 0), bf),
        "bb_pct": safe_div(p.get("walksAllowed", 0), bf),
        "whiff_pct": safe_div(strikes_swinging, pitch_count),
        "gb_pct": safe_div(ground_outs, (ground_outs + air_outs)) if (ground_outs + air_outs) else 0.0,
        "outs": outs,
        "bf": bf,
    }


def percentile(value, pool, invert=False):
    """
    Percentile rank of `value` within `pool` (a list of numbers), 0-100.
    invert=True means lower is better (ERA, WHIP, K%-allowed, whiff%-allowed
    from a pitcher's perspective is actually "higher is better" for the
    pitcher, so invert only applies to ERA/WHIP/BB% here).
    """
    pool = [x for x in pool if x is not None]
    if not pool:
        return 50
    below = sum(1 for x in pool if x < value)
    equal = sum(1 for x in pool if x == value)
    rank = below + 0.5 * equal
    pct = 100 * rank / len(pool)
    return round(100 - pct) if invert else round(pct)


def build_batting_percentiles(player_row, all_batting_rows):
    qualified = [batting_rates(r) for r in all_batting_rows if (r.get("plateAppearances") or 0) >= BATTING_MIN_PA]
    my = batting_rates(player_row)

    def pool(key):
        return [q[key] for q in qualified]

    return {
        "avg": (my["avg"], percentile(my["avg"], pool("avg"))),
        "obp": (my["obp"], percentile(my["obp"], pool("obp"))),
        "slg": (my["slg"], percentile(my["slg"], pool("slg"))),
        "ops": (my["ops"], percentile(my["ops"], pool("ops"))),
        "bb_pct": (my["bb_pct"], percentile(my["bb_pct"], pool("bb_pct"))),
        "k_pct": (my["k_pct"], percentile(my["k_pct"], pool("k_pct"), invert=True)),
        "whiff_pct": (my["whiff_pct"], percentile(my["whiff_pct"], pool("whiff_pct"), invert=True)),
        "gb_pct": (my["gb_pct"], percentile(my["gb_pct"], pool("gb_pct"), invert=True)),
    }


def build_pitching_percentiles(player_row, all_pitching_rows):
    qualified = [pitching_rates(r) for r in all_pitching_rows
                 if (r.get("advancedPitching", {}).get("inningsPitchedOuts") or 0) >= PITCHING_MIN_OUTS]
    my = pitching_rates(player_row)

    def pool(key):
        return [q[key] for q in qualified]

    return {
        "era": (my["era"], percentile(my["era"], pool("era"), invert=True)),
        "whip": (my["whip"], percentile(my["whip"], pool("whip"), invert=True)),
        "k_pct": (my["k_pct"], percentile(my["k_pct"], pool("k_pct"))),
        "bb_pct": (my["bb_pct"], percentile(my["bb_pct"], pool("bb_pct"), invert=True)),
        "whiff_pct": (my["whiff_pct"], percentile(my["whiff_pct"], pool("whiff_pct"))),
        "gb_pct": (my["gb_pct"], percentile(my["gb_pct"], pool("gb_pct"))),
    }


def fmt3(x):
    """Format a rate like .300 the way baseball stat lines conventionally do."""
    if x is None:
        return "---"
    s = f"{x:.3f}"
    return s[1:] if s.startswith("0.") else (("-" + s[2:]) if s.startswith("-0.") else s)


def fmt_pct(x):
    return f"{x * 100:.1f}%" if x is not None else "---"


def fmt1(x):
    return f"{x:.1f}" if x is not None else "---"


def fmt2(x):
    return f"{x:.2f}" if x is not None else "---"
