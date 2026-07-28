# CBL Stats

## This session: fielding month/day-night splits, count splits (both sides), roster status labels + filter, by-team splits, collapsible dropdowns

- **Fielding monthly and day/night splits** -- new functions in
  `fielding_splits.py`, reusing the same MONTH_NAMES/day-night
  conventions batting splits already use. Tested against constructed
  multi-game data.
- **Count splits (3-2, 1-2, etc.)** -- added to both `splits.py`
  (batting) and `pitching_splits.py` (pitching), via a `_final_count()`
  helper that reconstructs the real ball-strike count each plate
  appearance ended on (including the standard rule that a foul with 2
  strikes doesn't end the at-bat). Rides the same at-bat walk each
  module already does for its other situational splits -- no new
  schedule pass. Tested against 5 constructed pitch sequences plus
  full integration on both sides, including two separate strikeouts on
  the identical count correctly accumulating together, and canonical
  count ordering (0-0 through 3-2) rather than encounter order.
- **Team roster status labels** -- wired last session's `transactions.py`
  into `/team/<name>`, tagging each player with their real roster
  status (Inactive/Injured/Released/Traded/Call-Up) via name match;
  active players stay unlabeled by design. Plus an `?active_only=1`
  filter toggle. Tested end-to-end: badge shows correctly, filter
  correctly hides/shows the right players.
- **Player page splits by team** -- when a player has rows for more
  than one team this season, a collapsible "By Team" breakdown now
  shows each team's line separately, using the already-existing
  unmerged per-team rows (no new data fetch).
- **Collapsible dropdowns for situational splits** -- converted the
  shared `split_table`/`pitch_split_table` macros (used for every
  Monthly/Baserunner/Outs/Inning/Game Type/Two-Strike/Late & Close/
  Count Splits section) to native `<details>/<summary>` elements, no
  JS required. Converting these two macros converts every section that
  uses them at once, rather than touching each individually.


## Yes -- CBL's real transaction feed now backstops "active roster" status

Confirmed the endpoint you shared is real and live: CBL's own front-
office transaction log (Sign/Release/Trade/Inactive List/Injured List/
Call-Up List), a Supabase REST API entirely separate from the main
cbl.ca API this app otherwise uses exclusively. This is a genuinely
better source than the game-roster proxy `get_active_roster_pitchers()`
was using before -- a player can be signed or released without that
lining up with when they last actually appeared in a game.

**New `transactions.py`.** The real complexity here is that this feed
is free-text sentences, not structured fields -- I fetched the live
endpoint directly and built the parser against every distinct phrasing
actually observed in that real data, not assumed ones: `Sign`,
`Release`, both "Move to X List. Name" and the reversed "Move Name(s)
to X List" phrasing, trade acquisitions (filtering "Cash
Considerations" out as a non-player trade component, confirmed
alongside real players in the same transaction), trades to another
league entirely, a distinct "Call-Up List" status, and multi-player
transactions with both comma and ampersand separators ("A, B, C & D"
and "A & B"). Tested all 15 of these exact real patterns directly,
then ran the parser against a larger real sample and manually verified
every resolved status.

**Wired in as a safety-net filter**, not a wholesale replacement: the
broadcast notes' pitcher dropdown (`team_schedule.get_active_roster_pitchers`)
still uses the game-roster proxy as its base (since that's the only
source with real player IDs, which the dropdown needs to actually
work), but now cross-checks each candidate's name against the
transaction feed and excludes anyone whose most recent transaction
shows them released, inactive, injured, or traded away -- even if they
were still on the roster as of their last game. Tested the actual
exclusion case directly (a pitcher released after their last game
appearance is correctly dropped), confirmed a pitcher with no matching
transaction record is correctly NOT filtered (fail-safe, not an
accidental removal), and confirmed the whole thing degrades gracefully
if the external transactions endpoint itself fails or is unreachable
-- the roster-based result still comes back intact either way.

**Real limitations, stated plainly (see transactions.py's own
docstring for the full list):** matching is by player NAME, not ID --
this feed has no player ID field at all, so two players sharing an
exact name would be indistinguishable, and a name spelled slightly
differently between this feed and the main stats feed would simply
fail to match rather than guess. And no "reactivate" transaction type
has been observed in a real fetch of this feed -- if CBL's system
silently reactivates a player without writing a transaction row, this
wouldn't catch it. Both are called out directly in the module rather
than glossed over.

This is now available more broadly too (`transactions.build_roster_status()`,
`transactions.player_status()`) if there's a use for it beyond the
pitcher dropdown -- a "Recent Transactions" section on team pages, for
instance, would be a natural next step if wanted.


## Park detail page: season totals at the top

Added a "Season Totals at This Park" row above the existing rate-stat
line -- Games, Total Runs, Total Hits, Total HR, Total RBI, Total BB,
Total SO, both teams combined. While adding this, caught myself about
to compute Runs/Game with inline division in the template
(`detail.overall.r / detail.overall.games`) -- fixed that to compute
it properly in `stadiums.py`'s `_finalize_batting`, alongside the
`hr_per_game` field that was already computed there, rather than doing
math in the template. Tested against real at-bat data and confirmed
every total matches exactly (1 game, 1 run, 2 hits, 1 HR, 1 RBI, 1 BB,
1 SO from a constructed single/walk/strikeout/HR sequence).


## Park detail pages -- click any Stadiums row for in-depth stats

Each venue on the Stadiums leaderboard now links to `/stadiums/<venue>`,
showing:
- **Home/away split at that specific park** -- does batting with the
  last at-bat there actually play differently than batting on the
  road there (both teams' data combined by role, not one team's own
  split).
- **Pitching at that park** (runs-allowed-based ERA/WHIP/AVG against,
  both staffs combined) -- the park's own scoring environment.
- **Top hitters there this season** (min 10 PA), by AVG and by HR,
  linked to their player pages.
- **The season's actual game log at that venue** (date, score, linked
  to the game page).

Refactored `stadiums.py`'s accumulation logic into shared `_accumulate_batting`/
`_finalize_batting` helpers so the leaderboard's season-wide pass and
the new per-park detail walk share the same logic instead of
duplicating it. The detail page is a second full-season walk, filtered
by venue as it goes -- there's no way to skip non-matching games
without still fetching each one's gameday payload just to check its
venue, so this costs the same as the leaderboard's own pass. Accepted
as a reasonable tradeoff for a detail page someone clicks into
occasionally, not something polled repeatedly.

Tested directly: confirmed the home/away split correctly attributes
by which team is actually home/away in each specific game (not a
fixed team), confirmed a game at a different park is correctly
excluded from another park's detail, confirmed the top-hitters lists
apply the same PA-minimum small-sample guard used elsewhere in this
app, and confirmed a venue name with no completed games returns a
real 404 rather than an all-zeroes page.


## New Stadiums tab -- league-wide batting stats by park

New `/leaderboard/stadiums`, alongside Batting/Pitching/Fielding/Teams
in the top nav. Shows every park's batting line this season (games,
AVG, OBP, SLG, OPS, HR, HR/G, etc.) -- both teams' hitters combined
per venue, since this is a park-factor-style view (how the park itself
plays) rather than a team or player stat.

`stadiums.py` walks the full season schedule **once** (`gamelog._all_games`,
every game in the league), not once per team -- a single game involves
two teams, so a per-team walk would process every game's at-bats twice
for no reason. Games with no identifiable venue are grouped under
"Unknown Venue" rather than dropped, so the page's totals still add up
to the season's real completed-game count instead of quietly
undercounting.

Tested directly: confirmed two games at the same park correctly
accumulate into one row rather than overwrite each other, confirmed a
non-completed (scheduled) game is correctly excluded, confirmed both
teams' hitters from the same game count toward that park's totals, and
verified the route/sorting/nav integration end-to-end.


## Broadcast Key Notes: two critical crash bugs fixed, plus the requested additions

**The PDF was completely broken.** `pitching_splits.build_bullpen_availability` and
`pitching_splits.BULLPEN_AVAILABILITY_DAYS` were referenced but never
actually existed in `pitching_splits.py` -- every single PDF generation
crashed with an `AttributeError`. Confirmed the crash directly, then
built the real function: walks a team's last few days of games (not a
full-season walk per pitcher), reads each game's own box score, and
**excludes whoever started that game** -- a rotation starter isn't a
bullpen arm, and listing them would incorrectly suggest a starter is
"available" out of the pen. Tested this exclusion directly against
constructed data (starter correctly excluded, reliever correctly
included with the right days-ago and pitch count, an appearance
outside the window correctly excluded).

**Second crash, same shape:** `_headshot_and_logo` called
`_fetch_image_reader`, which also never existed (only
`_fetch_image_bytes` did). This one was sneakier -- it only fires when
`build_pitcher_deep_dive` actually has a real pitcher with game history,
so my first regression pass (using empty test data) didn't catch it.
Re-tested with a real pitcher and real game history to confirm this
path specifically, not just that *a* PDF could be generated.

**Team colors** now come from each team's own logo, not a hardcoded
red/navy: `_dominant_logo_color` reads the actual dominant color off
the logo image already being fetched for page 2 (excluding near-white/
near-black pixels, which are almost always background fill or an
outline, not the real team color) -- CBL has no explicit color field
anywhere, so this is the most honest available substitute. Tested with
a synthetic logo (red circle, white background, black outline) and
confirmed it correctly extracts the circle's color, not the background
or outline. Falls back to the generic brand accent if extraction fails
for any reason. Page 1's team columns and page 2's pitcher cards now
share the same computed color per team.

**Hit streaks** added for batters on Batters to Watch, using rolling.py's
own plain streak count (not the momentum score this sheet already
removed) -- shares a game-log walk with the existing "Last Game" lookup
rather than adding a second one. Shown as a compact suffix on the Last
Game cell ("2-4, HR, 2 RBI (5G hit streak)") rather than a new column,
to protect the two-page layout.

Removed the software/methodology disclaimer footer per request.
Confirmed the sheet is still exactly 2 pages with all of the above
included.


## Broadcast Key Notes PDF: momentum removed, full page-2 pitcher deep dive, last-game lines everywhere

**Momentum is gone.** "Batters to Watch" now uses a plain season-OPS
leaderboard, matching how "Pitchers to Watch" already worked by ERA --
same minimum-PA qualifier, just no derived/weighted score. Cleaned up
the now-unused `rolling`/`gamelog` imports (though `gamelog` came back
for a different reason below) and fixed a stale footer sentence that
was still referencing "Momentum Meter" after the fact.

