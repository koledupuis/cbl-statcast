"""
CBL Stats — a Canadian Baseball League stats site in the spirit of
Baseball Savant, built on the public cbl.ca stats API.

Run with:  python app.py
Then visit http://localhost:5000
"""
from flask import Flask, render_template, abort, request, redirect, url_for, jsonify, send_file
from markupsafe import Markup, escape

import analytics
import baserunning
import broadcast_notes
import broadcast_overlay
import cbl_api
import compare_stats
import fielding_splits
import gameday
import gamelog
import glossary
import pitch_discipline
import pitching_extra
import pitching_splits as pitcher_splits
import player_merge
import rolling
import splits as player_splits
import stats
import obscure_stats
import stadiums
import team_schedule
import transactions

app = Flask(__name__)


@app.template_filter("fmt3")
def _fmt3(x):
    return stats.fmt3(x)


@app.template_filter("fmt_pct")
def _fmt_pct(x):
    return stats.fmt_pct(x)


@app.template_filter("fmt1")
def _fmt1(x):
    return stats.fmt1(x)


@app.template_filter("fmt2")
def _fmt2(x):
    return stats.fmt2(x)


@app.template_filter("abbr")
def _abbr(text):
    """Wraps a stat label in <abbr title="..."> if it's a known
    abbreviation (see glossary.py), so hovering it shows the full term
    -- native browser tooltip, no JS needed. Unrecognized labels pass
    through unchanged rather than breaking anything.

    Only the title attribute is escaped; `text` itself is trusted as-is
    (it's always a short template-authored label, never user input --
    some labels intentionally contain HTML entities like `&minus;`,
    which would render literally if escaped again here)."""
    definition = glossary.GLOSSARY.get(text)
    if not definition:
        return text
    return Markup(f'<abbr title="{escape(definition)}">{text}</abbr>')


def _find_player(player_id, rows):
    for r in rows:
        if r.get("playerId") == player_id:
            return r
    return None


# Sortable columns per leaderboard category. Maps the `sort` query param to
# the field on each row (or a callable for derived values), plus whether a
# bigger number is "better" by default (used only to pick the initial sort
# direction the first time a column is clicked).
_BATTING_SORT_FIELDS = {
    "team": (lambda r: (r.get("teamName") or "").lower(), False),
    "name": (lambda r: (r.get("fullName") or "").lower(), False),
    "g": (lambda r: r.get("games") or 0, True),
    "pa": (lambda r: r.get("plateAppearances") or 0, True),
    "ab": (lambda r: r.get("atBats") or 0, True),
    "r": (lambda r: r.get("runs") or 0, True),
    "h": (lambda r: r.get("hits") or 0, True),
    "hr": (lambda r: r.get("homeRuns") or 0, True),
    "rbi": (lambda r: r.get("rbi") or 0, True),
    "avg": (lambda r: r.get("battingAvg") or 0, True),
    "obp": (lambda r: r.get("obp") or 0, True),
    "slg": (lambda r: r.get("slg") or 0, True),
    "ops": (lambda r: r.get("ops") or 0, True),
}

_PITCHING_SORT_FIELDS = {
    "team": (lambda r: (r.get("teamName") or "").lower(), False),
    "name": (lambda r: (r.get("fullName") or "").lower(), False),
    "g": (lambda r: r.get("games") or 0, True),
    "w": (lambda r: r.get("wins") or 0, True),
    "l": (lambda r: r.get("losses") or 0, True),
    "sv": (lambda r: r.get("saves") or 0, True),
    "so": (lambda r: r.get("strikeoutsPitching") or 0, True),
    "era": (lambda r: r.get("era") if r.get("era") is not None else 999, False),
    "whip": (lambda r: r.get("whip") if r.get("whip") is not None else 999, False),
}

_FIELDING_SORT_FIELDS = {
    "team": (lambda r: (r.get("teamName") or "").lower(), False),
    "name": (lambda r: (r.get("fullName") or "").lower(), False),
    "g": (lambda r: r.get("games") or 0, True),
    "po": (lambda r: r.get("putouts") or 0, True),
    "a": (lambda r: r.get("assists") or 0, True),
    "e": (lambda r: r.get("errors") or 0, False),
    "fpct": (lambda r: r.get("fieldingPct") or 0, True),
}


def _team_options(rows):
    return sorted({r.get("teamName") for r in rows if r.get("teamName")})


def _apply_sort_and_team_filter(rows, sort_fields, default_key_fn, default_reverse):
    """Shared logic for the batting/pitching/fielding leaderboards: optional
    ?team= filter, then optional ?sort=&dir= override of the category's
    normal default sort."""
    team = (request.args.get("team") or "").strip()
    if team:
        rows = [r for r in rows if r.get("teamName") == team]

    sort_key = (request.args.get("sort") or "").strip().lower()
    dir_param = (request.args.get("dir") or "").strip().lower()

    if sort_key in sort_fields:
        key_fn, bigger_is_better = sort_fields[sort_key]
        reverse = bigger_is_better if dir_param not in ("asc", "desc") else (dir_param == "desc")
        rows = sorted(rows, key=key_fn, reverse=reverse)
        active_sort, active_dir = sort_key, ("desc" if reverse else "asc")
    else:
        rows = sorted(rows, key=default_key_fn, reverse=default_reverse)
        active_sort, active_dir = None, None

    return rows, team, active_sort, active_dir


@app.route("/version")
def version_info():
    """Reports when the running container image was actually built (see
    the Dockerfile's `RUN date ... > build_info.txt` step). The single
    fastest way to confirm whether a Render deploy actually happened:
    hit this route before and after a push. If the timestamp doesn't
    change, the deploy itself never ran (wrong branch, auto-deploy
    off, a build that failed silently, or you're looking at a
    different service/URL than you think) -- that's something to
    chase down on Render's dashboard, not a code bug. If the timestamp
    DOES change but the site still looks the same, the deploy is fine
    and the issue is elsewhere (browser cache, or stale disk-cached
    game data from before a correction -- see cbl_api.clear_cache())."""
    try:
        with open("build_info.txt") as f:
            build_time = f.read().strip()
    except Exception:
        build_time = "unknown -- build_info.txt not found (normal if running locally with `python app.py` instead of the Docker image)"
    return jsonify({"build_time_utc": build_time})


@app.route("/")
def home():
    return leaderboard_batting()


