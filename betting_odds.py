"""
Modeled win-probability estimates for CBL's own scheduled games --
NOT real betting odds. No market data, no vig, no line movement, no
injury reports or weather, and this app isn't accepting any wagers.
A curiosity feature only, sitting behind an unlisted page rather than
anything linked from the main site.

Given how short this league's season is (48 games), these are
noisier, rougher estimates than anything from a full pro season --
early in the year especially, a team's Pythagorean win% is built on a
handful of games and can swing hard from one series to the next.

MODEL: the Log5 formula (Bill James), the standard sabermetric method
for estimating head-to-head win probability from each team's own
overall strength:

  P(A beats B) = (PA - PA*PB) / (PA + PB - 2*PA*PB)

PA/PB are each team's PYTHAGOREAN win% (not raw W-L record) --
already computed per team by team_schedule.build_team_season_record,
and the standard choice for this kind of model since it's less noisy
over a short season than raw record, being driven by full-season run
differential rather than how many one-run games happened to break a
team's way.

Adjusted by a home-field factor derived from THIS LEAGUE's own actual
home/away split (see league_home_field_win_pct) rather than an
assumed MLB-style ~54%, which has no particular reason to hold for a
short amateur summer season with different travel, ballparks, and
scheduling than affiliated pro ball.
"""
import math
import statistics

import cbl_api
import gamelog
import team_schedule

MIN_GAMES_FOR_HOME_FIELD_CALC = 20  # league-wide completed games needed before trusting a derived home-field split
FALLBACK_HOME_WIN_PCT = 0.54  # commonly-cited MLB long-run average; only used until the league has enough games of its own


def league_home_field_win_pct(team_names, season_year=None):
    """League-wide home-team win % across every completed game on
    record, derived from the same game_results every team's own
    season-record walk already produces (each entry already carries
    is_home). Each game is shared by two teams' own game_results lists
    (the home team's and the away team's), so entries are deduped by
    public_game_id -- taking either team's entry for a given game is
    fine, since "home team won" is determined by cross-referencing
    is_home against result, not by which side's list the entry came
    from (an away team's own "L" means the home team won that game
    just as validly as a home team's own "W" does).

    Falls back to FALLBACK_HOME_WIN_PCT if there aren't yet enough
    league-wide completed games to trust a derived number, or if the
    walk fails for any reason -- the return value clearly flags which
    case a caller got via "is_derived"."""
    home_wins = 0
    total = 0
    seen_game_ids = set()
    for team_name in team_names:
        try:
            record = team_schedule.build_team_season_record(team_name, season_year)
        except Exception:
            continue
        for g in (record or {}).get("game_results", []):
            gid = g.get("public_game_id")
            if gid:
                if gid in seen_game_ids:
                    continue
                seen_game_ids.add(gid)
            total += 1
            home_won = (g.get("is_home") and g.get("result") == "W") or \
                       (not g.get("is_home") and g.get("result") == "L")
            if home_won:
                home_wins += 1

    if total < MIN_GAMES_FOR_HOME_FIELD_CALC:
        return {"win_pct": FALLBACK_HOME_WIN_PCT, "games": total, "is_derived": False}
    return {"win_pct": home_wins / total, "games": total, "is_derived": True}


MIN_GAMES_FOR_RUN_STATS = 20  # league-wide completed games needed before trusting empirical run-differential/total spread
FALLBACK_DIFF_STDEV = 4.5  # rough MLB-scale game-to-game run-differential std dev, used only as a fallback
FALLBACK_TOTAL_MEAN = 9.0
FALLBACK_TOTAL_STDEV = 4.5