**Page 2 is a full starting-pitcher comparison**, entirely assembled
from functions this app already had -- `analytics.pitching_advanced`
for every rate stat (K%, BB%, K-BB%, FIP, GB%/FB%, BABIP against),
`pitching_splits.py` for situational/platoon splits and quality
starts, a fresh `longest single-game outing` computed from the game
log. Two side-by-side cards (brick red vs. navy, so the two pitchers
are visually distinct at a glance), each with a colored header bar, a
row of boxed hero-stat callouts (IP / Starts / Quality Starts /
Longest Outing), then Advanced Rates / Situational Splits / Platoon
Splits sub-tables. Whichever pitcher is first in "Pitchers to Watch"
on page 1 -- the explicit starter if you picked one, otherwise the top
ERA leader -- automatically gets the page-2 treatment, so the two
pages never disagree about who "the starter" is. The PDF is now
genuinely 2 pages (confirmed via an actual page-count check, not
assumed); page 1 is still deliberately compact to fit its own content
in one page, but the whole document isn't trying to be 1 page anymore
now that page 2 exists on purpose.

**Every featured player now shows their most recent game.** Batters
get a "2-4, HR, 2 RBI"-style line, pitchers get "6.0 IP, 2 ER, 7 K" --
both formatted to only call out what actually happened (a quiet game
just shows the bare line, not a string of zeros). Wired into the
Batters/Pitchers to Watch tables on page 1 and the page-2 deep dive.
Refactored the pitcher-side lookup so the scoreless-streak and
last-outing data share a single game-log walk instead of two separate
ones -- this was already a pattern used elsewhere in the app
(broadcast_overlay.py's own comments on the same tradeoff), just
hadn't been applied here yet.

Tested all of this against realistic constructed data end-to-end:
confirmed the batter/pitcher last-game lines format correctly against
real at-bat sequences, confirmed page 2 gracefully shows "No Starter
Selected" for a team with no qualifying pitcher rather than crashing,
and confirmed the full document renders as 2 pages with both the
selected-starter and auto-ERA-leader paths.


## Broadcast Key Notes PDF: select starting pitchers, scoped to the active roster

The Compare page's PDF form now has starting-pitcher dropdowns for
both teams, populated dynamically once you pick a team. When a
specific pitcher is selected, that PDF replaces the auto season-ERA-
leader pick for that team entirely -- an ERA leader who isn't even
pitching today isn't useful on a broadcast sheet, so an explicit
selection always wins. Leaving a pitcher on "Auto" keeps the exact
same ERA-leader behavior as before (fully backward compatible; no
change if you don't touch the new dropdowns).

**"Active roster" is scoped honestly.** CBL doesn't expose a dedicated
current-roster endpoint, so `team_schedule.get_active_roster_pitchers()`
uses the closest real proxy: the roster CBL itself attached to that
team's most recent game. That's a genuine, meaningful distinction from
season stat rows -- a pitcher released in May still has a stat line
in July, but wouldn't show up on the roster from the team's last
game. New `/api/team/<team_name>/active-pitchers` endpoint powers the
dropdown.

Handles a real edge case: if the selected starter hasn't recorded any
innings yet this season (e.g. a recent call-up), the PDF still shows
them by name (resolved from the roster, not season stats) with `---`
for ERA/WHIP rather than silently dropping them for having no stat
line -- an operator who explicitly picked that pitcher should see them
acknowledged, not disappear.

Tested all three states (no selection / a selection with stats / a
selection with none) directly, confirmed the one-page PDF guarantee
still holds, and verified the new dropdown correctly returns only
pitchers from a team's most recent game roster -- specifically
confirmed a pitcher only seen in an *older* game (implying they've
since left) is correctly excluded.


## Broadcast overlay: Quality PA% gauge -- a stat that was already being computed and thrown away

You asked for a genuinely obscure stat, and the best answer turned out
to be something already sitting in the backend: `build_batter_situational_splits`
was calling `splits.build_player_splits()` -- which returns three
things (sections, quality_pa, batted_ball_profile) -- and discarding
the second and third with `sections, _, _ =`. Quality PA% (and the
batted-ball type/direction profile) were being fully computed on every
single poll of this page and then thrown away.

Quality PA% isn't a hits/AVG stat at all -- a plate appearance counts
as "quality" if it's a hit, walk, HBP, a hard-hit ball even when it's
an out, a sacrifice fly/bunt or any PA that scored a run, a 6+ pitch
battle, or fighting back from an 0-2 count. A .200 hitter can still be
running quality at-bats every time up; this is the number that shows
it, and it's exactly the kind of thing a broadcast audience wouldn't
expect a stats page to have. New 4th chart block in the bottom bar: a
gauge (donut with the percentage in the center) reusing the existing
proven donut-chart renderer, plus the count it's built from.

Since this data was already being computed as part of the same walk
build_batter_situational_splits needs anyway for RISP/2-strikes/etc,
exposing it cost nothing extra -- not a second pass over the games,
just capturing a return value that was being discarded.

Tested the underlying computation directly (a 4-PA sequence with a
hit, a walk, a quick 3-pitch strikeout, and a 7-pitch battle correctly
comes back as 3-of-4 quality, correctly excluding only the quick
strikeout) and the gauge math itself (percentages round correctly,
0% and 100% render without breaking) before wiring it end-to-end.


## Broadcast overlay: added a third chart to the bottom bar (and the pitcher panel)

New horizontal bar chart of the AVG splits (vs LHP/RHP, RISP, Bases
Empty, 2 Strikes, Late & Close for batters; vs LHB/RHB, RISP, Bases
Empty for pitchers) -- sits alongside the two existing donut charts in
the bottom bar, and got added to the pitcher panel too since it's the
exact same function reused, at zero extra backend cost. This uses data
that was already flowing through both cards (the same `platoon`/
`situational` objects the existing text tables already render from) --
purely a new visual on data the backend was already computing, not a
new feature needing its own fetch.

Bar length is relative to a fixed scale ceiling (.500), not each
chart's own max value, so bars stay comparable across different
players and polls rather than silently rescaling. Tested the actual
scaling/clamping math directly (a value at exactly half the ceiling
renders at 50% width, a value exceeding the ceiling clamps to 100%
instead of overflowing, missing data is excluded rather than rendered
broken) before trusting it, then verified both panels render the chart
correctly end-to-end.


## Umpire Splits: added strikeouts and walks for both batters and pitchers

Both the batting and pitching Umpire Splits tables were already
tracking `so`/`bb` in their underlying totals (`gamelog.py`'s
`BOX_COUNTING_KEYS` and `pitching_splits.py`'s `COUNTING_KEYS` both
already include them) -- they just weren't exposed as columns in the
template. Added `BB`/`SO` columns to both tables. Tested end-to-end
with a real-shaped scenario (one strikeout, one walk, one specific
umpire) and confirmed both counts show up correctly on both the
batter's and pitcher's umpire splits rows.


## Validated against a real, live in-progress game -- two real corrections

You shared an actual live game's payload (Brantford Red Sox vs.
London Majors, bottom 3rd, confirmed `status: "in_progress"` right
then), which let me test against the real thing instead of just
constructed scenarios. Two things changed as a result -- plus a
correction after the fact, worth including here rather than editing
away:

**`currentAtBat` was null in the sample, but that turned out to be a
specific edge case, not proof it's dormant in general.** The payload's
`outs: 0, balls: 0, strikes: 0` and `currentInningTracking` (0 pitches
thrown that half-inning) show the snapshot landed at the exact start
of the half-inning -- the gap *between* at-bats, where there's
arguably nothing "current" to describe yet. My first pass concluded
`currentAtBat` "stays null even during genuinely live play," which
overclaimed what one between-at-bats sample actually shows. Whether it
populates once a batter is actually mid-count is still unconfirmed.
The fallback path (`currentBatterIndex` + lineup) is verified correct
regardless -- tested directly against this exact real data and it
resolved to Cassidy Watt, the actual batter due up -- so nothing about
the *code* was wrong, just how confidently the docstring described
`currentAtBat`'s real-world behavior. Toned that down to reflect what
was actually confirmed.

**Team logos are real, confirmed fields** — I told you when scoping
the Now Pitching panel that there was no data source for logos. That
was wrong; your payload has `homeTeamLogoUrl`/`awayTeamLogoUrl` right
at the top level (also duplicated under `homeTeamAssets.logoUrl`).
Added them to the scoreboard panel next to each team name, tested
against the real URLs from your payload.


## Broadcast overlay: left panel added -- worth flagging the shape tradeoff

New left-side panel: ball/strike count (as dots, same visual language
as the existing outs indicator), a base-runner diamond with occupied
bases highlighted and runner names resolved from the game's own
roster data, and — as a bonus once base-runner data was already being
extracted — the current at-bat's own pitch-by-pitch sequence (ball/
strike/foul chips), when the live feed's `currentAtBat.pitches`
carries one.

Worth being explicit about one thing: this changes the overlay's
actual shape. It started as a deliberate **L** (right panel + bottom
panel, open video everywhere else) specifically so the video would
show through the majority of the frame. Adding a left panel makes it
closer to a **U** — three sides now carry graphics, less open video
area in the middle. That's a real tradeoff, not just an addition;
worth keeping in mind if the open video area turns out to matter more
than expected once this is actually running over a live stream.

The current at-bat pitch sequence has one honest caveat: `currentAtBat`
itself is confirmed non-null only while an at-bat is genuinely in
progress (verified against a real completed-game payload, where it's
null), but its own `.pitches` sub-field hasn't been independently
verified against an actual live, in-progress game yet — it degrades to
nothing shown if that field turns out to be structured differently
than expected, rather than guessing at a shape.


## Broadcast overlay: Now Pitching panel, momentum replaced with pie charts

Scoped against a reference broadcast graphic to what's actually
achievable with CBL's real data (skipped team logos, "where to
watch," and the live field diagram -- no data source for any of
those; kept everything else that maps to a confirmed real field).

**Now Pitching panel** — new section stacked below the scoreboard in
the right column. Season line (W-L, SV, IP, ERA, WHIP, SO, BB) plus
**today's actual in-game workload** (pitch count, ball/strike split),
pulled from this specific game's own `playerPitchingStats` — not a
season aggregate. Throwing hand comes from the player analytics feed's
`player.throws` field.

**Momentum meter removed, replaced with two donut charts** filling the
same space in the batter panel:
- **Last-50-PA outcome breakdown** (Hits/HR/BB+HBP/SO/Other Out) — a
  *bounded* walk that stops once it's collected enough plate
  appearances, not a full-season scan, since this page polls every
  couple of seconds and a full-season walk on every poll would add up.