@app.route("/leaderboard/batting")
def leaderboard_batting():
    all_rows = cbl_api.get_batting()
    rows, team, active_sort, active_dir = _apply_sort_and_team_filter(
        all_rows, _BATTING_SORT_FIELDS,
        default_key_fn=lambda r: r.get("ops") or 0, default_reverse=True,
    )
    return render_template("leaderboard.html", category="batting", rows=rows, active="batting",
                            teams=_team_options(all_rows), selected_team=team,
                            active_sort=active_sort, active_dir=active_dir)


@app.route("/leaderboard/pitching")
def leaderboard_pitching():
    all_rows = cbl_api.get_pitching()
    rows, team, active_sort, active_dir = _apply_sort_and_team_filter(
        all_rows, _PITCHING_SORT_FIELDS,
        default_key_fn=lambda r: (r.get("inningsPitched") in (None, 0), r.get("era") or 999),
        default_reverse=False,
    )
    return render_template("leaderboard.html", category="pitching", rows=rows, active="pitching",
                            teams=_team_options(all_rows), selected_team=team,
                            active_sort=active_sort, active_dir=active_dir)


@app.route("/leaderboard/fielding")
def leaderboard_fielding():
    all_rows = cbl_api.get_fielding()
    rows, team, active_sort, active_dir = _apply_sort_and_team_filter(
        all_rows, _FIELDING_SORT_FIELDS,
        default_key_fn=lambda r: r.get("fieldingPct") or 0, default_reverse=True,
    )
    return render_template("leaderboard.html", category="fielding", rows=rows, active="fielding",
                            teams=_team_options(all_rows), selected_team=team,
                            active_sort=active_sort, active_dir=active_dir)


@app.route("/leaderboard/obscure")
def leaderboard_obscure():
    """Fun/obscure stat leaderboards nobody thinks to look up -- see
    obscure_stats.py for exactly what's computed and the cost tradeoffs
    of each category (some are cheap season-row arithmetic, others need
    a per-qualifying-player game-log walk)."""
    try:
        categories = obscure_stats.build_all_obscure_stats()
    except Exception:
        categories = []
    return render_template("obscure_stats.html", categories=categories, active="obscure")


@app.route("/leaderboard/stadiums")
def leaderboard_stadiums():
    """League-wide batting stats by park -- both teams' hitters
    combined per venue, since this is a park-factor-style view (how
    does the ball carry at this specific field), not a team stat.
    See stadiums.py for exactly how this is walked and why it's a
    single whole-season pass rather than per-team."""
    try:
        rows = stadiums.build_stadium_stats()
    except Exception:
        rows = []
    sort_key = (request.args.get("sort") or "games").strip()
    sort_fields = {
        "games": lambda r: r.get("games") or 0,
        "avg": lambda r: r.get("avg") or 0,
        "ops": lambda r: r.get("ops") or 0,
        "hr": lambda r: r.get("hr") or 0,
        "hr_per_game": lambda r: r.get("hr_per_game") or 0,
    }
    key_fn = sort_fields.get(sort_key, sort_fields["games"])
    rows = sorted(rows, key=key_fn, reverse=True)
    return render_template("stadiums.html", rows=rows, active="stadiums", active_sort=sort_key)


@app.route("/stadiums/<venue_name>")
def park_detail(venue_name):
    """In-depth stats for one specific park -- home/away split,
    pitching-against, top hitters there, and the season's game log at
    that venue. See stadiums.build_park_detail for exactly what's
    computed and why it's a second full-season walk rather than
    derived from the leaderboard's own pass."""
    try:
        detail = stadiums.build_park_detail(venue_name)
    except Exception:
        detail = None
    if not detail:
        abort(404)
    return render_template("park_detail.html", detail=detail, active="stadiums")


@app.route("/leaderboard/teams")
def leaderboard_teams():
    batting_rows = cbl_api.get_batting()
    pitching_rows = cbl_api.get_pitching()
    pitching_ctx = analytics.league_pitching_context(pitching_rows)
    try:
        team_games = team_schedule.count_completed_games_by_team()
    except Exception:
        team_games = None

    team_batting = list(analytics.team_batting_stats(batting_rows, team_games=team_games).values())
    team_pitching = list(analytics.team_pitching_stats(pitching_rows, pitching_ctx, team_games=team_games).values())

    all_team_names = _team_options(batting_rows + pitching_rows)
    try:
        standings = team_schedule.build_standings(all_team_names)
    except Exception:
        standings = []

    sort_key = (request.args.get("sort") or "").strip().lower()
    dir_param = (request.args.get("dir") or "").strip().lower()

    standings_fields = {
        "team": (lambda r: r["team"].lower(), False),
        "win_pct": (lambda r: r["win_pct"] if r["win_pct"] is not None else -1, True),
        "run_differential": (lambda r: r["run_differential"], True),
        "games_behind": (lambda r: r["games_behind"], False),
    }
    batting_fields = {
        "team": (lambda r: r["team"].lower(), False), "ops": (lambda r: r["ops"] or 0, True),
        "obp": (lambda r: r["obp"] or 0, True), "slg": (lambda r: r["slg"] or 0, True),
        "iso": (lambda r: r["iso"] or 0, True), "woba": (lambda r: r["woba"] or 0, True),
        "babip": (lambda r: r["babip"] or 0, True), "rpg": (lambda r: r["runs_per_game"] or 0, True),
    }
    pitching_fields = {
        "team": (lambda r: r["team"].lower(), False), "era": (lambda r: r["era"] or 999, False),
        "whip": (lambda r: r["whip"] or 999, False), "fip": (lambda r: r["fip"] or 999, False),
        "gb_pct": (lambda r: r["gb_pct"] or 0, True), "rapg": (lambda r: r["runs_allowed_per_game"] or 999, False),
    }

    standings_sort_key = (request.args.get("standings_sort") or "").strip().lower()
    standings_dir = (request.args.get("standings_dir") or "").strip().lower()
    if standings_sort_key in standings_fields:
        key_fn, bigger_is_better = standings_fields[standings_sort_key]
        reverse = bigger_is_better if standings_dir not in ("asc", "desc") else (standings_dir == "desc")
        standings = sorted(standings, key=key_fn, reverse=reverse)
        active_standings_sort, active_standings_dir = standings_sort_key, ("desc" if reverse else "asc")
    else:
        active_standings_sort, active_standings_dir = None, None

    if sort_key in batting_fields:
        key_fn, bigger_is_better = batting_fields[sort_key]
        reverse = bigger_is_better if dir_param not in ("asc", "desc") else (dir_param == "desc")
        team_batting = sorted(team_batting, key=key_fn, reverse=reverse)
    else:
        team_batting = sorted(team_batting, key=lambda r: r["ops"] or 0, reverse=True)

    if sort_key in pitching_fields:
        key_fn, bigger_is_better = pitching_fields[sort_key]
        reverse = bigger_is_better if dir_param not in ("asc", "desc") else (dir_param == "desc")
        team_pitching = sorted(team_pitching, key=key_fn, reverse=reverse)
    else:
        team_pitching = sorted(team_pitching, key=lambda r: r["era"] or 999, reverse=False)

    return render_template("team_leaderboard.html", active="teams",
                            standings=standings,
                            active_standings_sort=active_standings_sort, active_standings_dir=active_standings_dir,
                            team_batting=team_batting, team_pitching=team_pitching,
                            active_sort=sort_key or None, active_dir=dir_param or None)


