"""Betpawa: the fourth book.

Read-only recon came first (21 Sep 2026) and everything below was measured
against the live site rather than carried over from the other three modules.

TWO API VERSIONS, AND THE SPLIT IS NOT GUESSABLE. v4 serves events and
categories; v3 serves prices and booking codes. Read out of their own Next.js
bundle (`getEventByIdV4`, `postBookingNumber`, ...), not by probing.

NO AUTH OF ANY KIND. No cookie, no token, no session. A bad code answers
`400 BOOKING_CODE_NOT_FOUND`, which is a refusal rather than a block page, and
that is the proof the route is open rather than merely unprotected today.

THEY REFUSE LOUDLY, WHICH BETKING DOES NOT. A selection id that does not exist
answers `400 SPORTSBOOK_WRONG_SELECTION`, and so does a multiple carrying two
legs from one match. That is the opposite of BetKing, which answers an ordinary
code for both and hands the reader an empty slip. The read-back in
generate_code stays anyway: "they refuse loudly today" is an observation about
their validator, not a guarantee, and the cost of checking is one GET.
"""

import json
import logging
import time

from curl_cffi import requests

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"

SITE = "https://www.betpawa.ng"
API = SITE + "/api/sportsbook"
LIST_URL = API + "/v4/events/lists/by-queries"
EVENT_URL = API + "/v4/events/%s"
BOOKING_URL = API + "/v3/booking-number"

FOOTBALL = "2"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Their own page size, stated in the refusal: asking for 200 answers
# BAD_REQUEST / "The maximum number of events is 100".
PAGE = 100

# NOT READ FROM THEM, AND DELIBERATELY SO. Their booking endpoint accepted 300
# selections on one code (21 Sep, 300 distinct fixtures, all 300 read back), so
# there is no ceiling of theirs to respect here. This is ours, matching the
# other three books, because a slip that size is a different problem from a
# bookmaker limit and should be refused for our own reasons.
BETSLIP_MAX = 60


def _headers(extra=None):
    """Their brand headers are not decoration - the API is multi-country."""
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": SITE + "/",
        "Origin": SITE,
        "x-pawa-brand": "betpawa-nigeria",
        "x-pawa-language": "en",
        "devicetype": "web",
    }
    if extra:
        h.update(extra)
    return h


# --- the markets we model ---------------------------------------------------
# code -> (marketTypeId, line, outcomeTypeId), every value a string because
# that is how their JSON carries them and a str/int mix is how a lookup table
# silently stops matching.
#
# `line` is None for a market that has exactly one row, and the line itself for
# one that has several. See _line() for which of the two places it is read
# from, and why the row's own `handicap` field is not it.
MARKET_MAP = {
    "1": ("3743", None, "3744"),
    "X": ("3743", None, "3745"),
    "2": ("3743", None, "3746"),
    # Named in their own order for once - 1X, X2, 12. BetKing runs 9/10/11 with
    # 10 meaning home-or-away, so two books agreeing here is a coincidence and
    # must not be carried to a fifth.
    "1X": ("4693", None, "4694"),
    "X2": ("4693", None, "4695"),
    "12": ("4693", None, "4696"),
    "OVER_1.5": ("5000", "1.5", "5001"),
    "OVER_2.5": ("5000", "2.5", "5001"),
    "OVER_3.5": ("5000", "3.5", "5001"),
    "GG": ("3795", None, "3796"),
    "FH_OVER_0.5": ("4958", "0.5", "4959"),
    # Team totals are their own market per side, sharing nothing with the match
    # total: 5006 is home, 5003 is away, and each has its own outcome ids.
    "HOME_OVER_0.5": ("5006", "0.5", "5007"),
    "HOME_OVER_1.5": ("5006", "1.5", "5007"),
    "AWAY_OVER_0.5": ("5003", "0.5", "5004"),
    "AWAY_OVER_1.5": ("5003", "1.5", "5004"),
    # The under side of every line. The builders never offer these; they are
    # here to de-vig, exactly as in bet9ja.py and betking.py - over and under
    # together give the book's implied probability with the margin taken out,
    # and a book with no unders blends nothing.
    "UNDER_1.5": ("5000", "1.5", "5002"),
    "UNDER_2.5": ("5000", "2.5", "5002"),
    "UNDER_3.5": ("5000", "3.5", "5002"),
    "NG": ("3795", None, "3797"),
    "FH_UNDER_0.5": ("4958", "0.5", "4960"),
    "HOME_UNDER_0.5": ("5006", "0.5", "5008"),
    "HOME_UNDER_1.5": ("5006", "1.5", "5008"),
    "AWAY_UNDER_0.5": ("5003", "0.5", "5005"),
    "AWAY_UNDER_1.5": ("5003", "1.5", "5005"),
}