- **Contact Quality** (hard/medium/soft) — pulled from CBL's own
  precomputed analytics feed, which is already being fetched for
  platoon splits anyway, so this adds zero extra network calls rather
  than requiring its own full-season batted-ball walk.

Built the donut charts as plain SVG (stroke-dasharray arcs) rather
than pulling in a charting library, to keep this self-contained for
an OBS browser-source context. Verified the arc math directly before
trusting it (a 50/50 split produces exactly equal arc lengths, unequal
segments sum to exactly the full circle, empty data falls back to a
neutral ring instead of a broken chart) — not just that it looked
right, the actual geometry.


## Broadcast overlay: adjustable delay for when the stream lags the scorekeeping

Added `?delay=N` (seconds) to `/broadcast/overlay`. The page now polls
CBL's live feed frequently in the background (every 2 seconds) and
buffers what it sees with a timestamp, but always *renders* whichever
buffered state was true `N` seconds ago -- not whatever's live right
this second. That's the actual fix for the stream-lag problem: if your
stream runs 15 seconds behind the scorekeeper's real-time entries, set
`delay=15` and the graphic shows what was true when that 15-second-old
moment is what's actually on screen for viewers, instead of a batter
who (from the viewer's perspective) hasn't come up yet.

The delay value can also be changed live without touching the URL or
reloading the page — press **D** to toggle a small input in the
top-left corner (hidden by default, since this composites directly
into the broadcast frame and shouldn't show up on stream unless
explicitly opened). Verified the underlying buffering math directly
with simulated timestamps before trusting it: a 5-second delay
correctly serves the state from ~5 seconds prior, a 0-second delay
shows the live state, and old buffered entries get pruned so the
buffer doesn't grow unbounded over a multi-hour broadcast.


## Live broadcast overlay -- "now batting" graphic, L-shaped around the video

New: `/broadcast/overlay?game=<public_game_id>`. Meant to be used as an
OBS/vMix browser-source layer directly over the actual game video --
the page has no background at all except the two graphic panels
(right-side scoreboard, bottom current-batter card), so the video
shows through everywhere else, forming an L-shape around it.

Deliberately **not** a scorekeeper: it doesn't track pitches, has no
input UI, and holds no state of its own beyond which game to watch.
Every few seconds it re-reads CBL's own live gameday feed fresh
(`broadcast_overlay.py`) and derives everything from that -- if the
game isn't actively being scored on CBL, it says so rather than
showing stale or guessed data. Current batter resolution prefers
`liveGame.currentAtBat` when present (confirmed non-null only while an
at-bat is actually in progress), falling back to
`currentBatterIndex` + the lineup array for whichever side is actually
batting.

The batter card reuses this app's existing analytics/splits/momentum
modules entirely rather than computing anything new -- season line,
OPS+/ISO, the Momentum Meter, hit streak, L/R platoon splits. Headshot
comes from the current game's own roster data (confirmed real field),
not assumed to exist on the season stats row.

Right-side panel: score, inning/half, outs (as dots), and the full
line score. Bottom panel: headshot, name/team/position, AVG/OBP/SLG/
OPS/HR/RBI, and a momentum or hit-streak badge when there's something
worth calling out.

Tested end-to-end against three resolution paths (currentAtBat
present, fallback to index+lineup, game not found at all) plus the
full live-game flow through the actual route.


## The two "bigger" items: Team Situational Hitting and Late & Close Splits

Both now built. Worth noting how this actually went, since it wasn't a
clean start-to-finish build:

- **Late & Close Splits** turned out to already be fully implemented in
  `splits.py` (7th inning or later, score within 2 runs either
  direction) when I went to build it — I don't have full visibility
  into how that happened, so rather than trust it, I reviewed the logic
  line by line and then tested it against a constructed scenario
  designed to catch the subtle part (does a play that itself causes a
  blowout still correctly count as Late & Close, since the score check
  happens *before* that play is applied?). It did, correctly. It just
  wasn't wired into anywhere visible — turns out it didn't need to be:
  the Splits tab already generically iterates every section
  `build_player_splits` returns, so it was already showing up on the
  page with no further work.
- **Team Situational Hitting** was partially wired already too —
  `app.py` and `team_splits.html` were already calling
  `build_team_situational_batting()`, a function that didn't exist yet
  (my first attempt built a differently-named, differently-shaped
  version — a dict of multiple sections instead of the flat list the
  template already expected, mirroring `build_player_splits`' own
  `baserunner_rows` pattern with a trailing "Scoring Position" row).
  Rewrote to match that existing contract exactly rather than change
  the template to match a new shape. "Team w/ Bases Loaded" and "Team
  RISP" are both covered by this single flat list — bases loaded is
  already one of the 8 base-state rows, same as it is for a single
  player.

Both walk the team's schedule **once**, not once per player (crediting
every at-bat from whichever half-inning the team actually bats in,
excluding the opponent's at-bats in the same game) — verified this
exclusion directly, along with a grand slam correctly counting as 4
runs on one play rather than 1, which is exactly why team-level splits
needed a separate accumulator from the per-player one (a single
player's own runs-scored is a 0/1 question; a team's is a real count).


## The two "bigger" items: Team Situational Hitting and Late & Close Splits

**Team Situational Hitting** (new section on the Team Splits page) —
whole-roster RISP/base-state hitting, built as one dedicated pass over
the team's own schedule rather than calling the per-player split
builder once per batter (which would build a full Monthly/Outs/
Inning/Game-Type breakdown per player just to extract one bucket from
each — wasteful). Had to handle run-counting carefully: the existing
per-player accumulator only ever adds 1 run when its "scored" flag is
truthy (correct for one batter, who can only score once on their own
plate appearance), which would silently undercount a team total
whenever multiple runners score on the same play. Verified against a
real grand slam in an uploaded game file — correctly shows all 4 runs
in the Bases Loaded bucket, not capped at 1.

**Late & Close Splits** (new section on the player Splits tab) — a
plate appearance counts if it's the 7th inning or later with the score
within 2 runs either direction, checked using the score as it stood
*before* that specific plate appearance began. This is a
**simplification** of the traditional (Elias Sports Bureau) Late &
Close definition, which also requires the tying run to specifically be
on base, at bat, or on deck — this app doesn't track baserunner
identity closely enough to reconstruct that nuance reliably, so it
uses the simpler inning + score-margin bar instead of pretending to be
the official stat. Built by walking every at-bat in a game
chronologically (not just the observed player's own) and reconstructing
the running score by half-inning. Verified two specific, easy-to-get-
wrong correctness properties directly: (1) an at-bat with the right
inning but the wrong margin is correctly excluded, and (2) when the
observed player's own at-bat is what changes the score (e.g., a
go-ahead home run), the late/close check correctly uses the score as
it stood *before* their own swing, not after — otherwise a player's
biggest hit of the game would incorrectly disqualify itself from
counting as a "late & close" plate appearance.

Known gap, stated plainly: the score reconstruction doesn't include
runs that scored via a wild pitch, passed ball, or steal of home
between at-bats (a gap this app has elsewhere too, e.g. the pitcher
game log before an earlier fix) — so the reconstructed score can
undercount by a run or two in that specific, fairly rare scenario.


## Five new stats, all verified against real data

- **Range Factor** — new column on the Fielding tab, (PO+A)/Games per
  position. Verified against a real player from an uploaded game file
  (2.00, matching hand-calculated).
- **Batted-ball profile** — new Advanced tab section (GB%/LD%/FB%/Pop%,
  Left/Center/Right Field %). Caught a real bug while testing this
  against real data: the per-at-bat `battedBall.type` field is
  snake_case (`fly_ball`, `ground_ball`, `line_drive`) — different
  casing from the camelCase `byType` breakdown used elsewhere in this
  app (a season-aggregate field, a different part of CBL's API). My
  first pass assumed camelCase and would have silently shown 0%
  everywhere while the real data sat in an untracked key. Also
  corrected the initial "Pull %"/"Opposite Field %" labeling to plain
  "Left/Center/Right Field %" before shipping — the original labels
  would have implied a batter-handedness adjustment this doesn't
  actually do.
- **Two-Strike Splits** — new split section showing how a player hits
  once the count reaches 2 strikes, using the same pitch-sequence
  reconstruction already built for "Battling Back." Plugs into the
  Splits tab automatically since that tab already iterates whatever
  sections come back.
- **Pitcher scoreless streak** — current streak of consecutive
  scoreless appearances (total runs, not earned-only — "scoreless"
  means literally zero runs). Unit-tested against three scenarios
  (mid-streak, streak broken by the most recent outing, no data), then
  wired into both the player page and the broadcast PDF's "Pitchers to
  Watch" table.
- **Milestone Watch** (broadcast PDF) — compact "player is N away from
  M" callouts, using amateur-league-scaled thresholds (5/10/15 HR, not
  MLB-scale numbers that would basically never be reached in a ~28-game
  season). This one took real iteration to get right: a global cap of
  4 let one batter's multiple simultaneous near-misses crowd out a
  genuine pitcher milestone entirely (caught by my own test); fixing
  that to separate batting/pitching caps initially still overflowed the
  PDF to 2 pages under a worst-case stress test (confirmed via an
  actual page-count check, not assumed); tightened to 1 batting + 1
  pitching milestone per team, re-verified at exactly 1 page with real
  margin to spare (checked that the closing disclaimer text wasn't
  truncated, not just that the page count read "1").


## Quality PA % (real coaching definition) and a new Fielding tab

**Quality PA %** now uses the full standard coaching definition, not
just the single deep-count-pitches criterion from the first pass. A
plate appearance counts as "quality" if it meets any of: base hit;
walk or HBP; a hard-hit ball even if it's an out (uses the real
per-at-bat `battedBall.quality` field, confirmed "hard"/"medium"/
"soft" in a real payload); a sacrifice fly/bunt, or any PA where a run
scored; a 6+ pitch count; or battling back from an 0-2 count to survive
3+ more pitches (reconstructs the actual pitch-by-pitch ball/strike
count, including the standard rule that a foul with 2 strikes doesn't
end the at-bat). One criterion is deliberately left out: a plain
groundout that merely advances a runner without scoring, since this
feed has no before/after base-state comparison to verify that
specifically — rather than guess, that case isn't counted. Tested
directly against the real uploaded game file for every criterion,
including a dedicated test of the pitch-count reconstruction logic
across four different count scenarios.

