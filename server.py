import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote

from curl_cffi import requests
from curl_cffi.requests import RequestsError
from flask import Flask, jsonify, request
from flask_cors import CORS

import bet9ja
import betking
import betpawa
import onexbet
from srid import sr_id

app = Flask(__name__)
CORS(app)

# Logs go to stdout/stderr, which Railway captures. Prefer log.* over print so
# messages carry a level + timestamp and exceptions carry a traceback.
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("soccerwizard")

# Error tracking (opt-in). Set SENTRY_DSN in Railway > Variables to enable; with
# it unset this is a complete no-op. Wrapped so a missing/broken SDK degrades to
# a warning instead of taking the app down on boot.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.0,  # errors only - no perf tracing overhead on the trial
            environment=os.environ.get("RAILWAY_ENVIRONMENT_NAME", "production"),
        )
        log.info("Sentry error tracking enabled")
        _sentry = sentry_sdk
    except Exception as ex:  # noqa: BLE001
        log.warning("SENTRY_DSN set but Sentry init failed (is sentry-sdk installed?): %s", ex)
        _sentry = None
else:
    _sentry = None


REPORT_EVERY_S = 600          # one Sentry event per warning message per 10 minutes
_REPORT_SENT = {}             # message -> monotonic time it last reached Sentry
_REPORT_LOCK = threading.Lock()
_report_now = time.monotonic  # swapped in tests


def report(message, level="warning", **context):
    """Log it, and send it to Sentry as a searchable event when one is set up.

    A booking rejection is not an exception, so nothing here ever raised and
    Sentry never saw one - the only record of a failed slip was the line the
    user read on their phone. Log lines are fine for reading after the fact but
    poor for noticing: nobody trawls Railway output to discover that a market
    started failing an hour ago.

    The tag is what makes it useful. Sentry groups by message, so every "no
    market" rejection lands in one issue with a count and a graph, rather than
    scattering. Context carries the detail - which markets, how many legs -
    without putting it in the title.

    A complete no-op with SENTRY_DSN unset, and it never raises: this sits on
    the booking path, and an error reporter that can break a booking is worse
    than no error reporter.
    """
    # The level travels to the log as well as to Sentry. It used to be a
    # warning here whatever the caller said, so a line reported as routine was
    # still shouted at Railway - and a log where everything is a warning is a
    # log where nothing is.
    log.log(getattr(logging, level.upper(), logging.WARNING), "%s | %s", message,
            " ".join(f"{k}={v}" for k, v in sorted(context.items())))
    if not _sentry:
        return
    # The Sentry plan is 5k events a month and these notes were most of it:
    # routine booking notes (info) about a third, one "rejected a slip" warning
    # firing ~60 times a day nearly as much again. Info stays in the Railway log
    # only. A warning reaches Sentry once per REPORT_EVERY_S per message - the
    # issue, its count trend and its context survive; the duplicates do not.
    # Errors are never held back.
    if level == "info":
        return
    if level == "warning":
        now = _report_now()
        with _REPORT_LOCK:
            last = _REPORT_SENT.get(message)
            if last is not None and now - last < REPORT_EVERY_S:
                return
            _REPORT_SENT[message] = now
    try:
        with _sentry.push_scope() as scope:
            scope.set_tag("area", "booking")
            for k, v in context.items():
                scope.set_extra(k, v)
            _sentry.capture_message(message, level=level)
    except Exception as ex:   # never let reporting break the thing it reports on  # noqa: BLE001
        log.warning("sentry capture failed: %s", ex)


# Prediction code -> SportyBet market/outcome (+ specifier for totals).
# Both sides of each two-way market are listed so the frontend can de-vig
# and blend (needs over AND under, GG AND NG).
MARKET_MAP = {
    "1":         {"marketId": "1",  "outcomeId": "1"},
    "X":         {"marketId": "1",  "outcomeId": "2"},
    "2":         {"marketId": "1",  "outcomeId": "3"},
    "1X":        {"marketId": "10", "outcomeId": "9"},
    "12":        {"marketId": "10", "outcomeId": "10"},
    "X2":        {"marketId": "10", "outcomeId": "11"},
    "OVER_1.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=1.5"},
    "UNDER_1.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=1.5"},
    "OVER_2.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=2.5"},
    "UNDER_2.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=2.5"},
    # Over 3.5 rides market 18, already fetched for Over 1.5 and Over 2.5, so
    # it costs no extra request - and the model has produced o35 all along.
    "OVER_3.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=3.5"},
    "UNDER_3.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=3.5"},
    # Under 4.5, the readers' near-sure leg (owner, 8 Oct 2026). Same market 18.
    "UNDER_4.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=4.5"},
    "GG":        {"marketId": "29", "outcomeId": "74"},
    "NG":        {"marketId": "29", "outcomeId": "76"},
    # First half, at least one goal. The model has predicted this all along
    # (fh_o05) but it was not bookable, so the site could only ever show it.
    # Market 68 carries total=0.5 on 199 of 200 upcoming events.
    "FH_OVER_0.5":  {"marketId": "68", "outcomeId": "12", "specifier": "total=0.5"},
    "FH_UNDER_0.5": {"marketId": "68", "outcomeId": "13", "specifier": "total=0.5"},
    # How many one side scores on its own. Market 19 is always the HOME
    # team's total and 20 the AWAY team's - verified across 200 events, where
    # 19's own description matched the home team 96 times and the away team
    # never, and 20 the reverse. Getting these round the wrong way would book
    # the opposing team's goals, so it was worth proving rather than assuming.
    "HOME_OVER_0.5":  {"marketId": "19", "outcomeId": "12", "specifier": "total=0.5"},
    "HOME_UNDER_0.5": {"marketId": "19", "outcomeId": "13", "specifier": "total=0.5"},
    "HOME_OVER_1.5":  {"marketId": "19", "outcomeId": "12", "specifier": "total=1.5"},
    "HOME_UNDER_1.5": {"marketId": "19", "outcomeId": "13", "specifier": "total=1.5"},
    "AWAY_OVER_0.5":  {"marketId": "20", "outcomeId": "12", "specifier": "total=0.5"},
    "AWAY_UNDER_0.5": {"marketId": "20", "outcomeId": "13", "specifier": "total=0.5"},
    "AWAY_OVER_1.5":  {"marketId": "20", "outcomeId": "12", "specifier": "total=1.5"},
    "AWAY_UNDER_1.5": {"marketId": "20", "outcomeId": "13", "specifier": "total=1.5"},
}

# Reverse lookup: (marketId, outcomeId, specifier) -> code, for reading odds.
# --- markets we move but do not model --------------------------------------
# The SportyBet half of bet9ja.PASSTHROUGH_MAP. Same reasoning: a converter
# needs the selection's identity, not a probability, so these sit beside
# MARKET_MAP rather than in it and nothing that predicts, prices or grades ever
# sees them.
#
# 1UP is market 60200 and 2UP is 60100 - the lower id is the LATER promotion,
# which is the sort of thing worth writing down once rather than rediscovering.
# Outcome 1/2/3 is home/draw/away on both. Read off their own catalogue:
# "1X2 - 1UP" and "1X2 - 2UP", 13 Sep 2026.
PASSTHROUGH_MAP = {
    "UP1_1": {"marketId": 60200, "outcomeId": 1, "specifier": ""},
    "UP1_X": {"marketId": 60200, "outcomeId": 2, "specifier": ""},
    "UP1_2": {"marketId": 60200, "outcomeId": 3, "specifier": ""},
    "UP2_1": {"marketId": 60100, "outcomeId": 1, "specifier": ""},
    "UP2_X": {"marketId": 60100, "outcomeId": 2, "specifier": ""},
    "UP2_2": {"marketId": 60100, "outcomeId": 3, "specifier": ""},
    # DOUBLE CHANCE WITH THE 1UP PROMOTION, market 60110. Found in a real
    # punter's code (HCVKA1, 14 Sep) as `60110/11/` - three legs of thirty-one
    # that read back as an unknown market and cost the whole ticket.
    # THE OUTCOME IDS ARE NOT IN SIGN ORDER, exactly as market 85 is not:
    # 9 is Home or Draw, 10 is Home or AWAY, 11 is Draw or Away. Reading the
    # pair the obvious way books 12 as 1X. Same trap, second market - so it is
    # written out here rather than generated.
    "DC1UP_1X": {"marketId": 60110, "outcomeId": 9,  "specifier": ""},
    "DC1UP_12": {"marketId": 60110, "outcomeId": 10, "specifier": ""},
    "DC1UP_X2": {"marketId": 60110, "outcomeId": 11, "specifier": ""},
    # WHOLE Over/Under lines. SportyBet prices them, Bet9ja does not - their
    # card carries 0.5/1.5/2.5/3.5/4.5/5.5 and nothing else, checked on a live
    # event. So these read and split on SportyBet and can only CONVERT by
    # changing the bet, which lib/convert says out loud rather than doing
    # quietly. A whole line pushes: exactly two goals on "Over 2" returns the
    # stake, which is why this can never become a market the model predicts.
    "OVER_2": {"marketId": 18, "outcomeId": 12, "specifier": "total=2"},
    "OVER_3": {"marketId": 18, "outcomeId": 12, "specifier": "total=3"},
    "UNDER_2": {"marketId": 18, "outcomeId": 13, "specifier": "total=2"},
    "UNDER_3": {"marketId": 18, "outcomeId": 13, "specifier": "total=3"},
    # 1X2-or-Over/Under, the family a real punter's codes leaned on hardest -
    # 45 legs of 180. SportyBet sells the six combinations as six markets with
    # a Yes/No outcome, 854 through 859 in a block, and ONLY on the 2.5 line.
    # Bet9ja sells the same six as outcomes of one market and only at 1.5 and
    # 3.5. The lines do not overlap, so these read and split here and cross to
    # the other book only when somebody has asked for the line to be changed.
    "MIX_1_OV_2.5": {"marketId": 854, "outcomeId": 74, "specifier": "total=2.5"},
    "MIX_1_UN_2.5": {"marketId": 855, "outcomeId": 74, "specifier": "total=2.5"},
    "MIX_X_OV_2.5": {"marketId": 856, "outcomeId": 74, "specifier": "total=2.5"},
    "MIX_X_UN_2.5": {"marketId": 857, "outcomeId": 74, "specifier": "total=2.5"},
    "MIX_2_OV_2.5": {"marketId": 858, "outcomeId": 74, "specifier": "total=2.5"},
    "MIX_2_UN_2.5": {"marketId": 859, "outcomeId": 74, "specifier": "total=2.5"},
    # 1X2 or GG. 860/861/862 against their S_CHANCEMIX, no line either side.
    "MIXGG_1": {"marketId": 860, "outcomeId": 74, "specifier": ""},
    "MIXGG_X": {"marketId": 861, "outcomeId": 74, "specifier": ""},
    "MIXGG_2": {"marketId": 862, "outcomeId": 74, "specifier": ""},
    # 1X2 OR NO-GOAL, WHICH THEY DO NOT CALL NO-GOAL. This was recorded as a
    # real gap - mapped on Bet9ja, absent here - because a search for "NG"
    # found nothing. Their name for it is "Any Clean Sheet": 863/864/865,
    # "Home Team or Any Clean Sheet" and its two siblings, read off their own
    # catalogue on 14 Sep 2026 (sr:match:67015370).
    #
    # At least one clean sheet IS no-goal: one side failing to score is exactly
    # "not both teams scored". Same bet, different word, and the difference is
    # why it looked missing for a week.
    #
    # Outcome 74 is Yes and 76 is No on all three, the same pair the GG markets
    # above use. 76 is NOT the other half of the family - "not home and not
    # clean sheet" is its own bet - so only Yes is mapped.
    "MIXNG_1": {"marketId": 863, "outcomeId": 74, "specifier": ""},
    "MIXNG_X": {"marketId": 864, "outcomeId": 74, "specifier": ""},
    "MIXNG_2": {"marketId": 865, "outcomeId": 74, "specifier": ""},
}

# TEAM CARDS. Market 800060, and the outcome id carries both the team and
# the threshold: 800060:000000N is the HOME side with N or more, 800060:
# 000001N the away side. A composite id rather than the small integers every
# other market here uses, which is worth knowing before someone tries to
# parse it as a number.
# "N or more cards" and "over N-0.5 cards" are the same bet, which is what
# makes this pair with Bet9ja's S_OUBOOKHOME / S_OUBOOKAWAY at all. Their
# home side runs to 3.5 and their away side stops at 2.5, so home 1+ to 4+
# and away 1+ to 3+ are what can cross; anything above that reads and splits
# here and has nowhere to land there.
for _n in range(1, 7):
    PASSTHROUGH_MAP[f"CARD_H_{_n:d}"] = {
        "marketId": 800060, "outcomeId": f"800060:{_n:08d}", "specifier": ""}
    PASSTHROUGH_MAP[f"CARD_A_{_n:d}"] = {
        "marketId": 800060, "outcomeId": f"800060:{100 + _n:08d}", "specifier": ""}

# CORNERS, total for the match. Market 166, outcome 12 over and 13 under - the
# same shape as goals on market 18, which is why it needed no thought once it
# was looked at. Both books quote half lines, so there is nothing to reconcile
# and no push to worry about.
# They do not carry the same ones. SportyBet runs 6.5 to 12.5 and Bet9ja 7.5 to
# 14.5, so 7.5 through 12.5 cross and the ends do not: a 6.5 leg reads and
# splits here with nowhere to land there, and the same for their 13.5 and 14.5.
# TOTAL SHOTS, market 900394 - corners' shape exactly, outcome 12 over and 13
# under on a total= specifier. Read off Netherlands v Germany, 23 Sep 2026.
# The line moves with the game (Kosovo v Ireland near 21.5, Portugal v Wales
# near 27.5), so every half line the card has shown is mapped.
for _line in ("19.5", "20.5", "21.5", "22.5", "23.5", "24.5", "25.5", "26.5", "27.5", "28.5", "29.5", "30.5", "31.5"):
    PASSTHROUGH_MAP[f"SHOTS_OV_{_line}"] = {
        "marketId": 900394, "outcomeId": 12, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"SHOTS_UN_{_line}"] = {
        "marketId": 900394, "outcomeId": 13, "specifier": f"total={_line}"}

# TEAM SHOTS, 900552 home and 900553 away - total shots' shape exactly, outcome
# 12 over and 13 under on total=N.5. The owner's code SAJ9y6 (USA over 13.5,
# 29 Sep 2026) is how we learned they exist; the 23 Sep note saying SportyBet
# had no team shots had only looked at "Most Shots", a player duel. Each team's
# line sits near its own average - 7.5 to 18.5 seen over 20 fixtures - so the
# span mapped is wider than anything seen.
for _line in [f"{n}.5" for n in range(4, 23)]:
    for _side, _mid in (("H", 900552), ("A", 900553)):
        PASSTHROUGH_MAP[f"SHOTS_{_side}_OV_{_line}"] = {
            "marketId": _mid, "outcomeId": 12, "specifier": f"total={_line}"}
        PASSTHROUGH_MAP[f"SHOTS_{_side}_UN_{_line}"] = {
            "marketId": _mid, "outcomeId": 13, "specifier": f"total={_line}"}

for _line in ("6.5", "7.5", "8.5", "9.5", "10.5", "11.5", "12.5"):
    PASSTHROUGH_MAP[f"CORNERS_OV_{_line}"] = {
        "marketId": 166, "outcomeId": 12, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"CORNERS_UN_{_line}"] = {
        "marketId": 166, "outcomeId": 13, "specifier": f"total={_line}"}

