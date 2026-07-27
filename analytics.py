"""
Advanced sabermetric stats layered on top of the season stat lines from
cbl_api.get_batting() / get_pitching(), plus team-level aggregates.

Scope note: everything here is computed strictly from data the CBL feed
actually provides. A lot of "advanced analytics" wishlists (clutch/late-
and-close splits, pinch-hit detection, leverage index, defensive range
factor, stolen bases) need play-level context this feed doesn't carry --
see gameday.py's docstring and the README's "What's not included" section.
Rather than invent numbers for those, this module sticks to what's real
and documents every approximation it does make:

  - BABIP / Secondary Average / Runs Created formulas that normally
    subtract sacrifice flies can't here -- the batting stat line doesn't
    break out SF, so AB is used as-is. This slightly understates BABIP
    and SecA for high-SF hitters.
  - Secondary Average's stolen-base term is dropped entirely: the
    outcome vocabulary has no SB/CS event today (see gameday.py).
  - wOBA uses a standard published linear-weights set (not fit to this
    league specifically -- that would need CBL-specific run-expectancy
    data this feed doesn't provide).
  - OPS+ / ERA+ are scaled to *this league's own* season averages for
    qualified players, not any external league-average or park-adjusted
    baseline (no park factors exist for a summer league schedule).
  - FIP's constant is derived from this league's own season totals (so
    league-average FIP lines up with league-average ERA) rather than
    an imported MLB constant.
  - xFIP substitutes a league-average HR/(fly ball) rate for the
    pitcher's actual HR rate. "Fly balls" here means air outs + home
    runs allowed (outs-based proxy from advancedPitching), since batted-
    ball-type counts aren't in the season pitching stat line the way
    they are for batting.
  - "Batting Runs Above Average" and "Estimated wRC" are rough linear-
    weights estimates, explicitly labeled "estimated" wherever shown.
"""

import team_schedule

WOBA_WEIGHTS = {
    "bb": 0.690,
    "hbp": 0.722,
    "single": 0.888,
    "double": 1.271,
    "triple": 1.616,
    "hr": 2.101,
}
WOBA_SCALE = 1.15  # rough runs-per-woba-point-above-average conversion

BATTING_MIN_PA = 10
PITCHING_MIN_OUTS = 9


def safe_div(n, d):
    return (n / d) if d else None


def _singles(row):
    h = row.get("hits") or 0
    return h - (row.get("doubles") or 0) - (row.get("triples") or 0) - (row.get("homeRuns") or 0)


def _total_bases(row):
    singles = _singles(row)
    return singles + 2 * (row.get("doubles") or 0) + 3 * (row.get("triples") or 0) + 4 * (row.get("homeRuns") or 0)


# ---------------------------------------------------------------- batting --