def league_run_stats(team_names, season_year=None):
    """Empirical mean and standard deviation of per-game run
    differential (home minus away) and per-game combined TOTAL runs,
    computed directly from every completed game on record -- the
    spread used for the run-line and total-runs normal-approximation
    estimates below, derived from this league's own actual results
    rather than an assumed number.

    Same sharing/dedup situation as league_home_field_win_pct: each
    completed game appears in two teams' own game_results lists.
    Total runs and |differential| are identical from either team's
    entry, but the SIGN of differential must be normalized to "home
    minus away" regardless of which team's entry got captured (an
    away team's own team_runs/opp_runs are already correct from ITS
    perspective, so the sign just needs flipping when the captured
    entry belongs to the away side).

    Falls back to rough MLB-scale constants if there aren't yet
    enough league-wide completed games to trust an empirical number --
    flagged via "is_derived" same as league_home_field_win_pct."""
    diffs = []
    totals = []
    seen_game_ids = set()
    for team_name in team_names:
        try:
            record = team_schedule.build_team_season_record(team_name, season_year)
        except Exception:
            continue
        for g in (record or {}).get("game_results", []):
            gid = g.get("public_game_id")
            if gid:
                if gid in seen_game_ids:
                    continue
                seen_game_ids.add(gid)
            team_runs, opp_runs = g.get("team_runs"), g.get("opp_runs")
            if team_runs is None or opp_runs is None:
                continue
            totals.append(team_runs + opp_runs)
            diffs.append((team_runs - opp_runs) if g.get("is_home") else (opp_runs - team_runs))

    if len(diffs) < MIN_GAMES_FOR_RUN_STATS:
        return {
            "diff_mean": 0.0, "diff_stdev": FALLBACK_DIFF_STDEV,
            "total_mean": FALLBACK_TOTAL_MEAN, "total_stdev": FALLBACK_TOTAL_STDEV,
            "games": len(diffs), "is_derived": False,
        }
    return {
        "diff_mean": statistics.mean(diffs), "diff_stdev": statistics.pstdev(diffs) or FALLBACK_DIFF_STDEV,
        "total_mean": statistics.mean(totals), "total_stdev": statistics.pstdev(totals) or FALLBACK_TOTAL_STDEV,
        "games": len(diffs), "is_derived": True,
    }


def _normal_cdf(x, mean, stdev):
    """Standard normal CDF via math.erf -- no scipy dependency needed
    for just this. Degenerates safely to a step function if stdev is
    somehow 0 (shouldn't happen given the fallbacks above, but never
    divide by zero on a real request regardless)."""
    if stdev <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1 + math.erf((x - mean) / (stdev * math.sqrt(2))))


def round_to_half(x):
    """Rounds to the nearest 0.5 -- standard sportsbook convention for
    a spread/total line, and deliberately avoids landing on a whole
    number (which would allow an exact push)."""
    return round(x * 2) / 2


def run_line_probability(expected_margin, diff_stdev, line):
    """Probability the HOME team covers `line` (given as a signed
    home-team spread, e.g. -1.5 means home is favored and must win by
    2+ runs to cover; +1.5 means home is the underdog and covers by
    losing by 1 or winning outright) -- a normal approximation around
    the modeled expected home-minus-away margin, using the league's
    own empirically derived (or fallback) game-to-game standard
    deviation. Real per-game run differential is a discrete, somewhat
    skewed quantity (a Skellam-style difference of two low counts),
    not a perfect bell curve -- a normal approximation is a reasonable
    simplification at typical baseball scoring levels, not an exact
    model."""
    threshold = -line
    return 1 - _normal_cdf(threshold, expected_margin, diff_stdev)


def total_runs_probability(expected_total, total_stdev, line):
    """Probability of going OVER `line`, same normal-approximation
    approach and same caveat as run_line_probability above."""
    return 1 - _normal_cdf(line, expected_total, total_stdev)


def log5_win_probability(team_a_pyth_pct, team_b_pyth_pct):
    """Bill James' Log5 formula -- head-to-head win probability for
    team A over team B, from each team's own Pythagorean win%.
    Clamped to a narrow [0.01, 0.99] range before the formula runs, so
    an early-season team that's literally 0-5 or 5-0 (a real, if
    extreme, possible input) never produces a divide-by-zero or a
    nonsensical 0%/100% probability against a real opponent."""
    pa = min(max(team_a_pyth_pct, 0.01), 0.99)
    pb = min(max(team_b_pyth_pct, 0.01), 0.99)
    denom = pa + pb - 2 * pa * pb
    if denom <= 0:
        return 0.5
    return (pa - pa * pb) / denom