# --- markets we carry but never model --------------------------------------
# A CONVERTER NEEDS IDENTITY, NOT A PREDICTION. Everything above is a market
# the model has an opinion about. These are not: they exist so a slip somebody
# else built can be read, re-cut and moved between books without us pretending
# to rate it. Nothing here is ever produced by tipCode, so no board, no record
# and no calibration changes by their being here.
#
# GENERATED, NEVER TYPED - tools/bpgen.js, 21 Sep 2026, against the eight
# deepest cards on the board (Barcelona-Getafe at 137 markets down to
# Norway-Denmark at 126). Every candidate is proposed from the meaning of our
# own code and kept ONLY if Betpawa actually prices it on one of them. 197 of
# 382 crossed; the twelve that did not, and the 173 refused on meaning, are in
# NOT_CARRIED below with a reason each.
#
# PROPOSED IN THEIR WORDS, NOT THEIR IDS. Betpawa names every market and every
# outcome in full - "Total Score Over/Under - FT - Home Team", "Home by 3+" -
# so the generator says what it MEANS and the card answers with the ids. The
# last book had to guess numeric ids from siblings, which invented 24 codes
# that matched nothing anywhere.
#
# THE HANDICAP SIGNS WERE CHECKED AGAINST THE PRICES, PER CARD. The side
# receiving the bigger head start must be shorter, and it is: 54 comparisons
# to 0 on the Asian market, 60 to 0 on the three-way one. Pooling prices
# ACROSS cards - the first version of that check - reported three
# disagreements out of sixteen, because Barcelona's -1.5 is shorter than a
# Norwegian fixture's -0.5 for reasons that have nothing to do with the sign.
#
# AND THE DRAW OUTCOME NAMES WHICHEVER SIDE IS GIVING. On a three-way handicap
# row where the home team gives a goal the X price reads "Home -1"; on the row
# where the away team gives one it reads "Away -1", never "Home +1". Read off
# three cards rather than continued from the pattern the other two outcomes
# follow, which had cost the whole family.
PASSTHROUGH_MAP = {
    # --- asian handicap (28) ---
    "AH_1_-0.5": ("3774", "-0.5", "3775"),
    "AH_1_-1.5": ("3774", "-1.5", "3775"),
    "AH_1_-2.5": ("3774", "-2.5", "3775"),
    "AH_1_-3.5": ("3774", "-3.5", "3775"),
    "AH_1_-4.5": ("3774", "-4.5", "3775"),
    "AH_1_0.5": ("3774", "+0.5", "3775"),
    "AH_1_1.5": ("3774", "+1.5", "3775"),
    "AH_1_2.5": ("3774", "+2.5", "3775"),
    "AH_2_-0.5": ("3774", "+0.5", "3776"),
    "AH_2_-1.5": ("3774", "+1.5", "3776"),
    "AH_2_-2.5": ("3774", "+2.5", "3776"),
    "AH_2_-3.5": ("3774", "+3.5", "3776"),
    "AH_2_-4.5": ("3774", "+4.5", "3776"),
    "AH_2_0.5": ("3774", "-0.5", "3776"),
    "AH_2_1.5": ("3774", "-1.5", "3776"),
    "AH_2_2.5": ("3774", "-2.5", "3776"),
    "FH_AH_1_-0.5": ("3747", "-0.5", "3748"),
    "FH_AH_1_-1.5": ("3747", "-1.5", "3748"),
    "FH_AH_1_0.5": ("3747", "+0.5", "3748"),
    "FH_AH_2_-0.5": ("3747", "+0.5", "3749"),
    "FH_AH_2_-1.5": ("3747", "+1.5", "3749"),
    "FH_AH_2_0.5": ("3747", "-0.5", "3749"),
    "SH_AH_1_-0.5": ("3756", "-0.5", "3757"),
    "SH_AH_1_-1.5": ("3756", "-1.5", "3757"),
    "SH_AH_1_0.5": ("3756", "+0.5", "3757"),
    "SH_AH_2_-0.5": ("3756", "+0.5", "3758"),
    "SH_AH_2_-1.5": ("3756", "+1.5", "3758"),
    "SH_AH_2_0.5": ("3756", "-0.5", "3758"),
    # --- corners (10) ---
    "CORNERS_OV_10.5": ("1096783", "10.5", "1099466"),
    "CORNERS_OV_6.5": ("1096783", "6.5", "1099466"),
    "CORNERS_OV_7.5": ("1096783", "7.5", "1099466"),
    "CORNERS_OV_8.5": ("1096783", "8.5", "1099466"),
    "CORNERS_OV_9.5": ("1096783", "9.5", "1099466"),
    "CORNERS_UN_10.5": ("1096783", "10.5", "1099467"),
    "CORNERS_UN_6.5": ("1096783", "6.5", "1099467"),
    "CORNERS_UN_7.5": ("1096783", "7.5", "1099467"),
    "CORNERS_UN_8.5": ("1096783", "8.5", "1099467"),
    "CORNERS_UN_9.5": ("1096783", "9.5", "1099467"),
    # --- corner ranges (3) ---
    "CORNRANGE_0_8": ("1096803", None, "1099437"),
    "CORNRANGE_12": ("1096803", None, "1099439"),
    "CORNRANGE_9_11": ("1096803", None, "1099438"),
    # --- double chance 1UP (3) ---
    "DC1UP_12": ("80000", None, "80003"),
    "DC1UP_1X": ("80000", None, "80001"),
    "DC1UP_X2": ("80000", None, "80002"),
    # --- draw no bet (2) ---
    "DNB_1": ("4703", None, "4704"),
    "DNB_2": ("4703", None, "4705"),
    # --- european handicap (42) ---
    "EH_0_1_1": ("4724", "Home -1", "4725"),
    "EH_0_1_2": ("4724", "Away +1", "4727"),
    "EH_0_1_X": ("4724", "Home -1", "4726"),
    "EH_0_2_1": ("4724", "Home -2", "4725"),
    "EH_0_2_2": ("4724", "Away +2", "4727"),
    "EH_0_2_X": ("4724", "Home -2", "4726"),
    "EH_0_3_1": ("4724", "Home -3", "4725"),
    "EH_0_3_2": ("4724", "Away +3", "4727"),
    "EH_0_3_X": ("4724", "Home -3", "4726"),
    "EH_0_4_1": ("4724", "Home -4", "4725"),
    "EH_0_4_2": ("4724", "Away +4", "4727"),
    "EH_0_4_X": ("4724", "Home -4", "4726"),
    "EH_0_5_1": ("4724", "Home -5", "4725"),
    "EH_0_5_2": ("4724", "Away +5", "4727"),
    "EH_0_5_X": ("4724", "Home -5", "4726"),
    "EH_1_0_1": ("4724", "Home +1", "4725"),
    "EH_1_0_2": ("4724", "Away -1", "4727"),
    "EH_1_0_X": ("4724", "Away -1", "4726"),
    "EH_2_0_1": ("4724", "Home +2", "4725"),
    "EH_2_0_2": ("4724", "Away -2", "4727"),
    "EH_2_0_X": ("4724", "Away -2", "4726"),
    "EH_3_0_1": ("4724", "Home +3", "4725"),
    "EH_3_0_2": ("4724", "Away -3", "4727"),
    "EH_3_0_X": ("4724", "Away -3", "4726"),
    "FH_EH_0_1_1": ("4716", "Home -1", "4717"),
    "FH_EH_0_1_2": ("4716", "Away +1", "4719"),
    "FH_EH_0_1_X": ("4716", "Home -1", "4718"),
    "FH_EH_0_2_1": ("4716", "Home -2", "4717"),
    "FH_EH_0_2_2": ("4716", "Away +2", "4719"),
    "FH_EH_0_2_X": ("4716", "Home -2", "4718"),
    "FH_EH_1_0_1": ("4716", "Home +1", "4717"),
    "FH_EH_1_0_2": ("4716", "Away -1", "4719"),
    "FH_EH_1_0_X": ("4716", "Away -1", "4718"),
    "SH_EH_0_1_1": ("4720", "Home -1", "4721"),
    "SH_EH_0_1_2": ("4720", "Away +1", "4723"),
    "SH_EH_0_1_X": ("4720", "Home -1", "4722"),
    "SH_EH_0_2_1": ("4720", "Home -2", "4721"),
    "SH_EH_0_2_2": ("4720", "Away +2", "4723"),
    "SH_EH_0_2_X": ("4720", "Home -2", "4722"),
    "SH_EH_1_0_1": ("4720", "Home +1", "4721"),
    "SH_EH_1_0_2": ("4720", "Away -1", "4723"),
    "SH_EH_1_0_X": ("4720", "Away -1", "4722"),
    # --- exact goals (12) ---
    "EXACT_0": ("4926", None, "4927"),
    "EXACT_1": ("4926", None, "4928"),
    "EXACT_2": ("4926", None, "4929"),
    "EXACT_3": ("4926", None, "4930"),
    "EXACT_4": ("4926", None, "4931"),
    "EXACT_5": ("4926", None, "4932"),
    "EXACT_6": ("4926", None, "1096818"),
    "EXACT_FH_0": ("4898", None, "4899"),
    "EXACT_FH_1": ("4898", None, "4900"),
    "EXACT_SH_0": ("4912", None, "4913"),
    "EXACT_SH_1": ("4912", None, "4914"),
    "EXACT_SH_2": ("4912", None, "1089749"),
    # --- team goals (24) ---
    "FH_AWAY_OVER_0.5": ("4961", "0.5", "4962"),
    "FH_AWAY_OVER_1.5": ("4961", "1.5", "4962"),
    "FH_AWAY_OVER_2.5": ("4961", "2.5", "4962"),
    "FH_AWAY_UNDER_0.5": ("4961", "0.5", "4963"),
    "FH_AWAY_UNDER_1.5": ("4961", "1.5", "4963"),
    "FH_AWAY_UNDER_2.5": ("4961", "2.5", "4963"),
    "FH_HOME_OVER_0.5": ("4964", "0.5", "4965"),
    "FH_HOME_OVER_1.5": ("4964", "1.5", "4965"),
    "FH_HOME_OVER_2.5": ("4964", "2.5", "4965"),
    "FH_HOME_UNDER_0.5": ("4964", "0.5", "4966"),
    "FH_HOME_UNDER_1.5": ("4964", "1.5", "4966"),
    "FH_HOME_UNDER_2.5": ("4964", "2.5", "4966"),
    "SH_AWAY_OVER_0.5": ("4979", "0.5", "4980"),
    "SH_AWAY_OVER_1.5": ("4979", "1.5", "4980"),
    "SH_AWAY_OVER_2.5": ("4979", "2.5", "4980"),
    "SH_AWAY_UNDER_0.5": ("4979", "0.5", "4981"),
    "SH_AWAY_UNDER_1.5": ("4979", "1.5", "4981"),
    "SH_AWAY_UNDER_2.5": ("4979", "2.5", "4981"),
    "SH_HOME_OVER_0.5": ("4982", "0.5", "4983"),
    "SH_HOME_OVER_1.5": ("4982", "1.5", "4983"),
    "SH_HOME_OVER_2.5": ("4982", "2.5", "4983"),
    "SH_HOME_UNDER_0.5": ("4982", "0.5", "4984"),
    "SH_HOME_UNDER_1.5": ("4982", "1.5", "4984"),
    "SH_HOME_UNDER_2.5": ("4982", "2.5", "4984"),
    # --- 1X2-or-total, first half (6) ---
    "FH_MIX_1_OV_1.5": ("25451913", "1.5", "25451914"),
    "FH_MIX_1_UN_1.5": ("25451913", "1.5", "25451917"),
    "FH_MIX_2_OV_1.5": ("25451913", "1.5", "25451916"),
    "FH_MIX_2_UN_1.5": ("25451913", "1.5", "25451919"),
    "FH_MIX_X_OV_1.5": ("25451913", "1.5", "25451915"),
    "FH_MIX_X_UN_1.5": ("25451913", "1.5", "25451918"),
    # --- first goal (3) ---
    "FIRSTGOAL_1": ("28000224", "1", "28000225"),
    "FIRSTGOAL_2": ("28000224", "1", "28000227"),
    "FIRSTGOAL_N": ("28000224", "1", "28000226"),
    # --- higher-scoring half (9) ---
    "HIGHHALF_1": ("4728", None, "4729"),
    "HIGHHALF_2": ("4728", None, "4730"),
    "HIGHHALF_A_1": ("4732", None, "4733"),
    "HIGHHALF_A_2": ("4732", None, "4734"),
    "HIGHHALF_A_E": ("4732", None, "4735"),
    "HIGHHALF_E": ("4728", None, "4731"),
    "HIGHHALF_H_1": ("4736", None, "4737"),
    "HIGHHALF_H_2": ("4736", None, "4738"),
    "HIGHHALF_H_E": ("4736", None, "4739"),
    # --- winning margin (7) ---
    "MARGIN_A1": ("28000209", None, "28000213"),
    "MARGIN_A2": ("28000209", None, "28000214"),
    "MARGIN_A3": ("28000209", None, "28000215"),
    "MARGIN_DRAW": ("28000209", None, "28000216"),
    "MARGIN_H1": ("28000209", None, "28000210"),
    "MARGIN_H2": ("28000209", None, "28000211"),
    "MARGIN_H3": ("28000209", None, "28000212"),
    # --- 1X2-or-both-score (6) ---
    "MIXGG_1": ("3591790", None, "3591791"),
    "MIXGG_2": ("3591790", None, "3591793"),
    "MIXGG_X": ("3591790", None, "3591795"),
    "MIXNG_1": ("3591790", None, "3591792"),
    "MIXNG_2": ("3591790", None, "3591794"),
    "MIXNG_X": ("3591790", None, "3591796"),
    # --- 1X2-or-total (18) ---
    "MIX_1_OV_1.5": ("1096755", "1.5", "1099337"),
    "MIX_1_OV_2.5": ("1096755", "2.5", "1099337"),
    "MIX_1_OV_3.5": ("1096755", "3.5", "1099337"),
    "MIX_1_UN_1.5": ("1096755", "1.5", "1099340"),
    "MIX_1_UN_2.5": ("1096755", "2.5", "1099340"),
    "MIX_1_UN_3.5": ("1096755", "3.5", "1099340"),
    "MIX_2_OV_1.5": ("1096755", "1.5", "1099339"),
    "MIX_2_OV_2.5": ("1096755", "2.5", "1099339"),
    "MIX_2_OV_3.5": ("1096755", "3.5", "1099339"),
    "MIX_2_UN_1.5": ("1096755", "1.5", "1099342"),
    "MIX_2_UN_2.5": ("1096755", "2.5", "1099342"),
    "MIX_2_UN_3.5": ("1096755", "3.5", "1099342"),
    "MIX_X_OV_1.5": ("1096755", "1.5", "1099338"),
    "MIX_X_OV_2.5": ("1096755", "2.5", "1099338"),
    "MIX_X_OV_3.5": ("1096755", "3.5", "1099338"),
    "MIX_X_UN_1.5": ("1096755", "1.5", "1099341"),
    "MIX_X_UN_2.5": ("1096755", "2.5", "1099341"),
    "MIX_X_UN_3.5": ("1096755", "3.5", "1099341"),
    # --- second-half goals (6) ---
    "SH_OVER_0.5": ("4976", "0.5", "4977"),
    "SH_OVER_1.5": ("4976", "1.5", "4977"),
    "SH_OVER_2.5": ("4976", "2.5", "4977"),
    "SH_UNDER_0.5": ("4976", "0.5", "4978"),
    "SH_UNDER_1.5": ("4976", "1.5", "4978"),
    "SH_UNDER_2.5": ("4976", "2.5", "4978"),
    # --- team goals exact (8) ---
    "TEAMGOALS_A_0": ("4938", None, "4939"),
    "TEAMGOALS_A_1": ("4938", None, "4940"),
    "TEAMGOALS_A_2": ("4938", None, "1080456"),
    "TEAMGOALS_A_3": ("4938", None, "1080457"),
    "TEAMGOALS_H_0": ("4942", None, "4943"),
    "TEAMGOALS_H_1": ("4942", None, "4944"),
    "TEAMGOALS_H_2": ("4942", None, "1080454"),
    "TEAMGOALS_H_3": ("4942", None, "1080455"),
    # --- 1UP / 2UP (6) ---
    "UP1_1": ("28000810", None, "28000811"),
    "UP1_2": ("28000810", None, "28000813"),
    "UP1_X": ("28000810", None, "28000812"),
    "UP2_1": ("28000850", None, "28000851"),
    "UP2_2": ("28000850", None, "28000853"),
    "UP2_X": ("28000850", None, "28000852"),
    # --- win either half (4) ---
    "WINHALF_A_N": ("1096809", None, "1099452"),
    "WINHALF_A_Y": ("1096809", None, "1099451"),
    "WINHALF_H_N": ("1096806", None, "1099446"),
    "WINHALF_H_Y": ("1096806", None, "1099445"),
}

