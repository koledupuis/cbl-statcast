"""
Broadcaster "key notes" PDF for a two-team matchup -- page 1 is a
highlight sheet (team comparison, streaks, head-to-head, players to
watch), page 2 is a full starting-pitcher deep-dive. Built entirely
from data this app already computes elsewhere; nothing here is
AI-generated commentary or subjective narrative.

- "Batters to Watch" and "Pitchers to Watch" are both plain season-
  stat leaderboards (OPS / ERA respectively, each with a minimum
  playing-time qualifier) -- a rule-based pick, not a derived or
  weighted score. An earlier version of this sheet ranked batters by
  a momentum score instead (recent form vs season pace); that's gone
  now in favor of the same straightforward season-stat approach used
  for pitchers.
- "Pitchers to Watch" defaults to season ERA (min innings pitched) --
  there's no pitching-side momentum meter built (the existing one is
  batting-specific: recent OPS/hit-streak/RBI), so this stays
  season-based rather than inventing a new metric under time pressure.
  Would need a parallel rolling-ERA-style meter to do the same thing
  for pitchers -- a reasonably contained follow-up if wanted.
  build_matchup_notes accepts an optional explicit starting-pitcher ID
  per team (pitcher_a_id / pitcher_b_id) that replaces this ERA-based
  pick entirely for that team -- an ERA leader who isn't even pitching
  today isn't useful on a broadcast sheet, so an explicit pick always
  wins over the automatic one when given.
- Every featured batter and pitcher on page 1, plus the page-2 starter,
  carries a "Last Game" / "Last Outing" line (see _fmt_batting_line /
  _fmt_pitching_line) -- their most recent completed game's actual
  line, not a season figure, pulled from the same per-game rows the
  site's own Game Log tabs use.
- Head-to-head record is read directly from team_schedule.py's own
  game-by-game results for one team, filtered to games against the
  other.
- Every number here is one this app computes and displays elsewhere
  too -- this module's only job is picking a subset of it and laying
  it out on a page, not deriving anything new. Page 2's deep dive in
  particular (build_pitcher_deep_dive) is entirely assembled from
  functions this app already had: analytics.pitching_advanced for
  every rate stat (K%/BB%/FIP/GB%/BABIP-against), pitching_splits.py
  for situational/platoon splits and quality starts, nothing computed
  fresh for this page.

Page 1's layout uses a two-column, per-team card (Batters to Watch /
Pitching Matchup / Bullpen Availability side by side for Team A and
Team B) below a full-width Team Comparison + Team Leaders section --
the same "two cards side by side" technique page 2's pitcher
comparison already used, just applied to page 1 too. Standings, Team
Leaders at a Glance, the season-series game log, Days Rest, and
Bullpen Availability were all added on top of the original content;
once they pushed page 1 onto a second sheet (stacking every section
full-width, once per team, one under the other), switching those
three per-team sections to a shared two-column layout bought back
enough vertical room to fit everything back onto one page, so the
whole sheet is a fixed two pages again -- page 1 highlights, page 2
(forced onto its own fresh page via PageBreak, see the bottom of
render_pdf) the starting-pitcher deep dive. The batter/pitcher/bullpen
tables on page 1 are intentionally trimmer than before (fewer columns,
smaller font) to fit a half-width card -- OBP/SLG/QPA%/scoreless-streak/
days-rest detail for these same players is still one click away on
this app's own player pages; nothing was dropped from what's computed,
only from what's printed on this specific half-width card.

Additions on top of the original sheet, all built the same way as
everything else here -- reusing a value this app already computes
elsewhere, not deriving anything new:
- "Days Rest" for the page-2 starter -- last outing's own game-log
  date (already fetched for the "Last Outing" line) compared to
  today (see _days_since). Free once you have the date.
- "QPA%" (Quality PA%) -- splits.build_player_splits' quality_pa
  return value, the same Quality-PA computation already used by the
  live broadcast overlay, still computed and attached to each Batters
  to Watch entry; page 1's own trimmed card doesn't have a column for
  it anymore (see above), but it's there in the data for anyone
  extending this sheet.
- "Team Leaders at a Glance" -- one line per HR/RBI/SB/SV/SO leader,
  each a plain max() over the same season stat rows (all_batting/
  all_pitching) everything else on this page already reads from, with
  SB pulled from baserunning.build_player_stolen_bases per-candidate
  since stolen bases aren't on the season stat row itself.
- "Standings" row in the Team Comparison table -- team_schedule.
  build_standings' own rank/games-behind, computed once for all teams
  and looked up per side.
- Season series game-by-game line -- team_schedule.build_team_season_
  record's own game_results, already walked for the head-to-head
  won-loss tally, filtered the same way and just also kept in full
  instead of only being counted.
- "Bullpen Availability" -- pitching_splits.build_bullpen_availability,
  which walks the last few days of completed games the same way every
  other game-log function in this app does and reads each pitcher's
  per-game pitch count off gameday.build_pitching_box (the same box
  score gamelog.py's own game logs use).
- Page 2 pitcher card additions -- K/9, BB/9, HR/9 (already returned
  by analytics.pitching_advanced, just not previously surfaced here),
  First-Pitch Strike%/Ball% (pitch_discipline.build_pitcher_first_pitch,
  an existing module this sheet hadn't used before), Bats/Throws (the
  same feed["player"] fields templates/player.html already shows), and
  a headshot + team logo pulled from the starter's own most recent
  completed game (that game's roster profileImageUrl/headshotUrl and
  top-level homeTeamLogoUrl/awayTeamLogoUrl -- both confirmed real
  fields, see gameday.build_player_lookup and this module's own
  _headshot_and_logo) -- reusing that game's already-fetched/cached
  gameday payload rather than a new network call. No hometown or age
  field has ever been confirmed anywhere in this app's data (checked
  against real cached gameday payloads and the player-analytics feed),
  so this sheet doesn't show either rather than inventing one.
"""
import io
from datetime import date, datetime

import requests
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
                                 HRFlowable, Image, KeepInFrame)

import analytics
import baserunning
import gameday
import gamelog
import cbl_api
import pitch_discipline
import pitching_splits
import rolling
import splits
import stats
import team_schedule
import transactions

IMAGE_FETCH_TIMEOUT = 6

MIN_PA_FOR_LEADER = 20
MIN_IP_FOR_LEADER = 10
TOP_BATTERS_SHOWN = 3
TOP_PITCHERS_SHOWN = 2

BRAND_RED = colors.HexColor("#8B1E1E")
BRAND_NAVY = colors.HexColor("#1E3A5F")  # page-2 accent for the "Team B" side of the pitcher comparison
LIGHT_GREY = colors.HexColor("#cccccc")
PALE_GREY = colors.HexColor("#F2F2F2")