**Fielding tab**: new tab on the player page breaking down fielding
stats by position (PO/A/E/TC/FPCT/DP/TP), built from each game's own
per-position breakdown (`playerFieldingStats[pid].positionStats`,
confirmed real structure). A player who changed positions mid-game
gets credited at each position separately for that game, not lumped
under one. Verified end-to-end against a real player from the uploaded
game file, matching the real assists/errors/fielding-% numbers exactly.


## Game Log ERA is now real earned-run ERA, not the runs-allowed approximation

You found it: the real gameday JSON you uploaded has
`liveGame.playerPitchingStats[pid].earnedRuns` as a genuine per-game
field, separate from `.runs` (total runs allowed) — confirmed directly
in your file. The Pitching Game Log previously derived its "ERA*" by
counting `runsScored` across at-bats, which can't distinguish earned
from unearned runs at all (hence the asterisk). Now it uses CBL's own
per-game earned-run total directly whenever it's present.

Also picked up a smaller, related fix while in there: CBL's per-game
total (`.runs`) is authoritative and includes wild-pitch/passed-ball/
steal-of-home runs that this app's own at-bat walk has a known gap on
elsewhere (`gameday.get_extra_scoring_events`, used for the line score
but not previously wired into the pitcher game log) — so both the run
total and the earned-run split got more accurate at once.

Every row, month, and season total now carries `era_exact` — true only
if every constituent game had real per-game earned-run data. The
header just says "ERA" now (no longer "ERA*"), and a `*` appears next
to a specific value only when that exact row/period had to fall back
(no per-game data for at least one game, so every run in it was
treated as earned rather than guessed at) — precise instead of a
blanket caveat on everything. Pitching Park Splits and Umpire Splits
inherit this automatically since they're built from the same game log
rows.

**Scope limit, stated plainly**: this only works at the whole-game
level, because that's the granularity CBL's own earned-run field is
at. The *situational* pitching splits elsewhere on the page (by
baserunner state, inning, outs, etc.) necessarily span partial games —
there's no way to attribute one game's earned-run total to a subset of
its at-bats — so those still use the runs-allowed proxy and correctly
still show "ERA*".


## RA/G, third time: games-count fix was correct, but the numerator was earned runs, not total runs allowed

You cross-referenced our site against CBL's actual standings page
(games, and CBL's own "RA" column) across all nine teams, and the
pattern was consistent and telling: every single team's implied
numerator (site RA/G × real games) came in lower than CBL's official
RA total by a plausible season's worth of unearned runs (16-41 per
team). That's not noise — that's a systematic definitional gap.

Root cause: `team_pitching_stats()` was computing "runs_allowed_per_game"
from `earnedRuns`, but "RA/G" by definition means *total* runs allowed
(earned + unearned, e.g. runs that scored via a fielding error). CBL's
own analytics feed confirms these are two separate numbers
(`official.pitching.runsAllowed` vs `earnedRuns` in a real payload
shared earlier) — ERA correctly uses earned runs only, but RA/G needs
the total.

Fixed: RA/G now sums a total-runs-allowed field (`runsAllowed`,
trying a couple of plausible alternate spellings, falling back to
earned runs only if no such field exists at all rather than crashing).
ERA is untouched — still correctly earned-runs-based. Verified against
your exact real numbers: with earned runs at 217 and total runs
allowed at 253 for a 28-game team, RA/G now comes out to exactly 9.04,
matching CBL's own site precisely.

Three rounds on this one bug, so to be fully transparent about what
each one actually fixed: **round 1** was the games-count denominator
(using one player's own appearances instead of real team games).
**Round 2** was a team-name-spelling mismatch between the schedule and
stats feeds that made the round-1 fix silently fail to apply. **Round
3** (this one) is the numerator itself — earned runs vs total runs
allowed. All three were real, distinct bugs, not the same one
persisting; each one was caught by checking actual output against real
numbers rather than assuming the previous fix covered it.


## RA/G: the first fix was correct but incomplete -- found the actual reason it persisted

The first fix (real team games from the schedule instead of
`max(individual player's own games)`) was the right idea, but the
lookup connecting the two was a plain, un-normalized dict `.get(team)`
call. CBL's schedule data is known to spell team names inconsistently
in this exact league (that's the whole reason `gamelog._normalize()`
exists — the "Chatham-Kent" vs "Chatham Kent" hyphen issue, discovered
earlier in this project). If the season stats feed and the schedule
feed spell a team's name even slightly differently, the lookup misses
silently and falls straight back to the old broken `max()` behavior —
which is almost certainly what was still happening for Chatham-Kent
specifically, given that team's name is already known to have exactly
this inconsistency.

Fixed by keying `team_schedule.count_completed_games_by_team()`'s
result by normalized team name, and adding a `games_for_team()` helper
that normalizes the lookup key the same way — used everywhere
`analytics.py` reads from that dict now, instead of a raw `.get()`.

Verified directly: reproduced the user's exact numbers (253 ER, 28
games) with the team spelled two different ways between the stats rows
and the schedule (mirroring the real Chatham-Kent case), confirmed the
naive un-normalized lookup returns `None` for this exact mismatch
(proving it's a real, reachable failure mode, not a hypothetical), and
confirmed the fixed version correctly returns games=28 and RA/G=9.04
even with the spelling difference in play.


## Broadcast PDF: momentum-based batters, guaranteed one page

Two changes to `broadcast_notes.py`:

- **Batters to Watch now ranks by momentum score**, not season OPS —
  reuses the site's existing Momentum Meter (`rolling.py`: recent OPS
  vs season pace, current hit streak, recent RBI production), the same
  thing already shown on individual player pages. This is explicitly
  entertainment-style, not a rigorous stat (see `rolling.py`'s own
  comments on the weights). Pitchers stay ranked by season ERA — there
  isn't a pitching-side momentum meter built, so this wasn't changed;
  a parallel one would be a reasonably contained follow-up if wanted.
- **Layout rebuilt to guarantee one page**: combined the four
  previously-separate comparison tables into one, tightened margins/
  fonts/spacing throughout. Verified with an actual page-count check
  (not just "it looks like it should fit") using a genuine worst-case
  scenario — both teams fully stocked with qualifying batters and
  pitchers, all with real recent-game data — and confirmed it still
  renders as exactly one page.

Caught my own test-fixture mistake again while verifying this (same
class of error as earlier in this project: hardcoding which half-
inning a mocked at-bat belongs to, rather than matching it to which
team was actually home vs away for that specific game) — worth
flagging since it's an easy mistake to repeat when hand-building
play-by-play test data, not something specific to this feature.


## Four things: a serious team-games bug fix, Runs Created, umpire splits, WAR (skipped)

- **Fixed a real, serious bug**: team-level "per game" rates (Runs
  Allowed/Game, Runs/Game) were using `max(any individual player's own
  games count)` as a stand-in for "games the team played" — which is
  wrong, since no single player appears in every team game (pitchers
  especially). This is exactly what produced the reported RA/G of
  16.69 instead of the correct ~9.04. Added
  `team_schedule.count_completed_games_by_team()` — one cheap pass
  over the schedule, no per-game network calls — and wired it through
  every call site (`analytics.py`'s `team_batting_stats` /
  `team_pitching_stats`, and all four places that call them: the team
  leaderboard, Compare page, team roster page, and the broadcast PDF).
  Reproduced the exact bug with synthetic data matching your numbers
  (253 ER, a 28-game schedule, pitchers who individually appeared in
  fewer games) and confirmed the fix produces exactly 9.04.
- **Runs Created** added to the Team Splits page batting table — this
  was already computed (used on individual player pages), just not
  exposed there yet.
- **Umpire splits** — new "Umpire Splits" tables on both batter and
  pitcher pages, grouped by home plate umpire. You shared the actual
  real payload structure, which was genuinely important: the reliable
  field is `snapshot.setup.umpireAssignments.homePlate` (a direct
  role-to-name mapping), not something inferrable from the plain
  `umpires` list alone (which has no per-entry role in the real data —
  my first attempt would have actually failed to identify the plate
  umpire on this exact real example, since it has 3 unlabeled names).
  Fixed to check `umpireAssignments` first, falling back to the
  `umpires` list's single-entry case for older games that might not
  have the assignments field. Verified directly against your real
  payload structure, plus the fallback path for games without it.
- **WAR**: skipped per your call — a real WAR needs defensive range
  metrics, positional value calibration, and a replacement-level
  baseline from a large player sample, none of which exist for this
  league. Building something and calling it "WAR" without those would
  be misleading rather than just incomplete.


## Batters Faced = 0, confirmed and closed with real data

You shared the actual raw analytics payload for a pitcher, which
settled this for good: CBL's split-level `outcomes` objects (used in
`byBatterHandedness`, `byOpponent`, `byHomeAway`) genuinely have no
`battersFaced` field at all — the count is called `plateAppearances`
there, same field name as the batting side, just reused for a
different meaning depending on context. The season-level `official`
stats object is different again — it really does use `battersFaced`
(confirmed: `official.pitching.battersFaced: 165` in your payload) —
so this was never a single consistent naming convention across the
whole API, just two different parts of the same response using
different field names for a similar concept.

Verified directly against your real payload (not a synthetic guess)
that BF now shows correctly on the player page's platoon splits table,
the vs-opponent table, and the JSON API. Reordered the fallback checks
to try `plateAppearances` first now that it's confirmed, rather than
falling through three dead guesses every time.

Bonus confirmation from the same payload: your `byBatterHandedness`
data really does include `switch` and `unknown` categories alongside
`left`/`right` — which is exactly the scenario the earlier "Batters
Faced silently overwritten" bug fix was guarding against. Good to see
it's not just a hypothetical case.


## Batters Faced still showing 0 after the first fallback fix — added one more candidate

The first fix (trying `battersFaced` / `batters_faced` / `bf`) still
came back 0, which means none of those three guesses matched CBL's
real field name for this specific nested object either. Rather than
keep guessing blindly, here's the one more reasoned candidate worth
trying before this needs a look at the actual raw payload:

We already know `plateAppearances` works correctly for the *batting*
platoon splits — confirmed, no issues there. If CBL reuses the same
generic `outcomes` object shape for both batting and pitching splits
(a very normal thing for an API to do), a pitcher's "batters faced"
might just be stored under that same `plateAppearances` key, not a
separate `battersFaced` field at all. Added `plateAppearances` as a
fourth fallback candidate everywhere BF is read (both template spots
on the player page, the team splits page, and both JSON API
split-builders in `app.py`) and verified directly: simulated CBL's
outcomes object using only `plateAppearances` (no `battersFaced`
variant present at all) and confirmed BF now shows the real number
instead of 0.