# The market type ids the sweep asks for. Derived from the table rather than
# typed, so a market added above is swept without a second edit - the drift
# between those two lists is exactly how a book ends up unable to book a market
# it can price.
SWEEP_MARKETS = sorted({m for m, _line, _out in MARKET_MAP.values()})

# --- what this book does NOT carry, and why ---------------------------------
# A code in one book's table and not another's is a leg that reads and splits
# and can never convert. Kept as an explicit table with a reason each, so a
# line quietly added to one side never looks like a line deliberately left off
# this one - and so that "verified absent" is never written where "nobody has
# checked" is the truth. Every reason below is the second kind of sentence:
# each was measured by tools/bpgen.js against eight deep cards on 21 Sep 2026.
#
# Longest prefix wins, which matters: AH_ is half carried (the half balls) and
# FH_CARD_ must not inherit FH_'s answer.
NOT_CARRIED = {
    "AH_": "verified absent: their Asian card is half balls only, -5.5 to "
           "+5.5 in whole steps of one (checked on eight deep cards). Quarter "
           "balls are not sold at all, and whole balls exist ONLY on the "
           "three-way Handicap 1X2, which is a DIFFERENT bet - the draw is "
           "its own outcome and nothing pushes - so mapping onto it narrows "
           "the punter's bet. The depth also stops at 2.5 for the side giving "
           "and 4.5 for the side receiving.",
    "FH_AH_": "verified absent: their first-half Asian card runs -2.5 to "
              "+2.5, so the deeper lines have no rung.",
    "SH_AH_": "verified absent: their second-half Asian card runs -2.5 to "
              "+2.5, same as the first half.",
    "EH_": "carried, except at depth: their three-way handicap reaches "
           "Home -5 and Away +5, and the draw outcome names whichever side is "
           "GIVING, so a line past that has nothing to map onto.",
    "SHOTS_": "not read yet: total shots was mapped on "
              "23 Sep 2026 from SportyBet (900394) and Bet9ja (S_OUSHOTS) "
              "cards only, and this book's card has not been read for it. The "
              "site's shots chip offers SportyBet and Bet9ja alone until it is.",
    "CORNERS_": "verified absent past 10.5: their total-corners card is 6.5 "
                "to 10.5 and nothing above it appeared on any of eight cards.",
    "CORNERS_H_": "verified absent: they sell total corners, corner 1X2 and "
                  "corner odd/even, and no per-team corner line appears on "
                  "any deep card.",
    "CORNERS_A_": "verified absent: see CORNERS_H_.",
    "CORNRANGE_H_": "verified absent: see CORNERS_H_.",
    "CORNRANGE_A_": "verified absent: see CORNERS_H_.",
    "CARD_": "verified absent: they sell total bookings and "
             "team-with-most-bookings; no per-team booking count exists on "
             "any deep card.",
    "FH_CARD_": "verified absent: see CARD_.",
    "FH_CARDUN_": "verified absent: see CARD_.",
    "HMC_": "verified absent: their booking markets carry no half-versus-half "
            "bet.",
    "HALFCORNER_": "verified absent: their corner markets carry no "
                   "half-versus-half bet.",
    "PEN_": "verified absent: no penalty-awarded market on any deep card.",
    "EARLY_": "verified absent: their early market is a 1X2 over the first "
              "ten minutes, not a goals line over them.",
    "EXGOALS_": "verified absent: a bet that the total is anything BUT n is "
                "not sold here in any form.",
    "GOALRANGE_": "verified absent: their grouped totals exist on the halves "
                  "only (0-1, 2-3, 4+). The full-time equivalent is "
                  "Multigoals, whose rungs start at 1-2 and never carry 0-1.",
    "BOUNDS_": "verified absent: their per-team Multigoals rungs are 1-2, "
               "1-3, 2-3, 4+ and no goal. Ours are a different set and the "
               "ones that look alike are not - our 1-3 is one-to-three-OR-"
               "MORE - while the rung that would agree is already "
               "TEAMGOALS_x_0, and one triple cannot decode back to two codes.",
    "BOTHHALVES_": "verified absent: their both-halves markets are about a "
                   "TEAM scoring in each half, not about each half clearing a "
                   "goals line.",
    "DC2_": "verified absent: they run the 1UP promotion on double chance and "
            "not 2UP.",
    "FIRSTGOAL_FH_": "verified absent: their Goal market is numbered (1st, "
                     "2nd, ...) rather than split by half, so there is no "
                     "first-goal-in-the-half bet to map onto.",
    "FIRSTGOAL_SH_": "verified absent: see FIRSTGOAL_FH_.",
    "EXACT_FH_": "verified absent above 1: their first-half exact-goals card "
                 "stops at 2+ where ours stops at 3+, so their top rung means "
                 "MORE than our exactly-2 and there is no rung at all for our "
                 "3. Pairing them by position would hand somebody a wider bet "
                 "than they placed.",
    "OVER_": "verified absent: every total they sell is a half line. A whole "
             "line pushes on the number and the half line beside it does not, "
             "so they are different bets.",
    "UNDER_": "verified absent: see OVER_.",
}