def _ops_leaders(all_batting, team_name):
    """Every qualifying batter's season OPS for one team, sorted
    highest first, top TOP_BATTERS_SHOWN kept -- same minimum-PA
    qualification and shape as _era_leaders' pitching-side pick, so
    "Batters to Watch" and "Pitchers to Watch" both work the same way:
    a straightforward season-stat leaderboard, not a derived/weighted
    score.

    Filtered to currently-active players first (see
    transactions.filter_active_players) -- a released or inactive
    player's OLD season numbers could still be good enough to rank,
    but they don't belong on a "who to watch tonight" list. This was
    a real gap: nothing here checked roster status at all before,
    unlike the team roster page and the individual player page, which
    both already did."""
    candidates = [r for r in all_batting
                  if r.get("teamName") == team_name and (r.get("plateAppearances") or 0) >= MIN_PA_FOR_LEADER]
    candidates = transactions.filter_active_players(candidates)
    candidates.sort(key=lambda r: r.get("ops") or 0, reverse=True)
    return candidates[:TOP_BATTERS_SHOWN]


def _era_leaders(all_pitching, team_name):
    """Auto-selected "Pitchers to Watch" when no explicit starter was
    given (see _pitcher_row_for_starter for that separate path, which
    intentionally does NOT filter by roster status -- if someone
    explicitly picked a starter, they already know that pitcher is
    starting today). Filtered to currently-active pitchers first, same
    reasoning and same shared filter as _ops_leaders above."""
    pitchers = [r for r in all_pitching if r.get("teamName") == team_name]
    pitchers = [r for r in pitchers
                if ((r.get("advancedPitching") or {}).get("inningsPitchedOuts") or 0) / 3 >= MIN_IP_FOR_LEADER]
    pitchers = transactions.filter_active_players(pitchers)
    pitchers.sort(key=lambda r: r.get("era") if r.get("era") is not None else 999)
    return pitchers[:TOP_PITCHERS_SHOWN]


def _stat_leader(rows, team_name, stat_key):
    """Simple max() leader for one team in one counting stat (HR, RBI,
    saves, strikeouts) -- a plain leaderboard pick over the same
    season stat rows everything else on this page already reads from,
    not a derived score. None if nobody on the team has a positive
    value for this stat yet."""
    candidates = [r for r in rows if r.get("teamName") == team_name and (r.get(stat_key) or 0) > 0]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: r.get(stat_key) or 0)
    return {"name": best.get("fullName", ""), "value": best.get(stat_key) or 0}


def _sb_leader(all_batting, team_name):
    """Team stolen-base leader -- unlike HR/RBI/saves/strikeouts, SB
    isn't a season stat row field (see baserunning.py's own docstring
    for why), so this checks each team batter individually via
    baserunning.build_player_stolen_bases, which itself rides on
    already-cached gameday fetches this page is walking anyway for
    other sections. None if nobody on the team has a positive SB total
    on record (including the case where SB data just isn't available
    for this team's games at all)."""
    best = None
    for r in all_batting:
        if r.get("teamName") != team_name:
            continue
        pid = r.get("playerId")
        if not pid:
            continue
        try:
            sb_data = baserunning.build_player_stolen_bases(pid, team_name)
        except Exception:
            continue
        if not sb_data.get("available"):
            continue
        sb = sb_data.get("sb") or 0
        if sb <= 0:
            continue
        if best is None or sb > best["value"]:
            best = {"name": r.get("fullName", ""), "value": sb}
    return best


def _rank_label(n):
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', etc."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _standings_context(team_name, standings):
    """This team's own entry in team_schedule.build_standings' result
    -- rank (1-indexed) among the teams in that standings list, plus
    games behind the leader. None if the team isn't found in the
    standings (e.g. build_standings itself failed or returned
    nothing)."""
    for i, r in enumerate(standings or []):
        if r.get("team") == team_name:
            return {"rank": i + 1, "of": len(standings), "games_behind": r.get("games_behind"),
                     "wins": r.get("wins"), "losses": r.get("losses")}
    return None


def _pitcher_row_for_starter(all_pitching, team_name, starting_pitcher_id):
    """Season stat row for one specific, explicitly-selected starting
    pitcher, resolved by ID -- used instead of _era_leaders' auto pick
    whenever the broadcast operator has told us who's actually
    starting today (an ERA leader who isn't even pitching today isn't
    useful on this sheet).

    Falls back to a placeholder row (zero stats, name resolved from
    the active roster rather than season stats) if this pitcher hasn't
    recorded any innings yet this season -- a real, possible case for
    e.g. a recent call-up -- rather than silently dropping the
    person the operator specifically asked for."""
    for r in all_pitching:
        if r.get("playerId") == starting_pitcher_id:
            return r
    name = None
    try:
        for p in team_schedule.get_active_roster_pitchers(team_name):
            if p.get("id") == starting_pitcher_id:
                name = p.get("name")
                break
    except Exception:
        pass
    return {
        "playerId": starting_pitcher_id, "teamName": team_name, "fullName": name,
        "era": None, "whip": None, "inningsPitched": "0.0",
        "strikeoutsPitching": 0, "walksAllowed": 0,
        "advancedPitching": {"inningsPitchedOuts": 0},
    }


def _fmt_batting_line(g):
    """'2-4, HR, 2 RBI' style summary of one game's batting line, or
    '--' if there's no game to summarize. Only calls out extra-base
    hits/RBI/BB/SO when they actually happened, so a quiet game just
    reads as the bare H-AB line rather than a string full of zeros."""
    if not g:
        return "--"
    parts = [f"{g.get('h', 0) or 0}-{g.get('ab', 0) or 0}"]
    if g.get("hr"):
        parts.append(f"{g['hr']} HR")
    elif g.get("triples"):
        parts.append(f"{g['triples']} 3B")
    elif g.get("doubles"):
        parts.append(f"{g['doubles']} 2B")
    if g.get("rbi"):
        parts.append(f"{g['rbi']} RBI")
    if g.get("bb"):
        parts.append(f"{g['bb']} BB")
    if g.get("so"):
        parts.append(f"{g['so']} K")
    return ", ".join(parts)


def _fmt_pitching_line(g):
    """'6.0 IP, 2 ER, 7 K' style summary of one appearance, or '--' if
    there's no game to summarize. Uses real per-game earned runs when
    available (see pitching_splits.py's own ERA-exactness tracking),
    falling back to total runs allowed for that one game only when
    CBL's per-game earned-run data isn't there -- same fallback this
    app already uses everywhere else it shows a per-game ER figure."""
    if not g:
        return "--"
    er = g.get("er") if g.get("er") is not None else g.get("r")
    parts = [f"{g.get('ip', '0.0')} IP", f"{er if er is not None else 0} ER"]
    if g.get("so"):
        parts.append(f"{g['so']} K")
    if g.get("bb"):
        parts.append(f"{g['bb']} BB")
    return ", ".join(parts)