# ---------------------------------------------------------------------------
# THE SIBLING FAMILIES. Half-versions and per-team versions of markets already
# carried, taken off the same catalogue read. Nothing exotic here on purpose:
# each one has a full-match or whole-team twin above, so the shape was already
# known and only the ids needed reading.
#
# EUROPEAN HANDICAP, which is NOT the handicap on market 16. That one is Asian
# - two outcomes, the draw eliminated. This is three outcomes with a scoreline
# head start, so it keeps its own name rather than another AH line, and a leg
# that reads as "Home (2:0)" means the away side starts two goals up.
# The halves carry a shorter card than the match does - 0:1, 0:2 and 1:0 only,
# on all eight events checked - so they get their own line list. Generating the
# full-match lines for them produced 24 codes that matched nothing on any card,
# which is a mapping nobody could ever book.
for _mid, _pre, _lines in (
        (14, "", ((0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (1, 0), (2, 0), (3, 0))),
        (65, "FH_", ((0, 1), (0, 2), (1, 0))),
        (87, "SH_", ((0, 1), (0, 2), (1, 0)))):
    for _h, _a in _lines:
        for _sfx, _out in (("1", 1711), ("X", 1712), ("2", 1713)):
            PASSTHROUGH_MAP[f"{_pre}EH_{_h:d}_{_a:d}_{_sfx}"] = {
                "marketId": _mid, "outcomeId": _out,
                "specifier": f"hcp={_h:d}:{_a:d}"}

# ASIAN HANDICAP WITHIN ONE HALF. 66 and 88 against 16 for the match, and the
# same two outcomes - 1714 home, 1715 away.
for _pre, _mid in (("FH_", 66), ("SH_", 88)):
    for _l in ("-2", "-1.5", "-1", "-0.5", "0", "0.5"):
        PASSTHROUGH_MAP[f"{_pre}AH_1_{_l}"] = {
            "marketId": _mid, "outcomeId": 1714, "specifier": f"hcp={_l}"}
        PASSTHROUGH_MAP[f"{_pre}AH_2_{_l}"] = {
            "marketId": _mid, "outcomeId": 1715, "specifier": f"hcp={_l}"}

# FIRST-HALF 1X2 & TOTAL. The full-match twin is six separate Yes/No markets
# (854-859); this is ONE market with six outcomes, so the ids are read off it
# directly rather than derived from the sign. 1.5 is the only line they quote.
for _sfx, _out in (("1_UN", 794), ("1_OV", 796), ("X_UN", 798),
                   ("X_OV", 800), ("2_UN", 802), ("2_OV", 804)):
    _sign, _dir = _sfx.split("_")
    PASSTHROUGH_MAP[f"FH_MIX_{_sign}_{_dir}_1.5"] = {
        "marketId": 79, "outcomeId": _out, "specifier": "total=1.5"}

# CORNER RANGE, the match and each side. Same composite-id shape as goal range
# on market 25: the variant is named in the specifier AND carried inside the
# outcome id, and the two must agree.
_VAR_PR12 = "variant=sr:point_range:12+"
_VAR_PR7 = "variant=sr:point_range:7+"
for _band, _out in (("0_8", 1141), ("9_11", 1142), ("12", 1143)):
    PASSTHROUGH_MAP[f"CORNRANGE_{_band}"] = {
        "marketId": 169, "outcomeId": f"sr:point_range:12+:{_out:d}",
        "specifier": _VAR_PR12}
for _side, _mid in (("H", 170), ("A", 171)):
    for _band, _out in (("0_2", 1144), ("3_4", 1145), ("5_6", 1146), ("7", 1147)):
        PASSTHROUGH_MAP[f"CORNRANGE_{_side}_{_band}"] = {
            "marketId": _mid, "outcomeId": f"sr:point_range:7+:{_out:d}",
            "specifier": _VAR_PR7}

# ONE SIDE'S BOOKINGS IN THE FIRST HALF. 900306 home, 900307 away, outcome 30
# over and 31 under - the same shape as team corners below, not the composite
# ids the full-match team-cards market uses. "N or more bookings" is "over
# N-0.5", which is how the full-match CARD_ family is already named, so these
# follow it.
for _pre, _mid in (("H", 900306), ("A", 900307)):
    for _n, _line in ((1, "0.5"), (2, "1.5"), (3, "2.5")):
        PASSTHROUGH_MAP[f"FH_CARD_{_pre}_{_n:d}"] = {
            "marketId": _mid, "outcomeId": 30, "specifier": f"total={_line}"}
        PASSTHROUGH_MAP[f"FH_CARDUN_{_pre}_{_n:d}"] = {
            "marketId": _mid, "outcomeId": 31, "specifier": f"total={_line}"}
# ---------------------------------------------------------------------------

# ONE SIDE'S CORNERS. 900300 is the HOME team's total and 900301 the away
# team's, outcome 30 over and 31 under, the line in the specifier. Read off
# their catalogue on 14 Sep: 3.5 through 7.5 on the home side, and a real
# punter's code carried `900300/30/total=3.5` - which read back as an unknown
# market because only one line of this family had ever been mapped.
# WIDENED 25 Sep 2026 when the site began BUILDING these, not just reading
# them: the away side's card sits lower (1.5-5.5 on Armenia v Latvia and
# Arbroath v Queens Park), so 0.5-2.5 and 8.5-9.5 join. Same ids re-read that
# day on the live card: 30 over, 31 under - NOT market 166's 12 and 13.
for _line in ("0.5", "1.5", "2.5", "3.5", "4.5", "5.5", "6.5", "7.5", "8.5", "9.5"):
    PASSTHROUGH_MAP[f"CORNERS_H_OV_{_line}"] = {
        "marketId": 900300, "outcomeId": 30, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"CORNERS_H_UN_{_line}"] = {
        "marketId": 900300, "outcomeId": 31, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"CORNERS_A_OV_{_line}"] = {
        "marketId": 900301, "outcomeId": 30, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"CORNERS_A_UN_{_line}"] = {
        "marketId": 900301, "outcomeId": 31, "specifier": f"total={_line}"}

# EXCLUDED NUMBER OF GOALS, market 450004 for the match and 810002 for the
# first half. The bet is "the total will be anything BUT this number", and the
# outcome id IS the number - 0,1,2,3,4 and 5 meaning five-or-more on the match,
# 3 meaning three-or-more in the half. Nothing else in these tables uses the
# outcome id as a value, which is worth knowing before somebody reads it as an
# index. Found in PV5CLL, a reader's code: two legs of thirty-nine.
for _n in ("0", "1", "2", "3", "4", "5"):
    PASSTHROUGH_MAP[f"EXGOALS_{_n}"] = {
        "marketId": 450004, "outcomeId": int(_n), "specifier": ""}
for _n in ("0", "1", "2", "3"):
    PASSTHROUGH_MAP[f"EXGOALS_FH_{_n}"] = {
        "marketId": 810002, "outcomeId": int(_n), "specifier": ""}

# GOAL BOUNDS, one side's goals as a RANGE: 450002 is the home team and 450003
# the away team. The outcome id spells the range in digits - 0 is none, 1 is
# exactly one, 12 is one-to-two, 13 is one-to-three-or-more, 33 is three-plus -
# so the ids are not sequential and cannot be generated from a count. Written
# out from their own card, PV5CLL carried `450003/23/` (two to three or more).
_GOAL_BOUNDS = ("0", "1", "2", "11", "12", "13", "22", "23", "33")
for _b in _GOAL_BOUNDS:
    PASSTHROUGH_MAP[f"BOUNDS_H_{_b}"] = {
        "marketId": 450002, "outcomeId": int(_b), "specifier": ""}
    PASSTHROUGH_MAP[f"BOUNDS_A_{_b}"] = {
        "marketId": 450003, "outcomeId": int(_b), "specifier": ""}

# ------------------------------------------------------------------------
# THE MARKETS SPORTYBET SURFACES BY DEFAULT, mapped in one pass rather than
# one reader complaint at a time.
#
# Every entry above this line was added because a punter's code carried it and
# could not be read. That is a poor way to find out: two codes on 14 Sep turned
# up six families between them. So this tranche was taken from their own
# catalogue instead - the markets they flag as `favourite`, which is their own
# statement about what people play - filtered to the ones with a fixed set of
# outcomes. Player props are deliberately absent: their outcomes are one per
# player, named per fixture, and cannot be enumerated in a table.
#
# TWO SHAPES APPEAR HERE THAT NOTHING ABOVE USES:
#   a `variant=` specifier, where the market id alone does not identify the
#   bet - Exact Goals at 6+ is a different market from Exact Goals at 3+;
#   and composite outcome ids like `sr:exact_goals:6+:68`, which carry the
#   variant inside the id. Both must be sent back exactly as read.
_VAR_EXACT6 = "variant=sr:exact_goals:6+"
_VAR_EXACT3 = "variant=sr:exact_goals:3+"
_VAR_EXACT2 = "variant=sr:exact_goals:2+"
_VAR_RANGE7 = "variant=sr:goal_range:7+"
_VAR_MARGIN = "variant=sr:winning_margin:3+"

# FIRST GOAL - who scores it, or nobody. Outcome 6 home, 7 none, 8 away, and
# the `goalnr=1` specifier is what makes it the FIRST one.
for _code, _mkt in (("FIRSTGOAL", 8), ("FIRSTGOAL_FH", 62), ("FIRSTGOAL_SH", 84)):
    for _sfx, _out in (("1", 6), ("N", 7), ("2", 8)):
        PASSTHROUGH_MAP[f"{_code}_{_sfx}"] = {
            "marketId": _mkt, "outcomeId": _out, "specifier": "goalnr=1"}

# EXACT GOALS, match and each half. The ceiling differs per scope - 6+ on the
# match, 3+ in the first half, 2+ in the second - and it is part of both the
# specifier and every outcome id.
for _n, _out in zip(range(7), range(68, 75)):
    PASSTHROUGH_MAP[f"EXACT_{_n:d}"] = {
        "marketId": 21, "outcomeId": f"sr:exact_goals:6+:{_out:d}",
        "specifier": _VAR_EXACT6}
for _n, _out in zip(range(4), range(88, 92)):
    PASSTHROUGH_MAP[f"EXACT_FH_{_n:d}"] = {
        "marketId": 71, "outcomeId": f"sr:exact_goals:3+:{_out:d}",
        "specifier": _VAR_EXACT3}
for _n, _out in zip(range(3), range(85, 88)):
    PASSTHROUGH_MAP[f"EXACT_SH_{_n:d}"] = {
        "marketId": 93, "outcomeId": f"sr:exact_goals:2+:{_out:d}",
        "specifier": _VAR_EXACT2}

# ONE SIDE'S EXACT GOALS. Same variant as the first half above, and the same
# outcome ids - 23 is the home team and 24 the away team.
for _side, _mkt in (("H", 23), ("A", 24)):
    for _n, _out in zip(range(4), range(88, 92)):
        PASSTHROUGH_MAP[f"TEAMGOALS_{_side}_{_n:d}"] = {
            "marketId": _mkt, "outcomeId": f"sr:exact_goals:3+:{_out:d}",
            "specifier": _VAR_EXACT3}

# GOAL RANGE - the whole match's goals as a band.
for _name, _out in (("0_1", 1342), ("2_3", 1343), ("4_6", 1344), ("7", 1345)):
    PASSTHROUGH_MAP[f"GOALRANGE_{_name}"] = {
        "marketId": 25, "outcomeId": f"sr:goal_range:7+:{_out:d}",
        "specifier": _VAR_RANGE7}

# WINNING MARGIN, including the draw - which is a seventh outcome here rather
# than a market of its own.
for _name, _out in (("H1", 113), ("H2", 114), ("H3", 115),
                    ("A1", 116), ("A2", 117), ("A3", 118), ("DRAW", 119)):
    PASSTHROUGH_MAP[f"MARGIN_{_name}"] = {
        "marketId": 15, "outcomeId": f"sr:winning_margin:3+:{_out:d}",
        "specifier": _VAR_MARGIN}

# BOTH HALVES OVER / UNDER 1.5. Two markets, each a plain Yes/No - 74 and 76,
# the pair the combination markets use.
for _code, _mkt in (("BOTHHALVES_OV", 58), ("BOTHHALVES_UN", 59)):
    for _sfx, _out in (("Y", 74), ("N", 76)):
        PASSTHROUGH_MAP[f"{_code}_{_sfx}"] = {
            "marketId": _mkt, "outcomeId": _out, "specifier": "total=1.5"}

# SECOND-HALF GOALS, whole match and per side, and the first half per side.
# Outcome 12 over and 13 under throughout, the line in the specifier - the
# same shape as market 18, which is why these need no thought beyond the ids.
for _line in ("0.5", "1.5", "2.5"):
    PASSTHROUGH_MAP[f"SH_OVER_{_line}"] = {
        "marketId": 90, "outcomeId": 12, "specifier": f"total={_line}"}
    PASSTHROUGH_MAP[f"SH_UNDER_{_line}"] = {
        "marketId": 90, "outcomeId": 13, "specifier": f"total={_line}"}
    for _half, _h_mkt, _a_mkt in (("FH", 69, 70), ("SH", 91, 92)):
        PASSTHROUGH_MAP[f"{_half}_HOME_OVER_{_line}"] = {
            "marketId": _h_mkt, "outcomeId": 12, "specifier": f"total={_line}"}
        PASSTHROUGH_MAP[f"{_half}_HOME_UNDER_{_line}"] = {
            "marketId": _h_mkt, "outcomeId": 13, "specifier": f"total={_line}"}
        PASSTHROUGH_MAP[f"{_half}_AWAY_OVER_{_line}"] = {
            "marketId": _a_mkt, "outcomeId": 12, "specifier": f"total={_line}"}
        PASSTHROUGH_MAP[f"{_half}_AWAY_UNDER_{_line}"] = {
            "marketId": _a_mkt, "outcomeId": 13, "specifier": f"total={_line}"}

# GOALS IN THE FIRST N MINUTES, market 60180, outcome 12 over and 13 under.
# The specifier carries BOTH numbers - `minsnr=10|total=1.5` is "over 1.5 goals
# in the first ten minutes" - which is why this cannot be folded into the plain
# over/under family: the same market id serves every window, and dropping the
# minsnr half would book a full-match line instead of a ten-minute one.
# Six legs of the thirty-one in HCVKA1 were these, all unreadable until now.
# The windows SportyBet publishes, read off their own card: 10 minutes at 1.5,
# 30 at 2.5, 50 at 3.5. A window they do not sell is not one to invent.
for _mins, _total in (("10", "1.5"), ("30", "2.5"), ("50", "3.5")):
    PASSTHROUGH_MAP[f"EARLY_OV_{_mins}_{_total}"] = {
        "marketId": 60180, "outcomeId": 12,
        "specifier": f"minsnr={_mins}|total={_total}"}
    PASSTHROUGH_MAP[f"EARLY_UN_{_mins}_{_total}"] = {
        "marketId": 60180, "outcomeId": 13,
        "specifier": f"minsnr={_mins}|total={_total}"}

# WIN EITHER HALF. 50 is the home side and 51 the away side, 74 Yes and 76 No.
PASSTHROUGH_MAP.update({
    "WINHALF_H_Y": {"marketId": 50, "outcomeId": 74, "specifier": ""},
    "WINHALF_H_N": {"marketId": 50, "outcomeId": 76, "specifier": ""},
    "WINHALF_A_Y": {"marketId": 51, "outcomeId": 74, "specifier": ""},
    "WINHALF_A_N": {"marketId": 51, "outcomeId": 76, "specifier": ""},
    # DRAW NO BET. 11/4 home, 11/5 away - the stake back if it finishes level.
    "DNB_1": {"marketId": 11, "outcomeId": 4, "specifier": ""},
    "DNB_2": {"marketId": 11, "outcomeId": 5, "specifier": ""},
    # SECOND-HALF DOUBLE CHANCE. Their outcome ids are not in the order the
    # signs are usually written: 9 is Home or Draw, 10 is Home or AWAY, and 11
    # is Draw or Away. Taking 10 for the middle sign would book 12 as 1X.
    "DC2_1X": {"marketId": 85, "outcomeId": 9,  "specifier": ""},
    "DC2_12": {"marketId": 85, "outcomeId": 10, "specifier": ""},
    "DC2_X2": {"marketId": 85, "outcomeId": 11, "specifier": ""},
})

# HIGHEST SCORING HALF. 52 is the match, 53 the home team's own goals and 54
# the away team's, and all three share one outcome triple: 436 is the 1st half,
# 438 the 2nd and 440 Equal. The ids are NOT 1/2/3 and not consecutive, which
# is why they were read rather than assumed - taken off their catalogue on
# 14 Sep 2026 and checked identical on three events (sr:match:72221274,
# sr:match:72221290, sr:match:67015370), two of them ordinary fixtures rather
# than featured ones, so this is not a big-league-only family.
for _hh, _mkt in (("", 52), ("H_", 53), ("A_", 54)):
    for _sfx, _out in (("1", 436), ("2", 438), ("E", 440)):
        PASSTHROUGH_MAP[f"HIGHHALF_{_hh}{_sfx}"] = {
            "marketId": _mkt, "outcomeId": _out, "specifier": ""}

# ASIAN HANDICAP. Market 16, outcome 1714 the home side and 1715 the away side,
# with the line in the specifier. The line is always quoted from the HOME
# team's point of view on both books, so hcp=-1 is the home side giving a goal
# and the away outcome on that same line is receiving it.
# Generated across the lines both books quote, quarters included. A line a book
# does not price on a given fixture is refused by the existing not_priced path,
# which is a named answer rather than a silent one.
# Halves and wholes only. Their card quotes -4.5 to 5 in half steps and no
# quarter lines at all, measured across 355 events, so the quarters this
# generated were codes that could never be booked here. Bet9ja does quote
# them, and those legs read and split there rather than crossing.
_AH_LINES = ["-4.5", "-4", "-3.5", "-3", "-2.5", "-2", "-1.5", "-1", "-0.5",
             "0", "0.5", "1", "1.5", "2", "2.5", "3", "3.5", "4", "4.5", "5"]
for _l in _AH_LINES:
    PASSTHROUGH_MAP[f"AH_1_{_l}"] = {
        "marketId": 16, "outcomeId": 1714, "specifier": f"hcp={_l}"}
    PASSTHROUGH_MAP[f"AH_2_{_l}"] = {
        "marketId": 16, "outcomeId": 1715, "specifier": f"hcp={_l}"}
# Deliberately NOT merged into MARKET_MAP. That table means "markets we model,
# and therefore fetch on every sweep", and test_every_mapped_market_is_actually
# _fetched enforces exactly that. Merging these made six promotion markets look
# like markets the board prices, which would have widened a 49-request sweep
# this server has been blocked for less than. The test caught it.
def market_for(code):
    """Ids for a market code, modelled or pass-through."""
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


_ODDS_LOOKUP = {}
for _code, _m in list(MARKET_MAP.items()) + list(PASSTHROUGH_MAP.items()):
    _ODDS_LOOKUP[(str(_m["marketId"]), str(_m["outcomeId"]), _m.get("specifier", "") or "")] = _code

_FIXTURES_CACHE = {"at": 0, "data": None}
# Longer than it was. Every refresh is fifty-odd requests to SportyBet, and
# fixtures for the days ahead barely move between builds - so the useful
# thing to optimise is how rarely we ask, not how fast we ask.
# THE SAME REQUEST BUDGET, SPENT WHERE THE SLIPS ARE (owner, 9 Oct 2026).
# Refusals come from games kicking off soon, whose lines SportyBet moves and
# closes; games days away barely change. So the whole card is swept every 90
# minutes instead of 45, and in between the next 12 hours alone (their own
# timeline=12 filter) every 15 minutes - slowed further whenever that window
# is big, so it never spends more than _NEAR_PER_HOUR. Together that is no
# more an hour than the 45-minute sweep was: ~200 + up to ~210.
_FIXTURES_TTL = 90 * 60
_NEAR_EVERY = 15 * 60
_NEAR_HOURS = 12
_NEAR_PER_HOUR = 200
_LAST_FETCH = {"requests": 0, "complete": True}
_LIVE_CACHE = {"at": 0, "data": None}
_LIVE_TTL = 30
# Bet9ja has no bulk endpoint: one request per competition, 170 of them, about
# two minutes. Same reasoning as above - the thing to optimise is how rarely we
# ask. Their fixtures move no faster than SportyBet's.
_BET9JA_CACHE = {"at": 0, "data": None}
_BET9JA_TTL = 45 * 60
# BetKing needs three requests for the same window, one per day, because their
# day feed is a bulk endpoint. Same TTL anyway: their prices move no faster
# than the other two, and the point of the interval is how rarely we ask.
_BETKING_CACHE = {"at": 0, "data": None}
_BETKING_TTL = 45 * 60
# Betpawa pages its whole board rather than crawling dates: ten requests for
# 920 fixtures reaching two months out. Same TTL as the rest, for the same
# reason - the interval is about how rarely we ask, not how fast we can.
_BETPAWA_CACHE = {"at": 0, "data": None}
_BETPAWA_TTL = 45 * 60
_ONEXBET_CACHE = {"at": 0, "data": None}
_ONEXBET_TTL = 45 * 60
# What the last sweep read and skipped, served beside the feed (review M5), so
# a date missing because the sweep hit its deadline is visible from outside.
_ONEXBET_STATS = {"listed": 0, "skipped": 0}

# --- Shared cache (opt-in) -------------------------------------------------
# With one process the in-memory dicts above are fine. Set REDIS_URL (add a
# Redis service on Railway) and the fixture/live caches move to Redis so multiple
# gunicorn workers / replicas share one copy instead of each keeping its own and
# each scraping SportyBet. Unset = unchanged behavior. Every Redis call falls
# back to the local dict on error, so a Redis blip can never take an endpoint down.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis = None
if REDIS_URL:
    try:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True,
                                socket_connect_timeout=3, socket_timeout=3)
        _redis.ping()
        log.info("Redis enabled: fixture/live cache shared across workers")
    except Exception as ex:  # noqa: BLE001
        log.warning("REDIS_URL set but Redis unavailable; using in-memory cache: %s", ex)
        _redis = None