def reason_uncarried(code):
    """Why Betpawa does not carry one of our codes, or None if it does.

    Longest prefix wins, so FH_AH_ beats AH_ and FH_CARD_ beats FH_.
    """
    if market_for(code):
        return None
    best = None
    for prefix, why in NOT_CARRIED.items():
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, why)
    return best[1] if best else None


# their key -> our code, derived rather than typed for the same reason. Both
# tables, because a pasted code resolves through exactly the same index - and
# the modelled table is written LAST so that where two of our names describe
# one bet, the modelled name is the one a reader is shown.
_BY_TRIPLE = {triple: code
              for code, triple in list(PASSTHROUGH_MAP.items()) +
              list(MARKET_MAP.items())}


def market_for(code):
    """The one place that answers "do we carry this market on Betpawa?".

    Returns the triple or None. NEVER a default: an unmapped market that falls
    back to something plausible books a bet nobody asked for, and the book
    answers success because the selection it received was real.

    BOTH TABLES, and every caller asks this rather than a table directly.
    Keyed on MARKET_MAP alone, every handicap and corner leg would come back
    "not_mapped" however well Betpawa prices it, so no converted slip carrying
    one could ever be booked here.
    """
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


def _line(price, row):
    """The line of one outcome, as a string, or None.

    THE LINE LIVES ON THE OUTCOME, AND THE ROW'S OWN NUMBER IS NOT IT. Their
    row carries `handicap` as an integer in quarter units - 10 for a 2.5 goals
    line, -8 for a two-goal handicap - while the price beside it carries "2.5"
    and "Home -2" as text. Keying on the row would fold every line of a market
    onto one entry and book whichever was seen last, which is the same trap
    BetKing's corners and handicaps set.

    The row's `specifier` is the fallback, because that is where the read-back
    carries it when the outcome does not.
    """
    got = price.get("handicap")
    if got not in (None, ""):
        return str(got)
    spec = (row or {}).get("specifier") or {}
    for key in ("total", "hcp"):
        if spec.get(key) not in (None, ""):
            return str(spec[key])
    return None