**If this still shows 0**, all four guessed field names are wrong, and
continuing to guess isn't a great use of time — the fastest path from
there is pulling up `https://www.cbl.ca/api/stats-api/players/<any
pitcher's id>/analytics?format=json` directly (or however you already
have access to check "venue" fields) and looking at what
`eventAnalytics.pitching.splits.byBatterHandedness[0].outcomes`
actually calls that field. Once we know the real name, it's a one-line
fix everywhere.


## Fixed blank "Batters Faced" in pitching platoon/opponent splits

Root cause: `analytics.py`/`stats.py` already had a defensive fallback
for reading `battersFaced` off the season stats' `advancedPitching`
object (`adv.get("battersFaced") or adv.get("batters_faced") or 0`),
because that field's exact casing was never confirmed against a live
response. The platoon-splits and vs-opponent code — both in `app.py`
and directly in the templates — never got that same treatment; it only
ever tried `battersFaced` with no fallback, in four separate spots
(`player.html`'s two pitching-split tables, `team_splits.html`'s
pitching table, and both JSON API split-builders in `app.py`). If
CBL's real field name for *this specific nested object* is spelled
differently, it silently came back blank instead of falling back —
which is exactly what "spaces not showing Batters Faced" was.

Fixed all four spots with the same `battersFaced or batters_faced or
bf` fallback pattern already established elsewhere in this codebase.
Verified directly: simulated CBL's field as `batters_faced` (the most
likely alternate spelling) and confirmed BF now shows correctly on the
player page's platoon splits table, the vs-opponent table, the team
splits page, and the `/api/player/<id>` JSON endpoint used by the
Google Sheets integrations.


## Park splits, pitcher-vs-team, team splits page, and a broadcast PDF export

Four additions in one batch:

- **Park splits** — new "Park Splits" tables on both batter and pitcher
  pages, grouped by the actual venue CBL's feed provides (confirmed to
  exist as a `venue` field, though the exact key/casing/nesting wasn't
  independently verified against a live response, so `gameday.get_venue()`
  tries several plausible spots and falls back to labeling by home team
  for any specific game missing it — mixed real-venue and fallback rows
  can coexist in the same table without breaking anything). No new
  network calls: both `gamelog.build_park_splits()` and
  `pitching_splits.build_pitcher_park_splits()` just re-bucket the game
  log rows already fetched for the Game Log tab. Verified with a test
  confirming two games at the same real venue correctly combine into
  one row, while a game missing the venue field correctly falls back
  to a team-name label instead of breaking.
- **Pitcher-vs-team splits** — turned out CBL's analytics feed already
  provides `byOpponent` splits for pitchers, the same way it already
  did for batters (just unused until now). New "Pitching: Vs. Opponent"
  table on the player page.
- **Whole-team splits page** — `/team/<name>/splits`, showing every
  player's platoon (L/R) splits on one page instead of clicking into
  each player individually. Deliberately scoped to platoon splits only
  (cheap, from CBL's precomputed feed) rather than situational splits
  like RISP, which would need a full schedule walk per player and make
  a whole-roster page too slow.
- **Broadcast Key Notes PDF** — pick two teams on the Compare page,
  download a 1-2 page PDF (`broadcast_notes.py`, using `reportlab`, a
  pure-Python library with no system dependencies to keep the Render
  deploy simple) with team records, current streaks (new
  `team_schedule.py` streak calculator), head-to-head this season, and
  rule-based "Players to Watch" (top OPS batters / top ERA pitchers
  above a minimum sample size, plus each featured batter's current hit
  streak). Nothing here is invented commentary — it's a fixed selection
  of numbers this app already computes elsewhere, laid out on a page.
  Caught and fixed two real bugs while testing this: a test fixture
  that misattributed which team's runs were which (not an app bug, but
  worth knowing this class of mistake is easy to make when hand-
  building play-by-play test data), and a genuine wording bug where a
  tied head-to-head series incorrectly said one team "leads" instead of
  reporting the tie.


## Fixed a real bug: pitcher (and batter) platoon splits could get silently overwritten

You were right that "vs RHB" looked off. Root cause, found in the JSON
API endpoints built for the Google Sheets integrations (`/api/player/<id>`
and `/api/export/splits` in `app.py`, plus the standalone direct
script's batting splits) — **not** in the main website itself, which
was always fine (it displays whatever handedness category CBL sends,
verbatim, rather than forcing it into a strict left/right bucket).

The buggy pattern: `key = "vsRight" if (s.get("key") or "").lower() ==
"left" else "vsRight"`-style logic treated *anything that wasn't
literally `"left"`* as `"right"`. If CBL's feed ever includes a third
handedness category — a switch-hitter, or any value this app hadn't
anticipated — that entry's stats got written into the exact same
dictionary key as the real "right" data. Since this was a plain
assignment (not an accumulation), whichever entry the loop processed
*last* simply **overwrote** whatever came before it. If a switch-hitter
category appeared after "right" in CBL's array, the real vs-RHB numbers
didn't get blended with bad data — they vanished entirely, replaced
outright by the switch-hitter's stats.

Fixed everywhere this pattern appeared (4 places in `app.py`, 1 in
`google-apps-script-direct/Code.gs`): now explicitly checks for both
`"left"` and `"right"`, and **skips** (doesn't add to either bucket)
anything that's neither, rather than guessing. Verified with a test
reproducing exactly this scenario — a pitcher with left, right, *and*
an unrecognized "switch" category in CBL's own data — confirming
vs-RHB now shows exactly the real "right" entry's numbers, with the
unrecognized category correctly excluded instead of silently
clobbering it.


## `/api/export/splits` — bulk data source for the full Sheets workbook

New endpoint, built for the Google Apps Script "Build Full Team Splits
Workbook" feature (see `google-apps-script/README.md`): every team's
batters and pitchers, season stats plus L/R platoon splits, in **one**
JSON response — `{"teams": {"Team Name": {"batters": [...], "pitchers": [...]}}}`.
Exists specifically so the Sheets workbook builder doesn't need one
HTTP call per player (would be slow and could hit Apps Script's own
execution-time/URL-fetch quotas on a bigger league); this does the
per-player analytics lookups server-side in a single request instead.
Multi-team players (see `player_merge.py`) appear once per team
they've played for, since the point here is "give me a per-team
roster," not a deduplicated league-wide list. Can be slow with a lot
of players (one real network call per player to fetch splits) — meant
to be triggered occasionally from a Sheets menu, not loaded like a
normal page.


## `/version` endpoint — prove whether a Render deploy actually happened

Added because "the deployment isn't updating" is impossible to diagnose
from code alone — I can't see your Render dashboard, your git repo, or
whether a push actually landed. `/version` returns the UTC timestamp of
when the running container image was actually built (baked in at
Docker build time via a new `RUN date ... > build_info.txt` step). Hit
it before and after a deploy:

- **Timestamp doesn't change** → the deploy itself never ran. That's a
  Render/git problem, not a code problem — check, in this order:
  1. Did the push actually land on the branch Render is watching? (`git
     log` on that branch, or check GitHub/GitLab directly)
  2. Is **Auto-Deploy** actually enabled for this service in Render's
     dashboard? (Settings → Build & Deploy)
  3. Check Render's **deploy log** for that push — did the build fail
     silently, or did it not trigger at all?
  4. Are you checking the URL of the *actual* service tied to this
     repo, and not a different/older Render service or a preview URL?
  5. As a hard reset: Render's dashboard has a **"Clear build cache &
     deploy"** option — try that once to rule out a stale cached layer
     on Render's build infrastructure specifically (separate from
     Docker's own layer cache, which already gets correctly invalidated
     by any actual code change — see the Dockerfile comment above the
     `RUN date` step for why).
- **Timestamp changes but the site still looks the same** → the deploy
  is fine, and the issue is somewhere else: browser caching (hard
  refresh), or stale disk-cached game data from before CBL corrected
  something (`cbl_api.clear_cache(include_disk=True)`), not a
  deployment problem at all.


## Fixed the actual root cause of the "tie" bug

You found it: a game was coming out as a tie in this app's data, which
can't happen in real baseball — meaning the actual deciding run wasn't
being counted at all. Root cause: every run-counting function in this
app only ever read `runsScored` off `at_bat_complete` events. A run
that scores **independently of any batter's own plate appearance** —
a wild pitch, a passed ball, a balk, defensive indifference, or a
straight steal of home — has no `at_bat_complete` event at all. It
only exists as a `runner_advance` event with its own `scored: true`
flag, which nothing in this app had ever looked at. A game decided by
exactly that kind of run came out with the wrong score entirely, and
if that made it look tied, `team_schedule.py`'s tie-guard (ties
"shouldn't happen," so it's treated as a data-entry safety valve, not
as "this game legitimately doesn't count") silently dropped the whole
game from that team's record — explaining both the missing game *and*
the win/loss inaccuracy in one shot.

Fixed at the source: `gameday.get_extra_scoring_events()` pulls every
`runner_advance` event with `scored: true`, and both `build_line_score()`
(team score) and `build_batting_box()` (the runner's own individual
runs-scored stat) now add these in. Verified directly: built a game
where the deciding run scores on a wild pitch, confirmed it used to
compute as a false 3-3 tie and now correctly comes out 4-3, and
confirmed `team_schedule.py` now counts it as a real win instead of
silently skipping the whole game.

**One honest residual gap:** a `runner_advance` event has no pitcherId,
so a run scored this way can be credited to the correct team and the
correct runner, but not yet to a specific pitcher's runs-allowed —
that would need inferring who was on the mound at that exact moment
from nearby pitch events, which isn't implemented yet. Pitching stats
(ERA, runs allowed) can therefore still slightly undercount for a
pitcher who allowed a run exactly this way, even though the team-level
score and the batter's own stats are both correct now. Flagged clearly
in `get_extra_scoring_events()`'s docstring if this needs picking up
later.


## Standings, and a real fix for missing-game diagnostics