def _cache_get(name, mem):
    """Return the {'at','data'} entry for a cache. Prefer Redis when enabled,
    fall back to the process-local dict on any miss/error."""
    if _redis:
        try:
            v = _redis.get("sw:cache:" + name)
            if v:
                return json.loads(v)
        except Exception as ex:  # noqa: BLE001
            log.warning("redis get %s failed, using local: %s", name, ex)
    return mem if mem.get("data") is not None else None

def _cache_put(name, mem, data):
    """Store a cache entry. Always update the local dict (fallback + no-Redis
    path); mirror to Redis when enabled."""
    mem["at"] = time.time(); mem["data"] = data
    if _redis:
        try:
            _redis.set("sw:cache:" + name, json.dumps({"at": mem["at"], "data": data}))
        except Exception as ex:  # noqa: BLE001
            log.warning("redis set %s failed: %s", name, ex)

# Markets pulled for each upcoming fixture, merged by eventId so the frontend
# gets every side it needs to de-vig, blend, and show real odds:
#   1  = 1X2 (home/draw/away)      10 = double chance (1X/12/X2)
#   18 = over/under totals          29 = both teams to score (GG/NG)
#   68 = first-half over/under      (total=0.5 is the one the model predicts)
#   19 = home team goals             20 = away team goals
# Double chance is a default-enabled market and legOdd() reads its odds directly,
# so 10 must be fetched or those picks fall back to estimated odds. The same
# now applies to 68: a market the builder can select has to arrive with real
# odds, or every first-half leg is priced off an estimate.
#
# THE CHANCE-MIX FAMILY WAS THE RULE ABOVE BEING BROKEN. The builder offers
# "Draw or over 2.5", "Result or over 2.5", "Draw or GG" and "Result or GG",
# and none of them were fetched - so they were the only markets on the site
# priced from the model instead of from the book. Reported as combo odds being
# wildly high, and that is exactly the shape it takes: these legs are short
# (real prices read back from BetKing: 1.05, 1.07, 1.10, 1.11, 1.21), so a
# target needs thirty or forty of them, and a few percent of error per leg
# compounds - 1.08^40 is about twenty-one times. Every other market looked
# right because every other market carried the bookmaker's own number.
#
#   854 = 1 or over 2.5     856 = X or over 2.5     858 = 2 or over 2.5
#   860 = 1 or GG           861 = X or GG           862 = 2 or GG
#
# The UNDER halves (855, 857, 859) are deliberately not here: the builder does
# not offer them, and each id is a full pass over the card.
#
# LAST ON PURPOSE. Each id is its own paged sweep, this doubles the passes from
# seven to thirteen, and the 900-second deadline above truncates whatever has
# not run yet. Ordered so a slow day costs the combo prices - which fall back
# to an estimate, as they always did - rather than 1X2 or over/under, which
# everything from the board to the booking pre-flight leans on.
FIXTURE_MARKET_IDS = ("1", "10", "18", "29", "68", "19", "20",
                      "854", "856", "858", "860", "861", "862",
                      # Corners, total for the match. Swept for AVAILABILITY
                      # as much as price: books open corners a few days before
                      # kick-off and some leagues never, and the site's corners
                      # chip offers a line only where this feed quotes it. On
                      # 23 Sep 2026 the list endpoint returned 17 of 300 events
                      # with it open - the chip without this built slips no
                      # book would take. The codes already sit in
                      # PASSTHROUGH_MAP, so _ODDS_LOOKUP maps them unchanged.
                      "166",
                      # Total shots - the same reason as corners: sold on
                      # marquee games about a day out, lines that move with
                      # the game, and the site offers only what this quotes.
                      "900394",
                      # Asian handicap, every line (30 Sep 2026), for the
                      # site's Handicap chip: books sell a few lines per game
                      # around its own handicap, so the chip offers only lines
                      # this feed quotes. Without it the chip booked lines no
                      # book listed ("a lot of markets are closed").
                      "16",
                      # Corners per team, home then away (25 Sep 2026), for
                      # the site's Team corners chip: 108 listed events had
                      # them against 54 with the total. LAST on purpose - if
                      # the sweep ever runs out of time these are what gets
                      # cut, never the markets above.
                      "900300", "900301",
                      # Team shots, home then away (30 Sep 2026). After team
                      # corners, so a sweep short on time loses these first:
                      # only the Team shots chip leans on them.
                      "900552", "900553")


def _headers(region="ng"):
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


# THE BOOKING QUEUE (owner, 9 Oct 2026 - "SportyBet throttling Railway is the
# risk"). Every booking, probe call and live-card read goes to SportyBet from
# one IP, and a burst from a datacentre IP is what they refuse. So at most
# _SPORTY_SLOTS of them are in flight at once, whatever the traffic; the rest
# wait their turn, and one that waits too long is told "busy" honestly rather
# than adding to the burst. One process (workers 1), so this is global.
_SPORTY_SLOTS = 3
_SPORTY_SEM = threading.BoundedSemaphore(_SPORTY_SLOTS)
_SLOT_WAIT_S = 10


def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    headers = dict(_headers(region)); headers["Content-Type"] = "application/json"
    if not _SPORTY_SEM.acquire(timeout=_SLOT_WAIT_S):
        log.warning("booking queue full for %ss; answered busy", _SLOT_WAIT_S)
        return {"error": "busy", "busy": True, "sent": selections_list}
    try:
        response = requests.post(url, json={"selections": selections_list},
                                 headers=headers, impersonate="chrome120", timeout=10)
        data = response.json()
        if data.get("bizCode") == 10000:
            return {"code": data.get("data", {}).get("shareCode")}
        return {"error": data.get("message") or data, "sent": selections_list}
    except Exception as e:  # noqa: BLE001
        # Deliberately broad: this is a user-facing path and the route relies on
        # always getting a dict back (never a 500). Log so failures are visible.
        log.warning("booking request to SportyBet failed: %s", e)
        return {"error": f"request failed: {e}", "sent": selections_list}
    finally:
        _SPORTY_SEM.release()


def _extract_odds(event):
    """Return {code: odds_float} for the markets we care about."""
    odds = {}
    for mk in (event.get("markets") or []):
        mid = str(mk.get("id"))
        spec = mk.get("specifier") or ""
        for oc in (mk.get("outcomes") or []):
            oid = str(oc.get("id"))
            od = oc.get("odds")
            if od in (None, "", "-"):
                continue
            code = _ODDS_LOOKUP.get((mid, oid, spec))
            if code:
                try:
                    odds[code] = float(od)
                except (ValueError, TypeError):
                    pass  # non-numeric odds value - skip this outcome
    return odds


def fetch_sportybet_fixtures(region="ng", timeline=None):
    """Fetch upcoming events and merge odds across the markets we bet on.

    The pcUpcomingEvents endpoint returns each event's `markets` array filtered
    to the marketId requested, so a single-market fetch (the old behaviour) only
    ever yielded 1X2 odds - OVER/UNDER and GG/NG never arrived and the frontend
    had nothing to de-vig or blend for those. We now fetch each market and merge
    odds by eventId. Event metadata (teams, kickoff) is taken from whichever
    market first surfaces the event.

    Cost: ~3x the requests, paid only on a cache miss (TTL _FIXTURES_TTL).
    Partial failure (one market down) still returns the odds we did get; total
    failure raises so the caller can serve stale.
    """
    headers = _headers(region)
    by_event = {}   # eventId -> merged match dict
    order = []      # preserve first-seen order
    errors = 0

    # Sequential, deliberately, after parallel took the endpoint down.
    # Eight markets by seven pages is fifty-six round trips, and issuing them
    # concurrently from Railway got every single one refused - SportyBet is
    # tolerant of a steady caller and not of a burst from a datacentre IP.
    # Locally, on a residential connection, the same code ran in 6.6s, which
    # is exactly the kind of difference that only shows up in production.
    #
    # So: one at a time, with a deadline instead of a worker timeout deciding
    # when to stop. Whatever has arrived by then is returned and cached;
    # partial data serves the site, a killed worker serves nothing. The
    # gunicorn timeout sits well beyond this so the deadline is always what
    # ends the fetch.
    # Seven markets by seven pages is forty-nine round trips at roughly a
    # second each, plus the pause between them - a fifty-five second budget
    # cut the last markets off entirely and cached a partial feed, which is
    # why team totals had no real odds after the first successful fetch.
    # Gunicorn allows ninety, so this leaves headroom and still ends the
    # fetch itself rather than letting a killed worker do it.
    # Generous, because nobody is waiting on this any more - it runs on a
    # background thread, not inside a visitor's request.
    # Raised from 240 after measuring what it was actually costing.
    #
    # Coverage of the published feed fell almost exactly in fetch order:
    # 1X2 100%, double chance 98%, totals 90%, GG 86%, then team totals 75%
    # and 70%. Splitting the feed by position made the cause plain - across
    # the first tenth of events every market sat at 98%, and across the last
    # tenth 1X2 was still 100% while away totals had collapsed to 36%. Missing
    # odds were clustered in the last pages of the last markets, which is the
    # shape of a fetch running out of time, not of a bookmaker declining to
    # price a game.
    #
    # That cost real money: a leg with no odds cannot be booked, and one
    # unbookable leg rejects the whole slip. The site was reporting "SportyBet
    # wouldn't take this slip" for markets SportyBet was in fact offering.
    #
    # Nothing waits on this. It runs on a background thread every forty-five
    # minutes, so even a full fifteen-minute fetch is a third of the cycle.
    # The old budget was the constraint; there was never a reason for it to be
    # this tight.
    deadline = time.time() + (300 if timeline else 900)
    window = f"&timeline={int(timeline)}" if timeline else ""
    requests_made, complete = 0, True

    for market_id in FIXTURE_MARKET_IDS:
        # Seven pages was seven hundred events, and SportyBet currently lists
        # sixteen hundred across seventeen. Everything past the seventh page
        # simply did not exist as far as this site was concerned - which is
        # why a National League tie on page eight showed as unavailable while
        # SportyBet was plainly offering it, and why the cup ties were thin.
        # Empty pages break the loop below, so a market with less to say
        # still costs only one wasted request rather than thirteen.
        for page in range(1, 21):
            if time.time() > deadline:
                # Reported, not just logged. This truncates the feed and the
                # damage shows up far away - as an unbookable slip - so it has
                # to be visible somewhere other than a log nobody trawls.
                report("fixtures fetch hit its deadline",
                       level="warning", market=market_id, page=page,
                       events=len(by_event),
                       markets_done=FIXTURE_MARKET_IDS.index(market_id),
                       markets_total=len(FIXTURE_MARKET_IDS))
                log.warning("fixtures fetch hit its deadline at market %s page %s; "
                            "returning %d events", market_id, page, len(by_event))
                complete = False
                break
            url = (f"https://www.sportybet.com/api/{region}/factsCenter/pcUpcomingEvents"
                   f"?sportId=sr:sport:1&marketId={market_id}&pageSize=100&pageNum={page}{window}")
            # One retry before abandoning a market. A single refused request
            # used to zero every market it touched, which is how one bad
            # minute turned into an empty feed.
            data = None
            for attempt in (1, 2):
                requests_made += 1
                try:
                    r = requests.get(url, headers=headers, impersonate="chrome120", timeout=12)
                    data = r.json()
                    break
                except (RequestsError, ValueError) as ex:
                    errors += 1
                    log.warning("fixtures fetch failed (market %s page %s, try %s): %s",
                                market_id, page, attempt, ex)
                    if attempt == 1:
                        # Longer than it looks like it needs to be. These
                        # failures are a throttle, not a blip, and coming
                        # straight back just spends the second try on the
                        # same refusal.
                        time.sleep(4)
            if data is None:
                complete = False
                break  # give up on this market, move to the next
            # A real pause between pages, not a token one. At roughly one and
            # a half requests a second SportyBet started refusing partway
            # through - and because a refused first page abandons the whole
            # market, that silently dropped the team-total odds from the feed.
            # Slower here costs nothing: this runs on a background thread every
            # forty-five minutes, and nobody is waiting for it.
            time.sleep(0.45)
            if data.get("bizCode") != 10000:
                break
            d = data.get("data", {}) or {}
            # Keep each event paired with its tournament. Flattening the events
            # out of `tournaments` used to throw the competition away, which left
            # every fixture league-less and forced the consumer to guess - so a
            # cup tie between two Premier League sides came out as the Premier
            # League, and ordinary league games came out as "England Cup".
            # Same "{category} {name}" shape the livescores feed uses.
            events = []
            for t in (d.get("tournaments") or []):
                cat = ((t.get("category") or {}).get("name")) or t.get("categoryName") or ""
                nm = t.get("name") or ""
                lg = (f"{cat} {nm}").strip() if cat else nm
                for e in (t.get("events") or []):
                    events.append((lg, e))
            for e in (d.get("events") or []):
                events.append(("", e))
            if not events:
                break
            for lg, e in events:
                eid = e.get("eventId")
                if not eid:
                    continue
                m = by_event.get(eid)
                if m is None:
                    m = {
                        "eventId": eid,
                        "homeTeam": e.get("homeTeamName"),
                        "awayTeam": e.get("awayTeamName"),
                        "startTime": e.get("estimateStartTime"),
                        "league": lg,
                        "odds": {},
                    }
                    by_event[eid] = m
                    order.append(eid)
                elif lg and not m.get("league"):
                    # A later market can surface a tournament the first one
                    # listed loose under `events`, so fill the gap if we can.
                    m["league"] = lg
                # Merge this market's odds into whatever we already have.
                m["odds"].update(_extract_odds(e))
    _LAST_FETCH.update(requests=requests_made, complete=complete)
    if not by_event and errors:
        # Total failure - let the route serve stale rather than cache an empty set.
        raise RuntimeError("all SportyBet fixture fetches failed")

    # Per-market coverage, so a thin feed is a number rather than a guess.
    # A market well below the 1X2 count means its later pages did not arrive,
    # and every event it is missing is a leg that cannot be booked.
    total = len(by_event)
    priced = {}
    for m in by_event.values():
        for code in m["odds"]:
            priced[code] = priced.get(code, 0) + 1
    thin = sorted(
        ((c, n) for c, n in priced.items() if total and n < total * 0.9),
        key=lambda x: x[1])
    log.info("fixtures: %d events, %d markets priced, %d errors",
             total, len(priced), errors)
    if thin:
        log.warning("fixtures: thin coverage on %s",
                    ", ".join(f"{c} {n}/{total}" for c, n in thin[:8]))
    return [by_event[eid] for eid in order]


def _map_live_status(s):
    s = (s or "").upper()
    if s in ("FT", "AET", "PEN", "ENDED", "FINISHED"):
        return "FT"
    if s in ("HT", "HALFTIME", "PAUSE"):
        return "HT"
    return s or "LIVE"


def _extract_live_events(d):
    pairs = []
    tours = []
    if isinstance(d, list):
        tours = d
    elif isinstance(d, dict):
        tours = d.get("tournaments") or []
        if not tours and d.get("events"):
            return [("", e) for e in d["events"]]
    for t in tours:
        if not isinstance(t, dict):
            continue
        cat = ((t.get("category") or {}).get("name")) or t.get("categoryName") or ""
        nm = t.get("name") or ""
        lg = (f"{cat} {nm}").strip() if cat else nm
        evs = t.get("events")
        if evs:
            for e in evs:
                pairs.append((lg, e))
        else:
            pairs.append((lg, t))
    return pairs