def _outcome(info, line):
    """What they call one outcome, with the line put back into the sentence.

    Their displayName is a TEMPLATE - "Over {formattedHandicap}" - and the
    front end fills it. Shipped as-is it reaches the reader with the braces
    still in it, which is how an unmapped Betpawa leg would read on the panel.
    Falls back to the plain name when there is no line to substitute.
    """
    name = info.get("displayName") or info.get("name") or ""
    if "{" in name:
        if line in (None, ""):
            return info.get("name") or name
        name = name.replace("{formattedHandicap}", str(line))
        if "{" in name:                          # some other placeholder
            return info.get("name") or ""
    return name


def _get_json(url, params=None, timeout=30, attempts=3, pause=1.5):
    """One GET, retried on a transient blip.

    Their list endpoint answered a bodyless 200 once in ten pages of a sweep on
    21 Sep, so a single failure is not evidence of anything and a sweep that
    gives up on it loses a whole page of fixtures. A REFUSAL is different and
    is not retried here - it comes back as an exception to the caller.
    """
    last = None
    for n in range(attempts):
        try:
            r = requests.get(url, params=params, headers=_headers(),
                             timeout=timeout, impersonate=IMPERSONATE)
            return r.json()
        except Exception as ex:                  # noqa: BLE001 - upstream
            last = ex
            if n + 1 < attempts:
                time.sleep(pause * (n + 1))
    raise last