def batting_advanced(row):
    """Advanced batting stats for one /stats/batting row."""
    ab = row.get("atBats") or 0
    h = row.get("hits") or 0
    bb = row.get("walks") or 0
    so = row.get("strikeouts") or 0
    pa = row.get("plateAppearances") or 0
    doubles = row.get("doubles") or 0
    triples = row.get("triples") or 0
    hr = row.get("homeRuns") or 0
    singles = _singles(row)
    tb = _total_bases(row)
    games = row.get("games") or 0

    avg = row.get("battingAvg") or 0.0
    obp = row.get("obp") or 0.0
    slg = row.get("slg") or 0.0
    ops = row.get("ops") if row.get("ops") is not None else (obp + slg)

    iso = slg - avg

    babip_denom = ab - so - hr
    babip = safe_div(h - hr, babip_denom) if babip_denom > 0 else None

    woba_num = (WOBA_WEIGHTS["bb"] * bb + WOBA_WEIGHTS["single"] * singles +
                WOBA_WEIGHTS["double"] * doubles + WOBA_WEIGHTS["triple"] * triples +
                WOBA_WEIGHTS["hr"] * hr)
    woba = safe_div(woba_num, pa)

    rc = safe_div((h + bb) * tb, (ab + bb))
    rc_per_game = safe_div(rc, games) if rc is not None else None

    seca = safe_div((tb - h) + bb, ab)  # SB term intentionally omitted -- see module docstring
    xbh = doubles + triples + hr
    xbh_pct = safe_div(xbh, pa)
    extra_bases_per_hit = safe_div(tb - h, h)

    k_pct = safe_div(so, pa)
    bb_pct = safe_div(bb, pa)
    k_minus_bb_pct = (k_pct - bb_pct) if (k_pct is not None and bb_pct is not None) else None
    bb_so_ratio = safe_div(bb, so)
    pa_per_so = safe_div(pa, so)
    pa_per_bb = safe_div(pa, bb)

    return {
        "ops": ops, "obp": obp, "slg": slg, "avg": avg, "iso": iso,
        "babip": babip, "woba": woba, "rc": rc, "rc_per_game": rc_per_game,
        "seca": seca, "tb": tb, "xbh": xbh, "xbh_pct": xbh_pct,
        "extra_bases_per_hit": extra_bases_per_hit,
        "pa": pa, "ab": ab, "bb": bb, "so": so, "hr": hr, "games": games,
        "k_pct": k_pct, "bb_pct": bb_pct, "k_minus_bb_pct": k_minus_bb_pct,
        "bb_so_ratio": bb_so_ratio, "pa_per_so": pa_per_so, "pa_per_bb": pa_per_bb,
    }


def league_batting_context(rows, min_pa=BATTING_MIN_PA):
    """League totals/averages across qualified hitters -- feeds OPS+, est. wRC, etc."""
    qualified = [r for r in rows if (r.get("plateAppearances") or 0) >= min_pa]
    tot_pa = sum(r.get("plateAppearances") or 0 for r in qualified)
    tot_ab = sum(r.get("atBats") or 0 for r in qualified)
    tot_runs = sum(r.get("runs") or 0 for r in qualified)

    if not qualified or not tot_pa:
        return {"lg_obp": 0.0, "lg_slg": 0.0, "lg_woba": 0.0, "lg_runs_per_pa": 0.0, "qualified_count": 0}

    obp_num = sum((r.get("obp") or 0.0) * (r.get("plateAppearances") or 0) for r in qualified)
    slg_num = sum((r.get("slg") or 0.0) * (r.get("atBats") or 0) for r in qualified)
    woba_num = sum((batting_advanced(r)["woba"] or 0.0) * (r.get("plateAppearances") or 0) for r in qualified)

    return {
        "lg_obp": safe_div(obp_num, tot_pa) or 0.0,
        "lg_slg": safe_div(slg_num, tot_ab) or 0.0,
        "lg_woba": safe_div(woba_num, tot_pa) or 0.0,
        "lg_runs_per_pa": safe_div(tot_runs, tot_pa) or 0.0,
        "qualified_count": len(qualified),
    }


def batting_plus_stats(adv, league_ctx):
    """OPS+, estimated wRC, and estimated Batting Runs Above Average for one player."""
    lg_obp = league_ctx.get("lg_obp") or 0
    lg_slg = league_ctx.get("lg_slg") or 0
    ops_plus = None
    if lg_obp and lg_slg:
        ops_plus = round(100 * ((adv["obp"] / lg_obp) + (adv["slg"] / lg_slg) - 1))

    lg_woba = league_ctx.get("lg_woba") or 0
    bat_runs_above_avg = None
    est_wrc = None
    if adv["woba"] is not None and lg_woba and adv["pa"]:
        bat_runs_above_avg = round(((adv["woba"] - lg_woba) / WOBA_SCALE) * adv["pa"], 1)
        lg_rpa = league_ctx.get("lg_runs_per_pa") or 0
        est_wrc = round(bat_runs_above_avg + lg_rpa * adv["pa"], 1)

    return {"ops_plus": ops_plus, "batting_runs_above_avg": bat_runs_above_avg, "est_wrc": est_wrc}