def fetch_live_scores(region="ng"):
    headers = _headers(region)
    matches = []
    # liveOrPrematchEvents ignores pageNum: pages 1 through 5 come back with
    # byte-identical event lists. Looping them appended the same 71 events five
    # times, so the feed served 400 entries for 80 matches and every client
    # polling it every 30 seconds paid for four fifths of nothing. The loop
    # stays in case the endpoint ever grows real paging, but it now stops the
    # moment a page brings nothing new.
    seen_ids = set()
    for page in range(1, 6):
        url = (f"https://www.sportybet.com/api/{region}/factsCenter/liveOrPrematchEvents"
               f"?sportId=sr:sport:1&marketId=1&pageSize=100&pageNum={page}")
        r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
        data = r.json()
        if data.get("bizCode") != 10000:
            break
        pairs = _extract_live_events(data.get("data"))
        if not pairs:
            break
        fresh = []
        for lg, e in pairs:
            if not isinstance(e, dict):
                continue
            # eventId when there is one, otherwise the pairing and its
            # competition - an event with no id must still not arrive twice.
            key = e.get("eventId") or "{}|{}|{}".format(
                e.get("homeTeamName"), e.get("awayTeamName"), lg)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            fresh.append((lg, e))
        if not fresh:
            break
        pairs = fresh
        for lg, e in pairs:
            if not isinstance(e, dict):
                continue
            status_raw = (e.get("matchStatus") or e.get("period")
                          or e.get("eventStatus") or e.get("playStatus") or "")
            gs = e.get("gameScore")
            ps = e.get("playedSeconds")
            _st = _map_live_status(status_raw)
            is_live = bool(ps) or e.get("matchStatus") in ("H1", "H2", "HT", "ET", "P") \
                or (isinstance(gs, list) and len(gs) > 0)
            # Ended games have no clock, so they'd be dropped - but the results
            # capture needs them. Keep FT too.
            if not is_live and _st != "FT":
                continue
            hs = e.get("homeScore")
            aw = e.get("awayScore")
            ss = e.get("setScore")
            # setScore is the running total. gameScore is the same thing split
            # by period - Crystal Palace v Man City at 73 minutes carried
            # setScore "1:3" and gameScore ["0:1","1:2"], which sums to it.
            #
            # This used to read gameScore[0] first, so it published the FIRST
            # HALF as the live score and never reached the setScore branch
            # below, because hs and aw were no longer None by then. Every match
            # that scored in the second half was reported wrong: 45 of the 71
            # live games on the board when this was found, Bayern Munich among
            # them, showing 1-0 at the 90th minute of a game that finished 4-1.
            # Downstream that is worse than a wrong number on a screen - the
            # results sweep banks these as final scores, so a tip that landed
            # gets recorded as a loss.
            if (hs is None or aw is None) and isinstance(ss, str) and ":" in ss:
                try:
                    p = ss.split(":"); hs = int(p[0]); aw = int(p[1])
                except (ValueError, IndexError):
                    pass
            # Only if setScore is missing: add the periods up rather than
            # taking one of them.
            if (hs is None or aw is None) and isinstance(gs, list) and gs:
                th = ta = 0
                ok = False
                for part in gs:
                    if not isinstance(part, str) or ":" not in part:
                        continue
                    try:
                        a, b = part.split(":"); th += int(a); ta += int(b); ok = True
                    except (ValueError, IndexError):
                        ok = False
                        break
                if ok:
                    hs, aw = th, ta
            minute = None
            if isinstance(ps, str):
                if ":" in ps:
                    try:
                        minute = int(ps.split(":")[0])
                    except (ValueError, IndexError):
                        pass
                elif ps.isdigit():
                    minute = int(ps) // 60
            elif isinstance(ps, (int, float)):
                minute = int(ps) // 60
            matches.append({
                "league": lg or "",
                "home": e.get("homeTeamName"),
                "away": e.get("awayTeamName"),
                "homeScore": hs, "awayScore": aw, "minute": minute,
                "status": _map_live_status(status_raw),
                "homeGoals": [], "awayGoals": [], "homeReds": 0, "awayReds": 0,
            })
    return matches


# --- background refresher -------------------------------------------------
# Seven markets by seven pages is forty-nine round trips to SportyBet. That
# never belonged inside a visitor's request: done serially it outran the
# worker timeout and the worker was killed before it could fall back to stale
# data, and done concurrently the burst got this server refused outright.
# Either way the endpoint answered 500 and the cache could never refresh.
#
# So the fetch runs on its own thread and the route only ever reads what that
# thread has stored. Nobody waits for SportyBet, a slow or refused fetch costs
# a stale answer rather than an error, and the request path cannot time out
# because it does no network work at all.
_REFRESH_LOCK = threading.Lock()

def _refresh_fixtures_once():
    """Replace the stored feed only with something at least as complete.

    A throttled pass does not fail outright, it comes back short. SportyBet
    starts refusing partway through, a refused page abandons the rest of that
    market, and what returns is a smaller feed that looks perfectly valid.
    Storing it would drop fixtures and whole markets from the site while every
    health check still read green, which is the worst kind of failure: quiet,
    and indistinguishable from a thin day.
    """
    try:
        matches = fetch_sportybet_fixtures()
        if not matches:
            log.warning("fixtures refresh returned nothing; keeping previous copy")
            return False
        prev = _cache_get("fixtures", _FIXTURES_CACHE)
        prev_n = len((prev or {}).get("data") or [])
        # A fifth down is weather: a card genuinely thins out overnight. Much
        # beyond that on a feed this size is the throttle, not the day.
        if prev_n and len(matches) < prev_n * 0.8:
            log.warning("fixtures refresh returned %d against %d stored, looks "
                        "truncated; keeping the fuller copy", len(matches), prev_n)
            return False
        _cache_put("fixtures", _FIXTURES_CACHE, matches)
        log.info("fixtures refreshed: %d events", len(matches))
        return True
    except Exception as ex:  # noqa: BLE001
        log.warning("fixtures refresh failed, keeping previous copy: %s", ex)
    return False

def _merge_near(old, near, complete):
    """The stored card with the next-12-hours pass laid over it. A game in
    both takes the fresh odds - whole, so a line SportyBet closed disappears,
    but only when every market answered; a short pass only adds and updates,
    so a throttled request never wipes a price. Games the pass did not mention
    are left exactly as they were; new ones are appended."""
    fresh = {m["eventId"]: m for m in near if m.get("eventId")}
    out, seen = [], set()
    for m in old:
        n = fresh.get(m.get("eventId"))
        if n is None:
            out.append(m)
            continue
        seen.add(n["eventId"])
        odds = dict(n["odds"]) if complete else dict(m.get("odds") or {}, **n["odds"])
        out.append(dict(m, odds=odds, startTime=n.get("startTime") or m.get("startTime")))
    out.extend(m for eid, m in fresh.items() if eid not in seen)
    return out


def _refresh_near_once():
    """Re-read only the games kicking off in the next _NEAR_HOURS. Never runs
    beside the full sweep - both live on the one refresher thread."""
    try:
        prev = _cache_get("fixtures", _FIXTURES_CACHE)
        if not prev or not prev.get("data"):
            return False
        near = fetch_sportybet_fixtures(timeline=_NEAR_HOURS)
        if not near:
            return False
        merged = _merge_near(prev["data"], near, _LAST_FETCH["complete"])
        _cache_put("fixtures", _FIXTURES_CACHE, merged)
        log.info("fixtures near pass: %d games in %dh, %d requests, complete=%s",
                 len(near), _NEAR_HOURS, _LAST_FETCH["requests"], _LAST_FETCH["complete"])
        return True
    except Exception as ex:  # noqa: BLE001
        log.warning("fixtures near pass failed, keeping previous copy: %s", ex)
    return False


def _near_wait():
    """Seconds until the next near pass: every _NEAR_EVERY, or longer when
    the last one was big, so near passes stay under _NEAR_PER_HOUR."""
    return max(_NEAR_EVERY, _LAST_FETCH["requests"] * 3600 / _NEAR_PER_HOUR)


def _fixtures_loop():
    # With a shared cache the copy in Redis outlives this process, so a
    # redeploy usually starts with data that is minutes old. Refetching it
    # straight away would spend forty-nine requests to replace something we
    # already have - and every one of those is a request that got this server
    # refused once. Wait out whatever is left of its life instead.
    # Near passes reset the stored copy's age, so the full sweep keeps its own
    # clock from here; after a redeploy the first full sweep simply waits one
    # interval and near passes cover the games that matter meanwhile.
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    next_full = time.time()
    if entry and entry.get("data"):
        next_full = time.time() + _FIXTURES_TTL
        log.info("fixtures cache present; first full sweep in %ds, near passes meanwhile",
                 _FIXTURES_TTL)
        time.sleep(_NEAR_EVERY)
    while True:
        if time.time() >= next_full:
            ok = _refresh_fixtures_once()
            # Retry sooner after a failure than after a success, but never so
            # soon that a refused IP gets hammered back into refusing.
            next_full = time.time() + (_FIXTURES_TTL if ok else 300)
            wait = _NEAR_EVERY
        else:
            _refresh_near_once()
            wait = _near_wait()
        time.sleep(max(60, min(wait, next_full - time.time())))