def apply_home_field(prob_home_before_adjustment, home_field_win_pct):
    """Nudges a pre-home-field win probability by the league's own
    derived (or fallback) home-field tendency -- an ADDITIVE shift
    (the home-field rate's deviation from a flat 50%), not a 50/50
    blend with the raw Log5 estimate. A blend was the original
    approach here, and it was a real bug: averaging a strong team-
    strength signal (say Log5 saying a team is a 70% favorite) with an
    independent number that's naturally close to 50% (the league's
    overall home-field rate) cuts the signal's deviation from a coin
    flip roughly in half every time -- a 70% favorite would show as
    just 60%, regardless of how lopsided the real talent gap actually
    is. Shifting additively by the home-field deviation instead (a
    league that's 53% home-favorable adds +3 points, not a wholesale
    average) preserves the actual team-strength signal, which is the
    whole point of a matchup page like this."""
    return min(max(prob_home_before_adjustment + (home_field_win_pct - 0.5), 0.01), 0.99)


MIN_H2H_GAMES_FOR_ANY_WEIGHT = 1  # a single meeting still gets SOME weight, just a small one
H2H_MAX_WEIGHT = 0.30  # even with many meetings, head-to-head can shift the estimate by at most this fraction of its own deviation from 50%
H2H_WEIGHT_SCALE = 10  # games-to-reach-roughly-half-of-H2H_MAX_WEIGHT -- a regression-to-the-mean constant, not derived from real data (there isn't enough within-season H2H volume in a 48-game schedule to fit this properly)


def apply_head_to_head(prob_home_before_adjustment, h2h):
    """Nudges a pre-head-to-head win probability toward how the home
    team has actually done against THIS SPECIFIC opponent this season
    -- also an additive shift, same reasoning as apply_home_field
    (preserve the underlying signal rather than blend it away), but
    weighted down significantly since a season's worth of head-to-head
    meetings between two specific teams is a much smaller, noisier
    sample than either team's full season record. A 2-0 season series
    is real information, but it's nowhere near as reliable as a
    team's full 30-game Pythagorean record, so this can only nudge the
    estimate by a modest amount at most (H2H_MAX_WEIGHT), and that
    weight itself scales up gradually with how many times they've
    actually met (H2H_WEIGHT_SCALE) rather than snapping to full
    weight after just one or two games.

    No adjustment (returned unchanged) if h2h is missing/None or the
    two teams haven't played each other yet this season -- "no data"
    is not the same as "they're evenly matched head-to-head," so this
    correctly does nothing rather than assume 50/50."""
    if not h2h or not h2h.get("games"):
        return prob_home_before_adjustment
    games = h2h["games"]
    h2h_home_win_pct = h2h["team_a_wins"] / games
    weight = H2H_MAX_WEIGHT * (games / (games + H2H_WEIGHT_SCALE))
    shift = (h2h_home_win_pct - 0.5) * weight
    return min(max(prob_home_before_adjustment + shift, 0.01), 0.99)


# Target combined implied probability after odds are shown -- what a
# real sportsbook's vig/juice looks like. A "true" 50/50 game doesn't
# get shown as +100/+100 on a real book; it gets shown as something
# like -110/-110 (52.4% + 52.4% = ~104.8% combined). FanDuel's actual
# vig varies by market and moves over time, so this is a reasonable
# representative number for a mainline moneyline/spread/total, not a
# live-scraped figure from FanDuel itself -- there's no way to keep
# this exactly in sync with a real book's current pricing, and this
# app doesn't try to.
VIG_TARGET = 1.045


def apply_vig(prob_a, prob_b, target_overround=VIG_TARGET):
    """Scales two complementary true probabilities up so they sum to
    target_overround instead of exactly 1.0 -- what turns a "true"
    modeled 50/50 into a realistic-looking -110/-110 line instead of
    an unrealistic +100/+100. Scaling proportionally (not adding a
    flat amount to each side) keeps the ratio between favorite and
    underdog intact -- a big favorite stays a big favorite, just
    priced the way a real book would price it."""
    total = prob_a + prob_b
    if total <= 0:
        return prob_a, prob_b
    scale = target_overround / total
    return prob_a * scale, prob_b * scale