# --------------------------------------------------------------- pitching --

def _parse_ip(ip_str):
    """Fallback: parse a printed '12.1' innings-pitched string into a float."""
    try:
        whole, frac = str(ip_str).split(".")
        return int(whole) + int(frac) / 3
    except (ValueError, AttributeError):
        try:
            return float(ip_str)
        except (TypeError, ValueError):
            return 0.0


def pitching_advanced(row, league_ctx=None):
    """Advanced pitching stats for one /stats/pitching row."""
    adv = row.get("advancedPitching") or {}
    outs = adv.get("inningsPitchedOuts") or adv.get("innings_pitched_outs") or 0
    ip = (outs / 3) if outs else _parse_ip(row.get("inningsPitched"))

    h = row.get("hitsAllowed") or 0
    bb = row.get("walksAllowed") or 0
    so = row.get("strikeoutsPitching") or 0
    hr = adv.get("homeRunsAllowed") or row.get("homeRunsAllowed") or 0
    bf = adv.get("battersFaced") or adv.get("batters_faced") or 0
    ground_outs = adv.get("groundOuts") or adv.get("ground_outs") or 0
    air_outs = adv.get("airOuts") or adv.get("air_outs") or 0
    batted_outs = ground_outs + air_outs

    era = row.get("era") or 0.0
    whip = row.get("whip") if row.get("whip") is not None else safe_div(h + bb, ip)

    k9 = safe_div(so * 9, ip)
    bb9 = safe_div(bb * 9, ip)
    hr9 = safe_div(hr * 9, ip)
    k_pct = safe_div(so, bf)
    bb_pct = safe_div(bb, bf)
    k_minus_bb_pct = (k_pct - bb_pct) if (k_pct is not None and bb_pct is not None) else None

    gb_pct = safe_div(ground_outs, batted_outs)
    fb_pct = safe_div(air_outs, batted_outs)
    gb_fb_ratio = safe_div(ground_outs, air_outs)

    babip_denom = bf - so - bb - hr
    babip_against = safe_div(h - hr, babip_denom) if babip_denom and babip_denom > 0 else None

    fip_constant = (league_ctx or {}).get("fip_constant", 3.10)
    fip = (safe_div(13 * hr + 3 * bb - 2 * so, ip) + fip_constant) if ip else None

    lg_hr_per_fb = (league_ctx or {}).get("lg_hr_per_fb")
    xfip = None
    if ip and lg_hr_per_fb is not None:
        flyballs = air_outs + hr  # outs-based proxy, see module docstring
        x_hr = flyballs * lg_hr_per_fb
        xfip = safe_div(13 * x_hr + 3 * bb - 2 * so, ip) + fip_constant

    return {
        "era": era, "whip": whip, "ip": round(ip, 2), "bf": bf,
        "k9": k9, "bb9": bb9, "hr9": hr9,
        "k_pct": k_pct, "bb_pct": bb_pct, "k_minus_bb_pct": k_minus_bb_pct,
        "gb_pct": gb_pct, "fb_pct": fb_pct, "gb_fb_ratio": gb_fb_ratio,
        "babip_against": babip_against, "fip": fip, "xfip": xfip,
        "h": h, "bb": bb, "so": so, "hr": hr,
    }