def _start_fixtures_thread():
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_fixtures_loop, name="fixtures-refresh", daemon=True)
    t.start()
    log.info("fixtures refresher started (every %dm)", _FIXTURES_TTL // 60)

# STARTED PER PROCESS, WHICH IS WHY THE PROCFILE PINS --workers 1.
#
# This sweep is fifty-six sequential round trips to SportyBet, kept sequential
# because issuing them concurrently got every one refused from a Railway IP.
# Gunicorn imports this module once per worker, so each extra worker starts
# another copy of this loop and another Bet9ja sweep below - N workers is N
# times the traffic at an endpoint that already refuses bursts.
#
# Concurrency for REQUESTS comes from threads instead (--worker-class gthread
# --threads 8): one process, one refresher, one cache, many handlers. The work
# is all I/O waiting on SportyBet, Bet9ja and Redis, so threads are free here.
# Before that, gunicorn's default single sync worker served one request at a
# time and booking taps queued behind whatever the page was already fetching:
# five concurrent hits on the trivial / endpoint returned at 2.2, 4.5, 5.6 and
# 9.5 seconds, and a sixth never did.
_start_fixtures_thread()


# --- Bet9ja fixtures -------------------------------------------------------
# The same pattern as above and for the same reasons, with one addition: Bet9ja
# publishes its own event count per competition, so a sweep can be checked
# against an outside opinion rather than only against the last one we stored.
# That matters here more than it does for SportyBet. Bet9ja answers a datacentre
# with a block page rather than an error, which is how these routes spent the
# first hour of their life reporting a successful fetch of nothing.
_BET9JA_LOCK = threading.Lock()

def _refresh_bet9ja_once():
    try:
        fixtures, stats = bet9ja.all_fixtures()
    except Exception as ex:                          # noqa: BLE001 - background
        log.warning("bet9ja refresh failed, keeping previous copy: %s", ex)
        return False

    expected = stats.get("expected") or 0
    got = len(fixtures)
    if not fixtures:
        log.warning("bet9ja refresh returned nothing; keeping previous copy")
        return False
    # Their own catalogue said how many events exist. Coming back well under
    # that is a throttled or blocked sweep, not a thin day, and storing it
    # would quietly shrink the board.
    if expected and got < expected * 0.9:
        log.warning("bet9ja refresh collected %d of %d they list; keeping "
                    "previous copy (failed=%d short=%d)", got, expected,
                    len(stats.get("failed") or []), len(stats.get("short") or []))
        return False
    prev = _cache_get("bet9ja", _BET9JA_CACHE)
    prev_n = len((prev or {}).get("data") or {})
    if prev_n and got < prev_n * 0.8:
        log.warning("bet9ja refresh returned %d against %d stored, looks "
                    "truncated; keeping the fuller copy", got, prev_n)
        return False

    _cache_put("bet9ja", _BET9JA_CACHE, fixtures)
    log.info("bet9ja refreshed: %d events of %d listed, %d competitions, "
             "%d failed", got, expected, stats.get("competitions"),
             len(stats.get("failed") or []))
    for s in (stats.get("short") or [])[:5]:
        log.info("bet9ja short: %s wanted %s got %s",
                 s.get("league"), s.get("want"), s.get("got"))
    return True

def _bet9ja_loop():
    entry = _cache_get("bet9ja", _BET9JA_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _BET9JA_TTL:
            time.sleep(_BET9JA_TTL - age)
    while True:
        ok = _refresh_bet9ja_once()
        time.sleep(_BET9JA_TTL if ok else 300)

def _start_bet9ja_thread():
    if not _BET9JA_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_bet9ja_loop, name="bet9ja-refresh", daemon=True)
    t.start()
    log.info("bet9ja refresher started (every %dm)", _BET9JA_TTL // 60)

_start_bet9ja_thread()


# --- BetKing fixtures ------------------------------------------------------
# The third book. Three requests rather than SportyBet's fifty or Bet9ja's
# hundred and seventy, because their day feed is bulk - but the same guard
# applies and for the same reason: their feed answers with a count of what it
# holds for the date, so a sweep that comes back well under what they say they
# have is a throttled sweep, not a quiet Tuesday.
_BETKING_LOCK = threading.Lock()

def _refresh_betking_once():
    try:
        fixtures, stats = betking.all_fixtures()
    except Exception as ex:                          # noqa: BLE001 - background
        log.warning("betking refresh failed, keeping previous copy: %s", ex)
        return False

    got, listed = len(fixtures), stats.get("listed") or 0
    if not fixtures:
        log.warning("betking refresh returned nothing; keeping previous copy")
        return False
    if listed and got < listed * 0.9:
        log.warning("betking refresh collected %d of %d they list; keeping "
                    "previous copy (failed days: %s)", got, listed,
                    ", ".join(stats.get("failed") or []) or "none")
        return False
    prev = _cache_get("betking", _BETKING_CACHE)
    prev_n = len((prev or {}).get("data") or {})
    if prev_n and got < prev_n * 0.8:
        log.warning("betking refresh returned %d against %d stored, looks "
                    "truncated; keeping the fuller copy", got, prev_n)
        return False

    _cache_put("betking", _BETKING_CACHE, fixtures)
    log.info("betking refreshed: %d events of %d listed over %d days",
             got, listed, stats.get("days"))
    return True

def _betking_loop():
    entry = _cache_get("betking", _BETKING_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _BETKING_TTL:
            time.sleep(_BETKING_TTL - age)
    while True:
        ok = _refresh_betking_once()
        time.sleep(_BETKING_TTL if ok else 300)

def _start_betking_thread():
    if not _BETKING_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_betking_loop, name="betking-refresh",
                         daemon=True)
    t.start()
    log.info("betking refresher started (every %dm)", _BETKING_TTL // 60)

_start_betking_thread()


# --- Betpawa, the fourth book ----------------------------------------------
# NO "LISTED" COUNT TO CHECK AGAINST, unlike the other three. Their board is an
# ordered list paged a hundred at a time and they publish no total, so the
# outside opinion that tells a throttled sweep from a quiet day does not exist
# here. What is left is the previous copy, which is why the short-sweep guard
# below is the only one - and why betpawa.all_fixtures reports how many pages
# it actually read rather than only what it collected.
_BETPAWA_LOCK = threading.Lock()

def _refresh_betpawa_once():
    try:
        fixtures, stats = betpawa.all_fixtures()
    except Exception as ex:                          # noqa: BLE001 - background
        log.warning("betpawa refresh failed, keeping previous copy: %s", ex)
        return False

    got = len(fixtures)
    if not fixtures:
        log.warning("betpawa refresh returned nothing; keeping previous copy")
        return False
    prev = _cache_get("betpawa", _BETPAWA_CACHE)
    prev_n = len((prev or {}).get("data") or {})
    if prev_n and got < prev_n * 0.8:
        log.warning("betpawa refresh returned %d against %d stored, looks "
                    "truncated; keeping the fuller copy", got, prev_n)
        return False

    _cache_put("betpawa", _BETPAWA_CACHE, fixtures)
    log.info("betpawa refreshed: %d events over %d pages",
             got, stats.get("pages"))
    return True

def _betpawa_loop():
    entry = _cache_get("betpawa", _BETPAWA_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _BETPAWA_TTL:
            time.sleep(_BETPAWA_TTL - age)
    while True:
        ok = _refresh_betpawa_once()
        time.sleep(_BETPAWA_TTL if ok else 300)

def _start_betpawa_thread():
    if not _BETPAWA_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_betpawa_loop, name="betpawa-refresh",
                         daemon=True)
    t.start()
    log.info("betpawa refresher started (every %dm)", _BETPAWA_TTL // 60)

_start_betpawa_thread()


# 1XBET: one card per fixture, because their game list carries no prices -
# about 18 minutes a sweep against a 45 minute timer (29 Sep 2026). The
# short-sweep guard is Betpawa's; the sweep also counts the games it skipped
# at its deadline, reported here so a cut-short sweep is visible rather than
# reading as a quiet day.
_ONEXBET_LOCK = threading.Lock()

def _refresh_onexbet_once():
    try:
        fixtures, stats = onexbet.all_fixtures()
    except Exception as ex:                          # noqa: BLE001 - background
        log.warning("1xbet refresh failed, keeping previous copy: %s", ex)
        return False
    got = len(fixtures)
    if not fixtures:
        log.warning("1xbet refresh returned nothing; keeping previous copy")
        return False
    prev = _cache_get("onexbet", _ONEXBET_CACHE)
    prev_n = len((prev or {}).get("data") or {})
    if prev_n and got < prev_n * 0.8:
        log.warning("1xbet refresh returned %d against %d stored, looks truncated; "
                    "keeping the fuller copy", got, prev_n)
        # A whole sweep ran and was judged short: waiting the full timer, not
        # five minutes, or 18-minute sweeps run back to back against 1xBet.
        return "kept"
    _ONEXBET_STATS.update(listed=stats.get("listed", 0), skipped=stats.get("skipped", 0))
    if stats.get("skipped"):
        report("1xbet sweep hit its deadline", skipped=stats["skipped"],
               listed=stats["listed"], kept=got)
    _cache_put("onexbet", _ONEXBET_CACHE, fixtures)
    log.info("1xbet refreshed: %d events (%d listed)", got, stats.get("listed"))
    return True

def _onexbet_loop():
    entry = _cache_get("onexbet", _ONEXBET_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _ONEXBET_TTL:
            time.sleep(_ONEXBET_TTL - age)
    while True:
        ok = _refresh_onexbet_once()
        time.sleep(_ONEXBET_TTL if ok else 300)

def _start_onexbet_thread():
    if not _ONEXBET_LOCK.acquire(blocking=False):
        return
    threading.Thread(target=_onexbet_loop, name="onexbet-refresh", daemon=True).start()
    log.info("1xbet refresher started (every %dm)", _ONEXBET_TTL // 60)

_start_onexbet_thread()


@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if entry and entry.get("data"):
        age = int(time.time() - entry["at"])
        try:
            refused = [k.split("|", 1) for k in _refused_now()]
        except Exception:                        # noqa: BLE001 - the odds still go out
            refused = []
        return jsonify({"success": True, "cached": True, "ageSeconds": age,
                        "stale": age > _FIXTURES_TTL,
                        "count": len(entry["data"]), "matches": entry["data"],
                        "refused": refused})
    # Nothing stored yet - the refresher is on its first pass. 503 rather than
    # 500 so callers treat it as "not ready", and the CDN in front does not
    # store it as the answer.
    return jsonify({"success": False, "warming": True,
                    "error": "fixtures not loaded yet", "matches": []}), 503


# --- Bet9ja (odds only, for now) -------------------------------------------
# Bet9ja rivals SportyBet for users in Nigeria, and a booking code is only any
# use to somebody who holds an account with the bookmaker that issued it - so
# this is a second source alongside, not a replacement.
#
# Both halves are verified end to end: a slip built here was booked through
# Bet9ja and the code loaded on their own site with the right three selections.
# See bet9ja.py for the fields that had to be exact.
@app.route('/api/bet9ja/fixtures', methods=['GET'])
def get_bet9ja_fixtures():
    """Every Bet9ja event, served from the background sweep.

    No `league` argument: the site pairs a fixture to a bookmaker event on team
    names and kick-off time, never on competition, so what it wants is one flat
    bag. Matching by league would mean maintaining a mapping from our 47
    leagues to their GIDs by hand, and buying nothing with it.

    `?league={gid}` still fetches a single competition live, which is for
    debugging one league rather than for the site.
    """
    gid = request.args.get("league")
    if gid:
        try:
            events = bet9ja.fetch_league(int(gid))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "league must be a number"}), 400
        except Exception as ex:                  # noqa: BLE001 - user-facing path
            report("bet9ja fixtures failed", league=gid, error=str(ex))
            return jsonify({"success": False, "error": str(ex), "matches": {}}), 502
        return jsonify({"success": True, "league": int(gid), "cached": False,
                        "count": len(events), "matches": events})

    entry = _cache_get("bet9ja", _BET9JA_CACHE)
    data = (entry or {}).get("data")
    if not data:
        # The sweep takes two minutes, so doing it here would time out. Say so
        # rather than returning an empty bag with success: true - that lie is
        # what made this integration's first outage invisible.
        return jsonify({"success": False, "count": 0, "matches": {},
                        "error": "bet9ja fixtures not loaded yet"}), 503
    return jsonify({"success": True, "cached": True,
                    "ageSeconds": int(time.time() - entry["at"]),
                    "count": len(data), "matches": data})


@app.route('/api/bet9ja/booking-code', methods=['POST'])
def api_bet9ja_code():
    """Turn a set of picks into a Bet9ja booking code.

    Body: {"selections": [{"league": 492, "eventId": "825683591",
                           "code": "1X"}, ...]}

    Odds are re-read from the live feed rather than trusted from the caller: a
    price the site showed a minute ago may have moved, and Bet9ja rejects a
    slip whose odds do not match theirs.
    """
    data = request.get_json(silent=True) or {}
    picks = data.get("selections") or []
    if not picks:
        return jsonify({"success": False, "error": "no selections"}), 400

    # One fetch per SELECTION, against the per-event endpoint. That is the only
    # way to get every market for any league: the league listings either miss
    # team goals or miss most of the competitions. A slip is a handful of legs,
    # so a request each is cheap, and the odds are read fresh at book time
    # anyway because Bet9ja rejects a slip whose prices have moved.
    # Every bad leg, not the first one.
    #
    # This used to return on the first pick Bet9ja would not price, which told
    # the caller about one leg out of forty and gave it nothing to retry with:
    # drop that leg, resend, discover the next one, forty round trips. The
    # SportyBet route has answered with a named `unbookable` list since the
    # "no market there" incident and the client already knows how to drop
    # exactly those and try again. Same shape here, so one client path serves
    # both bookmakers.
    # THREE DIFFERENT THINGS, which used to be one warning.
    #
    # They were logged under a single message, so the one that is a bug and the
    # one that is simply how Bet9ja works arrived as the same Sentry issue,
    # counted together, and re-opened it every hour. It got raised to High
    # priority on 2 September for a slip that was behaving perfectly correctly.
    #
    #   not_mapped   MARKET_MAP has no entry for this code, so the route
    #                refuses the leg on EVERY fixture no matter how well
    #                Bet9ja prices it. One line fixes it for good. THIS IS THE
    #                BUG, and it is the only one of the three worth waking up
    #                for. It cost us both-to-score on every Bet9ja slip once.
    #
    #   not_priced   Mapped, and Bet9ja does not price that market on this
    #                particular game. Verified on 823959654, Spartak Moscow v
    #                Rodina Moscow: 394 priced keys and team-to-score is not
    #                among them. No code change can conjure a price that the
    #                bookmaker is not offering, so the honest answer is to name
    #                the leg and let the punter drop it - which is what
    #                happens. Reported at info: it keeps its count and its
    #                graph without pretending to be a fault.
    #
    #   event_gone   Neither. Their API blinked, or the game left the board
    #                between building the slip and booking it.
    #
    #   suspended    Mapped, priced, and the price is 1 - which is how Bet9ja
    #                spells "this market is closed". Booking it 502s the whole
    #                slip, and it would pay nothing anyway.
    #
    # The mapping is checked BEFORE the fetch, so an unmapped leg no longer
    # costs a request to discover something already known locally.
    resolved = []
    unmapped, unpriced, event_gone, suspended = [], [], [], []
    try:
        for p in picks:
            code = p.get("code")
            # eventId and prediction are the contract: the site keys its retry
            # on exactly this pair. `reason` is additive.
            leg = {"eventId": p.get("eventId"), "prediction": code}
            # market_for, not MARKET_MAP: the pass-through table is a mapping
            # too. Keyed on MARKET_MAP alone, every handicap, corner, card and
            # 2UP leg came back "not_mapped" however well Bet9ja prices it -
            # so no converted slip carrying one could ever be booked here.
            # The same shape of mistake as the SportyBet pre-flight, on the
            # other book: the fetch below reads the full card, whose keys come
            # from BOTH tables, and build_selection resolves through
            # market_for as well.
            if bet9ja.market_for(code) is None:
                leg["reason"] = "not_mapped"
                unmapped.append(leg)
                continue
            ev = bet9ja.fetch_event(p.get("eventId"))
            if not ev:
                leg["reason"] = "event_gone"
                event_gone.append(leg)
                continue
            if code not in (ev.get("raw") or {}):
                leg["reason"] = "not_priced"
                unpriced.append(leg)
                continue
            # PRICED, AND PRICED AT 1. Bet9ja leaves a suspended market on the
            # board with its odds collapsed to 1 rather than removing it, so
            # `code in raw` is true and the leg looks perfectly bookable. Send
            # it and their booking origin 500s, which arrives here as a
            # Cloudflare 502 with no `unbookable` list and nothing to retry -
            # the whole slip dies for one dead market. Observed 13 Sep 2026 on
            # 828935992, Elversberg v Bayern Munich, an hour after kick-off:
            # that one leg 502'd on its own while the other four booked.
            # A price of 1 also pays nothing, so there is no version of this
            # leg worth keeping even if they did take it.
            try:
                price = float(ev["raw"][code])
            except (TypeError, ValueError):
                price = 0.0
            if price <= 1.01:
                leg["reason"] = "suspended"
                suspended.append(leg)
                continue
            resolved.append({"event": ev, "code": code})
    except Exception as ex:                      # noqa: BLE001 - user-facing path
        report("bet9ja odds fetch failed", error=str(ex))
        return jsonify({"success": False, "error": str(ex)}), 502

    def _detail(legs):
        return {
            "bad_legs": len(legs), "total_legs": len(picks),
            "markets": ", ".join(sorted({str(b["prediction"]) for b in legs})),
            "events": ", ".join(sorted({str(b["eventId"]) for b in legs})[:10])}

    # Separate messages, because Sentry groups by message. One issue per cause
    # is the whole point: a mapping gap should stand alone in the list instead
    # of being the third of fourteen events on a mixed issue nobody reads.
    if unmapped:
        report("booking: Bet9ja market is not mapped", **_detail(unmapped))
    if event_gone:
        report("booking: Bet9ja event would not load", **_detail(event_gone))
    if unpriced:
        report("booking: Bet9ja does not price this market on this fixture",
               level="info", **_detail(unpriced))
    if suspended:
        report("booking: Bet9ja has suspended this market",
               level="info", **_detail(suspended))

    # Order matters only in that the punter sees one list. Everything Bet9ja
    # will not take is still unbookable, whichever of the three it is.
    bad = unmapped + event_gone + unpriced + suspended
    if bad:
        return jsonify({
            "success": False,
            "message": "Bet9ja rejected the slip",
            "detail": f"no market there for {len(bad):d} of {len(picks):d} picks",
            "unbookable": bad,
        }), 400

    out = bet9ja.generate_code(resolved)
    if out.get("code"):
        return jsonify({"success": True, **out})
    report("bet9ja booking refused", legs=len(resolved), detail=str(out.get("error"))[:300])
    return jsonify({"success": False, **out}), 502


# --- BetKing ---------------------------------------------------------------
# Verified end to end on 14 Sep 2026: a three-leg slip built here was booked
# through BetKing as PM18D2 and read back with the right three selections at
# the right prices. See betking.py for the ids that had to be exact - in
# particular that the selection id is MatchOddsID and NOT the OutcomeID next to
# it, which books happily and produces a code containing nothing.
@app.route('/api/betking/fixtures', methods=['GET'])
def get_betking_fixtures():
    """Every BetKing event, served from the background sweep.

    One flat bag, no `league` argument, for the same reason Bet9ja's route has
    none: the site pairs on team names and kick-off, never on competition.

    `?date=YYYY-MM-DD` fetches a single day live, for debugging one day rather
    than for the site.
    """
    date = request.args.get("date")
    if date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return jsonify({"success": False,
                            "error": "date must be YYYY-MM-DD"}), 400
        try:
            events, listed = betking.fetch_day(date)
        except Exception as ex:                  # noqa: BLE001 - user-facing
            report("betking fixtures failed", date=date, error=str(ex))
            return jsonify({"success": False, "error": str(ex),
                            "matches": {}}), 502
        return jsonify({"success": True, "date": date, "cached": False,
                        "listed": listed, "count": len(events),
                        "matches": events})

    entry = _cache_get("betking", _BETKING_CACHE)
    data = (entry or {}).get("data")
    if not data:
        # Say so rather than returning an empty bag with success: true. That
        # lie is what made the Bet9ja integration's first outage invisible.
        return jsonify({"success": False, "count": 0, "matches": {},
                        "error": "betking fixtures not loaded yet"}), 503
    return jsonify({"success": True, "cached": True,
                    "ageSeconds": int(time.time() - entry["at"]),
                    "count": len(data), "matches": data})


@app.route('/api/betking/booking-code', methods=['POST'])
def api_betking_code():
    """Turn a set of picks into a BetKing booking code.

    Body: {"selections": [{"eventId": "1005309147", "code": "1X"}, ...]}

    The same three-way answer the other two books give, so one client path
    serves all of them: every leg BetKing will not take comes back named in
    `unbookable` with a reason, rather than the first one killing the request.

      not_mapped   MARKET_MAP has no entry for this code. Refused here before
                   any request is made, since it is already known locally.
      event_gone   Their feed would not return the fixture at all.
      not_priced   Mapped, and BetKing does not price that market on this
                   game. No code change conjures a price a bookmaker is not
                   offering, so it is reported at info: it keeps its count
                   without pretending to be a fault.

    There is no `suspended` case here, unlike Bet9ja. BetKing's suspended
    markets come back with the price collapsed and betking.py drops those while
    parsing, so they arrive as not_priced - which is what they are.
    """
    data = request.get_json(silent=True) or {}
    picks = data.get("selections") or []
    if not picks:
        return jsonify({"success": False, "error": "no selections"}), 400
    # THEIR CAP IS 40, NOT 50. Both other books take 50 selections on a slip
    # and BetKing's own global variables say 40, so a slip that books fine
    # elsewhere is refused here. Better to say so than to mint a code that
    # will not open.
    if len(picks) > betking.BETSLIP_MAX:
        return jsonify({"success": False,
                        "error": f"betking takes at most {betking.BETSLIP_MAX:d} selections",
                        "sent": len(picks)}), 400

    resolved = []
    unmapped, unpriced, event_gone, same_game = [], [], [], []
    # One deep fetch per EVENT, not per selection: the deep card is a megabyte
    # and a slip can name the same fixture twice.
    cache = {}
    # WHICH FIXTURES ARE ALREADY ON THE SLIP. BetKing takes one selection per
    # match on a multiple - their own compatibility list is empty on every
    # market of every event checked - and a coupon carrying two comes back as
    # a code with nothing in it. So the second leg on a game is named here and
    # the client drops exactly that one, rather than the whole slip dying.
    booked_games = set()
    try:
        for p in picks:
            code = p.get("code")
            event_id = p.get("eventId")
            # eventId and prediction are the contract the site keys its retry
            # on. `reason` is additive.
            leg = {"eventId": event_id, "prediction": code}
            if betking.market_for(code) is None:
                leg["reason"] = "not_mapped"
                unmapped.append(leg)
                continue
            if event_id in booked_games:
                leg["reason"] = "same_game"
                same_game.append(leg)
                continue
            if event_id not in cache:
                cache[event_id] = betking.fetch_event(event_id)
            ev = cache[event_id]
            if not ev:
                leg["reason"] = "event_gone"
                event_gone.append(leg)
                continue
            if code not in (ev.get("odds") or {}):
                leg["reason"] = "not_priced"
                unpriced.append(leg)
                continue
            booked_games.add(event_id)
            resolved.append({"event": ev, "code": code})
    except Exception as ex:                      # noqa: BLE001 - user-facing
        report("betking odds fetch failed", error=str(ex))
        return jsonify({"success": False, "error": str(ex)}), 502

    def _detail(legs):
        return {
            "bad_legs": len(legs), "total_legs": len(picks),
            "markets": ", ".join(sorted({str(b["prediction"]) for b in legs})),
            "events": ", ".join(sorted({str(b["eventId"]) for b in legs})[:10])}

    # One Sentry issue per cause. A mapping gap is a bug; a market they do not
    # sell on one fixture is not, and grouping them hides the first inside the
    # second.
    if unmapped:
        report("booking: BetKing market is not mapped", **_detail(unmapped))
    if event_gone:
        report("booking: BetKing event would not load", **_detail(event_gone))
    if unpriced:
        report("booking: BetKing does not price this market on this fixture",
               level="info", **_detail(unpriced))
    if same_game:
        # Their rule, not a fault of ours, and not something a retry can fix -
        # so info, like the unpriced case.
        report("booking: BetKing takes one selection per game on a multiple",
               level="info", **_detail(same_game))

    bad = unmapped + event_gone + unpriced + same_game
    if bad:
        # "No market there" is the sentence for two of these four and a plain
        # untruth for the third: a second leg on a game is refused because it
        # is a second leg, and the market is priced perfectly well. Say which.
        detail = ("one selection per game is all BetKing takes on a multiple"
                  if same_game and len(same_game) == len(bad)
                  else f"no market there for {len(bad):d} of {len(picks):d} picks")
        return jsonify({
            "success": False,
            "message": "BetKing rejected the slip",
            "detail": detail,
            "unbookable": bad,
        }), 400

    out = betking.generate_code(resolved)
    if out.get("code") and not out.get("error"):
        return jsonify({"success": True, **out})
    # An error WITH a code is the one failure BetKing will not tell us about
    # itself: the slip was accepted and the code is empty. It is a bug on our
    # side every time - a selection id they did not recognise - so it is
    # reported as one rather than shown to the punter as a bookmaker refusal.
    report("betking booking refused", legs=len(resolved),
           detail=str(out.get("error"))[:300], empty_code=out.get("code"))
    return jsonify({"success": False, **out}), 502


@app.route('/api/betpawa/fixtures', methods=['GET'])
def get_betpawa_fixtures():
    """Every Betpawa event, served from the background sweep.

    One flat bag and no `date` argument, unlike the BetKing route: their board
    is not addressable by date at all. `?page=N` reads one page live for
    debugging a sweep, and answers what that page holds rather than the board.
    """
    page = request.args.get("page")
    if page is not None:
        if not re.fullmatch(r"\d{1,3}", page):
            return jsonify({"success": False,
                            "error": "page must be a small number"}), 400
        try:
            events = betpawa.fetch_page(int(page) * betpawa.PAGE)
        except Exception as ex:                  # noqa: BLE001 - user-facing
            report("betpawa fixtures failed", page=page, error=str(ex))
            return jsonify({"success": False, "error": str(ex),
                            "matches": {}}), 502
        rows = {}
        for event in events:
            row = betpawa._row(event)
            betpawa._absorb(row, event)
            if row["odds"]:
                rows[row["eventId"]] = row
        return jsonify({"success": True, "page": int(page), "cached": False,
                        "count": len(rows), "matches": rows})

    entry = _cache_get("betpawa", _BETPAWA_CACHE)
    data = (entry or {}).get("data")
    if not data:
        # Say so rather than answering an empty bag with success: true - the
        # lie that made the Bet9ja integration's first outage invisible.
        return jsonify({"success": False, "count": 0, "matches": {},
                        "error": "betpawa fixtures not loaded yet"}), 503
    return jsonify({"success": True, "cached": True,
                    "ageSeconds": int(time.time() - entry["at"]),
                    "count": len(data), "matches": data})


@app.route('/api/betpawa/booking-code', methods=['POST'])
def api_betpawa_code():
    """Turn a set of picks into a Betpawa booking code.

    Body: {"selections": [{"eventId": "38090806", "code": "1X"}, ...]}

    Same three-way answer as the other three books, so one client path serves
    all four: every leg Betpawa will not take is named in `unbookable` with a
    reason rather than the first one killing the request.

      not_mapped   MARKET_MAP has no entry for this code, known locally and
                   refused before any request is made.
      event_gone   Their event endpoint would not return the fixture.
      not_priced   Mapped, and they do not price that market on this game.
      same_game    A second leg on a fixture already on the slip. Their
                   multiple refuses it - 400 SPORTSBOOK_WRONG_SELECTION,
                   naming nothing - so it is named here instead.
    """
    data = request.get_json(silent=True) or {}
    picks = data.get("selections") or []
    if not picks:
        return jsonify({"success": False, "error": "no selections"}), 400
    # OUR CAP, NOT THEIRS. Their booking endpoint took 300 selections on one
    # code and read all 300 back, so there is no limit of Betpawa's to respect
    # here - see betpawa.BETSLIP_MAX.
    if len(picks) > betpawa.BETSLIP_MAX:
        return jsonify({"success": False,
                        "error": f"betpawa slips are capped at {betpawa.BETSLIP_MAX:d} selections "
                                 "here",
                        "sent": len(picks)}), 400

    resolved = []
    unmapped, unpriced, event_gone, same_game = [], [], [], []
    cache = {}
    booked_games = set()
    try:
        for p in picks:
            code = p.get("code")
            event_id = p.get("eventId")
            leg = {"eventId": event_id, "prediction": code}
            if betpawa.market_for(code) is None:
                leg["reason"] = "not_mapped"
                unmapped.append(leg)
                continue
            if event_id in booked_games:
                leg["reason"] = "same_game"
                same_game.append(leg)
                continue
            if event_id not in cache:
                cache[event_id] = betpawa.fetch_event(event_id)
            ev = cache[event_id]
            if not ev:
                leg["reason"] = "event_gone"
                event_gone.append(leg)
                continue
            if code not in (ev.get("odds") or {}):
                leg["reason"] = "not_priced"
                unpriced.append(leg)
                continue
            booked_games.add(event_id)
            resolved.append({"event": ev, "code": code})
    except Exception as ex:                      # noqa: BLE001 - user-facing
        report("betpawa odds fetch failed", error=str(ex))
        return jsonify({"success": False, "error": str(ex)}), 502

    def _detail(legs):
        return {
            "bad_legs": len(legs), "total_legs": len(picks),
            "markets": ", ".join(sorted({str(b["prediction"]) for b in legs})),
            "events": ", ".join(sorted({str(b["eventId"]) for b in legs})[:10])}

    if unmapped:
        report("booking: Betpawa market is not mapped", **_detail(unmapped))
    if event_gone:
        report("booking: Betpawa event would not load", **_detail(event_gone))
    if unpriced:
        report("booking: Betpawa does not price this market on this fixture",
               level="info", **_detail(unpriced))
    if same_game:
        report("booking: Betpawa takes one selection per game on a multiple",
               level="info", **_detail(same_game))

    bad = unmapped + event_gone + unpriced + same_game
    if bad:
        detail = ("one selection per game is all Betpawa takes on a multiple"
                  if same_game and len(same_game) == len(bad)
                  else f"no market there for {len(bad)} of {len(picks)} picks")
        return jsonify({
            "success": False,
            "message": "Betpawa rejected the slip",
            "detail": detail,
            "unbookable": bad,
        }), 400

    out = betpawa.generate_code(resolved)
    if out.get("code") and not out.get("error"):
        return jsonify({"success": True, **out})
    # Betpawa refuses a bad selection outright rather than minting an empty
    # code, so an error here is usually theirs and not ours - but a code
    # ALONGSIDE an error is the BetKing failure and is reported the same way.
    report("betpawa booking refused", legs=len(resolved),
           detail=str(out.get("error"))[:300], empty_code=out.get("code"))
    return jsonify({"success": False, **out}), 502


@app.route('/api/onexbet/fixtures', methods=['GET'])
def get_onexbet_fixtures():
    """Every 1xBet event, served from the background sweep."""
    entry = _cache_get("onexbet", _ONEXBET_CACHE)
    data = (entry or {}).get("data")
    if not data:
        return jsonify({"success": False, "count": 0, "matches": {},
                        "error": "1xbet fixtures not loaded yet"}), 503
    return jsonify({"success": True, "cached": True,
                    "ageSeconds": int(time.time() - entry["at"]),
                    "count": len(data), "listed": _ONEXBET_STATS["listed"],
                    "skipped": _ONEXBET_STATS["skipped"], "matches": data})


@app.route('/api/onexbet/booking-code', methods=['POST'])
def api_onexbet_code():
    """Picks -> a 1xBet booking code. Same three-way answer as the other books:
    not_mapped / event_gone / not_priced / same_game, each leg named.

    THEIR CAP (50, ErrorCode 157972) is checked here before any request, and a
    code that reads back holding fewer legs than were sent is a 502, never a
    success - 1xBet drops a leg it will not take without saying so."""
    data = request.get_json(silent=True) or {}
    picks = data.get("selections") or []
    if not picks:
        return jsonify({"success": False, "error": "no selections"}), 400
    if len(picks) > onexbet.BETSLIP_MAX:
        return jsonify({"success": False, "sent": len(picks),
                        "error": f"1xbet slips hold at most {onexbet.BETSLIP_MAX:d} selections"}), 400
    resolved, bad, cache, booked = [], [], {}, set()
    # FAST ENOUGH FOR THE PROXIES (review C1, 29 Sep 2026). A full-time leg the
    # sweep already priced is booked off the swept card: 1xBet does not check
    # the price sent (the read-back reprices it) and the read-back below
    # names any leg that has since gone. Only a game whose legs need a half or
    # corners card, or that the sweep lacks, is read live - and only for the
    # periods those legs live on.
    swept = (_cache_get("onexbet", _ONEXBET_CACHE) or {}).get("data") or {}
    codes_by_event = {}
    for p in picks:
        codes_by_event.setdefault(p.get("eventId"), []).append(p.get("code"))
    try:
        for p in picks:
            code, event_id = p.get("code"), p.get("eventId")
            leg = {"eventId": event_id, "prediction": code}
            if onexbet.market_for(code) is None:
                bad.append(dict(leg, reason="not_mapped"))
                continue
            if event_id in booked:
                bad.append(dict(leg, reason="same_game"))
                continue
            if event_id not in cache:
                row = swept.get(str(event_id))
                periods = onexbet.periods_for(codes_by_event[event_id])
                if row and periods == [""] and code in (row.get("odds") or {}):
                    cache[event_id] = row
                else:
                    cache[event_id] = onexbet.fetch_event(event_id, periods=periods)
            ev = cache[event_id]
            if not ev:
                bad.append(dict(leg, reason="event_gone"))
                continue
            if code not in (ev.get("odds") or {}):
                bad.append(dict(leg, reason="not_priced"))
                continue
            booked.add(event_id)
            resolved.append({"event": ev, "code": code})
    except Exception as ex:                      # noqa: BLE001 - user-facing
        report("1xbet odds fetch failed", error=str(ex))
        return jsonify({"success": False, "error": str(ex)}), 502
    if bad:
        # Each reason counted on its own (review M3): a same-game leg is not a
        # missing market, and saying so sent readers looking for the wrong fix.
        same = sum(b["reason"] == "same_game" for b in bad)
        other = len(bad) - same
        if not other:
            detail = "one selection per game is all 1xBet takes on a multiple"
        else:
            detail = f"no market there for {other} of {len(picks)} picks"
            if same:
                detail += f", and {same} more on a game already on the slip"
        return jsonify({"success": False, "message": "1xBet rejected the slip",
                        "detail": detail, "unbookable": bad}), 400
    out = onexbet.generate_code(resolved)
    if out.get("code") and not out.get("error"):
        return jsonify({"success": True, **out})
    if out.get("missing"):
        # Named by the read-back, so the client drops exactly these and books
        # the rest - the same answer as a leg refused before minting.
        return jsonify({"success": False, "message": "1xBet rejected the slip",
                        "detail": f"1xBet would not keep {len(out['missing'])} of {len(picks)} picks",
                        "unbookable": [dict(m, reason="not_priced") for m in out["missing"]]}), 400
    report("1xbet booking refused", legs=len(resolved),
           detail=str(out.get("error"))[:300], empty_code=out.get("code"))
    return jsonify({"success": False, **out}), 502


# --- reading a booking code back -------------------------------------------
# BOTH BOOKS WILL HAND A CODE BACK, which is the fact the converter and the
# splitter both rest on, and neither was reachable by guessing: SportyBet
# answers on the same /orders/share path it books on, and Bet9ja on a third
# hostname (see bet9ja.read_coupon).
#
# It is read-only and costs the bookmaker one request, so it is not rationed
# the way booking is. It is still a request made in somebody else's name, so
# the code is checked for shape before it is sent anywhere.
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,16}$")


_EVENT_NAME_CACHE = {}
_EVENT_NAME_MAX = 12


def _sporty_event_name(event_id, region="ng"):
    """Teams, competition and status for ONE event, asked of SportyBet direct.

    The share read returns `sr:match:` ids and nothing else - no names - so the
    names normally come from our own fixtures cache. That cache holds UPCOMING
    matches: SportyBet drops a fixture the moment it kicks off and our sweep
    follows, so a code read an hour after kick-off had legs nothing could name.
    Reported on HCVKA1, where five of thirty-one legs came back as "a game we
    don't carry" for games we carried that morning.

    Their own event endpoint still answers for a match in play, and it carries
    the status too - which is the honest thing to tell the reader: that game
    has started, not that it is unknown to us.

    One request per unnamed leg, capped, cached for the life of the process,
    and best effort: a failure leaves the leg unnamed exactly as before.
    """
    if not event_id:
        return None
    if event_id in _EVENT_NAME_CACHE:
        return _EVENT_NAME_CACHE[event_id]
    if len(_EVENT_NAME_CACHE) >= _EVENT_NAME_MAX * 40:
        _EVENT_NAME_CACHE.clear()
    url = (f"https://www.sportybet.com/api/{region}/factsCenter/event"
           f"?eventId={quote(str(event_id))}&productId=3")
    try:
        r = requests.get(url, headers=_headers(region), impersonate="chrome120",
                         timeout=6)
        d = (r.json() or {}).get("data") or {}
    except Exception as ex:                      # noqa: BLE001 - best effort
        log.info("sportybet event lookup failed for %s: %s", event_id, ex)
        _EVENT_NAME_CACHE[event_id] = None
        return None
    if not d.get("homeTeamName"):
        _EVENT_NAME_CACHE[event_id] = None
        return None
    sport = d.get("sport") or {}
    cat = sport.get("category") or {}
    tour = cat.get("tournament") or {}
    out = {
        "homeTeam": d.get("homeTeamName") or "",
        "awayTeam": d.get("awayTeamName") or "",
        "league": " ".join(x for x in (cat.get("name"), tour.get("name")) if x),
        # NOT_STARTED / H1 / HT / H2 / ENDED - the reason the board let it go.
        "status": d.get("matchStatus") or d.get("status") or "",
    }
    _EVENT_NAME_CACHE[event_id] = out
    return out


def read_sporty_share(code, region="ng", timeout=12):
    """The legs behind a SportyBet booking code, in our own market codes.

    Their read returns `sr:match:` ids and nothing else - no team names, no
    league - so the names come from the fixtures cache this server already
    keeps. A leg whose event is not in that cache is still returned, named or
    not: a slip with a game we do not carry is a fact the caller has to see,
    not one to hide by dropping the leg.
    """
    url = f"https://www.sportybet.com/api/{region}/orders/share/{quote(str(code))}"
    try:
        r = requests.get(url, headers=_headers(region), impersonate="chrome120",
                         timeout=timeout)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("sportybet share read failed: %s", ex)
        return {"error": f"request failed: {ex}"}

    if (body or {}).get("bizCode") != 10000:
        return {"error": "not found", "notFound": True}

    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    by_event = {m.get("eventId"): m for m in ((entry or {}).get("data") or [])
                if isinstance(m, dict)}

    out = []
    ticket = ((body.get("data") or {}).get("ticket") or {})
    # THEIR PRICE FOR EACH LEG, off the same reply. `outcomes` carries every
    # event on the ticket with the selected market and its live odds; keyed
    # the way the ticket names a selection, so the total we quote after a
    # booking is SportyBet's own and not our cached estimate (owner, 28 Sep:
    # "the total booked accepted odds should show").
    live = {}
    for ev in ((body.get("data") or {}).get("outcomes") or []):
        for mk in (ev.get("markets") or []):
            for oc in (mk.get("outcomes") or []):
                try:
                    live[(ev.get("eventId"), str(mk.get("id")), str(oc.get("id")),
                          mk.get("specifier") or "")] = float(oc.get("odds"))
                except (TypeError, ValueError):
                    pass
    # THE BOARD DROPS A MATCH AT KICK-OFF, AND A PUNTER'S CODE DOES NOT.
    # SportyBet removes a fixture from its upcoming list the moment it starts,
    # our sweep follows, and a code read an hour later then had legs the cache
    # could not name - reported on HCVKA1, where five of thirty-one came back
    # as "a game we don't carry" for games we carried that morning.
    # The ticket itself carries the names; they were simply never read. Below,
    # the fixture cache is preferred (it is ours, and it carries the odds) and
    # the ticket answers for anything the cache has let go.
    for sel in (ticket.get("selections") or []):
        key = (str(sel.get("marketId")), str(sel.get("outcomeId")),
               sel.get("specifier") or "")
        eid = sel.get("eventId")
        fx = by_event.get(eid) or {}
        if not fx.get("homeTeam"):
            named = _sporty_event_name(eid, region)
            if named:
                fx = dict(fx)
                fx.update(named)
        pred = _ODDS_LOOKUP.get(key)
        out.append({
            "eventId": eid,
            "prediction": pred,
            "raw": "/".join(key),
            "home": fx.get("homeTeam") or "",
            "away": fx.get("awayTeam") or "",
            "league": fx.get("league") or "",
            "kickoff": fx.get("startTime") or "",
            "odds": live.get((eid,) + key)
                    or ((fx.get("odds") or {}).get(pred) if pred else None),
            # Only present when the fixture had to be named by asking SportyBet
            # directly, which happens when our board has let it go: H1 / HT /
            # H2 / ENDED says the game is on or over, which is a different
            # sentence from "we don't carry it".
            "status": fx.get("status") or "",
        })
    return {"legs": out}


def _stamp_sr(book, legs):
    """Give every leg Sportradar's match id where its book shares it, so the
    converter can pair a leg across books by id before it tries names.

    SportyBet's id is it. BetKing's coupon carries it (betking.read_coupon).
    Bet9ja's coupon does not - only its own event id - so it is read off the
    cached feed, where EXTID is kept. A leg the cache has not seen keeps no
    srId and the site falls back to names, as it always did. Betpawa's coupon
    lacks it too; its feed rows carry it off the SPORTRADAR widget (25 Sep).
    """
    if book == "sporty":
        for leg in legs:
            leg["srId"] = sr_id(leg.get("eventId"))
    elif book in ("bet9ja", "betpawa"):
        cache = _BET9JA_CACHE if book == "bet9ja" else _BETPAWA_CACHE
        data = (_cache_get(book, cache) or {}).get("data") or {}
        for leg in legs:
            row = data.get(str(leg.get("eventId")))
            leg["srId"] = (row or {}).get("srId")


@app.route('/api/slip', methods=['GET'])
def api_read_slip():
    """GET /api/slip?book=sporty|bet9ja|betking|betpawa|onexbet&code=XXXX -> the legs behind a code."""
    book = (request.args.get("book") or "sporty").strip().lower()
    code = (request.args.get("code") or "").strip()
    if not _CODE_RE.match(code):
        return jsonify({"success": False, "error": "that is not a booking code"}), 400
    if book not in ("sporty", "bet9ja", "betking", "betpawa", "onexbet"):
        return jsonify({"success": False, "error": "unknown bookmaker"}), 400

    # Named rather than defaulted, so a typo in `book` can never be read
    # against the wrong bookmaker and answered as though it were that one's.
    if book == "bet9ja":
        out = bet9ja.read_coupon(code)
    elif book == "betking":
        out = betking.read_coupon(code)
    elif book == "betpawa":
        out = betpawa.read_coupon(code)
    elif book == "onexbet":
        out = onexbet.read_coupon(code)
    else:
        out = read_sporty_share(code)
    if out.get("notFound"):
        return jsonify({"success": False, "notFound": True,
                        "error": "no slip behind that code"}), 404
    if out.get("error"):
        report("slip read failed", book=book, detail=str(out["error"])[:200])
        return jsonify({"success": False, "error": out["error"]}), 502

    legs = out.get("legs") or []
    if not legs:
        return jsonify({"success": False, "error": "that code has no games in it"}), 404
    _stamp_sr(book, legs)
    # A reprint is not a transcript - see bet9ja.read_coupon. The count is what
    # we READ, said plainly, so nothing downstream can imply it is the slip as
    # it was booked.
    out_json = {"success": True, "book": book, "code": code,
                "read": len(legs), "legs": legs}
    # AND WHEN THE BOOK CAN SAY WHAT IT DROPPED, SAY IT. A leg leaves a coupon
    # the moment its fixture starts, so a code pasted in the afternoon is
    # shorter than the one the punter was handed in the morning. Showing the
    # remainder without a word reads as "your code only had three games in it".
    # Only BetKing reports this; the other two thin out just as quietly and
    # tell us nothing, so the fields are absent rather than zero - a caller can
    # tell "none dropped" from "this book cannot say".
    if out.get("removed"):
        out_json["removed"] = out["removed"]
    if out.get("booked"):
        out_json["booked"] = out["booked"]
    return jsonify(out_json)


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    now = time.time()
    entry = _cache_get("live", _LIVE_CACHE)
    if entry and (now - entry["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(entry["data"]), "matches": entry["data"]})
    try:
        matches = fetch_live_scores()
        _cache_put("live", _LIVE_CACHE, matches)
        return jsonify({"success": True, "cached": False,
                        "count": len(matches), "matches": matches})
    except Exception as ex:
        # Broad by design - degrade to stale rather than 500. See get_fixtures.
        entry = _cache_get("live", _LIVE_CACHE)
        if entry:
            log.warning("livescores fetch failed, serving stale: %s", ex)
            return jsonify({"success": True, "cached": True, "stale": True,
                            "count": len(entry["data"]), "matches": entry["data"]})
        log.exception("livescores fetch failed and no cache to fall back on")
        return jsonify({"success": False, "error": str(ex), "matches": []}), 500


def _verify_sporty_code(code, raw_selections, region="ng"):
    """Does the code they just handed back hold the legs we sent?

    IT DOES NOT ALWAYS, AND THEY SAY NOTHING. Measured 22 Sep: five legs sent
    with one unknown event id among them came back as an ordinary share code
    holding FOUR. No error, no warning, no mention of the leg that vanished -
    exactly the failure BetKing taught us to check for, on the book this site
    was built around and the only one whose codes were never read back.

    A punter handed that code opens a slip with a game missing from it, and the
    record we file claims a bet they do not hold.

    Returns (ok, missing, odds) where `missing` holds the selections that did
    not survive and `odds` is the product of the legs' prices as the code
    holds them - None unless every leg carries one. On any failure to read it
    back - their read endpoint down, a code too fresh to resolve - it answers
    (True, [], None): unprovable is not the same as wrong, and refusing a code
    we cannot check would be worse than the failure this guards.
    """
    try:
        got = read_sporty_share(code)
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("sporty read-back failed for %s: %s", code, ex)
        return True, [], None
    if not isinstance(got, dict) or got.get("error"):
        return True, [], None
    legs = got.get("legs") or []
    if not legs:
        return True, [], None
    held = {str(l.get("eventId")) for l in legs if l.get("eventId")}
    if not held:
        return True, [], None
    missing = [it for it in raw_selections
               if str(it.get("eventId")) not in held]
    prices = [l.get("odds") for l in legs]
    odds = None
    if prices and all(isinstance(o, (int, float)) and o > 1 for o in prices):
        odds = 1.0
        for o in prices:
            odds *= o
        odds = round(odds, 2)
    return (not missing), missing, odds


# BISECTION, BECAUSE THEIR REFUSAL NAMES NOTHING AND OUR CACHE IS A GUESS.
#
# SportyBet answers a bad slip with one sentence about the whole slip -
# "invalid event data, no market there" - and names no event and no market.
# Everything above tries to guess which leg from OUR copy of their card, and
# that copy is both partial (their feed carries fewer markets than they sell)
# and stale (45 minutes). When it has a price for every leg there are no
# suspects at all, and the reader gets a flat "SportyBet wouldn't take this
# slip" about a slip they cannot correct. Reported again on 22 Sep.
#
# So ask the only authority there is. Their booking endpoint is free, fast and
# has no side effect worth worrying about - a refused slip mints nothing - so
# the slip is split in half and each half offered back. A half that books is
# clean and never split again; a half that is refused is split further. One
# bad leg in sixteen costs about eight calls rather than sixteen, and the
# legs it names are named by SportyBet rather than inferred.
#
# BOUNDED, because this runs while somebody waits: a call budget and a
# deadline, and whatever has been learned when either runs out is what gets
# returned. Finding nothing is a real answer too - it means they refuse the
# COMBINATION rather than any single leg, which is a different sentence and
# one the reader is owed.
#
# TWELVE CALLS WAS TOO FEW, AND HALF OF THEM WERE WASTED (24 Sep). A 23-leg
# slider slip with two refused legs needs 17-19 calls under plain bisection,
# so the probe ran out having named nothing, the reader was offered our own
# guess, and the retry died on the same sentence. Measured on the live card
# that day: 29 legs, 27 booked alone, 2 refused (Over 1.5 and a team total,
# both markets our copy of their card had no price for).
#
# Two changes, simulated over 2,000 random slips per size before shipping:
#   - The whole slip is already known to be refused, so it is not asked again;
#     and when the left half books, the fault is in the right half, so the
#     right half is split without being asked. That halves the cost: two bad
#     legs in 29 now take a median of 12 calls (p95 15), not 17.
#   - A leg reached only by that inference is asked on its own before it is
#     named. The inference assumes one leg is at fault; when the fault is the
#     COMBINATION it is false, and naming a leg that books alone would drop a
#     good bet. So every leg in `bad` has been refused by SportyBet by itself.
#   - Legs our cache already doubts go first, so the faults tend to share a
#     half and the search closes on them sooner.
# Sequential still, never parallel: a burst from Railway's IP gets refused.
# Calls run ~0.1s each there, so forty fits inside the deadline.
PROBE_MAX_CALLS = 40
PROBE_DEADLINE_S = 8.0
# A SHARED BUDGET ON TOP (owner, 9 Oct 2026). Forty calls for one reader is
# fine; forty each for a crowd refused at once is the burst that gets Railway
# throttled. So probes across every reader spend at most this many calls a
# minute - normal traffic never reaches it, and in a rush the probes go quiet
# first (no verdict, never a wrong one) while ordinary bookings carry on.
PROBE_PER_MIN = 60
_PROBE_SPENT = []
_PROBE_LOCK = threading.Lock()


def _probe_allowed():
    now = time.time()
    with _PROBE_LOCK:
        while _PROBE_SPENT and now - _PROBE_SPENT[0] > 60:
            _PROBE_SPENT.pop(0)
        if len(_PROBE_SPENT) >= PROBE_PER_MIN:
            return False
        _PROBE_SPENT.append(now)
        return True


def _probe_refusal(formatted, region="ng", first=()):
    """Which legs SportyBet refuses on their own. Returns (bad, how).

    `bad` holds indices into `formatted`. `how` records what the probe cost and
    whether it ran out, so a partial answer is never read as a complete one.
    `first` is indices to search before the rest - the legs we already doubt.
    Call only on a slip SportyBet has just refused whole.
    """
    started = time.time()
    calls = {"n": 0}

    def takes(subset):
        if (calls["n"] >= PROBE_MAX_CALLS
                or time.time() - started > PROBE_DEADLINE_S):
            return None                      # out of budget: no opinion
        if not _probe_allowed():
            return None                      # shared budget spent: no opinion
        calls["n"] += 1
        got = generate_sportybet_code([formatted[i] for i in subset], region)
        if got.get("busy"):
            return None                      # queue full is not a refusal
        return bool(got.get("code"))

    bad, ran_out = [], False

    def split(subset, refused):
        """`refused` is True when this subset is known to be refused - either
        asked, or inferred from its sibling booking. Returns True/False for
        refused, or None when the budget ran out before it could say."""
        nonlocal ran_out
        if not subset:
            return False
        asked = not refused
        if asked:
            ok = takes(subset)
            if ok is None:
                ran_out = True
                return None
            if ok:
                return False                 # this whole half is fine
        if len(subset) == 1:
            bad.append((subset[0], not asked))
            return True
        mid = len(subset) // 2
        left = split(subset[:mid], False)
        # Left booked, so the fault lies right: split it without asking. Left
        # refused or unknown says nothing about the right, so the right is asked.
        split(subset[mid:], left is False)
        return True

    head = [i for i in dict.fromkeys(first) if 0 <= i < len(formatted)]
    order = head + [i for i in range(len(formatted)) if i not in set(head)]
    split(order, True)
    # Confirm anything named by inference alone. Cheap - one call per such leg
    # - and it is what keeps a combination refusal from being blamed on a leg
    # SportyBet would take by itself. Unconfirmed when the budget runs out
    # means unnamed: a guess is never sent as their answer.
    confirmed = []
    for i, inferred in bad:
        if not inferred:
            confirmed.append(i)
            continue
        ok = takes([i])
        if ok is None:
            ran_out = True
        elif not ok:
            confirmed.append(i)
    bad = confirmed
    return bad, {"calls": calls["n"], "ran_out": ran_out,
                 "seconds": round(time.time() - started, 2)}


# ASK THEIR LIVE CARD WHICH LEG IS DEAD, BEFORE GUESSING.
#
# HANDOFF.md (3 Sep) said this could not be done: "there is no single-event
# endpoint to re-check against". There is - factsCenter/event, which
# _sporty_event_name already calls for team names. It returns the event's whole
# market list (299 markets on Arsenal v Leeds) with a status per market and
# isActive per outcome. Measured 25 Sep against our 40-minute-old copy: goals
# markets 1.4% closed, team corners 5%, total corners 10.5%, total shots 15% -
# SportyBet re-lines corners and shots as the price moves and deletes the old
# line, so a slip with three or four of them was refused about half the time.
#
# Only AFTER they refuse, never before: a leg is not refused on our evidence
# before SportyBet has answered (JTEJA5, see _unbookable). One request per
# event, one at a time, remembered for a minute - against _probe_refusal's
# up-to-40 sequential re-bookings, which it now runs ahead of.
LIVE_TTL = 60
LIVE_MAX_EVENTS = 30
LIVE_BUDGET_S = 6
_LIVE_CACHE = {}


def _event_markets(event_id, region="ng"):
    """One event's live card: {"status", "markets": {(mid, oid, spec): (status,
    isActive)}}, or None when they did not answer."""
    hit = _LIVE_CACHE.get(event_id)
    if hit and time.time() - hit[0] < LIVE_TTL:
        return hit[1]
    url = (f"https://www.sportybet.com/api/{region}/factsCenter/event"
           f"?eventId={quote(str(event_id))}&productId=3")
    out = None
    if not _SPORTY_SEM.acquire(timeout=3):
        return None                              # unknown, never a verdict
    try:
        r = requests.get(url, headers=_headers(region), impersonate="chrome120",
                         timeout=5)
        d = (r.json() or {}).get("data") or {}
        if d.get("homeTeamName"):
            out = {"status": d.get("status"), "markets": {}, "odds": {}}
            for m in d.get("markets") or []:
                for o in m.get("outcomes") or []:
                    k = (str(m.get("id")), str(o.get("id")), m.get("specifier") or "")
                    out["markets"][k] = (m.get("status"), o.get("isActive"))
                    try:
                        out["odds"][k] = float(o.get("odds"))
                    except (TypeError, ValueError):
                        pass
    except Exception as ex:                      # noqa: BLE001 - best effort
        log.info("sportybet live card failed for %s: %s", event_id, ex)
    finally:
        _SPORTY_SEM.release()
    if len(_LIVE_CACHE) > 2000:
        _LIVE_CACHE.clear()
    _LIVE_CACHE[event_id] = (time.time(), out)
    return out


def _nearest_open_line(markets, key):
    """The open line of the same market and side closest to the one refused,
    as our code - "SHOTS_OV_27.5" for a refused SHOTS_OV_25.5 - or None."""
    mid, oid, spec = key
    try:
        old = float(spec.split("=", 1)[1])
    except (IndexError, ValueError):
        return None
    best = None
    for (m, o, s), st in markets.items():
        if m != mid or o != oid or not s.startswith("total=") or st != (0, 1):
            continue
        code = _ODDS_LOOKUP.get((m, o, s))
        try:
            gap = abs(float(s.split("=", 1)[1]) - old)
        except ValueError:
            continue
        if code and (best is None or gap < best[0]):
            best = (gap, code)
    return best[1] if best else None


def _live_verdicts(raw_selections, region="ng"):
    """For a slip SportyBet has just refused: the legs their live card says are
    closed, as [{"i", "reason", "now"?}]. reason is "started", "closed" or
    "line_moved" (with `now`, the current line). A leg is only named when their
    card was read and positively lacks it open - silence is never a verdict."""
    evs = []
    for it in raw_selections:
        if it.get("eventId") and it["eventId"] not in evs:
            evs.append(it["eventId"])
    # ONE AT A TIME. Concurrent requests from Railway's IP got every one
    # refused (see _start_fixtures_thread), so this stays sequential under a
    # deadline; a card not read in time is unknown, and the probe follows.
    cards, deadline = {}, time.time() + LIVE_BUDGET_S
    for e in evs[:LIVE_MAX_EVENTS]:
        if time.time() > deadline:
            break
        cards[e] = _event_markets(e, region)
    bad = []
    for i, it in enumerate(raw_selections):
        live = cards.get(it.get("eventId"))
        m = market_for(it.get("prediction"))
        if not live or not m:
            continue
        started = live["status"] not in (0, None)
        if not live["markets"] and not started:
            continue                    # an empty card that has not started says nothing
        key = (str(m["marketId"]), str(m["outcomeId"]), m.get("specifier") or "")
        if live["markets"].get(key) == (0, 1):
            continue
        v = {"i": i, "reason": "started" if started else "closed"}
        if not started and key[2].startswith("total="):
            now = _nearest_open_line(live["markets"], key)
            if now and now != it.get("prediction"):
                v.update(reason="line_moved", now=now)
                nm = market_for(now)
                price = (live.get("odds") or {}).get(
                    (str(nm["marketId"]), str(nm["outcomeId"]), nm.get("specifier") or ""))
                if price:
                    v["odds"] = price
        bad.append(v)
    return bad


def _unbookable(raw_selections):
    """Which of these picks SportyBet has no market for.

    Roughly half the card carries no team-totals market at all - 888 of 1797
    fixtures on the day this was written - and asking to book one comes back
    "invalid event data, no market there", which takes the whole slip down.
    One unplaceable leg among forty loses all forty.

    THAT PREMISE WAS FALSE, AND THIS NO LONGER REFUSES ANYTHING. It used to say
    "we already hold every event's odds in the fixtures cache, so the answer is
    known here without asking SportyBet". We do not: their fixtures feed
    returns a PARTIAL market set per event. Measured 15 Sep on the next day's
    card - Russian Premier League events carried 1/X/2, double chance, GG and
    the first-half lines and no Over/Under at all; Swiss Super League events
    carried every Over/Under line and no 1X2 at all. Both book perfectly well.

    A reader's own SportyBet code settled it. JTEJA5 holds four legs this
    function was refusing, on the very event ids we match:

        sr:match:74374472  1X        Lugano v FC St. Gallen 1879
        sr:match:74374468  1X        FC Thun v Servette Geneva
        sr:match:72334336  OVER_1.5  FC Baltika Kaliningrad v FK Zenit
        sr:match:72334340  OVER_2.5  Lokomotiv Moscow v PFK Krylia Sovetov

    Every one came back from this route as "no market there", under a message
    naming SportyBet, who had never been asked. The absence was in our cache,
    the refusal was ours, and the punter was told the bookmaker had said no.

    So the picks go through now and SportyBet answers for itself: it names the
    legs it will not take, and the client already drops exactly those and
    retries. The counting stays - a leg we would once have refused is worth
    knowing about, so it is reported as a `suspect` and sent anyway.

    Silent when the cache is empty: no prices is not the same as prices that
    exclude a market, and refusing a slip because this server has just started
    would be worse than the failure it prevents.

    Returns (bad, how) where `how` records WHAT THE VERDICT WAS MADE ON. The
    cache lives 45 minutes and the browser read its own copy at page load, so
    the two disagree about time rather than about markets - a market thinned
    since our last refresh still shows a price on their screen. A refusal from
    a 44-minute-old cache and one from a 2-minute-old cache mean different
    things, and until now Sentry could not tell them apart: 54 refusals in five
    days and no way to say whether they were real gaps or our copy being stale.
    Shortening the TTL is not the answer - a refresh is ~49 sequential requests
    and this server has been blocked for less - so measure first.
    """
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    rows = (entry or {}).get("data") or []
    if not rows:
        return [], {"cache_age_s": None, "judged": 0, "unknown": 0,
                    "suspect": 0, "suspect_markets": [], "suspects": []}
    odds_by_event = {}
    for m in rows:
        if isinstance(m, dict) and m.get("eventId"):
            odds_by_event[m["eventId"]] = m.get("odds") or {}
    suspect = []
    judged = unknown = 0
    for item in raw_selections:
        ev, pred = item.get("eventId"), item.get("prediction")
        # A MARKET WE DO NOT MODEL CANNOT BE JUDGED FROM THIS CACHE.
        # The fixtures sweep fetches the 24 markets MARKET_MAP names and
        # nothing else, so a pass-through pick - corners, a handicap, 2UP -
        # has no price here whether SportyBet sells it or not. Judged anyway,
        # every converted leg came back "no market there" and the slip was
        # refused before it ever reached the bookmaker. Found by converting a
        # real code on the live site: Le Mans +0.5, which SportyBet prices
        # perfectly well.
        if pred not in MARKET_MAP:
            unknown += 1
            continue
        prices = odds_by_event.get(ev)
        if prices is None:          # event not in the cache - cannot judge it
            unknown += 1
            continue
        judged += 1
        price = prices.get(pred)
        if not price or price <= 1.01:
            # Suspect, not condemned. Our cache is missing a price for it; that
            # is now known to be weak evidence, so it is counted for telemetry
            # and the pick still goes to SportyBet, who can answer for their
            # own card.
            suspect.append({"eventId": ev, "prediction": pred})
    age = entry.get("at")
    # `bad` is deliberately always empty: nothing here is refused any more.
    # The shape stays so the caller keeps its telemetry and so a future rule
    # with better evidence has somewhere to live.
    #
    # The suspects themselves travel with the telemetry now, unsent. They are
    # not evidence enough to refuse a leg BEFORE asking SportyBet - that was
    # the JTEJA5 mistake and it stands. They are the only evidence there is
    # AFTERWARDS: SportyBet's refusal names nothing at all, so without this the
    # reader is told "invalid event data, no market there" about a slip of
    # forty and given no leg to remove. See the rejection branch below.
    return [], {
        "cache_age_s": int(time.time() - age) if age else None,
        "judged": judged,
        "unknown": unknown,
        "suspect": len(suspect),
        "suspect_markets": sorted({str(x["prediction"]) for x in suspect}),
        "suspects": suspect,
    }


# SHARED REFUSALS (owner, 9 Oct 2026). A game SportyBet refused for one reader
# is left out of the builders for every reader for half an hour, instead of
# each of them building the same dead line. Fed only by SportyBet's own
# answers - a booking refusal or a live-card read - so it costs no request.
# Served beside the odds in /api/fixtures, never inside them: the prices the
# predictions read are untouched.
_REFUSED_TTL = 30 * 60
_REFUSED_CACHE = {"at": 0, "data": None}
_REFUSED_REASONS = {"line_moved", "closed", "started", "refused_alone", "dropped_by_book"}


def _refused_now():
    entry = _cache_get("refused", _REFUSED_CACHE) or {}
    now = time.time()
    return {k: t for k, t in (entry.get("data") or {}).items() if t > now}


def _remember_refused(legs):
    keep = [l for l in legs if l.get("reason") in _REFUSED_REASONS and l.get("eventId")]
    if not keep:
        return
    live = _refused_now()
    for l in keep:
        live[f"{l['eventId']}|{l.get('prediction')}"] = time.time() + _REFUSED_TTL
    if len(live) > 3000:                          # bounded: newest 3000
        live = dict(sorted(live.items(), key=lambda kv: kv[1])[-3000:])
    _cache_put("refused", _REFUSED_CACHE, live)


def _logged(legs):
    _log_refused(legs)
    return legs


def _log_refused(legs):
    """ONE LINE PER GAME SPORTYBET REFUSED (owner, 9 Oct 2026). Market,
    league, the price our copy held and how old that copy was - so which
    markets and leagues get refused is measured, not guessed. Logs only; it
    sends nothing anywhere and changes no answer."""
    try:
        entry = _cache_get("fixtures", _FIXTURES_CACHE) or {}
        at = entry.get("at")
        age = int((time.time() - at) / 60) if at else -1
        rows = {m.get("eventId"): m for m in (entry.get("data") or []) if isinstance(m, dict)}
        for leg in legs:
            m = rows.get(leg.get("eventId")) or {}
            price = (m.get("odds") or {}).get(leg.get("prediction"))
            log.info("refused leg | market=%s league=%s price=%s cache_min=%s reason=%s",
                     leg.get("prediction"), m.get("league") or "?",
                     price if price else "-", age, leg.get("reason") or "?")
    except Exception as ex:                      # noqa: BLE001 - logging must not break booking
        log.info("refused leg log failed: %s", ex)
    try:
        _remember_refused(legs)
    except Exception as ex:                      # noqa: BLE001 - nor may remembering
        log.info("refused leg store failed: %s", ex)


LINE_CHECK_EVENTS = 8
# Handicaps joined 30 Sep 2026: their lines close and move with the price like
# corners and shots, and the Handicap chip put them on big slips. A closed one
# is flagged, never re-lined - _nearest_open_line reads total= lines only, and
# a different handicap line is a different bet.
_LINE_PREFIXES = ("CORNERS_", "SHOTS_", "AH_")


@app.route('/api/sporty/live-check', methods=['POST'])
def api_sporty_live_check():
    """The corners and shots legs of a slip, checked on SportyBet's live card
    BEFORE booking. Those are the lines they re-line as the price moves and
    delete (15% of shots lines stale inside our 45-minute cache, 25 Sep), so
    they are the ones worth a request; goals legs (1.4%) are not. Nothing is
    refused here - the page shows the reader what moved and asks.

    Body {"selections": [{"eventId", "prediction"}]}. Returns {"verdicts":
    [{"eventId", "prediction", "reason", "now"?, "odds"?}]}, `odds` being the
    live price of `now`. At most LINE_CHECK_EVENTS games are read."""
    sel = (request.json or {}).get("selections") or []
    legs, evs = [], set()
    for s in sel:
        if not isinstance(s, dict) or not str(s.get("prediction") or "").startswith(_LINE_PREFIXES):
            continue
        if s.get("eventId") not in evs and len(evs) >= LINE_CHECK_EVENTS:
            continue
        evs.add(s.get("eventId"))
        legs.append({"eventId": s.get("eventId"), "prediction": s.get("prediction")})
    out = []
    for v in _live_verdicts(legs):
        leg = dict(legs[v["i"]], reason=v["reason"])
        for k in ("now", "odds"):
            if v.get(k):
                leg[k] = v[k]
        out.append(leg)
    _remember_refused(out)
    return jsonify({"verdicts": out, "checked": len(legs)})


@app.route('/api/generate-booking-code', methods=['POST'])
def api_generate_code():
    data = request.json or {}
    raw_selections = data.get("selections", [])

    # A MARKET IN NEITHER TABLE IS REFUSED, NEVER SUBSTITUTED.
    #
    # Below, the selection was built as `market_for(pred) or MARKET_MAP["1"]`,
    # so a code this server does not know silently became HOME WIN: marketId 1,
    # outcomeId 1, no specifier. SportyBet accepted it and returned a booking
    # code, so nothing looked wrong anywhere - the punter asked for "Leeds or
    # over 1.5", got a code, loaded it, and held a straight Leeds win instead.
    # Found by sending a Bet9ja-only market to this route on purpose and
    # reading the code back: one leg, `raw: "1/1/"`.
    #
    # Checked here, before the pre-flight, because it is knowable locally and
    # costs nothing - the same order bet9ja's route uses. The shape is the one
    # the client already retries on.
    unmapped = [{"eventId": i.get("eventId"), "prediction": i.get("prediction"),
                 "reason": "not_mapped"}
                for i in raw_selections if market_for(i.get("prediction")) is None]
    if unmapped:
        report("booking: SportyBet market is not mapped",
               bad_legs=len(unmapped), total_legs=len(raw_selections),
               markets=", ".join(sorted({str(b["prediction"]) for b in unmapped})))
        return jsonify({
            "success": False,
            "message": "SportyBet rejected the slip",
            "detail": f"no market there for {len(unmapped):d} of {len(raw_selections):d} picks",
            "unbookable": unmapped,
        }), 400

    # NOTHING IS REFUSED HERE ANY MORE - _unbookable carries the code that
    # proved why. A leg our cache cannot price is still worth watching, so it
    # is reported and sent: if SportyBet takes it, this line is the record that
    # our cache was wrong about their card; if they refuse it they say so by
    # name, and the client drops exactly that leg and retries.
    _, how = _unbookable(raw_selections)
    if how.get("suspect"):
        # INFO, NOT A WARNING. SportyBet's feed is partial by design - their
        # card lists more than their odds feed carries - so a leg we cannot
        # price is the normal case, not a fault, and most of these slips are
        # accepted. Left at warning it drowned the real refusals, which is the
        # signal this reporter exists for.
        report("booking: picks our cache cannot price, sent anyway",
               level="info",
               suspect_legs=how["suspect"], total_legs=len(raw_selections),
               markets=", ".join(how.get("suspect_markets") or []),
               # What the old verdict would have been made on. A refusal off a
               # 44-minute-old cache is a different animal from one off a
               # 2-minute-old cache, and only this can tell them apart.
               cache_age_s=how["cache_age_s"],
               legs_judged=how["judged"], legs_unknown=how["unknown"])

    formatted_selections = []
    for item in raw_selections:
        # Never `or MARKET_MAP["1"]`: see the unmapped check above. Anything
        # that reaches here has a mapping, and if that ever stops being true
        # the KeyError is the right failure - loud, and not somebody else's
        # bet.
        mapping = market_for(item.get("prediction"))
        formatted_selections.append({
            "eventId": item.get("eventId"),
            "marketId": mapping["marketId"],
            "outcomeId": mapping["outcomeId"],
            "specifier": mapping.get("specifier", "")
        })
    result = generate_sportybet_code(formatted_selections)
    if result.get("busy"):
        return jsonify({"success": False, "busy": True,
                        "message": "SportyBet is busy right now - try again in a moment"}), 503
    if result.get("code"):
        # READ IT BACK BEFORE HANDING IT OVER. They mint a perfectly ordinary
        # code for a slip they only partly understood - see
        # _verify_sporty_code - and the reader would open it to find a game
        # missing with nothing anywhere saying so.
        ok, missing, odds = _verify_sporty_code(result["code"], raw_selections)
        if ok:
            return jsonify({"success": True, "booking_code": result["code"],
                            "odds": odds})
        report("booking: SportyBet dropped legs from the code it returned",
               legs=len(raw_selections), lost=len(missing),
               code=result["code"],
               markets=", ".join(sorted({
                   str(m.get("prediction")) for m in missing})))
        # Named, so the client drops exactly those and offers the rest - the
        # same path every other refusal on this API already takes.
        return jsonify({
            "success": False,
            "message": "SportyBet rejected the slip",
            "detail": "SportyBet returned a code holding "
                      f"{len(raw_selections) - len(missing)} of {len(raw_selections)} games",
            "unbookable": _logged([{"eventId": m.get("eventId"),
                            "prediction": m.get("prediction"),
                            "reason": "dropped_by_book"} for m in missing]),
        }), 400

    # Got past our own check and SportyBet still said no. That is the case
    # worth seeing: it means the cache disagreed with them, or something else
    # is wrong, and until now it was thrown away silently.
    # Got past our own check and SportyBet still said no: our cache and theirs
    # disagree, which is the one worth being told about rather than reading later.
    report("booking: SportyBet rejected a slip that passed validation",
           reason=str(result.get("error"))[:200], legs=len(raw_selections),
           markets=",".join(sorted({(i.get("prediction") or "?") for i in raw_selections})))
    # NAME SOMETHING, OR THE WHOLE SLIP DIES FOR A LEG NOBODY CAN FIND.
    #
    # SportyBet's refusal is one sentence about the slip - "invalid event data,
    # no market there" - and it names no event and no market. Every other
    # refusal on this API carries `unbookable`, the client drops exactly those
    # legs, names the matches, and asks whether to book the rest. This one
    # carried nothing, so the reader was shown a flat "SportyBet wouldn't take
    # this slip" over a slip they could not correct. Reported 20 Sep.
    #
    # The suspects are our cache's own doubts: markets we model, on events we
    # hold, where our copy has no price or a collapsed one. They are too weak
    # to refuse a leg BEFORE asking - that premise was false and cost a reader
    # four bookable legs (JTEJA5, see _unbookable) - but SportyBet has now
    # answered, and it said no. Against a refusal they are the only candidates
    # there are, so they are offered as such: the client asks before dropping
    # anything, and a wrong guess costs one retry rather than the slip.
    #
    # Nothing invented when there are no suspects: the plain error stands, the
    # same as today.
    body = {"success": False, "message": "SportyBet rejected the slip",
            "detail": result.get("error"), "sent": result.get("sent")}

    # THEIR LIVE CARD FIRST - see _live_verdicts. It names the dead legs with a
    # reason in one request per game; the probe below stays for what the card
    # cannot see (a combination rule, a card that did not answer).
    live_t = time.time()
    live_bad = _live_verdicts(raw_selections)
    report("booking: live card read after a SportyBet refusal",
           level="info", legs=len(raw_selections), found=len(live_bad),
           seconds=round(time.time() - live_t, 2),
           reasons=", ".join(sorted({v["reason"] for v in live_bad})),
           markets=", ".join(sorted({
               str(raw_selections[v["i"]].get("prediction")) for v in live_bad})))
    if live_bad and len(live_bad) < len(raw_selections):
        body["unbookable"] = []
        for v in live_bad:
            leg = {"eventId": raw_selections[v["i"]].get("eventId"),
                   "prediction": raw_selections[v["i"]].get("prediction"),
                   "reason": v["reason"]}
            if v.get("now"):
                leg["now"] = v["now"]
            body["unbookable"].append(leg)
        _log_refused(body["unbookable"])
        return jsonify(body), 400

    # ASK THEM WHICH LEG, RATHER THAN GUESSING FROM OUR OWN CACHE.
    # Only on a multi-leg slip: a single leg refused alone is already named by
    # being the only one there, and probing it would spend a call to learn
    # nothing.
    if len(formatted_selections) > 1:
        doubted = {(s.get("eventId"), s.get("prediction"))
                   for s in (how.get("suspects") or [])}
        bad_ix, probe = _probe_refusal(formatted_selections, first=[
            i for i, it in enumerate(raw_selections)
            if (it.get("eventId"), it.get("prediction")) in doubted])
        report("booking: probed a nameless SportyBet refusal",
               level="info", legs=len(raw_selections), found=len(bad_ix),
               calls=probe["calls"], seconds=probe["seconds"],
               ran_out=probe["ran_out"],
               markets=", ".join(sorted({
                   str(raw_selections[i].get("prediction")) for i in bad_ix})))
        if bad_ix and len(bad_ix) < len(raw_selections):
            body["unbookable"] = [
                {"eventId": raw_selections[i].get("eventId"),
                 "prediction": raw_selections[i].get("prediction"),
                 "reason": "refused_alone"} for i in bad_ix]
            _log_refused(body["unbookable"])
            return jsonify(body), 400
        if not bad_ix and not probe["ran_out"]:
            # EVERY LEG BOOKS ALONE AND THE SLIP DOES NOT. That is a statement
            # about the combination - two legs on one game, most likely - and
            # telling the reader to remove "a leg" would be advice about a
            # problem they do not have.
            body["combination"] = True
            return jsonify(body), 400

    suspects = how.get("suspects") or []
    if suspects and len(suspects) < len(raw_selections):
        body["unbookable"] = [dict(s, reason="suspect") for s in suspects]
    elif suspects:
        # EVERY LEG SUSPECT IS STILL WORTH SAYING, JUST NOT AS `unbookable`.
        # That field means "drop these and the rest may book", and when it
        # covers the whole slip there is no rest - the client would offer a
        # retry with nothing in it. So the guard above stays exactly as it is.
        #
        # The reader is owed the reason anyway. Measured against the live route
        # on 21 Sep: ten legs on events whose feed carries no such market came
        # back 400 with no `unbookable` and no reason at all, and the page could
        # only say "SportyBet wouldn't take this slip" about a slip it could not
        # explain. `suspect_markets` is the shortest true sentence available -
        # these are the markets our copy of their card has no price for - and it
        # is offered as information, never as a list to drop.
        body["suspectAll"] = True
        body["suspectMarkets"] = how.get("suspect_markets") or []
    return jsonify(body), 400


@app.route('/', methods=['GET'])
def home():
    """Also reports where the cache lives, so adding Redis can be confirmed
    from a browser rather than by trawling deploy logs. If this says
    "memory" after REDIS_URL is set, the variable did not take."""
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    live = _cache_get("live", _LIVE_CACHE)
    redis_ok = False
    if _redis:
        try:
            _redis.ping()
            redis_ok = True
        except Exception:  # noqa: BLE001
            redis_ok = False
    return jsonify({
        "status": "SoccerWizard API is running successfully!",
        # Which commit is serving, so "is my push live?" is one request, not a
        # guess from cache ages. Railway sets this on every deploy.
        "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or None,
        "cache": "redis" if redis_ok else "memory",
        "redisConfigured": bool(REDIS_URL),
        # Same reason as redisConfigured: after setting SENTRY_DSN this says
        # whether the variable actually took, without reading deploy logs.
        # "configured" is the DSN being present; "active" is the SDK having
        # started, which is the one that matters and can differ.
        "sentryConfigured": bool(SENTRY_DSN),
        "sentryActive": bool(_sentry),
        "fixtures": {
            "count": len((entry or {}).get("data") or []),
            "ageSeconds": int(time.time() - entry["at"]) if entry else None,
        },
        "livescores": {
            "count": len((live or {}).get("data") or []),
            "ageSeconds": int(time.time() - live["at"]) if live else None,
        },
        "markets": list(FIXTURE_MARKET_IDS),
    })


if __name__ == '__main__':
    app.run(port=5000, debug=True)