def _row(event):
    """The empty shell of one fixture, in the shape the other three books use."""
    parts = event.get("participants") or []
    names = [p.get("name") or "" for p in parts]
    teams = event.get("name") or " - ".join(names)
    return {
        "eventId": str(event.get("id")),
        "slotId": str(event.get("id")),
        "eventCode": "",
        "teams": teams,
        # An ISO stamp in UTC, passed through untouched. The site pairs on it,
        # and a rewritten stamp is a new way to be wrong.
        "kickoff": event.get("startTime") or "",
        "league": (event.get("competition") or {}).get("name") or "",
        "country": (event.get("region") or {}).get("name") or "",
        "startdate": (event.get("startTime") or "")[:10],
        "odds": {},
        "raw": {},
        # What build_selection needs: the price id, which is the ONLY thing
        # their booking endpoint takes. It is per event and per price, so it
        # cannot be computed and has to be carried beside the odd.
        "sel": {},
    }


def _absorb(row, event):
    """Fold one event's markets into its row, outcome by outcome."""
    for market in event.get("markets") or []:
        mid = str(((market.get("marketType") or {}).get("id")) or "")
        name = (market.get("marketType") or {}).get("name") or ""
        for r in market.get("row") or []:
            for price in r.get("prices") or []:
                code = _BY_TRIPLE.get(
                    (mid, _line(price, r), str(price.get("typeId") or "")))
                if not code:
                    continue
                try:
                    odd = float(price.get("odds"))
                except (TypeError, ValueError):
                    continue
                # A suspended outcome is left on the card with its price
                # collapsed. It pays nothing and cannot be booked, so it is
                # not a price.
                if odd <= 1.01:
                    continue
                row["odds"][code] = odd
                row["raw"][code] = str(odd)
                row["sel"][code] = {
                    "priceId": str(price.get("id")),
                    "marketTypeId": mid,
                    "marketName": name,
                    "selectionName": price.get("name") or "",
                    "odds": odd,
                }