def _player_search_index(all_batting, all_pitching, all_fielding):
    """Deduplicated id -> {"id","name","team"} for every player who
    appears in any season stat table, used to populate the Compare
    page's type-ahead search."""
    seen = {}
    for r in all_batting + all_pitching + all_fielding:
        pid = r.get("playerId")
        if pid and pid not in seen:
            seen[pid] = {"id": pid, "name": r.get("fullName"), "team": r.get("teamName")}
    return sorted(seen.values(), key=lambda x: (x.get("name") or "").lower())


@app.route("/export/broadcast-notes")
def export_broadcast_notes():
    """Downloads a 1-2 page PDF of broadcaster key notes for a two-team
    matchup -- see broadcast_notes.py for exactly what's on it and how
    it's selected."""
    team_a = (request.args.get("teamA") or "").strip()
    team_b = (request.args.get("teamB") or "").strip()
    if not team_a or not team_b:
        abort(400, "Both teamA and teamB are required")
    if team_a == team_b:
        abort(400, "Pick two different teams")

    pitcher_a = (request.args.get("pitcherA") or "").strip() or None
    pitcher_b = (request.args.get("pitcherB") or "").strip() or None

    matchup = broadcast_notes.build_matchup_notes(team_a, team_b, pitcher_a_id=pitcher_a, pitcher_b_id=pitcher_b)
    pdf_buf = broadcast_notes.render_pdf(matchup)

    safe_a = "".join(c for c in team_a if c.isalnum() or c in " -_").strip().replace(" ", "-")
    safe_b = "".join(c for c in team_b if c.isalnum() or c in " -_").strip().replace(" ", "-")
    filename = f"broadcast-notes-{safe_a}-vs-{safe_b}.pdf"

    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name=filename)



@app.route("/broadcast/overlay")
def broadcast_overlay_page():
    """L-shaped broadcast overlay page: right-side scoreboard + bottom
    current-batter card, meant to be used as an OBS/browser-source
    layer over the actual game video (which shows through the empty
    center/top-left area -- this page has no background there at all).

    Needs ?game=<public_game_id> for an actively-scored CBL game. No
    setup/config step beyond picking that ID -- the URL itself is the
    only "state" this needs, so it's trivial to reuse for a different
    game by just changing the query param."""
    public_game_id = (request.args.get("game") or "").strip()
    return render_template("broadcast_overlay.html", public_game_id=public_game_id)


@app.route("/broadcast/state.json")
def broadcast_state_json():
    """Polled every few seconds by the overlay page's own JS -- current
    score/inning/outs/line score plus a full stat card for whoever's
    actually at bat right now, re-derived fresh from CBL's live gameday
    feed on every call (see broadcast_overlay.py for exactly how
    "current batter" gets resolved and what happens when there's
    nothing live to show)."""
    public_game_id = (request.args.get("game") or "").strip()
    if not public_game_id:
        return jsonify({"found": False, "error": "missing ?game="})

    state = broadcast_overlay.get_live_game_state(public_game_id)
    batter_card = None
    pitcher_card = None
    due_up = []
    leaders = []
    if state["found"]:
        try:
            gd = cbl_api.get_gameday(public_game_id)
        except Exception:
            gd = None
        if state["current_batter_id"]:
            try:
                batter_card = broadcast_overlay.build_batter_card(
                    state["current_batter_id"], gd=gd, opposing_pitcher_id=state["current_pitcher_id"],
                )
            except Exception:
                batter_card = None
        if state["current_pitcher_id"]:
            try:
                pitcher_card = broadcast_overlay.build_pitcher_card(state["current_pitcher_id"], gd=gd)
            except Exception:
                pitcher_card = None
        try:
            due_up = broadcast_overlay.build_due_up(gd)
        except Exception:
            due_up = []
        try:
            leaders = broadcast_overlay.build_leader_rotation(state["home_team"], state["away_team"])
        except Exception:
            leaders = []

    state["batter"] = batter_card
    state["pitcher"] = pitcher_card
    state["due_up"] = due_up
    state["leaders"] = leaders
    return jsonify(state)