- **New Standings section** on the Teams page (`/leaderboard/teams`) —
  every team's W-L, Win %, games behind the leader, run differential,
  and Pythagorean Win %, sorted by Win % by default and click-to-sort
  on any column. Built by `team_schedule.build_standings()`, which
  computes every team's record via the same `build_team_season_record()`
  the individual team pages already used, cached in memory for 5
  minutes league-wide (computing all of it walks every team's full
  schedule, so there's no reason to redo that on every page load).
- **Every team's roster page now lists the actual games counted toward
  their record** — date, opponent, score, result, one row per game,
  collapsible — so a wrong-looking record can be checked directly
  against the games this app actually found, instead of taking the
  number on faith.
- **A real fix, not just another guess, for the Chatham-Kent report:**
  the earlier hyphen-normalization fix only handles case/punctuation
  differences in a team *name*. It can't catch a genuinely different
  **status word** for one game (e.g. `"final"` instead of
  `"completed"`) — that kind of entry gets filtered out by
  `team_games()` before any of this app's diagnostics even see it, so
  it was invisible with zero trace. `gamelog.team_schedule_status_breakdown()`
  fixes that specifically: it lists every raw status value seen across
  *all* of a team's schedule entries, regardless of status, and the
  team roster page now shows a banner naming them if anything besides
  `completed` shows up. If Chatham-Kent's record is still off, load
  their team page — if that banner appears, the status value it names
  is the actual bug; if it doesn't appear, the gap is a still-different,
  new failure mode and the "Games counted" list is the next place to
  compare against the known schedule by hand.


## Google Sheets / Apps Script integration + JSON API

Two new endpoints in `app.py` for pulling data out of this app
programmatically — built specifically so a Google Apps Script project
(bound to a Sheet) can call them over HTTP and pull player stats, most
notably L/R platoon splits, straight into cells or a formatted table.
Full setup instructions and the Apps Script project itself are in
`google-apps-script/` (separate from the Flask app — copy those three
files into an Apps Script project bound to whatever Sheet you want this
in, point `CBL_BASE_URL` at your deployed site, done).

- **`GET /api/players?q=<name>`** — lightweight search index
  (`{"players": [{"id","name","team"}, ...]}`), same data source as the
  Compare page's player picker. `q` is optional and does a
  case-insensitive substring match.
- **`GET /api/player/<id>`** — season batting/pitching/fielding stats,
  key advanced stats, and L/R platoon splits (from CBL's own
  `/players/<id>/analytics` endpoint) as JSON. Deliberately lighter
  than the full HTML player page: no game log, rolling stats, or
  momentum, since those need a 30-40-game schedule walk per player,
  which would be too slow for something a Sheets formula might
  recalculate repeatedly. Returns 404 (as JSON) for an unknown ID.

## Multi-team players, and Chatham-Kent's hyphen

Two real bugs, both reported directly:

- **Players who switched teams mid-season only showed their first
  team's stats.** CBL's season stat endpoints return one row per
  player *per team-stint*, so a traded player has two rows with the
  same playerId but different teamName. `_find_player()` grabbed
  whichever came first and silently discarded the rest — meaning the
  player's own page showed only one team's partial stats, and their
  game log only walked that one team's schedule. Fixed with a new
  `player_merge.py`: finds *every* row for a player, sums the counting
  stats, and recomputes rate stats from the combined totals (same
  simplified conventions — no HBP/SF — already used elsewhere in this
  app). Every schedule-walking function (`gamelog.team_games()` and
  everything built on it) now accepts a list of team names instead of
  just one, so a merged player's game log correctly walks every team
  they played for. The player header now links each team separately
  ("Team A / Team B") instead of one broken combined-name link, and a
  note explains the combined stats when this applies.
- **A team's win/loss record was missing a game** (Chatham-Kent
  Barnstormers, 7-18 instead of 7-19). Same schedule-matching bug class
  fixed before, but with a new wrinkle: `_normalize()` only handled
  case and whitespace, not punctuation — "Chatham-Kent" and "Chatham
  Kent" didn't match each other. `_normalize()` now also collapses
  hyphens/underscores/periods to spaces, so compound names like this
  match regardless of which way a given schedule entry happened to
  spell it.

Both verified together: a synthetic traded player whose two teams'
schedule entries include exactly this hyphen mismatch, confirmed their
combined season stats compute correctly and both games show up in
their game log.

## Theme inverted to light, fonts bolder

Swapped the color roles: what was near-black is now a soft warm white
(`#FAF5F1`), and what was off-white text is now a dark warm near-black
(`#201513`) — the dark red accent (`--brick`/`--gold`) is unchanged,
since only black/white were asked to swap. Same layered-gradient
approach as before (soft reddish glow near the top corners, subtle
overall gradient) just inverted in polarity, so it keeps the depth
rather than going flat. Headers (topbar, player/game headers, month
dividers) now use a light gradient with dark text instead of a dark
gradient with light text — same structure, inverted.

Bumped font weights throughout for a bolder overall feel: base body
text moved from regular (400) to medium (500), most labels/nav/table
headers moved to semibold or bold (600–700), and a few high-emphasis
elements (active tab, sort-active header, best-in-comparison cells,
game status tag) now use extra-bold (800). `Anton` (the display font
for the brand/section titles/player names) was already about as heavy
as a font gets, so it's untouched — the weight increase is concentrated
in `Inter` (UI text) and `JetBrains Mono` (stat numbers), which now
both pull in a wider weight range for the bump.

## Game Log coverage warning

Following up on the Yushin Ohta report: the discrepancy wasn't the
row/total math (already fixed and verified) — it's that this app's
**Game Log season totals** and the **Overview tab's season stats** are
two entirely different computations. The Overview tab's AVG/OBP/SLG/OPS
come straight from CBL's official `/stats/batting` (or `/stats/pitching`)
endpoint — that number is correct. The Game Log's own "Season totals"
row is independently built *by this app*, by walking every completed
game found in the schedule feed and adding them up — if that walk
misses even one game (the exact kind of schedule-matching gap already
fixed a few times over: roster listings, status casing, team-name
whitespace), its total will drift from the real season line, even
though the Overview tab right above it is unaffected and still right.

Since there could be more of that same bug class still undiscovered
without live data to test against, `app.py` now compares the number of
games our Game Log actually found against CBL's own official
games-played count on every player page. If they don't match, a clear
banner appears at the top of the Game Log tab (both batting and
pitching) stating exactly how many games are missing, confirming the
Overview tab is unaffected, and showing the official vs. logged AVG
side by side — turning a silent, confusing mismatch into an actionable
signal instead. No banner appears when the counts agree.

## Pitcher game logs, and dropped "as hitter" splits for pitchers

- **Pitching Game Log** — `pitching_splits.build_pitcher_game_log()`
  mirrors `gamelog.build_player_game_log()` but for a pitcher's own
  per-appearance line (BF, IP, H, R, HR, BB, SO, ERA*, WHIP, AVG
  allowed), grouped by month with the same self-consistent
  rows-are-the-source-of-truth totals as the batter version. Shows on
  the Game Log tab, which now appears for any player who's pitched, not
  just hitters. A two-way player sees both their batting and pitching
  game logs on the same tab; a pure pitcher sees just the pitching one.
- **Batting splits/platoon removed for pitchers** — any player who has
  pitched (has a `p_row`) no longer shows a "Splits (as hitter)"
  section, even if they also happen to have some incidental batting
  stats (common in leagues without a DH). Pure hitters are unaffected.

## Hover tooltips for stat abbreviations

Every abbreviated stat label across the site (leaderboards, player
pages, team pages, the Compare page, box scores) now shows its full
name on hover — plain native `<abbr title="...">` tags, so it's just
the browser's built-in tooltip, no JavaScript involved. The full
glossary lives in `glossary.py` as a simple label &rarr; definition
dict; `app.py`'s `abbr` Jinja filter wraps a label in the tag if it
finds an exact match there, and passes it through unchanged otherwise
— so a label that isn't in the glossary yet just displays normally
rather than breaking. Add new terms to `glossary.py` any time a new
abbreviated label shows up in a template; the key has to match the
visible label text exactly (case-sensitive).

## Compare

New "Compare" nav item, `/compare` — search for any number of players
and/or teams (type-ahead picker, no server-side session needed; the
selection lives entirely in the URL as `?players=id1,id2&teams=Team+A,Team+B`,
so a comparison is bookmarkable/shareable). Shows side-by-side tables
for Batting, Pitching, Team Record, Team Batting, and Team Pitching —
whichever sections apply to what's actually selected (a pitcher-only
player just won't have a Batting column filled in, same as everywhere
else in this app). The best value in each row is bolded.