def fetch_page(skip=0, markets=None, timeout=40):
    """One page of their football board, with the markets we asked for.

    THE QUERY PARAM IS `q`, AND IT CARRIES URL-ENCODED JSON. `events=` answers
    BAD_REQUEST / "Request is empty", which reads like a rejected request and
    is only a wrong parameter name.

    `view.marketTypes` takes market type IDS, not the slugs their front end
    shows - `_1X2` is accepted and silently returns events with no markets at
    all, which is the worst of both answers. Asking for our whole modelled set
    costs one request per hundred fixtures and removes the per-event fetch the
    board would otherwise need.

    A page past the end answers `{"responses": [{}]}` - a 200 with the inner
    key ABSENT rather than an empty list. Treated as the end of the board, not
    as a failure, or a sweep retries the terminal page three times and logs it
    as an error every cycle.
    """
    query = {"queries": [{
        "query": {"categories": [FOOTBALL], "zones": {}, "hasOdds": True},
        "view": {"marketTypes": list(markets or SWEEP_MARKETS)},
        "skip": int(skip), "take": PAGE,
    }]}
    body = _get_json(LIST_URL, {"q": json.dumps(query)}, timeout)
    responses = (body or {}).get("responses") or [{}]
    return responses[0].get("responses") or []


def all_fixtures(pages=20, pause=0.45, timeout=40):
    """The sweep: their whole football board, a hundred fixtures a request.

    NOT A PER-DATE CRAWL. Betpawa has no day endpoint - the board is one
    ordered list reaching about two months out, so the sweep pages it rather
    than asking for dates it has to guess. Measured 21 Sep: 920 fixtures over
    34 distinct dates in ten pages and 23 seconds, which is the same order as
    BetKing's eight-day crawl and covers further.

    `pages` is a stop, not a target: the loop ends when a page comes back
    empty. Twenty pages is two thousand fixtures, twice the measured board.

    Returns (fixtures, stats) like the other books. `stats["pages"]` is how
    many were actually read, so a sweep cut short by a refusal is visible
    rather than looking like a thin Tuesday.
    """
    out, read, failed = {}, 0, []
    for n in range(pages):
        skip = n * PAGE
        try:
            events = fetch_page(skip, timeout=timeout)
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("betpawa page at %d failed: %s", skip, ex)
            failed.append(skip)
            break
        if not events:
            break
        read += 1
        for event in events:
            row = _row(event)
            _absorb(row, event)
            # A fixture we can price nothing on is a fixture we can do nothing
            # with, and carrying it makes the count lie about coverage.
            if row["odds"]:
                out[row["eventId"]] = row
        if n + 1 < pages:
            time.sleep(pause)
    log.info("betpawa swept %d fixtures over %d pages", len(out), read)
    return out, {"pages": read, "failed": failed, "listed": len(out)}