def league_pitching_context(rows, min_outs=PITCHING_MIN_OUTS):
    """League totals/averages across qualified pitchers -- feeds ERA+, FIP constant, xFIP."""
    qualified = []
    for r in rows:
        adv = r.get("advancedPitching") or {}
        outs = adv.get("inningsPitchedOuts") or adv.get("innings_pitched_outs") or 0
        if outs >= min_outs:
            qualified.append(r)

    tot_ip = 0.0
    tot_era_ip = 0.0
    tot_hr = tot_bb = tot_so = tot_air_outs = 0
    for r in qualified:
        adv = r.get("advancedPitching") or {}
        outs = adv.get("inningsPitchedOuts") or adv.get("innings_pitched_outs") or 0
        ip = outs / 3 if outs else _parse_ip(r.get("inningsPitched"))
        tot_ip += ip
        tot_era_ip += (r.get("era") or 0.0) * ip
        tot_hr += adv.get("homeRunsAllowed") or r.get("homeRunsAllowed") or 0
        tot_bb += r.get("walksAllowed") or 0
        tot_so += r.get("strikeoutsPitching") or 0
        tot_air_outs += adv.get("airOuts") or adv.get("air_outs") or 0

    if not qualified or not tot_ip:
        return {"lg_era": 0.0, "fip_constant": 3.10, "lg_hr_per_fb": None, "qualified_count": 0}

    lg_era = safe_div(tot_era_ip, tot_ip) or 0.0
    raw_fip = safe_div(13 * tot_hr + 3 * tot_bb - 2 * tot_so, tot_ip) or 0.0
    fip_constant = lg_era - raw_fip
    lg_flyballs = tot_air_outs + tot_hr
    lg_hr_per_fb = safe_div(tot_hr, lg_flyballs)

    return {
        "lg_era": lg_era, "fip_constant": fip_constant,
        "lg_hr_per_fb": lg_hr_per_fb, "qualified_count": len(qualified),
    }


def pitching_plus_stats(adv, league_ctx):
    """ERA+ for one pitcher (100 = league average, higher is better)."""
    lg_era = league_ctx.get("lg_era") or 0
    era_plus = None
    if lg_era and adv["era"]:
        era_plus = round(100 * (lg_era / adv["era"]))
    return {"era_plus": era_plus}


# -------------------------------------------------------------------- team --

def _sum_field(rows, *names):
    total = 0
    for r in rows:
        for n in names:
            if n in r:
                total += r.get(n) or 0
                break
    return total


def team_batting_stats(all_batting_rows, team_games=None):
    """team_name -> aggregated advanced batting dict, built by summing every
    player's counting stats for that team and re-deriving rates on the total.

    team_games: optional {team_name: completed_game_count} from
    team_schedule.count_completed_games_by_team() -- the correct source
    for how many games a TEAM has played. Without it, this falls back to
    max(individual player's own games count), which is NOT the same
    thing and badly understates team games played (no single player
    appears in every team game), inflating runs_per_game. Pass
    team_games whenever it's cheaply available; the fallback exists only
    so this function doesn't hard-require a schedule fetch."""
    by_team = {}
    for r in all_batting_rows:
        by_team.setdefault(r.get("teamName") or "Unknown", []).append(r)

    out = {}
    for team, rows in by_team.items():
        ab = _sum_field(rows, "atBats")
        h = _sum_field(rows, "hits")
        bb = _sum_field(rows, "walks")
        so = _sum_field(rows, "strikeouts")
        pa = _sum_field(rows, "plateAppearances")
        doubles = _sum_field(rows, "doubles")
        triples = _sum_field(rows, "triples")
        hr = _sum_field(rows, "homeRuns")
        runs = _sum_field(rows, "runs")
        games = team_schedule.games_for_team(team_games, team)
        if games is None:
            games = max((r.get("games") or 0) for r in rows) if rows else 0
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr

        avg = safe_div(h, ab) or 0.0
        obp = safe_div(h + bb, pa) or 0.0
        slg = safe_div(tb, ab) or 0.0
        ops = obp + slg
        iso = slg - avg
        babip_denom = ab - so - hr
        babip = safe_div(h - hr, babip_denom) if babip_denom > 0 else None
        woba_num = (WOBA_WEIGHTS["bb"] * bb + WOBA_WEIGHTS["single"] * singles +
                    WOBA_WEIGHTS["double"] * doubles + WOBA_WEIGHTS["triple"] * triples +
                    WOBA_WEIGHTS["hr"] * hr)
        woba = safe_div(woba_num, pa)
        runs_per_game = safe_div(runs, games)

        out[team] = {
            "team": team, "players": len(rows), "games": games,
            "pa": pa, "ab": ab, "runs": runs,
            "avg": avg, "obp": obp, "slg": slg, "ops": ops, "iso": iso,
            "babip": babip, "woba": woba, "runs_per_game": runs_per_game,
        }
    return out