@app.route("/compare")
def compare_page():
    player_ids = [p for p in (request.args.get("players") or "").split(",") if p]
    team_names = [t for t in (request.args.get("teams") or "").split(",") if t]

    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()
    all_fielding = cbl_api.get_fielding()

    batting_ctx = analytics.league_batting_context(all_batting)
    pitching_ctx = analytics.league_pitching_context(all_pitching)

    players_compare = []
    for pid in player_ids:
        b_row = _find_player(pid, all_batting)
        p_row = _find_player(pid, all_pitching)
        f_row = _find_player(pid, all_fielding)
        base_row = b_row or p_row or f_row
        if not base_row:
            continue
        entry = {"id": pid, "name": base_row.get("fullName"), "team": base_row.get("teamName"),
                 "b_row": b_row, "p_row": p_row}
        if b_row:
            adv = analytics.batting_advanced(b_row)
            adv.update(analytics.batting_plus_stats(adv, batting_ctx))
            entry["batting"] = adv
        if p_row:
            adv = analytics.pitching_advanced(p_row, pitching_ctx)
            adv.update(analytics.pitching_plus_stats(adv, pitching_ctx))
            entry["pitching"] = adv
        players_compare.append(entry)

    try:
        team_games = team_schedule.count_completed_games_by_team()
    except Exception:
        team_games = None
    team_batting_all = analytics.team_batting_stats(all_batting, team_games=team_games)
    team_pitching_all = analytics.team_pitching_stats(all_pitching, pitching_ctx, team_games=team_games)

    teams_compare = []
    for tname in team_names:
        entry = {"name": tname, "batting": team_batting_all.get(tname), "pitching": team_pitching_all.get(tname)}
        try:
            entry["record"] = team_schedule.build_team_season_record(tname)
        except Exception:
            entry["record"] = None
        teams_compare.append(entry)

    has_batting = any(p.get("batting") for p in players_compare)
    has_pitching = any(p.get("pitching") for p in players_compare)
    has_team_batting = any(t.get("batting") for t in teams_compare)
    has_team_pitching = any(t.get("pitching") for t in teams_compare)
    has_team_record = any(t.get("record") for t in teams_compare)

    return render_template(
        "compare.html",
        active="compare",
        player_search_list=_player_search_index(all_batting, all_pitching, all_fielding),
        team_search_list=_team_options(all_batting + all_pitching),
        selected_player_ids=player_ids,
        selected_team_names=team_names,
        players_compare=players_compare,
        teams_compare=teams_compare,
        batting_rows=compare_stats.build_batting_compare_rows(players_compare) if has_batting else None,
        pitching_rows=compare_stats.build_pitching_compare_rows(players_compare) if has_pitching else None,
        team_batting_rows=compare_stats.build_team_batting_compare_rows(teams_compare) if has_team_batting else None,
        team_pitching_rows=compare_stats.build_team_pitching_compare_rows(teams_compare) if has_team_pitching else None,
        team_record_rows=compare_stats.build_team_record_compare_rows(teams_compare) if has_team_record else None,
    )


@app.route("/team/<team_name>")
def team_roster(team_name):
    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()
    all_fielding = cbl_api.get_fielding()

    batting_rows = [r for r in all_batting if r.get("teamName") == team_name]
    pitching_rows = [r for r in all_pitching if r.get("teamName") == team_name]
    fielding_rows = [r for r in all_fielding if r.get("teamName") == team_name]

    if not batting_rows and not pitching_rows and not fielding_rows:
        abort(404)

    # Real roster status (Active/Inactive/Injured/Released/etc) from
    # CBL's own transaction log -- see transactions.py. Matched by
    # name (that feed has no player ID); a name with no matching
    # transaction is left unlabeled rather than assumed active, since
    # "no transaction on record" isn't the same claim as "confirmed
    # active."
    try:
        roster_status = transactions.build_roster_status()
    except Exception:
        roster_status = {}

    def _attach_status(rows):
        for r in rows:
            info = roster_status.get((r.get("fullName") or "").strip().lower())
            r["rosterStatus"] = info["status"] if info else None
        return rows

    batting_rows = _attach_status(batting_rows)
    pitching_rows = _attach_status(pitching_rows)
    fielding_rows = _attach_status(fielding_rows)

    active_only = request.args.get("active_only", "1") != "0"
    off_roster = {transactions.STATUS_RELEASED, transactions.STATUS_INACTIVE,
                  transactions.STATUS_INJURED, transactions.STATUS_TRADED_AWAY, transactions.STATUS_LEFT_LEAGUE}
    if active_only:
        batting_rows = [r for r in batting_rows if r["rosterStatus"] not in off_roster]
        pitching_rows = [r for r in pitching_rows if r["rosterStatus"] not in off_roster]
        fielding_rows = [r for r in fielding_rows if r["rosterStatus"] not in off_roster]

    batting_rows = sorted(batting_rows, key=lambda r: r.get("ops") or 0, reverse=True)
    pitching_rows = sorted(pitching_rows, key=lambda r: (r.get("inningsPitched") in (None, 0), r.get("era") or 999))
    fielding_rows = sorted(fielding_rows, key=lambda r: r.get("fieldingPct") or 0, reverse=True)

    pitching_ctx = analytics.league_pitching_context(all_pitching)
    try:
        team_games = team_schedule.count_completed_games_by_team()
    except Exception:
        team_games = None
    team_batting_line = analytics.team_batting_stats(all_batting, team_games=team_games).get(team_name)
    team_pitching_line = analytics.team_pitching_stats(all_pitching, pitching_ctx, team_games=team_games).get(team_name)

    try:
        team_record = team_schedule.build_team_season_record(team_name)
    except Exception:
        team_record = None

    try:
        status_breakdown = gamelog.team_schedule_status_breakdown(team_name)
    except Exception:
        status_breakdown = {}

    return render_template(
        "team_roster.html",
        active="teams",
        team_name=team_name,
        batting_rows=batting_rows,
        pitching_rows=pitching_rows,
        status_breakdown=status_breakdown,
        fielding_rows=fielding_rows,
        team_batting_line=team_batting_line,
        team_pitching_line=team_pitching_line,
        active_only=active_only,
        team_record=team_record,
    )


@app.route("/team/<team_name>/splits")
def team_splits(team_name):
    """One page showing every player on a team's platoon (L/R) splits
    together, instead of clicking into each player's own page one at a
    time. Deliberately scoped to platoon + vs-opponent splits (both come
    straight from CBL's own precomputed analytics endpoint, one cheap
    call per player) rather than situational splits like RISP/Bases
    Loaded -- those need a full per-player schedule walk each (see
    splits.py), which would make a whole-roster page far too slow to
    load. Deeper situational splits are still available on each
    player's own page, linked from here."""
    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()

    batting_rows = sorted(
        [r for r in all_batting if r.get("teamName") == team_name],
        key=lambda r: r.get("ops") or 0, reverse=True,
    )
    pitching_rows = sorted(
        [r for r in all_pitching if r.get("teamName") == team_name],
        key=lambda r: (r.get("inningsPitched") in (None, 0), r.get("era") or 999),
    )

    if not batting_rows and not pitching_rows:
        abort(404)

    def batter_with_splits(row):
        entry = dict(row)
        entry["vsLeft"] = None
        entry["vsRight"] = None
        entry["rc"] = analytics.batting_advanced(row).get("rc")
        try:
            feed = cbl_api.get_player_analytics(row.get("playerId"))
        except Exception:
            feed = None
        if feed:
            bat_splits = ((feed.get("eventAnalytics") or {}).get("batting") or {}).get("splits") or {}
            for s in bat_splits.get("byPitcherHandedness") or []:
                raw_key = (s.get("key") or "").lower()
                if raw_key not in ("left", "right"):
                    continue
                entry["vsLeft" if raw_key == "left" else "vsRight"] = s.get("outcomes")
        return entry

    def pitcher_with_splits(row):
        entry = dict(row)
        entry["vsLeft"] = None
        entry["vsRight"] = None
        try:
            feed = cbl_api.get_player_analytics(row.get("playerId"))
        except Exception:
            feed = None
        if feed:
            pitch_splits = ((feed.get("eventAnalytics") or {}).get("pitching") or {}).get("splits") or {}
            for s in pitch_splits.get("byBatterHandedness") or []:
                raw_key = (s.get("key") or "").lower()
                if raw_key not in ("left", "right"):
                    continue
                entry["vsLeft" if raw_key == "left" else "vsRight"] = s.get("outcomes")
        return entry

    batters = [batter_with_splits(r) for r in batting_rows]
    pitchers = [pitcher_with_splits(r) for r in pitching_rows]

    try:
        team_situational = player_splits.build_team_situational_batting(team_name)
    except Exception:
        team_situational = None

    return render_template(
        "team_splits.html",
        active="teams",
        team_name=team_name,
        batters=batters,
        pitchers=pitchers,
        team_situational=team_situational,
    )