def prob_to_american_odds(p):
    """Win probability -> American odds format (e.g. -150 or +130) --
    standard sportsbook display convention (negative = favorite,
    positive = underdog) used here purely for familiarity. This is
    NOT a real market price: no vig/juice is baked in (a real book's
    two-sided line always implies combined probability over 100%;
    this is the raw modeled probability only), and it isn't rounded to
    any actual sportsbook's price ladder."""
    p = min(max(p, 0.01), 0.99)
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def _games_on_date(target_date, season_year=None):
    """Every game scheduled for target_date (YYYY-MM-DD string) that
    hasn't been played yet -- uses cbl_api.get_schedule (the real
    schedule feed, confirmed via a live payload to use "scheduled" as
    its actual not-yet-played status, and "postponed" for a
    rained-out game) rather than the game-ids feed this app uses
    everywhere else, which was never confirmed to include not-yet-
    played games at all -- that gap was the root cause of a real bug
    (the /betting page showing no games for a future date).

    Returns entries normalized to the flat {"homeTeam","awayTeam",...}
    shape the rest of build_daily_odds already expects (the schedule
    feed itself nests team info as {"name":...} under "homeTeam"/
    "awayTeam" -- unpacked here so nothing downstream needs to know
    the two feeds shape team names differently)."""
    all_games = cbl_api.get_schedule(season_year)
    if not isinstance(all_games, list):
        return []
    matches = []
    for g in all_games:
        if g.get("date") != target_date or g.get("status") != "scheduled":
            continue
        home = (g.get("homeTeam") or {}).get("name")
        away = (g.get("awayTeam") or {}).get("name")
        if not home or not away:
            continue
        matches.append({"homeTeam": home, "awayTeam": away, "gameDate": g.get("date")})
    return matches