def team_pitching_stats(all_pitching_rows, league_ctx=None, team_games=None):
    """team_name -> aggregated advanced pitching dict.

    team_games: same as team_batting_stats -- optional
    {team_name: completed_game_count} from
    team_schedule.count_completed_games_by_team(). Without it, this
    falls back to max(individual pitcher's own games count), which
    understates team games played (a reliever might appear in far fewer
    games than the team actually played) and badly inflates
    runs_allowed_per_game -- this is exactly what was producing wildly
    wrong RA/G numbers before.

    runs_allowed_per_game uses TOTAL runs allowed (earned + unearned),
    tried under a few plausible field names since the exact one on the
    season stats row hasn't been independently confirmed -- CBL's own
    analytics feed does expose earnedRuns and a separate total-runs
    field side by side (confirmed: official.pitching.runsAllowed vs
    earnedRuns in a real payload), so a team with any unearned runs
    (fielding errors, etc.) has runsAllowed > earnedRuns. Using
    earnedRuns here instead would silently produce a lower RA/G than
    CBL's own site shows, by exactly the team's unearned-run total for
    the season -- which is what was happening before this fix. ERA
    itself is correctly left on earnedRuns; only RA/G changes."""
    by_team = {}
    for r in all_pitching_rows:
        by_team.setdefault(r.get("teamName") or "Unknown", []).append(r)

    out = {}
    for team, rows in by_team.items():
        h = _sum_field(rows, "hitsAllowed")
        bb = _sum_field(rows, "walksAllowed")
        so = _sum_field(rows, "strikeoutsPitching")
        er = _sum_field(rows, "earnedRuns")
        ra = _sum_field(rows, "runsAllowed", "runs_allowed", "runs") or er
        hr = sum((r.get("advancedPitching") or {}).get("homeRunsAllowed") or r.get("homeRunsAllowed") or 0 for r in rows)
        ground_outs = sum((r.get("advancedPitching") or {}).get("groundOuts") or (r.get("advancedPitching") or {}).get("ground_outs") or 0 for r in rows)
        air_outs = sum((r.get("advancedPitching") or {}).get("airOuts") or (r.get("advancedPitching") or {}).get("air_outs") or 0 for r in rows)
        games = team_schedule.games_for_team(team_games, team)
        if games is None:
            games = max((r.get("games") or 0) for r in rows) if rows else 0

        ip = 0.0
        for r in rows:
            adv = r.get("advancedPitching") or {}
            outs = adv.get("inningsPitchedOuts") or adv.get("innings_pitched_outs") or 0
            ip += (outs / 3) if outs else _parse_ip(r.get("inningsPitched"))

        era = safe_div(er * 9, ip) or 0.0
        whip = safe_div(h + bb, ip)
        runs_allowed_per_game = safe_div(ra, games)

        fip_constant = (league_ctx or {}).get("fip_constant", 3.10) if league_ctx else 3.10
        fip = (safe_div(13 * hr + 3 * bb - 2 * so, ip) + fip_constant) if ip else None
        batted_outs = ground_outs + air_outs

        out[team] = {
            "team": team, "players": len(rows), "games": games, "ip": round(ip, 2),
            "era": era, "whip": whip, "fip": fip,
            "gb_pct": safe_div(ground_outs, batted_outs),
            "fb_pct": safe_div(air_outs, batted_outs),
            "earned_runs": er, "runs_allowed": ra,
            "runs_allowed_per_game": runs_allowed_per_game,
        }
    return out