def fetch_event(event_id, timeout=30):
    """Every market Betpawa lists for one fixture - 63 rows on a mid-table tie.

    The sweep already carries the modelled markets, so this exists for the same
    reason BetKing's does: a price id is per event AND per price, and booking
    against a swept id that has since been re-issued is how a slip dies on a
    stale number. Booking reads the card again.
    """
    try:
        event = _get_json(EVENT_URL % event_id, timeout=timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betpawa event %s failed: %s", event_id, ex)
        return None
    if not event or not event.get("id"):
        return None
    row = _row(event)
    _absorb(row, event)
    if not row["odds"]:
        return None
    return row


# --- booking ----------------------------------------------------------------

def build_selection(event, code):
    """One leg, which on this book is a single number.

    Their betslip is a list of price ids and nothing else: no market id, no
    line, no price. That means a mistake here cannot be caught by reading the
    request back - the id either IS the bet or is a different bet - which is
    why _absorb keys on the outcome's line rather than the row's.
    """
    sel = (event.get("sel") or {}).get(code)
    if sel is None:
        raise KeyError("no %s on event %s" % (code, event.get("eventId")))
    return int(sel["priceId"])


def read_coupon(code, timeout=20):
    """The legs behind a Betpawa booking code, in OUR vocabulary.

    [{eventId, prediction, home, away, league, kickoff, odds}], where
    `prediction` is our market code or None for a market we do not carry. The
    same contract bet9ja.read_coupon and betking.read_coupon answer on, so
    /api/slip needs no fourth dialect.

    THE LINE COMES BACK IN TWO PLACES HERE TOO. The selection carries
    `handicap` ("2.5") and the market carries `specifier` ({"total": "2.5"}),
    and a 1X2 leg has neither. Decoded through _BY_TRIPLE, which is derived
    from the forward table, so the read and the write cannot drift apart.
    """
    try:
        body = _get_json("%s/%s" % (BOOKING_URL, code), timeout=timeout)
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("betpawa coupon read failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    if not isinstance(body, dict) or body.get("error"):
        # BOOKING_CODE_NOT_FOUND is their answer for a code that never
        # existed, and it is the honest answer to the reader either way.
        return {"error": "not found", "notFound": True}

    items = body.get("items") or []
    if not items:
        return {"error": "not found", "notFound": True}

    out = []
    for item in items:
        info = item.get("eventInfo") or {}
        parts = info.get("participants") or []
        names = [p.get("name") or "" for p in parts]
        odds = (item.get("odds") or {}).get("price")
        try:
            odds = float(odds)
        except (TypeError, ValueError):
            odds = None
        for sel in item.get("selections") or []:
            market = sel.get("market") or {}
            got = sel.get("selectionInfo") or {}
            line = got.get("handicap")
            if line in (None, ""):
                spec = market.get("specifier") or {}
                line = spec.get("total") or spec.get("hcp")
            triple = (str(market.get("typeId") or ""),
                      str(line) if line not in (None, "") else None,
                      str(got.get("typeId") or ""))
            out.append({
                "eventId": str(info.get("id") or ""),
                "prediction": _BY_TRIPLE.get(triple),
                # Their own words, kept whether or not we mapped it: an
                # unmapped leg still has to be nameable on screen.
                "raw": "%s/%s" % (market.get("name") or "", _outcome(got, line)),
                "home": names[0] if names else "",
                "away": names[1] if len(names) > 1 else "",
                "league": (info.get("competition") or {}).get("name") or "",
                "kickoff": info.get("startTime") or "",
                "odds": odds,
            })
    # THEIR OWN COUNT OF WHAT THE CODE HELD. A leg whose fixture has started
    # drops out of the reprint, so `originalCount` above the number of legs
    # read is a thinned code rather than a bad one - the reader is told,
    # rather than shown a quietly shorter slip.
    try:
        booked = int(body.get("originalCount") or 0)
    except (TypeError, ValueError):
        booked = 0
    return {"legs": out, "available": len(out), "removed": [], "booked": booked}


def read_code(code, timeout=20):
    """(originalCount, legs) - the pair generate_code checks its work against."""
    got = read_coupon(code, timeout)
    if got.get("error"):
        return 0, []
    return got.get("booked") or 0, got.get("legs") or []


def generate_code(selections, timeout=30, verify=True):
    """Turn a list of {event, code} into a Betpawa booking code.

    The payload shape is theirs, read out of their bundle rather than guessed:
    `{"selections": {"selections": [{"type": "SINGLE", "selections": [id]}]}}`.
    The doubled key is not a typo. `type` as a number answers
    SPORTSBOOK_WRONG_SELECTION - it is the enum NAME on the wire.

    `verify` reads the code back and compares the leg count. This book refuses
    a bad selection outright, so the read-back has never yet caught anything
    here - it stays because BetKing taught that a booking endpoint answering
    "success" for a slip it did not understand is not a rare failure but an
    unannounced one, and one GET is a cheap way never to ship that again.
    """
    if not selections:
        return {"error": "no selections"}
    if len(selections) > BETSLIP_MAX:
        return {"error": "betpawa slips are capped at %d selections here"
                         % BETSLIP_MAX, "sent": len(selections)}

    # TWO LEGS FROM ONE MATCH ARE REFUSED, AND REFUSED LOUDLY - a multiple
    # carrying both answers 400 SPORTSBOOK_WRONG_SELECTION with no indication
    # of which leg offended. Named here so the client drops one leg instead of
    # losing the whole slip to a message that blames nothing.
    seen, dupes = set(), []
    for sel in selections:
        eid = str((sel.get("event") or {}).get("eventId") or "")
        if eid and eid in seen:
            dupes.append(sel.get("code"))
        seen.add(eid)
    if dupes:
        return {"error": "betpawa will not put two selections from one game "
                         "on a multiple (%s)"
                         % ", ".join(str(d) for d in dupes),
                "sent": len(selections)}

    try:
        ids = [build_selection(s["event"], s["code"]) for s in selections]
    except (KeyError, TypeError, ValueError) as ex:
        return {"error": "could not build selection: %s" % ex}

    odds_total = 1.0
    for sel in selections:
        odds_total *= float(
            (sel["event"]["sel"][sel["code"]])["odds"])

    payload = {"selections": {"selections": [
        {"type": "SINGLE", "selections": [i]} for i in ids]}}
    try:
        r = requests.post(
            BOOKING_URL, data=json.dumps(payload),
            headers=_headers({"Content-Type": "application/json"}),
            timeout=timeout, impersonate=IMPERSONATE)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betpawa booking failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    code = (body or {}).get("code")
    if not code:
        return {"error": json.dumps(body)[:400], "sent": len(ids)}

    if verify:
        try:
            booked, legs = read_code(code, timeout)
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("betpawa read-back failed for %s: %s", code, ex)
            return {"code": code, "odds": round(odds_total, 2),
                    "legs": len(ids), "verified": False}
        if len(legs) != len(ids):
            return {"error": "betpawa accepted the slip and returned a code "
                             "holding %d of %d legs" % (len(legs), len(ids)),
                    "code": code, "sent": len(ids), "available": booked}
        return {"code": code, "odds": round(odds_total, 2), "legs": len(ids),
                "verified": True}
    return {"code": code, "odds": round(odds_total, 2), "legs": len(ids)}