Nothing here is new data — `compare_stats.py` just arranges the same
numbers `analytics.py` and `team_schedule.py` already compute into
side-by-side rows and picks a winner per row (skipped for rows where
"better" isn't a well-defined direction, like BABIP). See that module
for the exact stat list and win-direction per row.

## Bunt singles weren't counting as hits

Root cause of the Hamilton game found: a bunt hit apparently gets its
own distinct outcome value from CBL — `"bunt_single"` — separate from
plain `"single"`, the same way `"sacrifice_bunt"` is already distinct
from a generic ground out. Since `"bunt_single"` wasn't in
`gameday.HIT_OUTCOMES`, it fell through as neither a hit nor a
non-at-bat outcome: the at-bat still counted, but the hit itself
silently didn't. Fixed by adding it to `HIT_OUTCOMES`.

**This one is a reasonable inference, not a directly confirmed fact** —
the exact string CBL uses for a bunt hit hasn't been seen in a raw
payload the way the other outcome values have. If bunt hits are still
missing after this fix, the real string is spelled differently and
needs checking against a live response; see the caveat left directly
in `gameday.py`'s outcome-vocabulary docstring.

## Game-log matching hardened (case/whitespace mismatches)

A user report of one specific game (June 21 @ Hamilton) not showing up
for a player, even after the roster-gating fix above, pointed to a
second real class of bug: `team_games()` and every module that derives
home/away side from a schedule entry (`gamelog.py`, `splits.py`,
`pitching_splits.py`, `team_schedule.py`) compared team names and game
status with exact string equality. A schedule feed is hand-entered
data — a single game with a stray trailing space ("Hamilton Cardinals ")
or different status casing ("Completed" instead of "completed") than
every other game would silently vanish from that team's game log, with
no error, while every other game kept working fine.

Fixed by normalizing (trimmed, case-insensitive) every team-name and
status comparison used for this kind of matching, across all four
modules. This doesn't change what counts as "completed" — a genuinely
different status still won't match — it just stops a harmless
formatting quirk in one schedule entry from silently excluding that
whole game. Verified with a test reproducing exactly this scenario
(trailing whitespace + different status casing on one game) and
confirmed it now shows up correctly with the right opponent and stats.

## Theme softened

Warmed and lightened the black/dark-red palette slightly (base tone is
now a warm near-black, `#14100f`, rather than pure `#0a0808`) and
replaced the flat solid background with a subtle layered gradient — a
soft reddish glow near the top-left and top-right fading into the dark
base, plus a gentle vertical gradient overall — for some visual depth
instead of one flat block of color, which is what was likely reading as
harsh. Headers (topbar, player header, game header, month dividers)
now use a subtle diagonal gradient instead of a single flat tone too,
for the same reason. Text and accent colors are unchanged in hue, just
very slightly toned down for less stark contrast against the lighter
base.



A Baseball-Savant-style stats site for the Canadian Baseball League, built
on the public `cbl.ca` stats API. Includes league leaderboards (batting,
pitching, fielding) and a per-player page with percentile rankings and a
batted-ball profile.

Runs as a normal Flask app, so it needs to run somewhere with network access
to `cbl.ca` — e.g. your Windows server — rather than in a sandboxed
environment.

## Missing-games bug fix (roster-gated box scores)

A user report ("2 games with hits aren't showing up in the game log")
led to a real bug in `gameday.py`'s box-score builders. `build_batting_box`
and `build_pitching_box` computed every player's stats correctly from
the play-by-play, but then **filtered the output by whether that
player's ID happened to appear in that specific game's `roster` array**.
If a roster listing was ever incomplete for a game (a late substitute,
a data sync gap — this app has no way to know why), a player's real,
correctly-computed hits got silently thrown away rather than shown,
which is exactly the reported symptom.

Fixed by deriving team side from each at-bat's own `halfInning` field
("top" = away batting, "bottom" = home batting; the pitcher's side is
the opposite) instead of gatekeeping inclusion on roster membership.
`halfInning` is self-contained in the at-bat record and can't have the
same incompleteness problem an external roster list can. The roster
(`lookup`) is still used for display enrichment (name, position) —
if a player is missing from it, their raw ID is shown as a fallback
name rather than being silently dropped, so a display gap now always
looks like "unknown name" instead of "player disappears entirely."

Every game log row now also carries its `public_game_id`, and the
Date column links directly to that game's full box score / play-by-play
page — see the next section for why.

## Gameday tab removed from nav

The standalone "Gameday" nav link (and its game-ID lookup form) has
been removed, since the natural way to reach a specific game's box
score is now from a player's own Game Log — click any date. The
underlying routes (`/game`, `/game/<public_game_id>`) still work if you
navigate to them directly; only the nav link and the general-purpose
lookup form are gone from the main navigation.

## Game Log totals bug fix

A user-reported screenshot showed a real, reproducible bug: a player's
"May totals" strikeout count didn't match the sum of that month's own
displayed game rows (5 vs. an actual sum of 4), while the season total
was silently consistent with the wrong monthly number instead of the
right one. Two fixes, both in `gamelog.py`:

- **`team_games()` now de-duplicates by public game ID.** If the season
  schedule feed ever lists the same game twice (e.g. a stale leftover
  entry from a reschedule), that game would previously get processed
  twice — silently double-counting its stats into any game log built
  from it, without necessarily showing as a visible duplicate row (the
  two entries could differ in date/time formatting while resolving to
  the same underlying game).
- **Totals are now derived from the rows actually displayed**, not
  accumulated in parallel while building them. Previously, month and
  season totals were built incrementally alongside each row; if any
  upstream cause (a duplicate game, a stale cache, a future bug) ever
  introduced a mismatch, the totals and the rows could silently
  disagree, exactly like the reported screenshot. Now the totals are a
  final summation *over* the collected rows, so a monthly or season
  total can never disagree with what's actually shown for that
  month/season — the bug class is closed structurally, regardless of
  what upstream cause created a specific instance of it.

If numbers still look off against the live cbl.ca site after this fix,
it's likely the completed-game disk cache (see "Gameday caching" below)
holding an old snapshot from before CBL corrected something after the
fact — call `cbl_api.clear_cache(include_disk=True)` to force a hard
refetch of everything.

## League name

This app now displays "Canadian Baseball League" instead of
"Intercounty Baseball League" throughout (nav bar, README, module
docstrings). The `CBL` abbreviation and all API endpoints/field names
are unaffected — this was a display-name-only change.

## Setup (Windows)

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) if not already installed.
2. Open PowerShell in this folder and install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Run the app:
   ```powershell
   python app.py
   ```
4. Open a browser to `http://localhost:5000` (or `http://<server-ip>:5000` from another machine on the network).

## Setup (Mac/Linux)

```bash
pip install -r requirements.txt
python app.py
```

## Configuration

Set these environment variables before running if you want to change defaults:

- `CBL_SEASON` — season string used in API calls, default `2026 Summer`
- `CBL_SEASON_YEAR` — season year for the game-ids feed, default `2026`
- `CBL_CACHE_TTL` — seconds to cache API responses in memory, default `120`
- `CBL_GAMEDAY_CACHE_DIR` — folder where completed games' gameday feeds are
  persisted to disk, default `.cbl_cache/gameday` (relative to wherever you
  run `python app.py`). See "Gameday caching" below.

Example (PowerShell):
```powershell
$env:CBL_SEASON = "2026 Summer"
python app.py
```

## What's included

- `app.py` — Flask routes: home/leaderboards, player page, search, gameday
- `cbl_api.py` — API client with lightweight in-memory caching
- `stats.py` — derived rate stats (K%, BB%, whiff%, etc.) and percentile ranking
- `gameday.py` — turns the raw pitch-by-pitch `/feed/public-gameday` payload into
  a line score, batting/pitching box scores, and inning-by-inning play-by-play
- `templates/` — Jinja2 templates (leaderboards, player page, search, gameday)
- `static/style.css` — ballpark-scorecard visual theme

## Gameday

Visit **Gameday** in the nav (or `/game`) and paste in a game's public ID —
the slug from its cbl.ca URL, e.g. `london-at-hamilton-2026-07-19` — to get:

- **Line score**: runs per inning, final R/H/E, computed from the at-bat log
  (not a separate boxscore endpoint)
- **Box scores**: per-player batting (AB/R/H/2B/3B/HR/RBI/BB/SO) and pitching
  (IP/H/R/BB/SO/HR/pitch count), aggregated from every at-bat
- **Play-by-play**: inning-by-inning, with pitch sequences (ball/strike/foul
  dots) and batted-ball type/quality/direction where recorded

Completed games are cached for an hour (they're static); anything not marked
`completed` is re-fetched every ~20s so a live game stays current. Tune with
`CBL_GAMEDAY_LIVE_TTL` / `CBL_GAMEDAY_FINAL_TTL` env vars if needed.

### Gameday caching

Completed games are also **persisted to disk** (as plain JSON files under
`CBL_GAMEDAY_CACHE_DIR`, default `.cbl_cache/gameday/`), not just kept in
memory. A completed game's box score never changes, so once it's on disk
it's never fetched from cbl.ca again — this matters because:

- The in-memory cache alone is wiped on every process restart, including
  Flask's debug-mode auto-reloader firing on every file save. Without a
  disk layer, an afternoon of local tweaks means re-fetching a whole
  team's schedule (see Game Log / Splits below — that's 30-40+ gameday
  requests per player) over and over.
- It survives deploys/reboots on your Windows server without any setup.

Games still `in_progress` or `scheduled` are **never** written to disk —
only the in-memory short-TTL cache applies to those, so a live game still
updates every ~20s. If a completed game ever needs a correction and you
want it re-fetched, delete its file from the cache folder (or call
`cbl_api.clear_cache(include_disk=True)` to wipe everything, live games
included).

Note: the feed only exposes the roster of the pitcher's own team per at-bat
via `pitcherId`/`batterId`, so ER (earned vs. unearned runs) isn't split out —
the pitching box shows total runs allowed (R) while that pitcher was on the mound.

## What's not included (data limitations)

Per the CBL API itself, pitch type, velocity, and precise pitch/batted-ball
location aren't recorded by the scorekeeping system, so there's no true
Statcast-style spray chart or exit-velocity/launch-angle data. The player
page instead shows:

- Rate-stat percentile bars (AVG/OBP/SLG/OPS, K%, BB%, whiff%, GB%) ranked
  against the rest of the league
- A batted-ball breakdown by type (ground ball / line drive / fly ball / popup),
  contact quality (soft/medium/hard), and direction (left/center/right)
- Splits by opponent and pitcher/batter handedness, pulled from the
  per-player `/analytics` endpoint

## Game Log & Home/Away Splits

Every batter's player page now has a **Game Log** tab right next to
**Overview** (no extra click-through, no separate URL). Selecting it
shows:

- A Home/Away split table
- A Baseball-Savant/MLB-Gameday-style log grouped by month, with
  per-game AVG/OBP/SLG/OPS plus month and season subtotal rows

Both tabs are rendered server-side on the same page load and toggled
client-side with a few lines of plain JS (no page reload, no separate
route) &mdash; see the `showPlayerTab()` script at the bottom of
`player.html`.

Under the hood, `gamelog.py` walks the full season schedule
(`/feed/game-ids`) for that player's team and pulls the boxscore of
every completed game to build these tables. That's on the order of
30-40 gameday requests the first time a given player's page is loaded;
each one is individually cached by `cbl_api.py`, so repeat visits (and
other players from a team whose games are already cached) are fast.
If a player's `/feed/game-ids` or gameday lookups fail for any reason,
the Game Log tab degrades to an empty-state message rather than
breaking the rest of the page.

**Field-name caveat:** `gamelog.py` was built against the XML preview of
`/feed/game-ids`, which renders dash-case tags (`<public-game-id>`). The
JSON response (`cbl_api.get_game_ids()` requests `format=json`) should
use the same camelCase convention as every other endpoint in this app,
but that's inferred, not confirmed against a live response. `gamelog._field()`
tries a few spellings per key so a mismatch degrades to "no rows found"
rather than a crash — if the Game Log tab comes up empty, check the
real JSON shape against `_field()`'s candidate key names first.

The player page's existing analytics-derived splits (`byOpponent`,
`byPitcherHandedness`) are unchanged, and any *other* split categories
the `/players/<id>/analytics` endpoint returns (e.g. by month, by
inning, if present) now render automatically in their own collapsible
section instead of needing a template change per split type.

## IMPORTANT: gameday data model (confirmed against a real payload)

A real `/feed/public-gameday` response was shared and used to verify this
whole section — here's what's actually true, replacing an earlier (wrong)
guess that the feed was events-only:

`snapshot.liveGame` carries **two parallel representations** of the same
game:

- **`atBats`** — a pre-built list, one entry per plate appearance, with
  outcome, batterId, pitcherId, fielders, rbiCount, runsScored,
  outsRecorded, baseRunnersBeforePlay, `runnersInScoringPosition`,
  `isLeadoffBatter`, and a nested `pitches` list. This is the shape the
  app was originally written against, and it's confirmed correct and
  complete — `gameday.get_at_bats()` uses it directly as the primary
  source.
- **`events`** — a flat, chronological stream of typed events (`pitch`,
  `at_bat_complete`, `runner_advance`, `inning_change`, `substitution`,
  `position_change`). This carries things `atBats` does **not**: stolen
  bases and caught stealing only show up as standalone `runner_advance`
  events (`cause`: `"stolen_base"` / `"caught_stealing"`, with a
  `runnerId`) — confirmed against real data. `gameday.get_at_bats()`
  falls back to reconstructing an equivalent list from `events` only if
  `atBats` is ever missing from a payload.

**Also confirmed and now used directly:** `liveGame.playerBattingStats[id]`
and `playerPitchingStats[id]` carry CBL's own pre-aggregated per-game
numbers — `stolenBases`, `caughtStealing`, `qualityStarts`, and a
`firstPitch` breakdown (`balls`/`strikes`/`swings`). `baserunning.py`,
`pitching_splits.py`, and `pitch_discipline.py` all prefer these
authoritative, server-computed numbers over re-deriving the same thing
from the raw at-bat/event log, falling back to their own derivation only
if a particular game's payload doesn't have that field. One caveat:
`playerPitchingStats[id].starts` was observed **not populated** even for
confirmed starting pitchers, so "who started" is still derived in
`pitching_splits.py` (first pitch of the game thrown by that pitcher for
their side) rather than trusted from that field.

## Opponent Line, Pitch Mix, Relief Stats, Team Win/Loss Splits

Two more modules, both built the same way as `baserunning.py`/`pitching_splits.py`
before them: sum CBL's own confirmed real per-game fields across a
player's or team's completed games, rather than re-deriving anything
this app can't verify.

**`pitching_extra.py`** (shown on the pitcher's Advanced tab):
- Opponent AVG/OBP/SLG/OPS/ISO/wOBA — built from summed
  singles/doubles/triples/HR/BB/HBP allowed
- Left On Base % — standard `(H+BB+HBP−R)/(H+BB+HBP−1.4×HR)` formula
- Strike %, Ball %, Called Strike %, Swinging Strike % — full-season,
  not just first-pitch, using CBL's real per-game `strikes`/`balls`/
  `strikesLooking`/`strikesSwinging` totals (confirmed relationship:
  `pitchCount == balls + strikes`, and `strikes == strikesLooking +
  strikesSwinging + fouls`)
- K Looking %, K Swinging % — from CBL's real `strikeoutsLooking`/
  `strikeoutsSwinging` per-game counts
- Holds, Hold Rate, Save Opportunities, Blown Saves, Save Conversion %,
  Inherited Runners, Inherited Runners Scored, Inherited Runner Scoring %
  — all from CBL's real per-game reliever fields. Hold Rate is defined
  here as holds per relief appearance (games − starts), since CBL
  doesn't track a "hold opportunity" count the way it tracks save
  opportunities.

**`team_schedule.py`** (shown on the team roster page as "Team Record"):
walks every completed game for a team and reads each one's inning-by-inning
line score to build W-L record, run differential, Pythagorean Win %
(exponent 1.83), and situational win% splits: one-run games, when
scoring first, when allowing the first run, and when leading after 5 or
7 innings.

**Still not built**, and why:
- Shutdown/Meltdown Games, High Leverage ERA, Team Clutch OPS/ERA — need
  leverage/win-probability data the feed doesn't have
- Double Play Rate — needs a tracked "DP opportunity" (runner on first,
  <2 outs) denominator, not currently derived
- Team Defensive Efficiency, Double Play Conversion %, Error Rate,
  Unearned Runs %, Team SB%, Team Extra-Base-Taken %, Team Line Drive % —
  no team-level fielding aggregate module yet (season fielding stats
  exist per-player but aren't summed to the team level the way batting/
  pitching are in `analytics.py`)

## Quality Starts, Momentum Meter, First-Pitch Discipline, Stolen Bases

- **Quality Starts** (`pitching_splits.py`) — derives which appearances were
  starts (pitcher threw the first pitch for their side) and flags 6+ IP /
  &le;3-runs-allowed starts. Since the feed doesn't separate earned from
  unearned runs, this uses *all* runs allowed, making it a slightly
  stricter proxy than a textbook Quality Start.
- **Momentum Meter** (`rolling.py`) — an explicitly custom 0-100 broadcast
  score (recent OPS vs. season OPS, hit streak, last-10-game RBI, with
  documented weights) shown on the batter Overview tab. It's a house
  formula over real numbers, not a standard sabermetric — treat it as
  entertainment, same spirit as the original "Broadcast Metrics" wishlist.
- **First-pitch swing/take % (batters) and strike/ball % (pitchers)**
  (`pitch_discipline.py`) — walks every at-bat's raw pitch sequence for
  the season. A first pitch that isn't a "ball" or "called_strike" is
  counted as a swing; this is a reasonable proxy but hasn't been
  confirmed against every pitch-result value CBL's live feed might use
  for a first-pitch ball in play.
- **Stolen bases / caught stealing** (`baserunning.py`) — prefers CBL's
  own per-game `playerBattingStats[id].stolenBases` / `.caughtStealing`
  (confirmed present and correct against real data), falling back to
  walking `runner_advance` events directly (`gameday.get_stolen_base_events`)
  if that field is ever missing. Shows "not available" only if this app
  couldn't find either source for a team's games that season.

## Team Rosters

Every team name across the site (leaderboards, search results, the Teams
page, a player's own header) now links to `/team/<team name>` &mdash; a
roster page with that team's full batting, pitching, and fielding lines
plus its aggregated team-level OPS/ERA line from the Teams page.

## Pitching Splits

Pitchers now get a **Splits** tab too, built by `pitching_splits.py` the
same way `splits.py` builds a hitter's: walking every completed game for
the pitcher's team and filtering the at-bat log by `pitcherId`. It covers
Monthly, Baserunner, Outs, Inning, and Game Type (home/away/day/night)
splits.

Two things it does *not* fabricate:
- **Platoon (vs. batter handedness) splits** only appear if CBL's
  `/players/<id>/analytics` endpoint returns them (`byBatterHandedness`)
  &mdash; the raw play-by-play log has no batter-handedness field to
  derive them from directly, unlike direction/baserunner/inning state
  which are all in the at-bat record itself.
- **"ERA"** in these split tables is actually runs-allowed-per-9 (all
  runs charged while that pitcher was on the mound), not true earned-run
  ERA, since the feed doesn't separate earned from unearned runs per
  play — only in the season totals. It's labeled `ERA*` in the table
  with a note underneath.

## Rolling Stats (Phase 2)

The Game Log tab now opens with a **Rolling Stats** section, built by
`rolling.py` on top of the game log `gamelog.py` already produces:

- Last 5 / 10 / 15 games, Last 30 Days, Last 30 PA, Last 50 PA
- Current Hitting Streak and Current On-Base Streak

Games a player didn't appear in are already excluded from the game log,
which is the *correct* behavior for streaks — a real hit streak isn't
broken by an off day, only by a game they played and went hitless. "Last
30 Days" is a calendar window ending at the player's most recent logged
game (not literally today, since the season may be over or on a break).
On-base for streak purposes is H + BB only; HBP isn't tracked at the
per-game level, same caveat as the rest of the Game Log tab.

## Sorting & filtering leaderboards

Every leaderboard (Batting, Pitching, Fielding) now has:

- A **Team filter** dropdown that narrows the table to one team.
- **Click-to-sort column headers** — click any underlined header (including
  **Team**, to group/alphabetize by team) to sort by that column; click again
  to flip direction. Sorting and the team filter combine and stay in the URL
  (`?team=...&sort=...&dir=...`), so a sorted/filtered view is bookmarkable
  and shareable.

There's also a new **Teams** page/tab with team-level batting and pitching
lines (see below), also sortable.

## Advanced Analytics Module

Every player page has a new **Advanced** tab (next to Overview / Game Log /
Splits) with sabermetric stats computed in `analytics.py`, and there's a new
**Teams** leaderboard with the same stats aggregated to the team level.

**Advanced Batting:** OPS, OBP, SLG, ISO, BABIP, wOBA, OPS+, Runs Created,
RC/Game, Estimated wRC, Batting Runs Above Average (estimated), Secondary
Average, Total Bases, Extra Base Hits/%, Extra Bases per Hit.

**Plate Discipline:** K%, BB%, K-BB%, BB/K ratio, PA per strikeout, PA per walk.

**Advanced Pitching:** ERA, WHIP, FIP, xFIP, ERA+, K/9, BB/9, HR/9, K%, BB%,
K-BB%, BABIP against, GB%/FB%/GB-FB ratio.

**Team Stats:** Team OPS/OBP/SLG/ISO/wOBA/BABIP, runs per game, team
ERA/WHIP/FIP/GB%/FB%, runs allowed per game.

### What's *not* in this module, and why

A lot of "advanced analytics" wishlists (situational/clutch splits, pinch-hit
detection, leverage-based stats, defensive range factor, stolen bases) need
data this feed doesn't expose at the season-stat level — see `gameday.py`'s
outcome-vocabulary docstring and the "What's not included" section above.
Rather than fake those numbers, this module sticks to what's computable and
documents every approximation it *does* make (non-league-specific wOBA
weights, a self-derived FIP constant, outs-based GB%/FB% for pitchers, no
sac-fly term in BABIP/Secondary Average, no SB term in Secondary Average)
directly in `analytics.py`'s module docstring — read that first if a number
looks off. First-pitch strike%, quality starts, and rolling/last-N-games
splits aren't wired up yet; they're natural next steps built the same way
`gamelog.py`/`splits.py` already walk the season schedule.

## Extending

- **Fielding percentiles**: `stats.py` only builds percentiles for batting
  and pitching right now; the same pattern can be extended to fielding
  (range factor, fielding %, etc.) if useful.
- **Game log**: the `/feed/game-ids` and `/feed/game-package` endpoints
  aren't wired up yet — a per-game log or box score view could use those.
- **Charts**: batted-ball bars are plain CSS; swapping in Chart.js (via CDN)
  would make it easy to add pie/donut charts if you want a closer visual
  match to Savant's spray-chart-style widgets.
