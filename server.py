import os
import re
import time
import json
import logging
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests
from urllib.parse import quote
import bet9ja
import betking
from curl_cffi.requests import RequestsError

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
    except Exception as ex:
        log.warning("SENTRY_DSN set but Sentry init failed (is sentry-sdk installed?): %s", ex)
        _sentry = None
else:
    _sentry = None


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
            " ".join("%s=%s" % (k, v) for k, v in sorted(context.items())))
    if not _sentry:
        return
    try:
        with _sentry.push_scope() as scope:
            scope.set_tag("area", "booking")
            for k, v in context.items():
                scope.set_extra(k, v)
            _sentry.capture_message(message, level=level)
    except Exception as ex:   # never let reporting break the thing it reports on
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
    PASSTHROUGH_MAP["CARD_H_%d" % _n] = {
        "marketId": 800060, "outcomeId": "800060:%08d" % _n, "specifier": ""}
    PASSTHROUGH_MAP["CARD_A_%d" % _n] = {
        "marketId": 800060, "outcomeId": "800060:%08d" % (100 + _n), "specifier": ""}

# CORNERS, total for the match. Market 166, outcome 12 over and 13 under - the
# same shape as goals on market 18, which is why it needed no thought once it
# was looked at. Both books quote half lines, so there is nothing to reconcile
# and no push to worry about.
# They do not carry the same ones. SportyBet runs 6.5 to 12.5 and Bet9ja 7.5 to
# 14.5, so 7.5 through 12.5 cross and the ends do not: a 6.5 leg reads and
# splits here with nowhere to land there, and the same for their 13.5 and 14.5.
for _line in ("6.5", "7.5", "8.5", "9.5", "10.5", "11.5", "12.5"):
    PASSTHROUGH_MAP["CORNERS_OV_%s" % _line] = {
        "marketId": 166, "outcomeId": 12, "specifier": "total=%s" % _line}
    PASSTHROUGH_MAP["CORNERS_UN_%s" % _line] = {
        "marketId": 166, "outcomeId": 13, "specifier": "total=%s" % _line}

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
            PASSTHROUGH_MAP["%sEH_%d_%d_%s" % (_pre, _h, _a, _sfx)] = {
                "marketId": _mid, "outcomeId": _out,
                "specifier": "hcp=%d:%d" % (_h, _a)}

# ASIAN HANDICAP WITHIN ONE HALF. 66 and 88 against 16 for the match, and the
# same two outcomes - 1714 home, 1715 away.
for _pre, _mid in (("FH_", 66), ("SH_", 88)):
    for _l in ("-2", "-1.5", "-1", "-0.5", "0", "0.5"):
        PASSTHROUGH_MAP["%sAH_1_%s" % (_pre, _l)] = {
            "marketId": _mid, "outcomeId": 1714, "specifier": "hcp=%s" % _l}
        PASSTHROUGH_MAP["%sAH_2_%s" % (_pre, _l)] = {
            "marketId": _mid, "outcomeId": 1715, "specifier": "hcp=%s" % _l}

# FIRST-HALF 1X2 & TOTAL. The full-match twin is six separate Yes/No markets
# (854-859); this is ONE market with six outcomes, so the ids are read off it
# directly rather than derived from the sign. 1.5 is the only line they quote.
for _sfx, _out in (("1_UN", 794), ("1_OV", 796), ("X_UN", 798),
                   ("X_OV", 800), ("2_UN", 802), ("2_OV", 804)):
    _sign, _dir = _sfx.split("_")
    PASSTHROUGH_MAP["FH_MIX_%s_%s_1.5" % (_sign, _dir)] = {
        "marketId": 79, "outcomeId": _out, "specifier": "total=1.5"}

# CORNER RANGE, the match and each side. Same composite-id shape as goal range
# on market 25: the variant is named in the specifier AND carried inside the
# outcome id, and the two must agree.
_VAR_PR12 = "variant=sr:point_range:12+"
_VAR_PR7 = "variant=sr:point_range:7+"
for _band, _out in (("0_8", 1141), ("9_11", 1142), ("12", 1143)):
    PASSTHROUGH_MAP["CORNRANGE_%s" % _band] = {
        "marketId": 169, "outcomeId": "sr:point_range:12+:%d" % _out,
        "specifier": _VAR_PR12}