def _days_since(date_str, as_of=None):
    """Whole days between `date_str` (a "YYYY-MM-DD" game-log date, the
    same field every game-log row in this app already carries) and
    today (or `as_of`) -- "days rest" for a pitcher's last outing, read
    straight off a date this app already has rather than computed as a
    new field anywhere upstream. None if there's no date to compare
    (no games played yet, or an unparseable/blank date string)."""
    if not date_str:
        return None
    try:
        last = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return ((as_of or date.today()) - last).days


def _batter_quality_pa(player_id, team_name):
    """Season Quality PA% for one batter -- splits.build_player_splits'
    own quality_pa return value, the exact same per-at-bat computation
    the live broadcast overlay already surfaces (see
    broadcast_overlay.build_batter_situational_splits); nothing
    computed fresh here, just read for this page too. None if the
    underlying game-log walk fails for any reason (offline feed, a
    brand-new player with no game log yet)."""
    try:
        _, quality_pa, _ = splits.build_player_splits(player_id, team_name)
    except Exception:
        return None
    return quality_pa


def _batter_recent_context(player_id, team_name):
    """Most recent completed game's batting line + current hit streak
    for one player, sharing a SINGLE game-log walk -- same tradeoff
    already applied on the pitcher side (see _pitcher_recent_context).

    Hit streak specifically is rolling.py's own plain _hit_streak
    count (consecutive most-recent games with a hit) -- a factual
    tally, not the weighted/derived momentum score this sheet removed
    elsewhere; reading just this one field off build_rolling_stats
    isn't reintroducing momentum, since momentum is a separate,
    subsequent combination step (build_momentum_meter) this never
    calls."""
    try:
        game_log = gamelog.build_player_game_log(player_id, team_name)
    except Exception:
        return {"last_game": None, "hit_streak": 0}
    game_rows = [r for bucket in (game_log or {}).get("months", {}).values() for r in bucket["rows"]]
    last_game = game_rows[-1] if game_rows else None
    try:
        hit_streak = rolling.build_rolling_stats(game_log).get("hit_streak") or 0
    except Exception:
        hit_streak = 0
    return {"last_game": last_game, "hit_streak": hit_streak}


def _pitcher_recent_context(player_id, team_name):
    """Scoreless-appearance streak + most recent appearance's line for
    one pitcher, sharing a SINGLE game-log walk -- these used to be two
    separate functions each building/walking their own copy of the
    same game log, which is exactly the kind of redundant work this
    app avoids everywhere else (see e.g. broadcast_overlay.py's own
    comments on the same tradeoff)."""
    try:
        game_log = pitching_splits.build_pitcher_game_log(player_id, team_name)
    except Exception:
        return {"streak": 0, "last_game": None}
    streak = pitching_splits.build_pitcher_scoreless_streak(game_log)
    game_rows = [r for bucket in (game_log or {}).get("months", {}).values() for r in bucket["rows"]]
    return {"streak": streak, "last_game": (game_rows[-1] if game_rows else None)}