@app.route("/api/export/splits")
def api_export_splits():
    """{"teams": {"Team Name": {"batters": [...], "pitchers": [...]}, ...}}

    Bulk export built specifically for the Google Sheets "full splits
    workbook" feature: every team's batters and pitchers, season stats
    plus L/R platoon splits, in ONE response. Doing this as N separate
    /api/player/<id> calls from Apps Script would mean one HTTP round
    trip per player in the league -- slow, and liable to run into Apps
    Script's own execution-time and URL-fetch quotas on a big league.
    This does all of it server-side in one request instead.

    Multi-team players (see player_merge.py) appear once per team
    they've played for, since the point of this export is "give me a
    per-team roster with splits," not a deduplicated league-wide list.

    Can be slow with a lot of players (one cbl.ca analytics call per
    player, each briefly cached but still a real network round trip) --
    this is meant to be triggered manually/infrequently (a Sheets menu
    action), not loaded like a normal page.
    """
    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()

    batting_ctx = analytics.league_batting_context(all_batting)
    pitching_ctx = analytics.league_pitching_context(all_pitching)

    batters_by_id = {}
    for r in all_batting:
        batters_by_id.setdefault(r.get("playerId"), []).append(r)
    pitchers_by_id = {}
    for r in all_pitching:
        pitchers_by_id.setdefault(r.get("playerId"), []).append(r)

    teams = {}

    def team_bucket(name):
        return teams.setdefault(name, {"batters": [], "pitchers": []})

    def batting_splits_for(pid):
        try:
            feed = cbl_api.get_player_analytics(pid)
        except Exception:
            return {}
        bat_splits = ((feed.get("eventAnalytics") or {}).get("batting") or {}).get("splits") or {} if feed else {}
        out = {}
        for s in bat_splits.get("byPitcherHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key == "left":
                key = "vsLeft"
            elif raw_key == "right":
                key = "vsRight"
            else:
                continue  # unrecognized handedness value (e.g. a switch-hitter category) -- don't guess which side it belongs on
            o = s.get("outcomes") or {}
            out[key] = {
                "pa": o.get("plateAppearances"), "ab": o.get("atBats"), "hr": o.get("homeRuns"),
                "avg": o.get("battingAverage"), "obp": o.get("onBasePercentage"),
                "slg": o.get("sluggingPercentage"), "ops": o.get("onBasePlusSlugging"),
            }
        return out

    def pitching_splits_for(pid):
        try:
            feed = cbl_api.get_player_analytics(pid)
        except Exception:
            return {}
        pitch_splits = ((feed.get("eventAnalytics") or {}).get("pitching") or {}).get("splits") or {} if feed else {}
        out = {}
        for s in pitch_splits.get("byBatterHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key == "left":
                key = "vsLeft"
            elif raw_key == "right":
                key = "vsRight"
            else:
                continue  # unrecognized handedness value -- don't guess which side it belongs on
            o = s.get("outcomes") or {}
            out[key] = {
                "bf": o.get("plateAppearances") or o.get("battersFaced") or o.get("batters_faced") or o.get("bf"), "hr": o.get("homeRuns"),
                "bb": o.get("walks"), "so": o.get("strikeouts"), "avg": o.get("battingAverage"),
            }
        return out

    for pid, rows in batters_by_id.items():
        b_row = player_merge.merge_batting_rows(rows)
        team_names = player_merge.team_names_for_rows(rows)
        adv = analytics.batting_advanced(b_row)
        adv.update(analytics.batting_plus_stats(adv, batting_ctx))
        entry = {
            "playerId": pid, "name": b_row.get("fullName"), "position": b_row.get("position"),
            "games": b_row.get("games"), "pa": b_row.get("plateAppearances"), "ab": b_row.get("atBats"),
            "avg": b_row.get("battingAvg"), "obp": b_row.get("obp"), "slg": b_row.get("slg"), "ops": b_row.get("ops"),
            "hr": b_row.get("homeRuns"), "rbi": b_row.get("rbi"), "bb": b_row.get("walks"), "so": b_row.get("strikeouts"),
            "iso": adv.get("iso"), "woba": adv.get("woba"), "ops_plus": adv.get("ops_plus"),
            "splits": batting_splits_for(pid),
        }
        for team_name in team_names:
            team_bucket(team_name)["batters"].append(entry)

    for pid, rows in pitchers_by_id.items():
        p_row = player_merge.merge_pitching_rows(rows)
        team_names = player_merge.team_names_for_rows(rows)
        adv = analytics.pitching_advanced(p_row, pitching_ctx)
        adv.update(analytics.pitching_plus_stats(adv, pitching_ctx))
        entry = {
            "playerId": pid, "name": p_row.get("fullName"), "position": p_row.get("position"),
            "games": p_row.get("games"), "ip": p_row.get("inningsPitched"), "era": p_row.get("era"),
            "whip": p_row.get("whip"), "so": p_row.get("strikeoutsPitching"), "bb": p_row.get("walksAllowed"),
            "w": p_row.get("wins"), "l": p_row.get("losses"),
            "fip": adv.get("fip"), "era_plus": adv.get("era_plus"),
            "splits": pitching_splits_for(pid),
        }
        for team_name in team_names:
            team_bucket(team_name)["pitchers"].append(entry)

    for team in teams.values():
        team["batters"].sort(key=lambda x: x.get("ops") or 0, reverse=True)
        team["pitchers"].sort(key=lambda x: x.get("era") if x.get("era") is not None else 999)

    return jsonify({"teams": teams})


@app.route("/api/players")
def api_players():
    """{"players": [{"id":..., "name":..., "team":...}, ...]}
    Lightweight search index for the Sheets sidebar's player picker --
    same data _player_search_index() already builds for the Compare
    page. Optional ?q= filters by name (case-insensitive substring)."""
    q = (request.args.get("q") or "").strip().lower()
    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()
    all_fielding = cbl_api.get_fielding()
    index = _player_search_index(all_batting, all_pitching, all_fielding)
    if q:
        index = [p for p in index if q in (p.get("name") or "").lower()]
    return jsonify({"players": index})


@app.route("/api/team/<team_name>/active-pitchers")
def api_team_active_pitchers(team_name):
    """{"pitchers": [{"id":..., "name":...}, ...]}
    Powers the broadcast notes form's starting-pitcher dropdown --
    deliberately scoped to the ACTIVE roster (see
    team_schedule.get_active_roster_pitchers for exactly what that
    means and why season-stat rows alone aren't a safe proxy for it),
    not every pitcher who has a stat line for this team at some point
    this season."""
    try:
        pitchers = team_schedule.get_active_roster_pitchers(team_name)
    except Exception:
        pitchers = []
    return jsonify({"pitchers": pitchers})


@app.route("/api/player/<player_id>")
def api_player(player_id):
    """JSON season stats + L/R platoon splits for one player -- built for
    the Google Apps Script / Sheets integration (see README). Kept
    deliberately lighter than the full player page: no per-game walk
    (game log, rolling stats, momentum, stolen bases), since a Sheets
    custom function can get recalculated a lot and a 30-40-request
    schedule walk per call would be far too slow. Splits come straight
    from CBL's own /players/<id>/analytics endpoint, which is already
    fast (no per-game walk needed there either)."""
    batting_rows = cbl_api.get_batting()
    pitching_rows = cbl_api.get_pitching()
    fielding_rows = cbl_api.get_fielding()

    b_rows = player_merge.find_player_rows(player_id, batting_rows)
    p_rows = player_merge.find_player_rows(player_id, pitching_rows)
    f_rows = player_merge.find_player_rows(player_id, fielding_rows)

    b_row = player_merge.merge_batting_rows(b_rows)
    p_row = player_merge.merge_pitching_rows(p_rows)
    f_row = player_merge.merge_fielding_rows(f_rows)

    base_row = b_row or p_row or f_row
    if not base_row:
        return jsonify({"error": "player not found", "playerId": player_id}), 404

    b_team_names = player_merge.team_names_for_rows(b_rows)
    p_team_names = player_merge.team_names_for_rows(p_rows)
    team_names = list(dict.fromkeys(b_team_names + p_team_names))

    payload = {
        "playerId": player_id,
        "name": base_row.get("fullName"),
        "teams": team_names,
        "position": base_row.get("position"),
    }

    if b_row:
        adv = analytics.batting_advanced(b_row)
        ctx = analytics.league_batting_context(batting_rows)
        adv.update(analytics.batting_plus_stats(adv, ctx))
        payload["batting"] = {
            "games": b_row.get("games"), "pa": b_row.get("plateAppearances"), "ab": b_row.get("atBats"),
            "avg": b_row.get("battingAvg"), "obp": b_row.get("obp"), "slg": b_row.get("slg"), "ops": b_row.get("ops"),
            "hr": b_row.get("homeRuns"), "rbi": b_row.get("rbi"), "bb": b_row.get("walks"), "so": b_row.get("strikeouts"),
            "iso": adv.get("iso"), "woba": adv.get("woba"), "ops_plus": adv.get("ops_plus"),
            "babip": adv.get("babip"), "k_pct": adv.get("k_pct"), "bb_pct": adv.get("bb_pct"), "rc": adv.get("rc"),
        }

    if p_row:
        p_ctx = analytics.league_pitching_context(pitching_rows)
        adv = analytics.pitching_advanced(p_row, p_ctx)
        adv.update(analytics.pitching_plus_stats(adv, p_ctx))
        payload["pitching"] = {
            "games": p_row.get("games"), "gs": p_row.get("gamesStarted"), "w": p_row.get("wins"), "l": p_row.get("losses"),
            "sv": p_row.get("saves"), "ip": p_row.get("inningsPitched"), "era": p_row.get("era"), "whip": p_row.get("whip"),
            "so": p_row.get("strikeoutsPitching"), "bb": p_row.get("walksAllowed"),
            "fip": adv.get("fip"), "xfip": adv.get("xfip"), "era_plus": adv.get("era_plus"),
            "k_pct": adv.get("k_pct"), "bb_pct": adv.get("bb_pct"), "k9": adv.get("k9"), "bb9": adv.get("bb9"),
        }

    if f_row:
        payload["fielding"] = {
            "games": f_row.get("games"), "po": f_row.get("putouts"), "a": f_row.get("assists"),
            "e": f_row.get("errors"), "tc": f_row.get("totalChances"), "fpct": f_row.get("fieldingPct"),
        }

    splits = {}
    try:
        analytics_feed = cbl_api.get_player_analytics(player_id)
    except Exception:
        analytics_feed = None
    if analytics_feed:
        bat_splits = ((analytics_feed.get("eventAnalytics") or {}).get("batting") or {}).get("splits") or {}
        for s in bat_splits.get("byPitcherHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key == "left":
                key = "vsLeft"
            elif raw_key == "right":
                key = "vsRight"
            else:
                continue  # unrecognized handedness value -- don't guess which side it belongs on
            o = s.get("outcomes") or {}
            splits[key] = {
                "pa": o.get("plateAppearances"), "ab": o.get("atBats"), "h": o.get("hits"), "hr": o.get("homeRuns"),
                "avg": o.get("battingAverage"), "obp": o.get("onBasePercentage"),
                "slg": o.get("sluggingPercentage"), "ops": o.get("onBasePlusSlugging"),
            }
        pitch_splits = ((analytics_feed.get("eventAnalytics") or {}).get("pitching") or {}).get("splits") or {}
        for s in pitch_splits.get("byBatterHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key == "left":
                key = "pitchingVsLeft"
            elif raw_key == "right":
                key = "pitchingVsRight"
            else:
                continue  # unrecognized handedness value -- don't guess which side it belongs on
            o = s.get("outcomes") or {}
            splits[key] = {
                "bf": o.get("plateAppearances") or o.get("battersFaced") or o.get("batters_faced") or o.get("bf"), "h": o.get("hits"), "hr": o.get("homeRuns"),
                "bb": o.get("walks"), "so": o.get("strikeouts"), "avg": o.get("battingAverage"),
            }
    payload["splits"] = splits

    return jsonify(payload)


@app.route("/player/<player_id>")
def player_page(player_id):
    batting_rows = cbl_api.get_batting()
    pitching_rows = cbl_api.get_pitching()
    fielding_rows = cbl_api.get_fielding()

    b_rows = player_merge.find_player_rows(player_id, batting_rows)
    p_rows = player_merge.find_player_rows(player_id, pitching_rows)
    f_rows = player_merge.find_player_rows(player_id, fielding_rows)

    b_row = player_merge.merge_batting_rows(b_rows)
    p_row = player_merge.merge_pitching_rows(p_rows)
    f_row = player_merge.merge_fielding_rows(f_rows)

    # A player who switched teams mid-season has more than one team
    # name here -- every schedule-walking builder below (game logs,
    # splits, rolling stats) accepts a list of team names for exactly
    # this case, so their combined game log covers every team's
    # schedule, not just whichever team's row happened to come first.
    b_team_names = player_merge.team_names_for_rows(b_rows)
    p_team_names = player_merge.team_names_for_rows(p_rows)
    f_team_names = player_merge.team_names_for_rows(f_rows)
    player_team_names = list(dict.fromkeys(b_team_names + p_team_names))

    base_row = b_row or p_row or f_row
    if not base_row:
        abort(404)

    # Real roster status from CBL's own transaction log (see
    # transactions.py) -- matched by name, same as the team roster
    # page. Display rules here are deliberately different from that
    # page's plain status badges: "released" reads as "Free Agent"
    # (clearer to a visitor than CBL's own internal transaction
    # wording), and "call_up_list" shows nothing at all -- a call-up
    # designation doesn't change anything meaningful about how this
    # page should present the player, so it's not worth a badge.
    roster_badge = None
    try:
        tx_status = transactions.player_status(base_row.get("fullName"))
    except Exception:
        tx_status = None
    if tx_status:
        status = tx_status["status"]
        if status == transactions.STATUS_RELEASED:
            roster_badge = {"text": "Free Agent", "css_class": "released"}
        elif status == transactions.STATUS_INACTIVE:
            roster_badge = {"text": "Inactive", "css_class": "inactive"}
        elif status == transactions.STATUS_INJURED:
            roster_badge = {"text": "Injured", "css_class": "injured"}
        elif status == transactions.STATUS_LEFT_LEAGUE:
            roster_badge = {"text": "Left League", "css_class": "left_league"}
        # STATUS_ACTIVE and STATUS_CALL_UP_LIST intentionally produce
        # no badge -- both are normal "on a team" states as far as
        # this page is concerned.

        # If the player's most recent transaction ties them to a
        # specific team (a Sign or a Trade acquisition both carry the
        # destination team on the transaction record itself), put that
        # team first in the list this page displays -- someone who
        # was traded mid-season should see their CURRENT team up
        # front, not whichever team's stat row happened to be fetched
        # first.
        current_team = tx_status.get("team")
        if current_team and current_team in player_team_names:
            player_team_names = [current_team] + [t for t in player_team_names if t != current_team]

    analytics_feed = None
    try:
        analytics_feed = cbl_api.get_player_analytics(player_id)
    except Exception:
        analytics_feed = None

    batting_pct = stats.build_batting_percentiles(b_row, batting_rows) if b_row and (b_row.get("plateAppearances") or 0) > 0 else None
    pitching_pct = stats.build_pitching_percentiles(p_row, pitching_rows) if p_row and (p_row.get("advancedPitching", {}).get("inningsPitchedOuts") or 0) > 0 else None

    # Advanced analytics module: season-level advanced batting/pitching
    # stats (OPS+, wOBA, FIP, xFIP, etc.), rated against this league's own
    # qualified-player pool. See analytics.py for exactly what's computed
    # and what's approximated.
    batting_adv = pitching_adv = None
    if b_row:
        adv = analytics.batting_advanced(b_row)
        ctx = analytics.league_batting_context(batting_rows)
        adv.update(analytics.batting_plus_stats(adv, ctx))
        batting_adv = adv
    if p_row:
        p_ctx = analytics.league_pitching_context(pitching_rows)
        adv = analytics.pitching_advanced(p_row, p_ctx)
        adv.update(analytics.pitching_plus_stats(adv, p_ctx))
        pitching_adv = adv

    event_batting = (analytics_feed or {}).get("eventAnalytics", {}).get("batting") if analytics_feed else None
    event_pitching = (analytics_feed or {}).get("eventAnalytics", {}).get("pitching") if analytics_feed else None

    game_log = None
    home_away = None
    season_splits = None
    rolling_stats = None
    momentum = None
    pitcher_season_splits = None
    pitcher_start_totals = None
    pitcher_extra_stats = None
    pitcher_game_log = None
    batter_first_pitch = None
    pitcher_first_pitch = None
    stolen_bases = None
    park_splits = None
    pitcher_park_splits = None
    umpire_splits = None
    pitcher_umpire_splits = None
    quality_pa = None
    batted_ball_profile = None
    fielding_position_splits = None
    fielding_monthly_splits = None
    fielding_daynight_splits = None
    pitcher_scoreless_streak = None

    game_log_coverage = None
    pitcher_game_log_coverage = None

    if b_row:
        try:
            game_log = gamelog.build_player_game_log(player_id, b_team_names)
            home_away = gamelog.build_home_away_splits(game_log)
        except Exception:
            game_log = None
            home_away = None
        if game_log:
            games_logged = sum(len(bucket["rows"]) for bucket in game_log["months"].values())
            official_games = b_row.get("games")
            if official_games is not None and games_logged != official_games:
                game_log_coverage = {
                    "games_logged": games_logged,
                    "official_games": official_games,
                    "missing": official_games - games_logged,
                    "official_avg": b_row.get("battingAvg"),
                    "logged_avg": game_log["season_totals"].get("avg"),
                }
            try:
                park_splits = gamelog.build_park_splits(game_log, b_row.get("teamName"))
            except Exception:
                park_splits = None
            try:
                umpire_splits = gamelog.build_umpire_splits(game_log)
            except Exception:
                umpire_splits = None
        try:
            season_splits, quality_pa, batted_ball_profile = player_splits.build_player_splits(player_id, b_team_names)
        except Exception:
            season_splits = None
            quality_pa = None
            batted_ball_profile = None
        if game_log:
            try:
                rolling_stats = rolling.build_rolling_stats(game_log)
                momentum = rolling.build_momentum_meter(rolling_stats, b_row.get("ops"))
            except Exception:
                rolling_stats = None
                momentum = None
        try:
            batter_first_pitch = pitch_discipline.build_batter_first_pitch(player_id, b_team_names)
        except Exception:
            batter_first_pitch = None
        try:
            stolen_bases = baserunning.build_player_stolen_bases(player_id, b_team_names)
        except Exception:
            stolen_bases = None

    if p_row:
        try:
            pitcher_season_splits = pitcher_splits.build_pitcher_splits(player_id, p_team_names)
        except Exception:
            pitcher_season_splits = None
        try:
            pitcher_start_totals = pitcher_splits.build_pitcher_start_totals(player_id, p_team_names)
        except Exception:
            pitcher_start_totals = None
        try:
            pitcher_first_pitch = pitch_discipline.build_pitcher_first_pitch(player_id, p_team_names)
        except Exception:
            pitcher_first_pitch = None
        try:
            pitcher_extra_stats = pitching_extra.build_pitcher_extra_stats(player_id, p_team_names, p_row=p_row)
        except Exception:
            pitcher_extra_stats = None
        try:
            pitcher_game_log = pitcher_splits.build_pitcher_game_log(player_id, p_team_names)
        except Exception:
            pitcher_game_log = None
        if pitcher_game_log:
            games_logged = sum(len(bucket["rows"]) for bucket in pitcher_game_log["months"].values())
            official_games = p_row.get("games")
            if official_games is not None and games_logged != official_games:
                pitcher_game_log_coverage = {
                    "games_logged": games_logged,
                    "official_games": official_games,
                    "missing": official_games - games_logged,
                }
            try:
                pitcher_park_splits = pitcher_splits.build_pitcher_park_splits(pitcher_game_log, p_row.get("teamName"))
            except Exception:
                pitcher_park_splits = None
            try:
                pitcher_umpire_splits = pitcher_splits.build_pitcher_umpire_splits(pitcher_game_log)
            except Exception:
                pitcher_umpire_splits = None
            try:
                pitcher_scoreless_streak = pitcher_splits.build_pitcher_scoreless_streak(pitcher_game_log)
            except Exception:
                pitcher_scoreless_streak = None

    if f_row:
        try:
            fielding_position_splits = fielding_splits.build_fielding_position_splits(player_id, f_team_names)
        except Exception:
            fielding_position_splits = None
        try:
            fielding_monthly_splits = fielding_splits.build_fielding_monthly_splits(player_id, f_team_names)
        except Exception:
            fielding_monthly_splits = None
        try:
            fielding_daynight_splits = fielding_splits.build_fielding_daynight_splits(player_id, f_team_names)
        except Exception:
            fielding_daynight_splits = None

    return render_template(
        "player.html",
        player=base_row,
        b_row=b_row,
        p_row=p_row,
        f_row=f_row,
        b_rows_by_team=b_rows if len(b_rows) > 1 else None,
        p_rows_by_team=p_rows if len(p_rows) > 1 else None,
        f_rows_by_team=f_rows if len(f_rows) > 1 else None,
        player_team_names=player_team_names,
        roster_badge=roster_badge,
        batting_pct=batting_pct,
        pitching_pct=pitching_pct,
        batting_adv=batting_adv,
        pitching_adv=pitching_adv,
        event_batting=event_batting,
        event_pitching=event_pitching,
        analytics=analytics_feed,
        game_log=game_log,
        home_away=home_away,
        season_splits=season_splits,
        pitcher_season_splits=pitcher_season_splits,
        pitcher_start_totals=pitcher_start_totals,
        pitcher_extra_stats=pitcher_extra_stats,
        pitcher_game_log=pitcher_game_log,
        game_log_coverage=game_log_coverage,
        pitcher_game_log_coverage=pitcher_game_log_coverage,
        park_splits=park_splits,
        pitcher_park_splits=pitcher_park_splits,
        umpire_splits=umpire_splits,
        pitcher_umpire_splits=pitcher_umpire_splits,
        quality_pa=quality_pa,
        batted_ball_profile=batted_ball_profile,
        fielding_position_splits=fielding_position_splits,
        fielding_monthly_splits=fielding_monthly_splits,
        fielding_daynight_splits=fielding_daynight_splits,
        pitcher_scoreless_streak=pitcher_scoreless_streak,
        rolling_stats=rolling_stats,
        momentum=momentum,
        batter_first_pitch=batter_first_pitch,
        pitcher_first_pitch=pitcher_first_pitch,
        stolen_bases=stolen_bases,
    )


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return render_template("search_results.html", q=q, results=[])

    seen = {}
    for r in cbl_api.get_batting() + cbl_api.get_pitching():
        name = r.get("fullName", "")
        if q in name.lower():
            seen[r.get("playerId")] = r
    results = list(seen.values())
    return render_template("search_results.html", q=q, results=results)


@app.route("/game")
def gameday_lookup():
    game_id = (request.args.get("id") or "").strip()
    if game_id:
        return redirect(url_for("game_page", public_game_id=game_id))
    return render_template("game_lookup.html")


@app.route("/game/<public_game_id>")
def game_page(public_game_id):
    gd = cbl_api.get_gameday(public_game_id)
    if not gd or not gd.get("snapshot"):
        abort(404)

    lookup = gameday.build_player_lookup(gd)

    return render_template(
        "game.html",
        gd=gd,
        setup=gd["snapshot"].get("setup", {}),
        line_score=gameday.build_line_score(gd),
        batting_box=gameday.build_batting_box(gd, lookup),
        pitching_box=gameday.build_pitching_box(gd, lookup),
        play_by_play=gameday.build_play_by_play(gd, lookup),
    )


if __name__ == "__main__":
    # Local dev only. In the Docker image this file is never run directly --
    # the container's CMD starts waitress instead (see Dockerfile), which is
    # a proper production WSGI server. debug=True is intentionally NOT set
    # here even for local runs: Flask's debug mode exposes a remote code
    # execution console on error pages, which is unsafe on anything but a
    # single-user localhost session.
    app.run(host="0.0.0.0", port=5000)