for _side, _mid in (("H", 170), ("A", 171)):
    for _band, _out in (("0_2", 1144), ("3_4", 1145), ("5_6", 1146), ("7", 1147)):
        PASSTHROUGH_MAP["CORNRANGE_%s_%s" % (_side, _band)] = {
            "marketId": _mid, "outcomeId": "sr:point_range:7+:%d" % _out,
            "specifier": _VAR_PR7}

# ONE SIDE'S BOOKINGS IN THE FIRST HALF. 900306 home, 900307 away, outcome 30
# over and 31 under - the same shape as team corners below, not the composite
# ids the full-match team-cards market uses. "N or more bookings" is "over
# N-0.5", which is how the full-match CARD_ family is already named, so these
# follow it.
for _pre, _mid in (("H", 900306), ("A", 900307)):
    for _n, _line in ((1, "0.5"), (2, "1.5"), (3, "2.5")):
        PASSTHROUGH_MAP["FH_CARD_%s_%d" % (_pre, _n)] = {
            "marketId": _mid, "outcomeId": 30, "specifier": "total=%s" % _line}
        PASSTHROUGH_MAP["FH_CARDUN_%s_%d" % (_pre, _n)] = {
            "marketId": _mid, "outcomeId": 31, "specifier": "total=%s" % _line}
# ---------------------------------------------------------------------------

# ONE SIDE'S CORNERS. 900300 is the HOME team's total and 900301 the away
# team's, outcome 30 over and 31 under, the line in the specifier. Read off
# their catalogue on 14 Sep: 3.5 through 7.5 on the home side, and a real
# punter's code carried `900300/30/total=3.5` - which read back as an unknown
# market because only one line of this family had ever been mapped.
for _line in ("3.5", "4.5", "5.5", "6.5", "7.5"):
    PASSTHROUGH_MAP["CORNERS_H_OV_%s" % _line] = {
        "marketId": 900300, "outcomeId": 30, "specifier": "total=%s" % _line}
    PASSTHROUGH_MAP["CORNERS_H_UN_%s" % _line] = {
        "marketId": 900300, "outcomeId": 31, "specifier": "total=%s" % _line}
    PASSTHROUGH_MAP["CORNERS_A_OV_%s" % _line] = {
        "marketId": 900301, "outcomeId": 30, "specifier": "total=%s" % _line}
    PASSTHROUGH_MAP["CORNERS_A_UN_%s" % _line] = {
        "marketId": 900301, "outcomeId": 31, "specifier": "total=%s" % _line}

# EXCLUDED NUMBER OF GOALS, market 450004 for the match and 810002 for the
# first half. The bet is "the total will be anything BUT this number", and the
# outcome id IS the number - 0,1,2,3,4 and 5 meaning five-or-more on the match,
# 3 meaning three-or-more in the half. Nothing else in these tables uses the
# outcome id as a value, which is worth knowing before somebody reads it as an
# index. Found in PV5CLL, a reader's code: two legs of thirty-nine.
for _n in ("0", "1", "2", "3", "4", "5"):
    PASSTHROUGH_MAP["EXGOALS_%s" % _n] = {
        "marketId": 450004, "outcomeId": int(_n), "specifier": ""}
for _n in ("0", "1", "2", "3"):
    PASSTHROUGH_MAP["EXGOALS_FH_%s" % _n] = {
        "marketId": 810002, "outcomeId": int(_n), "specifier": ""}