def _fetch_image_bytes(url):
    """Downloads an image URL and returns its raw bytes, or None if the
    URL is missing or the fetch fails for any reason. Headshots and
    team logos are both optional decoration on this sheet, never
    something a failed fetch should be allowed to break the page for,
    so every caller treats None as "just skip the image." Raw bytes
    (not a reportlab Image/ImageReader) are kept here so a fresh
    BytesIO can be handed to the Image flowable each time it's placed
    -- reportlab's own Image flowable wants a real file-like/path, not
    an ImageReader, and a single BytesIO can only be read once."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=IMAGE_FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _dominant_logo_color(image_bytes):
    """Best-effort dominant color read from a team's own logo image --
    CBL's API has no explicit team-color field anywhere (checked
    against every real payload this app has seen), so this is the
    most honest available substitute: read the actual color the team
    already uses on their own logo, rather than assign an arbitrary
    color or invent a "team colors" data source that doesn't exist.

    Near-white and near-black pixels are excluded from the count
    first, since those are almost always background fill or an
    outline/text stroke, not the team's actual identifying color --
    without that exclusion, most logos would just return white or
    black, which isn't a real "team color" in any useful sense.
    Similar shades are bucketed together (rounded to the nearest 16
    per channel) so anti-aliased edge pixels don't fragment what's
    visually one color into dozens of near-identical near-miss
    entries that each lose to a bucketed count.

    Returns a reportlab colors.Color, or None if the image can't be
    read/decoded for any reason -- never blocks the rest of the sheet
    on a failed color extraction; the call site falls back to the
    generic brand accent color instead."""
    if not image_bytes:
        return None
    try:
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGBA")
        img.thumbnail((60, 60))
        pixels = list(img.getdata())
    except Exception:
        return None

    counts = {}
    for r, g, b, a in pixels:
        if a < 128:
            continue
        if r > 235 and g > 235 and b > 235:
            continue
        if r < 25 and g < 25 and b < 25:
            continue
        key = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        return None
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    return colors.Color(best[0] / 255, best[1] / 255, best[2] / 255)


def _image_flowable(image_bytes, max_w, max_h):
    """A reportlab Image flowable for `image_bytes`, scaled down to fit
    inside a max_w x max_h box while preserving its natural aspect
    ratio (never upscaled past the source image's own resolution).
    None if `image_bytes` is None/unreadable."""
    if not image_bytes:
        return None
    try:
        iw, ih = ImageReader(io.BytesIO(image_bytes)).getSize()
    except Exception:
        return None
    if not iw or not ih:
        return None
    scale = min(max_w / iw, max_h / ih, 1.0)
    return Image(io.BytesIO(image_bytes), width=iw * scale, height=ih * scale)


def _headshot_and_logo(player_id, team_name, last_game):
    """(headshot ImageReader, team-logo ImageReader) for one starting
    pitcher, both sourced from that pitcher's own most recent
    completed game -- the same game already fetched for the "Last
    Outing" line, so this rides on cbl_api.get_gameday's existing
    completed-game disk cache rather than firing a new network
    request. The headshot comes from that game's own roster entry
    (gameday.build_player_lookup -- confirmed real field
    profileImageUrl, see gameday.py), the team logo from that same
    payload's top-level homeTeamLogoUrl/awayTeamLogoUrl (confirmed
    real fields, see this module's own README notes), matched to
    whichever side actually has this team's name. Both are (None,
    None) if there's no last game on record yet or the fetch/lookup
    fails for any reason -- an optional visual, never something that
    should break the rest of the deep dive."""
    if not last_game or not last_game.get("public_game_id"):
        return None, None
    try:
        gd = cbl_api.get_gameday(last_game["public_game_id"])
    except Exception:
        return None, None
    if not gd:
        return None, None

    headshot_url = None
    try:
        entry = gameday.build_player_lookup(gd).get(player_id)
        if entry:
            headshot_url = entry.get("headshotUrl") or entry.get("profileImageUrl")
    except Exception:
        pass

    logo_url = None
    if gd.get("home_team") == team_name:
        logo_url = gd.get("homeTeamLogoUrl")
    elif gd.get("away_team") == team_name:
        logo_url = gd.get("awayTeamLogoUrl")

    return _fetch_image_bytes(headshot_url), _fetch_image_bytes(logo_url)


def build_pitcher_deep_dive(row, team_name):
    """Full stat profile for one starting pitcher's page-2 deep dive --
    season line, advanced rate stats (K%/BB%/FIP/GB%/BABIP-against/etc,
    straight from analytics.pitching_advanced -- already computed
    elsewhere in this app, not reinvented here), situational splits
    (home/away, RISP, L/R platoon), longest outing and quality starts
    this season, runs allowed per appearance, and current scoreless
    streak.

    `row` is the pitcher's own season /stats/pitching row (or the
    zero-stat placeholder build_matchup_notes already builds for a
    starter with no innings on record yet) -- not re-fetched here,
    since the caller already has it. Every sub-lookup is independently
    wrapped so one missing piece (e.g. no analytics feed for a brand
    new player) degrades that one field to empty rather than losing
    the whole deep dive."""
    player_id = row.get("playerId")

    try:
        pitching_ctx = analytics.league_pitching_context(cbl_api.get_pitching())
        adv = analytics.pitching_advanced(row, pitching_ctx)
    except Exception:
        adv = {}

    splits_lookup = {}
    try:
        sections = pitching_splits.build_pitcher_splits(player_id, team_name)
        for section_rows in sections.values():
            for label, totals in section_rows:
                splits_lookup[label] = totals
    except Exception:
        pass

    platoon = {}
    bio = {}
    try:
        feed = cbl_api.get_player_analytics(player_id)
    except Exception:
        feed = None
    if feed:
        # "bats"/"throws" are confirmed real fields on feed["player"] --
        # already surfaced this same way on the player page (see
        # templates/player.html's "Bats {{ analytics.player.bats }} /
        # Throws {{ analytics.player.throws }}" line) -- read here too
        # rather than re-fetched or guessed. No hometown/age field has
        # ever been confirmed anywhere in this feed or the gameday
        # roster payload (checked against real cached payloads), so
        # this sheet doesn't show either -- inventing a birthplace or
        # age this app has no real source for isn't something a
        # broadcast operator can trust on air.
        bio = {"bats": (feed.get("player") or {}).get("bats"), "throws": (feed.get("player") or {}).get("throws")}
        pitch = (feed.get("eventAnalytics") or {}).get("pitching") or {}
        for s in (pitch.get("splits") or {}).get("byBatterHandedness") or []:
            raw_key = (s.get("key") or "").lower()
            if raw_key not in ("left", "right"):
                continue
            o = s.get("outcomes") or {}
            platoon["vsLeft" if raw_key == "left" else "vsRight"] = {
                "avg_against": o.get("battingAverage"),
                "bf": o.get("plateAppearances") or o.get("battersFaced"),
            }

    try:
        first_pitch = pitch_discipline.build_pitcher_first_pitch(player_id, team_name)
    except Exception:
        first_pitch = {}

    longest_outing = None
    runs_per_game = None
    scoreless_streak = 0
    last_game = None
    try:
        game_log = pitching_splits.build_pitcher_game_log(player_id, team_name)
        game_rows = [r for bucket in (game_log or {}).get("months", {}).values() for r in bucket["rows"]]
        if game_rows:
            longest_outing = max((r.get("ip_float") or 0) for r in game_rows)
            runs_per_game = stats.safe_div(sum((r.get("r") or 0) for r in game_rows), len(game_rows))
            last_game = game_rows[-1]
        scoreless_streak = pitching_splits.build_pitcher_scoreless_streak(game_log)
    except Exception:
        pass

    try:
        start_totals = pitching_splits.build_pitcher_start_totals(player_id, team_name)
    except Exception:
        start_totals = {"starts": 0, "quality_starts": 0}

    headshot_reader, logo_reader = _headshot_and_logo(player_id, team_name, last_game)
    team_color = _dominant_logo_color(logo_reader)

    return {
        "row": row,
        "adv": adv,
        "bio": bio,
        "first_pitch": first_pitch,
        "headshot": headshot_reader,
        "team_logo": logo_reader,
        "team_color": team_color,
        "home": splits_lookup.get("Home Games"),
        "away": splits_lookup.get("Away Games"),
        "risp": splits_lookup.get("Scoring Position"),
        "platoon": platoon,
        "longest_outing": longest_outing,
        "runs_per_game": runs_per_game,
        "starts": start_totals.get("starts", 0),
        "quality_starts": start_totals.get("quality_starts", 0),
        "scoreless_streak": scoreless_streak,
        "last_game": last_game,
        "days_rest": _days_since(last_game.get("date")) if last_game else None,
    }


def _head_to_head(team_a_record, team_b_name):
    if not team_a_record or not team_a_record.get("game_results"):
        return None
    games = [g for g in team_a_record["game_results"] if g["opponent"] == team_b_name]
    if not games:
        return {"games": 0, "team_a_wins": 0, "team_b_wins": 0, "results": []}
    a_wins = sum(1 for g in games if g["result"] == "W")
    return {"games": len(games), "team_a_wins": a_wins, "team_b_wins": len(games) - a_wins, "results": games}


def build_matchup_notes(team_a_name, team_b_name, pitcher_a_id=None, pitcher_b_id=None):
    """Gathers everything the PDF needs for a Team A vs Team B matchup.

    pitcher_a_id / pitcher_b_id: optional explicit starting-pitcher
    selection (a player ID from that team's active roster) -- when
    given, that specific pitcher replaces the auto ERA-leader pick
    entirely for that team's "Pitchers to Watch" section, rather than
    being added alongside it, since an ERA leader who isn't even
    pitching today isn't what a broadcast operator needs on this
    sheet."""
    all_batting = cbl_api.get_batting()
    all_pitching = cbl_api.get_pitching()
    pitching_ctx = analytics.league_pitching_context(all_pitching)
    try:
        team_games = team_schedule.count_completed_games_by_team()
    except Exception:
        team_games = None

    team_batting_all = analytics.team_batting_stats(all_batting, team_games=team_games)
    team_pitching_all = analytics.team_pitching_stats(all_pitching, pitching_ctx, team_games=team_games)

    # Standings are computed ONCE for every team in the league (not per
    # side of this matchup) since build_standings needs every team's
    # record to work out rank/games-behind -- team_schedule already
    # caches this call for a few minutes, so this doesn't add a real
    # cost on top of what the rest of this function already fetches.
    try:
        all_team_names = sorted({r.get("teamName") for r in all_batting + all_pitching if r.get("teamName")})
        standings = team_schedule.build_standings(all_team_names)
    except Exception:
        standings = []

    def team_bundle(team_name, other_team_name, starting_pitcher_id=None):
        try:
            record = team_schedule.build_team_season_record(team_name)
        except Exception:
            record = None
        if starting_pitcher_id:
            row = _pitcher_row_for_starter(all_pitching, team_name, starting_pitcher_id)
            ctx = _pitcher_recent_context(row.get("playerId"), team_name)
            pitchers = [{"row": row, "streak": ctx["streak"], "last_game": ctx["last_game"],
                         "days_rest": _days_since(ctx["last_game"]["date"]) if ctx["last_game"] else None}]
        else:
            pitchers = []
            for p in _era_leaders(all_pitching, team_name):
                ctx = _pitcher_recent_context(p.get("playerId"), team_name)
                pitchers.append({"row": p, "streak": ctx["streak"], "last_game": ctx["last_game"],
                                  "days_rest": _days_since(ctx["last_game"]["date"]) if ctx["last_game"] else None})
        batters = []
        for r in _ops_leaders(all_batting, team_name):
            ctx = _batter_recent_context(r.get("playerId"), team_name)
            batters.append({
                "row": r, "last_game": ctx["last_game"], "hit_streak": ctx["hit_streak"],
                "quality_pa": _batter_quality_pa(r.get("playerId"), team_name),
            })
        # Page 2's deep dive covers whichever pitcher ends up first here --
        # the explicit starter if one was given, otherwise the top ERA
        # leader -- so page 1 and page 2 always agree on who "the
        # starter" is for this matchup, never two different pitchers.
        deep_dive = build_pitcher_deep_dive(pitchers[0]["row"], team_name) if pitchers else None
        try:
            bullpen_availability = pitching_splits.build_bullpen_availability(team_name)
        except Exception:
            bullpen_availability = []
        return {
            "name": team_name,
            "record": record,
            "batting": team_batting_all.get(team_name),
            "pitching": team_pitching_all.get(team_name),
            "batters_to_watch": batters,
            "pitchers_to_watch": pitchers,
            "starter_deep_dive": deep_dive,
            "has_selected_starter": bool(starting_pitcher_id),
            "head_to_head": _head_to_head(record, other_team_name),
            "leaders": {
                "hr": _stat_leader(all_batting, team_name, "homeRuns"),
                "rbi": _stat_leader(all_batting, team_name, "rbi"),
                "sb": _sb_leader(all_batting, team_name),
                "sv": _stat_leader(all_pitching, team_name, "saves"),
                "so": _stat_leader(all_pitching, team_name, "strikeoutsPitching"),
            },
            "standings": _standings_context(team_name, standings),
            "bullpen_availability": bullpen_availability,
        }

    team_a = team_bundle(team_a_name, team_b_name, starting_pitcher_id=pitcher_a_id)
    team_b = team_bundle(team_b_name, team_a_name, starting_pitcher_id=pitcher_b_id)

    generated = date.today()
    return {"team_a": team_a, "team_b": team_b,
             "generated": f"{generated.strftime('%B')} {generated.day}, {generated.year}"}



def _table_style(header_only=False, font_size=8, padding=2.5):
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("GRID", (0, 0), (-1, -1), 0.5, LIGHT_GREY),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_RED),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if not header_only:
        style.append(("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"))
    return TableStyle(style)


def _stat_grid(cells, accent, cols=4, total_width=6.5 * inch):
    """A row of small boxed "hero stat" callouts (label above a big
    bold number) -- e.g. IP / SO / Quality Starts / Longest Outing --
    built as a single-row Table with each cell independently styled,
    rather than plain text, so the key numbers actually read as
    distinct at-a-glance callouts on the page rather than a sentence.
    `cells` is a list of (value, label) tuples.

    `total_width` defaults to a full page-width table (6.5in, matching
    where this is used on page 1), but MUST be passed explicitly by
    any caller placing this inside a narrower container -- e.g. the
    page-2 pitcher cards, each of which only has ~3.3in of usable
    width in a side-by-side layout. A real bug here (this always
    rendering at the full 6.5in regardless of context) was pushing the
    second pitcher's entire card off the page -- the grid alone was
    already wider than that whole half of the page, before anything
    below it even had a chance to render.

    Labels are wrapped in Paragraph objects (not plain strings) so a
    long one like "LONGEST OUTING (IP)" wraps onto a second line
    within its own cell instead of overflowing into the next column --
    a plain string has no width constraint of its own and was
    visually running into the neighboring label with no gap between
    them, a real bug seen in an actual generated PDF."""
    label_style = ParagraphStyle(
        "StatGridLabel", fontName="Helvetica", fontSize=6.5, leading=7.5,
        textColor=colors.grey, alignment=1,  # 1 = TA_CENTER
    )
    value_row = [c[0] for c in cells]
    label_row = [Paragraph(c[1], label_style) for c in cells]
    t = Table([value_row, label_row], colWidths=[total_width / cols] * len(cells))
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, 0), 15),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), accent),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, -1), PALE_GREY),
        ("LINEBELOW", (0, -1), (-1, -1), 1, LIGHT_GREY),
    ]))
    return t


def _mini_stat_table(title, rows, accent, styles, label_width=2.6 * inch, value_area_width=3.9 * inch):
    """Small two/three-column stat table used repeatedly inside a
    pitcher card (Advanced Rates, Situational Splits, Platoon Splits)
    -- rows is [[label, value, value2?], ...]; a row with only two
    entries just spans without a second value column.

    Same bug and same fix as _stat_grid just above: the widths here
    used to be hardcoded assuming a full 6.5in page width
    (label_width + value_area_width), but this is only ever placed
    inside a page-2 pitcher card with about 3.3in of usable width --
    real callers must pass narrower explicit widths, which
    _pitcher_card_flowables now does."""
    flowables = [Paragraph(title, ParagraphStyle(
        "MiniTitle", parent=styles["Normal"], fontSize=8, spaceBefore=6, spaceAfter=3,
        textColor=accent, fontName="Helvetica-Bold",
    ))]
    ncols = max(len(r) for r in rows)
    t = Table(rows, colWidths=[label_width] + [value_area_width / (ncols - 1)] * (ncols - 1) if ncols > 1 else [label_width])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
        ("FONTNAME", (1, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, PALE_GREY),
    ]))
    flowables.append(t)
    return flowables


def _pitcher_card_flowables(dive, team_name, accent, styles):
    """Everything for one pitcher's half of the page-2 side-by-side
    comparison -- name/team header bar, headline ERA/WHIP/W-L, a row
    of hero-stat callouts, then Advanced Rates / Situational Splits /
    Platoon Splits sub-tables. Every field is read straight from
    build_pitcher_deep_dive's already-computed values -- nothing here
    calculates a stat itself, this function only lays them out."""
    name_style = ParagraphStyle("CardName", parent=styles["Heading2"], fontSize=15,
                                 textColor=colors.white, spaceAfter=0, spaceBefore=0)
    team_style = ParagraphStyle("CardTeam", parent=styles["Normal"], fontSize=9,
                                 textColor=colors.white, spaceAfter=0)
    headline_style = ParagraphStyle("CardHeadline", parent=styles["Normal"], fontSize=12,
                                     spaceBefore=8, spaceAfter=2, fontName="Helvetica-Bold")

    if not dive:
        header = Table([[Paragraph("No Starter Selected", name_style)]], colWidths=[3.35 * inch])
        header.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent), ("TOPPADDING", (0, 0), (-1, -1), 10),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 10), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
        return [header]

    row, adv = dive["row"], dive["adv"]
    name = row.get("fullName") or "(name unavailable)"
    bio = dive.get("bio") or {}
    bio_style = ParagraphStyle("CardBio", parent=styles["Normal"], fontSize=8, textColor=colors.white, spaceBefore=1)

    # Team logo sits inline right before the team name -- a tiny (2-3
    # word) nested table rather than an <img> tag inside the Paragraph,
    # since reportlab Paragraph markup's <img> support wants a real
    # file path, not the raw in-memory bytes _fetch_image_bytes
    # already downloaded this into.
    logo_img = _image_flowable(dive.get("team_logo"), 0.22 * inch, 0.22 * inch)
    if logo_img:
        team_line = Table([[logo_img, Paragraph(team_name, team_style)]], colWidths=[0.28 * inch, 3.07 * inch])
        team_line.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
    else:
        team_line = Paragraph(team_name, team_style)

    left_col = [Paragraph(name, name_style), Spacer(1, 2), team_line,
                Paragraph(f"Bats {bio.get('bats') or '?'} / Throws {bio.get('throws') or '?'}", bio_style)]

    headshot_img = _image_flowable(dive.get("headshot"), 0.85 * inch, 0.85 * inch)
    right_col = [headshot_img] if headshot_img else [Spacer(1, 1)]

    header = Table([[left_col, right_col]], colWidths=[2.45 * inch, 0.9 * inch])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (0, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 8),
    ]))

    flowables = [header, Spacer(1, 6)]
    flowables.append(Paragraph(
        f"{stats.fmt2(row.get('era'))} ERA &nbsp;&middot;&nbsp; {stats.fmt2(row.get('whip'))} WHIP "
        f"&nbsp;&middot;&nbsp; {row.get('wins', 0) or 0}-{row.get('losses', 0) or 0}",
        headline_style,
    ))
    flowables.append(_stat_grid([
        (row.get("inningsPitched") or "--", "IP"),
        (str(dive.get("starts") or 0), "STARTS"),
        (str(dive.get("quality_starts") or 0), "QUALITY STARTS"),
        (stats.fmt1(dive.get("longest_outing")) if dive.get("longest_outing") else "--", "LONGEST OUTING (IP)"),
    ], accent, total_width=3.3 * inch))
    flowables.append(Spacer(1, 4))
    last_game_style = ParagraphStyle("LastGame", parent=styles["Normal"], fontSize=8.5,
                                      spaceBefore=2, spaceAfter=2)
    rest = dive.get("days_rest")
    rest_note = ""
    if rest is not None:
        rest_note = f" &nbsp;&middot;&nbsp; last pitched {rest} day{'s' if rest != 1 else ''} ago"
    flowables.append(Paragraph(
        f"<b>Last Outing:</b> {_fmt_pitching_line(dive.get('last_game'))}{rest_note}", last_game_style,
    ))

    flowables += _mini_stat_table("Advanced Rates", [
        ["K/9", stats.fmt2(adv.get("k9"))],
        ["BB/9", stats.fmt2(adv.get("bb9"))],
        ["HR/9", stats.fmt2(adv.get("hr9"))],
        ["K%", stats.fmt_pct(adv.get("k_pct"))],
        ["BB%", stats.fmt_pct(adv.get("bb_pct"))],
        ["K-BB%", stats.fmt_pct(adv.get("k_minus_bb_pct"))],
        ["FIP", stats.fmt2(adv.get("fip"))],
        ["GB% / FB%", f"{stats.fmt_pct(adv.get('gb_pct'))} / {stats.fmt_pct(adv.get('fb_pct'))}"],
        ["BABIP Against", stats.fmt3(adv.get("babip_against"))],
        ["R/G Allowed", stats.fmt2(dive.get("runs_per_game"))],
        ["Scoreless Streak", f"{dive.get('scoreless_streak') or 0}G"],
    ], accent, styles, label_width=1.9 * inch, value_area_width=1.4 * inch)

    fp = dive.get("first_pitch") or {}
    if fp.get("bf"):
        flowables += _mini_stat_table("First-Pitch Tendency", [
            ["First Pitch Strike%", stats.fmt_pct(fp.get("first_pitch_strike_pct"))],
            ["First Pitch Ball%", stats.fmt_pct(fp.get("first_pitch_ball_pct"))],
        ], accent, styles, label_width=1.9 * inch, value_area_width=1.4 * inch)

    home, away, risp = dive.get("home"), dive.get("away"), dive.get("risp")
    split_rows = [["Split", "AVG", "ERA"]]
    if home and home.get("bf"):
        split_rows.append(["Home", stats.fmt3(home.get("avg_against")), stats.fmt2(home.get("era"))])
    if away and away.get("bf"):
        split_rows.append(["Away", stats.fmt3(away.get("avg_against")), stats.fmt2(away.get("era"))])
    if risp and risp.get("bf"):
        split_rows.append(["RISP", stats.fmt3(risp.get("avg_against")), stats.fmt2(risp.get("era"))])
    if len(split_rows) > 1:
        flowables += _mini_stat_table("Situational Splits", split_rows, accent, styles,
                                       label_width=1.5 * inch, value_area_width=1.8 * inch)

    platoon = dive.get("platoon") or {}
    plat_rows = [["Split", "AVG Against"]]
    if platoon.get("vsLeft") and platoon["vsLeft"].get("bf"):
        plat_rows.append(["vs LHB", stats.fmt3(platoon["vsLeft"].get("avg_against"))])
    if platoon.get("vsRight") and platoon["vsRight"].get("bf"):
        plat_rows.append(["vs RHB", stats.fmt3(platoon["vsRight"].get("avg_against"))])
    if len(plat_rows) > 1:
        flowables += _mini_stat_table("Platoon Splits", plat_rows, accent, styles,
                                       label_width=1.9 * inch, value_area_width=1.4 * inch)

    return flowables


def _rec_field(team, key, fmt=None):
    r = team.get("record")
    if not r or r.get(key) is None:
        return "--"
    v = r[key]
    return fmt(v) if fmt else v


def _line_field(section, team, key, fmt=None):
    d = team.get(section)
    if not d or d.get(key) is None:
        return "--"
    v = d[key]
    return fmt(v) if fmt else v


def _standings_field(team):
    s = team.get("standings")
    if not s:
        return "--"
    label = f"{_rank_label(s['rank'])} of {s['of']}"
    gb = s.get("games_behind")
    if gb is not None and gb > 0:
        label += f", {gb:g} GB"
    return label


def _leader_field(team, key):
    l = (team.get("leaders") or {}).get(key)
    if not l:
        return "--"
    return f"{l['name']} ({l['value']})"


def _fmt_h2h_line(g):
    """'Jul 3 (H): W 6-2' style summary of one head-to-head game --
    reads the same game_results row team_schedule.py already builds
    for the season record (date/opponent/score/result), just not
    thrown away after being counted toward the W-L tally."""
    date_str = g.get("date") or ""
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        d = f"{parsed.strftime('%b')} {parsed.day}"
    except (ValueError, TypeError):
        d = date_str or "?"
    loc = "H" if g.get("is_home") else "A"
    return f"{d} ({loc}): {g.get('result')} {g.get('team_runs')}-{g.get('opp_runs')}"


def render_pdf(matchup):
    """Returns a BytesIO containing the finished, fixed two-page PDF
    (page 1 highlight sheet, page 2 starting-pitcher deep dive) --
    margins, font sizes, and the two-column per-team card layout are
    all tuned to fit that exactly; see the module docstring for how."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.45 * inch, bottomMargin=0.45 * inch,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MatchupTitle", parent=styles["Title"], fontSize=17, spaceAfter=2, leading=20)
    sub_style = ParagraphStyle("MatchupSub", parent=styles["Normal"], fontSize=8.5,
                                textColor=colors.grey, spaceAfter=8)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, spaceBefore=8,
                         spaceAfter=3, textColor=BRAND_RED)
    h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=9, spaceBefore=4, spaceAfter=2)
    body = ParagraphStyle("Body", parent=styles["Normal"], fontSize=8.5)
    footnote = ParagraphStyle("Footnote", parent=styles["Normal"], fontSize=6.5,
                               textColor=colors.grey, spaceBefore=2, spaceAfter=4)

    a, b = matchup["team_a"], matchup["team_b"]
    # Each team's own logo-derived color when available (see
    # _dominant_logo_color), falling back to the generic brand accent --
    # computed once here so page 1's team columns and page 2's pitcher
    # cards use the exact same color per team rather than each picking
    # independently.
    accent_a = (a.get("starter_deep_dive") or {}).get("team_color") or BRAND_RED
    accent_b = (b.get("starter_deep_dive") or {}).get("team_color") or BRAND_NAVY
    story = []

    story.append(Paragraph(f"{a['name']} vs {b['name']}", title_style))
    story.append(Paragraph(f"Broadcast Key Notes &middot; Generated {matchup['generated']}", sub_style))

    # ---------------------------------------------- team comparison --
    story.append(Paragraph("Team Comparison", h2))
    rows = [["", a["name"], b["name"]]]
    rows.append(["Record", f"{_rec_field(a, 'wins')}-{_rec_field(a, 'losses')}", f"{_rec_field(b, 'wins')}-{_rec_field(b, 'losses')}"])
    rows.append(["Win %", _rec_field(a, "win_pct", lambda v: f"{v:.3f}"), _rec_field(b, "win_pct", lambda v: f"{v:.3f}")])
    rows.append(["Current Streak", _rec_field(a, "current_streak"), _rec_field(b, "current_streak")])
    rows.append(["Run Differential", _rec_field(a, "run_differential", lambda v: f"{v:+d}"), _rec_field(b, "run_differential", lambda v: f"{v:+d}")])
    rows.append(["Pythagorean Win %", _rec_field(a, "pythagorean_win_pct", lambda v: f"{v:.3f}"), _rec_field(b, "pythagorean_win_pct", lambda v: f"{v:.3f}")])
    rows.append(["Team AVG", _line_field("batting", a, "avg", lambda v: f"{v:.3f}"), _line_field("batting", b, "avg", lambda v: f"{v:.3f}")])
    rows.append(["Team OPS", _line_field("batting", a, "ops", lambda v: f"{v:.3f}"), _line_field("batting", b, "ops", lambda v: f"{v:.3f}")])
    rows.append(["Runs / Game", _line_field("batting", a, "runs_per_game", lambda v: f"{v:.2f}"), _line_field("batting", b, "runs_per_game", lambda v: f"{v:.2f}")])
    rows.append(["Team ERA", _line_field("pitching", a, "era", lambda v: f"{v:.2f}"), _line_field("pitching", b, "era", lambda v: f"{v:.2f}")])
    rows.append(["Team WHIP", _line_field("pitching", a, "whip", lambda v: f"{v:.2f}"), _line_field("pitching", b, "whip", lambda v: f"{v:.2f}")])
    rows.append(["Standings", _standings_field(a), _standings_field(b)])
    t = Table(rows, colWidths=[1.7 * inch, 2.65 * inch, 2.65 * inch])
    t.setStyle(_table_style())
    story.append(t)

    # ---------------------------------------------- team leaders at a glance --
    story.append(Paragraph("Team Leaders at a Glance", h2))
    lead_rows = [["", a["name"], b["name"]]]
    for key, label in (("hr", "HR"), ("rbi", "RBI"), ("sb", "SB"), ("sv", "SV"), ("so", "SO")):
        lead_rows.append([label, _leader_field(a, key), _leader_field(b, key)])
    lt = Table(lead_rows, colWidths=[0.6 * inch, 3.15 * inch, 3.15 * inch])
    lt.setStyle(_table_style())
    story.append(lt)

    # ---------------------------------------------------- head-to-head --
    h2h = a.get("head_to_head")
    if h2h and h2h["games"]:
        plural = "s" if h2h["games"] != 1 else ""
        if h2h["team_a_wins"] == h2h["team_b_wins"]:
            h2h_text = f"Season series is tied {h2h['team_a_wins']}-{h2h['team_b_wins']} ({h2h['games']} game{plural} played)."
        else:
            if h2h["team_a_wins"] > h2h["team_b_wins"]:
                leader, w, l = a["name"], h2h["team_a_wins"], h2h["team_b_wins"]
            else:
                leader, w, l = b["name"], h2h["team_b_wins"], h2h["team_a_wins"]
            h2h_text = f"{leader} leads the season series {w}-{l} ({h2h['games']} game{plural} played)."
    else:
        h2h_text = "No completed games between these two teams found this season."
    story.append(Paragraph(f"<b>Head-to-Head:</b> {h2h_text}", body))

    if h2h and h2h.get("results"):
        series_line = " &nbsp;&middot;&nbsp; ".join(_fmt_h2h_line(g) for g in h2h["results"])
        story.append(Paragraph(series_line, footnote))

    # ------------------------------------------- per-team column cards --
    # Batters to Watch / Pitching Matchup / Bullpen Availability used to
    # each be their own full-width section repeated once per team (6
    # separate blocks stacked vertically) -- that's what pushed page 1
    # onto a second sheet. Laying the same data out as two side-by-side
    # team columns (same pattern already used for the page-2 pitcher
    # cards below) uses the page's full width for both teams at once
    # instead of only ever using half of it, which is what actually
    # buys back the vertical space needed to fit on one page.
    col_h3 = ParagraphStyle("ColH3", parent=styles["Heading3"], fontSize=9.5, spaceBefore=2, spaceAfter=2,
                             textColor=colors.white)
    col_section = ParagraphStyle("ColSection", parent=styles["Normal"], fontSize=7.5, spaceBefore=5, spaceAfter=1,
                                  textColor=BRAND_RED, fontName="Helvetica-Bold")
    col_footnote = ParagraphStyle("ColFootnote", parent=styles["Normal"], fontSize=6, textColor=colors.grey,
                                   spaceAfter=2)
    col_body = ParagraphStyle("ColBody", parent=styles["Normal"], fontSize=7, spaceAfter=2)

    def team_column(team, accent):
        name_hdr = Table([[Paragraph(team["name"], col_h3)]], colWidths=[3.4 * inch])
        name_hdr.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), accent), ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        flow = [name_hdr]

        # Batters to Watch (trimmed to the columns that fit a 3.4in card:
        # AVG/OPS/HR/RBI/Last Game -- OBP, SLG, and QPA% are still on
        # this app's own player pages for anyone who wants the detail).
        flow.append(Paragraph("Batters to Watch (Season OPS)", col_section))
        bt_rows = [["Batter", "AVG", "OPS", "HR", "RBI", "Last Game"]]
        for entry in team["batters_to_watch"]:
            r = entry["row"]
            last_game_text = _fmt_batting_line(entry.get("last_game"))
            streak = entry.get("hit_streak") or 0
            if streak >= 2:
                last_game_text += f" ({streak}G hit streak)"
            bt_rows.append([
                r.get("fullName", ""), stats.fmt3(r.get("battingAvg")), stats.fmt3(r.get("ops")),
                r.get("homeRuns", 0) or 0, r.get("rbi", 0) or 0, last_game_text,
            ])
        if len(bt_rows) > 1:
            bt = Table(bt_rows, colWidths=[0.8 * inch, 0.4 * inch, 0.4 * inch, 0.26 * inch, 0.3 * inch, 1.24 * inch])
            bt.setStyle(_table_style(header_only=True, font_size=6.5, padding=1.5))
            flow.append(bt)
        else:
            flow.append(Paragraph(f"No batters with {MIN_PA_FOR_LEADER}+ PA yet.", col_body))

        # Pitching Matchup (trimmed to ERA/WHIP/SO/BB/Last Outing --
        # scoreless streak and days rest are both still on page 2's
        # full deep dive for the actual starter).
        flow.append(Paragraph("Pitching Matchup", col_section))
        sub_label = "Today's Starting Pitcher" if team.get("has_selected_starter") else f"Team ERA Leaders (min {MIN_IP_FOR_LEADER} IP)"
        flow.append(Paragraph(sub_label, col_footnote))
        pit_rows = [["Pitcher", "ERA", "WHIP", "SO", "BB", "Last Outing"]]
        for entry in team["pitchers_to_watch"]:
            r = entry["row"]
            pit_rows.append([
                r.get("fullName") or "(name unavailable)", stats.fmt2(r.get("era")), stats.fmt2(r.get("whip")),
                r.get("strikeoutsPitching", 0) or 0, r.get("walksAllowed", 0) or 0,
                _fmt_pitching_line(entry.get("last_game")),
            ])
        if len(pit_rows) > 1:
            pt = Table(pit_rows, colWidths=[0.85 * inch, 0.4 * inch, 0.4 * inch, 0.3 * inch, 0.3 * inch, 1.15 * inch])
            pt.setStyle(_table_style(header_only=True, font_size=6.5, padding=1.5))
            flow.append(pt)
        elif team.get("has_selected_starter"):
            flow.append(Paragraph("Selected starter has no stat line on record yet.", col_body))
        else:
            flow.append(Paragraph(f"No pitchers with {MIN_IP_FOR_LEADER}+ IP yet.", col_body))

        # Bullpen Availability (capped to the 4 most recent arms so a
        # deep bullpen can't push a card taller than the other side's).
        flow.append(Paragraph(f"Bullpen Availability (Last {pitching_splits.BULLPEN_AVAILABILITY_DAYS} Days)", col_section))
        bp = (team.get("bullpen_availability") or [])[:4]
        if bp:
            bp_rows = [["Pitcher", "Last Pitched", "Pitches"]]
            for e in bp:
                days_ago = e["days_ago"]
                when = "Today" if days_ago == 0 else ("Yesterday" if days_ago == 1 else f"{days_ago}d ago")
                bp_rows.append([e.get("name") or "(name unavailable)", when, e.get("pitches") or 0])
            bpt = Table(bp_rows, colWidths=[1.7 * inch, 1.0 * inch, 0.7 * inch])
            bpt.setStyle(_table_style(header_only=True, font_size=6.5, padding=1.5))
            flow.append(bpt)
        else:
            flow.append(Paragraph(
                f"No pitchers with an appearance in the last {pitching_splits.BULLPEN_AVAILABILITY_DAYS} days on record.",
                col_body,
            ))
        return flow

    two_col_p1 = Table([[team_column(a, accent_a), team_column(b, accent_b)]], colWidths=[3.55 * inch, 3.55 * inch])
    two_col_p1.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 10),
        ("LEFTPADDING", (1, 0), (1, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    story.append(two_col_p1)

    # ============================================================ page 2 --
    # Full-page starting-pitcher deep dive, side by side -- built as a
    # single 2-column Table whose cells each hold a full list of
    # flowables (see _pitcher_card_flowables), which is the standard
    # reportlab way to lay out two independent "cards" next to each
    # other rather than as one shared table structure.
    story.append(PageBreak())
    page2_title = ParagraphStyle("Page2Title", parent=styles["Title"], fontSize=16, spaceAfter=2, leading=19)
    page2_sub = ParagraphStyle("Page2Sub", parent=styles["Normal"], fontSize=8.5,
                                textColor=colors.grey, spaceAfter=10)
    story.append(Paragraph("Starting Pitcher Matchup", page2_title))
    story.append(Paragraph(
        f"{a['name']} vs {b['name']} &middot; every number pulled straight from this app's own "
        f"pitching analytics -- nothing computed fresh for this page.",
        page2_sub,
    ))
    story.append(HRFlowable(width="100%", thickness=1.2, color=BRAND_RED, spaceAfter=10))

    card_a = _pitcher_card_flowables(a.get("starter_deep_dive"), a["name"], accent_a, styles)
    card_b = _pitcher_card_flowables(b.get("starter_deep_dive"), b["name"], accent_b, styles)
    two_col = Table([[card_a, card_b]], colWidths=[3.5 * inch, 3.5 * inch])
    two_col.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("LEFTPADDING", (1, 0), (1, 0), 12), ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    story.append(two_col)

    doc.build(story)
    buf.seek(0)
    return buf