def build_daily_odds(target_date, season_year=None):
    """Modeled win probability, run-line, and total-runs estimates for
    every not-yet-played game on record for target_date (YYYY-MM-DD
    string).

    Returns {"date", "home_field": {...}, "run_stats": {...}, "games":
    [{"home_team","away_team","home_win_pct","away_win_pct","home_odds",
    "away_odds","home_pythagorean","away_pythagorean","run_line",
    "home_cover_prob","away_cover_prob","home_cover_odds",
    "away_cover_odds","total_line","over_prob","under_prob",
    "over_odds","under_odds"}, ...]}. A game is skipped entirely (not
    shown with a placeholder) if either team doesn't have a
    Pythagorean win% AND runs-per-game on record yet (e.g. a brand
    new team with zero completed games) -- there's no responsible
    estimate to show in that case."""
    games = _games_on_date(target_date, season_year)
    if not games:
        return {"date": target_date, "home_field": None, "run_stats": None, "games": []}

    all_team_names = sorted({
        gamelog._field(g, "homeTeam", "home_team", "home-team") for g in games
    } | {
        gamelog._field(g, "awayTeam", "away_team", "away-team") for g in games
    })
    # Home-field and run-stats are both derived from the WHOLE league's
    # results, not just today's participants, since a handful of teams'
    # games wouldn't be enough of a sample on their own.
    try:
        standings_teams = [r["team"] for r in team_schedule.build_standings(all_team_names, season_year)]
    except Exception:
        standings_teams = all_team_names
    league_teams = standings_teams or all_team_names
    home_field = league_home_field_win_pct(league_teams, season_year)
    run_stats = league_run_stats(league_teams, season_year)

    team_cache = {}

    def team_info(team_name):
        """Pythagorean win% plus runs-per-game (scored/allowed), all
        from the one already-fetched season record -- cached per team
        name so a team appearing in multiple of today's games (not
        possible in a normal single-game-per-day schedule, but a safe
        guard regardless) doesn't refetch. Keeps the full record too
        (not just the derived numbers), since apply_head_to_head needs
        the home team's own game_results to look up meetings against
        today's specific opponent."""
        if team_name not in team_cache:
            try:
                record = team_schedule.build_team_season_record(team_name, season_year)
            except Exception:
                record = None
            record = record or {}
            games_played = record.get("games") or 0
            pyth = record.get("pythagorean_win_pct")
            rpg_scored = (record.get("runs_scored") / games_played) if games_played else None
            rpg_allowed = (record.get("runs_allowed") / games_played) if games_played else None
            team_cache[team_name] = {
                "pyth": pyth, "rpg_scored": rpg_scored, "rpg_allowed": rpg_allowed, "record": record,
            }
        return team_cache[team_name]

    results = []
    for g in games:
        home = gamelog._field(g, "homeTeam", "home_team", "home-team")
        away = gamelog._field(g, "awayTeam", "away_team", "away-team")
        if not home or not away:
            continue
        home_info, away_info = team_info(home), team_info(away)
        if home_info["pyth"] is None or away_info["pyth"] is None:
            continue

        raw_home_prob = log5_win_probability(home_info["pyth"], away_info["pyth"])
        home_prob = apply_home_field(raw_home_prob, home_field["win_pct"])
        h2h = team_schedule.head_to_head_record(home_info["record"], away)
        home_prob = apply_head_to_head(home_prob, h2h)
        away_prob = 1 - home_prob
        # Vig is applied only to the ODDS shown, not to the win% --
        # the win% column stays the model's honest true estimate;
        # the odds column is what actually gets displayed as a price,
        # same separation a real sportsbook makes between its internal
        # number and what it posts.
        home_vig_prob, away_vig_prob = apply_vig(home_prob, away_prob)

        row = {
            "home_team": home, "away_team": away,
            "home_pythagorean": home_info["pyth"], "away_pythagorean": away_info["pyth"],
            "head_to_head": h2h,
            "home_win_pct": home_prob, "away_win_pct": away_prob,
            "home_odds": prob_to_american_odds(home_vig_prob),
            "away_odds": prob_to_american_odds(away_vig_prob),
        }

        # Run line + total: needs both teams' runs-per-game on record,
        # separate from (but usually available alongside) Pythagorean
        # win%, so this is checked and added independently rather than
        # skipping the whole game if only these are missing.
        if None not in (home_info["rpg_scored"], home_info["rpg_allowed"],
                         away_info["rpg_scored"], away_info["rpg_allowed"]):
            expected_home_runs = (home_info["rpg_scored"] + away_info["rpg_allowed"]) / 2
            expected_away_runs = (away_info["rpg_scored"] + home_info["rpg_allowed"]) / 2
            expected_margin = expected_home_runs - expected_away_runs
            expected_total = expected_home_runs + expected_away_runs

            run_line = round_to_half(-expected_margin)  # e.g. home favored by 3 -> line of -2.5 or -3.5
            home_cover_prob = run_line_probability(expected_margin, run_stats["diff_stdev"], run_line)
            away_cover_prob = 1 - home_cover_prob
            home_cover_vig, away_cover_vig = apply_vig(home_cover_prob, away_cover_prob)

            total_line = round_to_half(expected_total)
            over_prob = total_runs_probability(expected_total, run_stats["total_stdev"], total_line)
            under_prob = 1 - over_prob
            over_vig, under_vig = apply_vig(over_prob, under_prob)

            row.update({
                "run_line": run_line,
                "home_cover_prob": home_cover_prob, "away_cover_prob": away_cover_prob,
                "home_cover_odds": prob_to_american_odds(home_cover_vig),
                "away_cover_odds": prob_to_american_odds(away_cover_vig),
                "total_line": total_line,
                "over_prob": over_prob, "under_prob": under_prob,
                "over_odds": prob_to_american_odds(over_vig),
                "under_odds": prob_to_american_odds(under_vig),
            })

        results.append(row)

    return {"date": target_date, "home_field": home_field, "run_stats": run_stats, "games": results}