# GOAL BOUNDS, one side's goals as a RANGE: 450002 is the home team and 450003
# the away team. The outcome id spells the range in digits - 0 is none, 1 is
# exactly one, 12 is one-to-two, 13 is one-to-three-or-more, 33 is three-plus -
# so the ids are not sequential and cannot be generated from a count. Written
# out from their own card, PV5CLL carried `450003/23/` (two to three or more).
_GOAL_BOUNDS = ("0", "1", "2", "11", "12", "13", "22", "23", "33")
for _b in _GOAL_BOUNDS:
    PASSTHROUGH_MAP["BOUNDS_H_%s" % _b] = {
        "marketId": 450002, "outcomeId": int(_b), "specifier": ""}
    PASSTHROUGH_MAP["BOUNDS_A_%s" % _b] = {
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
        PASSTHROUGH_MAP["%s_%s" % (_code, _sfx)] = {
            "marketId": _mkt, "outcomeId": _out, "specifier": "goalnr=1"}

# EXACT GOALS, match and each half. The ceiling differs per scope - 6+ on the
# match, 3+ in the first half, 2+ in the second - and it is part of both the
# specifier and every outcome id.
for _n, _out in zip(range(7), range(68, 75)):
    PASSTHROUGH_MAP["EXACT_%d" % _n] = {
        "marketId": 21, "outcomeId": "sr:exact_goals:6+:%d" % _out,
        "specifier": _VAR_EXACT6}
for _n, _out in zip(range(4), range(88, 92)):
    PASSTHROUGH_MAP["EXACT_FH_%d" % _n] = {
        "marketId": 71, "outcomeId": "sr:exact_goals:3+:%d" % _out,
        "specifier": _VAR_EXACT3}
for _n, _out in zip(range(3), range(85, 88)):
    PASSTHROUGH_MAP["EXACT_SH_%d" % _n] = {
        "marketId": 93, "outcomeId": "sr:exact_goals:2+:%d" % _out,
        "specifier": _VAR_EXACT2}

# ONE SIDE'S EXACT GOALS. Same variant as the first half above, and the same
# outcome ids - 23 is the home team and 24 the away team.
for _side, _mkt in (("H", 23), ("A", 24)):
    for _n, _out in zip(range(4), range(88, 92)):
        PASSTHROUGH_MAP["TEAMGOALS_%s_%d" % (_side, _n)] = {
            "marketId": _mkt, "outcomeId": "sr:exact_goals:3+:%d" % _out,
            "specifier": _VAR_EXACT3}

# GOAL RANGE - the whole match's goals as a band.
for _name, _out in (("0_1", 1342), ("2_3", 1343), ("4_6", 1344), ("7", 1345)):
    PASSTHROUGH_MAP["GOALRANGE_%s" % _name] = {
        "marketId": 25, "outcomeId": "sr:goal_range:7+:%d" % _out,
        "specifier": _VAR_RANGE7}

# WINNING MARGIN, including the draw - which is a seventh outcome here rather
# than a market of its own.
for _name, _out in (("H1", 113), ("H2", 114), ("H3", 115),
                    ("A1", 116), ("A2", 117), ("A3", 118), ("DRAW", 119)):
    PASSTHROUGH_MAP["MARGIN_%s" % _name] = {
        "marketId": 15, "outcomeId": "sr:winning_margin:3+:%d" % _out,
        "specifier": _VAR_MARGIN}

# BOTH HALVES OVER / UNDER 1.5. Two markets, each a plain Yes/No - 74 and 76,
# the pair the combination markets use.
for _code, _mkt in (("BOTHHALVES_OV", 58), ("BOTHHALVES_UN", 59)):
    for _sfx, _out in (("Y", 74), ("N", 76)):
        PASSTHROUGH_MAP["%s_%s" % (_code, _sfx)] = {
            "marketId": _mkt, "outcomeId": _out, "specifier": "total=1.5"}

# SECOND-HALF GOALS, whole match and per side, and the first half per side.
# Outcome 12 over and 13 under throughout, the line in the specifier - the
# same shape as market 18, which is why these need no thought beyond the ids.
for _line in ("0.5", "1.5", "2.5"):
    PASSTHROUGH_MAP["SH_OVER_%s" % _line] = {
        "marketId": 90, "outcomeId": 12, "specifier": "total=%s" % _line}
    PASSTHROUGH_MAP["SH_UNDER_%s" % _line] = {
        "marketId": 90, "outcomeId": 13, "specifier": "total=%s" % _line}
    for _half, _h_mkt, _a_mkt in (("FH", 69, 70), ("SH", 91, 92)):
        PASSTHROUGH_MAP["%s_HOME_OVER_%s" % (_half, _line)] = {
            "marketId": _h_mkt, "outcomeId": 12, "specifier": "total=%s" % _line}
        PASSTHROUGH_MAP["%s_HOME_UNDER_%s" % (_half, _line)] = {
            "marketId": _h_mkt, "outcomeId": 13, "specifier": "total=%s" % _line}
        PASSTHROUGH_MAP["%s_AWAY_OVER_%s" % (_half, _line)] = {
            "marketId": _a_mkt, "outcomeId": 12, "specifier": "total=%s" % _line}
        PASSTHROUGH_MAP["%s_AWAY_UNDER_%s" % (_half, _line)] = {
            "marketId": _a_mkt, "outcomeId": 13, "specifier": "total=%s" % _line}

# GOALS IN THE FIRST N MINUTES, market 60180, outcome 12 over and 13 under.
# The specifier carries BOTH numbers - `minsnr=10|total=1.5` is "over 1.5 goals
# in the first ten minutes" - which is why this cannot be folded into the plain
# over/under family: the same market id serves every window, and dropping the
# minsnr half would book a full-match line instead of a ten-minute one.
# Six legs of the thirty-one in HCVKA1 were these, all unreadable until now.
# The windows SportyBet publishes, read off their own card: 10 minutes at 1.5,
# 30 at 2.5, 50 at 3.5. A window they do not sell is not one to invent.
for _mins, _total in (("10", "1.5"), ("30", "2.5"), ("50", "3.5")):
    PASSTHROUGH_MAP["EARLY_OV_%s_%s" % (_mins, _total)] = {
        "marketId": 60180, "outcomeId": 12,
        "specifier": "minsnr=%s|total=%s" % (_mins, _total)}
    PASSTHROUGH_MAP["EARLY_UN_%s_%s" % (_mins, _total)] = {
        "marketId": 60180, "outcomeId": 13,
        "specifier": "minsnr=%s|total=%s" % (_mins, _total)}

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
        PASSTHROUGH_MAP["HIGHHALF_%s%s" % (_hh, _sfx)] = {
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
    PASSTHROUGH_MAP["AH_1_%s" % _l] = {
        "marketId": 16, "outcomeId": 1714, "specifier": "hcp=%s" % _l}
    PASSTHROUGH_MAP["AH_2_%s" % _l] = {
        "marketId": 16, "outcomeId": 1715, "specifier": "hcp=%s" % _l}
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
_FIXTURES_TTL = 45 * 60
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
    except Exception as ex:
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
        except Exception as ex:
            log.warning("redis get %s failed, using local: %s", name, ex)
    return mem if mem.get("data") is not None else None

def _cache_put(name, mem, data):
    """Store a cache entry. Always update the local dict (fallback + no-Redis
    path); mirror to Redis when enabled."""
    mem["at"] = time.time(); mem["data"] = data
    if _redis:
        try:
            _redis.set("sw:cache:" + name, json.dumps({"at": mem["at"], "data": data}))
        except Exception as ex:
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
                      "854", "856", "858", "860", "861", "862")


def _headers(region="ng"):
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    headers = dict(_headers(region)); headers["Content-Type"] = "application/json"
    try:
        response = requests.post(url, json={"selections": selections_list},
                                 headers=headers, impersonate="chrome120", timeout=10)
        data = response.json()
        if data.get("bizCode") == 10000:
            return {"code": data.get("data", {}).get("shareCode")}
        return {"error": data.get("message") or data, "sent": selections_list}
    except Exception as e:
        # Deliberately broad: this is a user-facing path and the route relies on
        # always getting a dict back (never a 500). Log so failures are visible.
        log.warning("booking request to SportyBet failed: %s", e)
        return {"error": f"request failed: {e}", "sent": selections_list}


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


def fetch_sportybet_fixtures(region="ng"):
    """Fetch upcoming events and merge odds across the markets we bet on.

    The pcUpcomingEvents endpoint returns each event's `markets` array filtered
    to the marketId requested, so a single-market fetch (the old behaviour) only
    ever yielded 1X2 odds - OVER/UNDER and GG/NG never arrived and the frontend
    had nothing to de-vig or blend for those. We now fetch each market and merge
    odds by eventId. Event metadata (teams, kickoff) is taken from whichever
    market first surfaces the event.

    Cost: ~3x the requests, paid only on a cache miss (TTL {}m). Partial failure
    (one market down) still returns the odds we did get; total failure raises so
    the caller can serve stale.
    """.format(_FIXTURES_TTL // 60)
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
    deadline = time.time() + 900

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
                break
            url = (f"https://www.sportybet.com/api/{region}/factsCenter/pcUpcomingEvents"
                   f"?sportId=sr:sport:1&marketId={market_id}&pageSize=100&pageNum={page}")
            # One retry before abandoning a market. A single refused request
            # used to zero every market it touched, which is how one bad
            # minute turned into an empty feed.
            data = None
            for attempt in (1, 2):
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
            key = e.get("eventId") or "%s|%s|%s" % (
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
    except Exception as ex:
        log.warning("fixtures refresh failed, keeping previous copy: %s", ex)
    return False

def _fixtures_loop():
    # With a shared cache the copy in Redis outlives this process, so a
    # redeploy usually starts with data that is minutes old. Refetching it
    # straight away would spend forty-nine requests to replace something we
    # already have - and every one of those is a request that got this server
    # refused once. Wait out whatever is left of its life instead.
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _FIXTURES_TTL:
            wait = _FIXTURES_TTL - age
            log.info("fixtures cache is %ds old; first refresh in %ds",
                     int(age), int(wait))
            time.sleep(wait)
    while True:
        ok = _refresh_fixtures_once()
        # Retry sooner after a failure than after a success, but never so
        # soon that a refused IP gets hammered back into refusing.
        time.sleep(_FIXTURES_TTL if ok else 300)

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


@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if entry and entry.get("data"):
        age = int(time.time() - entry["at"])
        return jsonify({"success": True, "cached": True, "ageSeconds": age,
                        "stale": age > _FIXTURES_TTL,
                        "count": len(entry["data"]), "matches": entry["data"]})
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
        return dict(
            bad_legs=len(legs), total_legs=len(picks),
            markets=", ".join(sorted({str(b["prediction"]) for b in legs})),
            events=", ".join(sorted({str(b["eventId"]) for b in legs})[:10]))

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
            "detail": "no market there for %d of %d picks" % (len(bad), len(picks)),
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
                        "error": "betking takes at most %d selections"
                                 % betking.BETSLIP_MAX,
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
        return dict(
            bad_legs=len(legs), total_legs=len(picks),
            markets=", ".join(sorted({str(b["prediction"]) for b in legs})),
            events=", ".join(sorted({str(b["eventId"]) for b in legs})[:10]))

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
                  else "no market there for %d of %d picks" % (len(bad), len(picks)))
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
    url = ("https://www.sportybet.com/api/%s/factsCenter/event"
           "?eventId=%s&productId=3" % (region, quote(str(event_id))))
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
    url = "https://www.sportybet.com/api/%s/orders/share/%s" % (region, quote(str(code)))
    try:
        r = requests.get(url, headers=_headers(region), impersonate="chrome120",
                         timeout=timeout)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("sportybet share read failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    if (body or {}).get("bizCode") != 10000:
        return {"error": "not found", "notFound": True}

    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    by_event = {m.get("eventId"): m for m in ((entry or {}).get("data") or [])
                if isinstance(m, dict)}

    out = []
    ticket = ((body.get("data") or {}).get("ticket") or {})
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
            "odds": (fx.get("odds") or {}).get(pred) if pred else None,
            # Only present when the fixture had to be named by asking SportyBet
            # directly, which happens when our board has let it go: H1 / HT /
            # H2 / ENDED says the game is on or over, which is a different
            # sentence from "we don't carry it".
            "status": fx.get("status") or "",
        })
    return {"legs": out}


@app.route('/api/slip', methods=['GET'])
def api_read_slip():
    """GET /api/slip?book=sporty|bet9ja|betking&code=XXXX -> the legs behind a code."""
    book = (request.args.get("book") or "sporty").strip().lower()
    code = (request.args.get("code") or "").strip()
    if not _CODE_RE.match(code):
        return jsonify({"success": False, "error": "that is not a booking code"}), 400
    if book not in ("sporty", "bet9ja", "betking"):
        return jsonify({"success": False, "error": "unknown bookmaker"}), 400

    # Named rather than defaulted, so a typo in `book` can never be read
    # against the wrong bookmaker and answered as though it were that one's.
    if book == "bet9ja":
        out = bet9ja.read_coupon(code)
    elif book == "betking":
        out = betking.read_coupon(code)
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
            "detail": "no market there for %d of %d picks" % (len(unmapped), len(raw_selections)),
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
    if result.get("code"):
        return jsonify({"success": True, "booking_code": result["code"]})

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
    suspects = how.get("suspects") or []
    if suspects and len(suspects) < len(raw_selections):
        body["unbookable"] = [dict(s, reason="suspect") for s in suspects]
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
        except Exception:
            redis_ok = False
    return jsonify({
        "status": "SoccerWizard API is running successfully!",
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
