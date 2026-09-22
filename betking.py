"""BetKing: odds and booking codes.

The third book, and the easiest of the three. Everything here was read off
their own endpoints and their public desktop bundle on 14 Sep 2026; nothing is
inferred from how SportyBet or Bet9ja behave, because the one time that was
tried on this project it cost a day (see the note about describing a system you
have not opened in the prediction-site notes).

  odds      an open CDN feed, anonymous, no token and no cookie. One request
            returns a whole day of football with the default markets; a second
            per-event request returns the deep card (206 markets on a Premier
            League tie) for the handful of games actually going on a slip
  markets   self-describing: a market is (OddsTypeID, SpecialBetValue) and an
            outcome is OddAttribute.OddTypeID. No hand-built triple table of
            the kind SportyBet needs
  booking   a JSON POST carrying the whole client-side coupon object, anonymous
            like the other two. It answers {"ResponseStatus":1,
            "BookedCouponCode":"N71UHB"}

Three things about their booking endpoint that are not obvious and that the
code below is shaped around:

  1. THE SELECTION ID IS MatchOddsID, NOT Outcome.OutcomeID. Both sit on the
     same odd and both are plausible. Booking with the OutcomeID is accepted
     and returns a perfectly ordinary code, and reading that code back gives
     ResponseStatus 405 with an empty Odds array - a code that exists and
     contains nothing. Their own bundle settles it: `oddId: b.MatchOddsID`.
  2. THEY DO NOT VALIDATE THE PRICE WE SEND. Booking the same selection with
     OddValue 99.9 returned the same code as booking it honestly - the coupon
     is keyed on the selection, not the price. So a slip cannot be refused for
     drifted odds here, which is a smaller failure surface than Bet9ja. It also
     means the price we send is decoration, so we send the live one and treat
     our own copy as untrustworthy anyway.
  3. THEY ACCEPT A SELECTION ID THAT DOES NOT EXIST and hand back a valid
     looking code. Nothing about the response says anything is wrong. That is
     the same shape of harm as booking an unmapped market as a home win: the
     punter holds a code that is not the bet they asked for. Every code this
     module mints is therefore READ BACK before it is returned, and a coupon
     that does not resolve to the number of legs we sent is reported as a
     failure rather than handed on.

Their slip cap is 40 selections and 40 events (MaxNoOfSelections /
MaxNoOfEvents in their own global variables), not the 50 both other books
allow.
"""

import datetime
import json
import logging
import threading
import time

# curl_cffi, not requests, for the reason written up in bet9ja.py: plain
# requests works from a laptop and collects a block page from a datacentre.
# BetKing answered this machine happily without it, which proves nothing at all
# about how it answers Railway - that difference is exactly what took the other
# two integrations down. IMPERSONATE goes on every call.
from curl_cffi import requests

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"

FEED = "https://sportsapicdn-desktop.betking.com"
BOOK_URL = "https://www.betking.com/api/sports/v1/bet/Book"
READ_URL = "https://www.betking.com/api/sports/v1/bet/Booked"
GLOBALS_URL = FEED + "/api/settings/globalvariables"

SOCCER = 1
LANG = "en"

# Their feed and their booking origin both refuse a request with no Referer.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Their own limit, read from globalvariables rather than assumed. The other two
# books stop at 50; a slip of 45 legs books fine there and is refused here.
BETSLIP_MAX = 40


def _headers(extra=None):
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.betking.com/",
        "Origin": "https://www.betking.com",
    }
    if extra:
        h.update(extra)
    return h


# --- the markets we model ---------------------------------------------------
# code -> (OddsTypeID, SpecialBetValue, OddTypeID)
#
# SpecialBetValue is the line, and it is part of the market's identity: 160 on
# its own is "Total Goals" and says nothing about which rung. OddTypeID is the
# outcome within the market and is NOT ordered the way the names are anywhere
# except by luck - every triple below was read off a live card (Leeds v
# Newcastle, 14 Sep 2026) rather than continued from a pattern.
MARKET_MAP = {
    "1":  (110, 0.0, 4),
    "X":  (110, 0.0, 2),
    "2":  (110, 0.0, 5),
    # 9/10/11 in name order for once - 1X, 12, X2. SportyBet's double chance
    # runs 9/10/11 with 10 meaning home-or-away, so the two books agree here by
    # coincidence and not by convention. Do not carry this over to a fourth.
    "1X": (146, 0.0, 9),
    "12": (146, 0.0, 10),
    "X2": (146, 0.0, 11),
    "OVER_1.5":  (160, 1.5, 12),
    "OVER_2.5":  (160, 2.5, 12),
    "OVER_3.5":  (160, 3.5, 12),
    "GG": (302, 0.0, 74),
    "FH_OVER_0.5": (161, 0.5, 12),
    # Team totals are their own markets, one per side, sharing the outcome ids
    # of the match total. 10283 is home, 10284 is away.
    "HOME_OVER_0.5": (10283, 0.5, 12),
    "AWAY_OVER_0.5": (10284, 0.5, 12),
    "HOME_OVER_1.5": (10283, 1.5, 12),
    "AWAY_OVER_1.5": (10284, 1.5, 12),
    # The under side of every line. The builders never offer these; they are
    # here for the same reason they are in bet9ja.py, to de-vig - over and
    # under together give the bookmaker's implied probability with the margin
    # taken out, and a book with no unders blends nothing.
    "UNDER_1.5": (160, 1.5, 13),
    "UNDER_2.5": (160, 2.5, 13),
    "UNDER_3.5": (160, 3.5, 13),
    "NG": (302, 0.0, 76),
    "FH_UNDER_0.5": (161, 0.5, 13),
    "HOME_UNDER_0.5": (10283, 0.5, 13),
    "AWAY_UNDER_0.5": (10284, 0.5, 13),
    "HOME_UNDER_1.5": (10283, 1.5, 13),
    "AWAY_UNDER_1.5": (10284, 1.5, 13),
}

# --- markets we carry but never model --------------------------------------
# A CONVERTER NEEDS IDENTITY, NOT A PREDICTION. Everything above is a market the
# model has an opinion about. These are not: they exist so a slip somebody else
# built can be read, re-cut and moved between books without us pretending to
# rate it. Nothing here is ever produced by tipCode, so no board, no record and
# no calibration changes by their being here.
#
# EVERY TRIPLE BELOW WAS SEEN ON A LIVE CARD. They were proposed from the
# meaning of our own code and then kept only if BetKing actually priced them on
# one of eight deep fixtures, 19-20 Sep 2026 - generated, never typed, because
# the last hand-built tranche on another book invented 24 codes that matched
# nothing anywhere.
#
# THE LINE COMES FROM THE OUTCOME, NOT THE COLLECTION. Total Goals sets
# SpecialBetValue and the outcome's SpecialValue to the same number, which is
# why a collection-keyed map worked for every modelled market. Corners (190) and
# both handicaps (305, 342) leave SpecialBetValue at 0 and carry the line only
# on the outcome - so the second element of every triple here is
# OddAttribute.SpecialValue. See _absorb.
#
# THE HANDICAP SIGN IS THEIR HOME SIDE'S, AND IT WAS CHECKED RATHER THAN READ.
# Their SpecialValue is one number for a two-sided bet, so the obvious reading
# is a coin toss that books the wrong team. Verified against the prices instead:
# across eight cards the home outcome shortens as the line rises and the away
# outcome lengthens, 23 comparisons to 0 against. That is why our AWAY codes
# carry the NEGATED line - AH_2_-0.5 is the away team giving half a goal, which
# is their "0.5 : 0".
PASSTHROUGH_MAP = {
    # --- 1UP (3) ---
    "UP1_1": (10974, 0, 4),
    "UP1_2": (10974, 0, 5),
    "UP1_X": (10974, 0, 2),
    # --- 1st half handicap (4) ---
    "FH_AH_1_-0.5": (344, -0.5, 1714),
    "FH_AH_1_0.5": (344, 0.5, 1714),
    "FH_AH_2_-0.5": (344, 0.5, 1715),
    "FH_AH_2_0.5": (344, -0.5, 1715),
    # --- 1st half team goals (12) ---
    "FH_AWAY_OVER_0.5": (10291, 0.5, 12),
    "FH_AWAY_OVER_1.5": (10291, 1.5, 12),
    "FH_AWAY_OVER_2.5": (10291, 2.5, 12),
    "FH_AWAY_UNDER_0.5": (10291, 0.5, 13),
    "FH_AWAY_UNDER_1.5": (10291, 1.5, 13),
    "FH_AWAY_UNDER_2.5": (10291, 2.5, 13),
    "FH_HOME_OVER_0.5": (10290, 0.5, 12),
    "FH_HOME_OVER_1.5": (10290, 1.5, 12),
    "FH_HOME_OVER_2.5": (10290, 2.5, 12),
    "FH_HOME_UNDER_0.5": (10290, 0.5, 13),
    "FH_HOME_UNDER_1.5": (10290, 1.5, 13),
    "FH_HOME_UNDER_2.5": (10290, 2.5, 13),
    # --- 2UP (3) ---
    "UP2_1": (10975, 0, 4),
    "UP2_2": (10975, 0, 5),
    "UP2_X": (10975, 0, 2),
    # --- 2nd half double chance (3) ---
    "DC2_12": (10299, 0, 10),
    "DC2_1X": (10299, 0, 9),
    "DC2_X2": (10299, 0, 11),
    # --- 2nd half handicap (6) ---
    "SH_AH_1_-0.5": (9335, -0.5, 1714),
    "SH_AH_1_-1.5": (9335, -1.5, 1714),
    "SH_AH_1_0.5": (9335, 0.5, 1714),
    "SH_AH_2_-0.5": (9335, 0.5, 1715),
    "SH_AH_2_-1.5": (9335, 1.5, 1715),
    "SH_AH_2_0.5": (9335, -0.5, 1715),
    # --- 2nd half team goals (12) ---
    "SH_AWAY_OVER_0.5": (10303, 0.5, 12),
    "SH_AWAY_OVER_1.5": (10303, 1.5, 12),
    "SH_AWAY_OVER_2.5": (10303, 2.5, 12),
    "SH_AWAY_UNDER_0.5": (10303, 0.5, 13),
    "SH_AWAY_UNDER_1.5": (10303, 1.5, 13),
    "SH_AWAY_UNDER_2.5": (10303, 2.5, 13),
    "SH_HOME_OVER_0.5": (10302, 0.5, 12),
    "SH_HOME_OVER_1.5": (10302, 1.5, 12),
    "SH_HOME_OVER_2.5": (10302, 2.5, 12),
    "SH_HOME_UNDER_0.5": (10302, 0.5, 13),
    "SH_HOME_UNDER_1.5": (10302, 1.5, 13),
    "SH_HOME_UNDER_2.5": (10302, 2.5, 13),
    # --- 2nd half total (6) ---
    "SH_OVER_0.5": (9280, 0.5, 12),
    "SH_OVER_1.5": (9280, 1.5, 12),
    "SH_OVER_2.5": (9280, 2.5, 12),
    "SH_UNDER_0.5": (9280, 0.5, 13),
    "SH_UNDER_1.5": (9280, 1.5, 13),
    "SH_UNDER_2.5": (9280, 2.5, 13),
    # --- asian handicap (16) ---
    "AH_1_-0.5": (305, -0.5, 1714),
    "AH_1_-1.5": (305, -1.5, 1714),
    "AH_1_-2.5": (305, -2.5, 1714),
    "AH_1_-3.5": (305, -3.5, 1714),
    "AH_1_0.5": (305, 0.5, 1714),
    "AH_1_1.5": (305, 1.5, 1714),
    "AH_1_2.5": (305, 2.5, 1714),
    "AH_1_3.5": (305, 3.5, 1714),
    "AH_2_-0.5": (305, 0.5, 1715),
    "AH_2_-1.5": (305, 1.5, 1715),
    "AH_2_-2.5": (305, 2.5, 1715),
    "AH_2_-3.5": (305, 3.5, 1715),
    "AH_2_0.5": (305, -0.5, 1715),
    "AH_2_1.5": (305, -1.5, 1715),
    "AH_2_2.5": (305, -2.5, 1715),
    "AH_2_3.5": (305, -3.5, 1715),
    # --- chance mix 1x2 or GG (3) ---
    "MIXGG_1": (9334, 0, 2348),
    "MIXGG_2": (9334, 0, 2344),
    "MIXGG_X": (9334, 0, 2346),
    # --- chance mix 1x2 or NG (3) ---
    "MIXNG_1": (9334, 0, 2349),
    "MIXNG_2": (9334, 0, 2345),
    "MIXNG_X": (9334, 0, 2347),
    # --- chance mix 1x2 or total (18) ---
    "MIX_1_OV_1.5": (9648, 1.5, 2354),
    "MIX_1_OV_2.5": (9648, 2.5, 2354),
    "MIX_1_OV_3.5": (9648, 3.5, 2354),
    "MIX_1_UN_1.5": (9648, 1.5, 2355),
    "MIX_1_UN_2.5": (9648, 2.5, 2355),
    "MIX_1_UN_3.5": (9648, 3.5, 2355),
    "MIX_2_OV_1.5": (9648, 1.5, 2350),
    "MIX_2_OV_2.5": (9648, 2.5, 2350),
    "MIX_2_OV_3.5": (9648, 3.5, 2350),
    "MIX_2_UN_1.5": (9648, 1.5, 2351),
    "MIX_2_UN_2.5": (9648, 2.5, 2351),
    "MIX_2_UN_3.5": (9648, 3.5, 2351),
    "MIX_X_OV_1.5": (9648, 1.5, 2352),
    "MIX_X_OV_2.5": (9648, 2.5, 2352),
    "MIX_X_OV_3.5": (9648, 3.5, 2352),
    "MIX_X_UN_1.5": (9648, 1.5, 2353),
    "MIX_X_UN_2.5": (9648, 2.5, 2353),
    "MIX_X_UN_3.5": (9648, 3.5, 2353),
    # --- double chance 1UP (2) ---
    "DC1UP_1X": (10987, 0, 9),
    "DC1UP_X2": (10987, 0, 11),
    # --- draw no bet (2) ---
    "DNB_1": (147, 0, 4),
    "DNB_2": (147, 0, 5),
    # --- european handicap (18) ---
    "EH_0_1_1": (342, -1, 1714),
    "EH_0_1_2": (342, -1, 1715),
    "EH_0_1_X": (342, -1, 1712),
    "EH_0_2_1": (342, -2, 1714),
    "EH_0_2_2": (342, -2, 1715),
    "EH_0_2_X": (342, -2, 1712),
    "EH_0_3_1": (342, -3, 1714),
    "EH_0_3_2": (342, -3, 1715),
    "EH_0_3_X": (342, -3, 1712),
    "EH_1_0_1": (342, 1, 1714),
    "EH_1_0_2": (342, 1, 1715),
    "EH_1_0_X": (342, 1, 1712),
    "EH_2_0_1": (342, 2, 1714),
    "EH_2_0_2": (342, 2, 1715),
    "EH_2_0_X": (342, 2, 1712),
    "EH_3_0_1": (342, 3, 1714),
    "EH_3_0_2": (342, 3, 1715),
    "EH_3_0_X": (342, 3, 1712),
    # --- excluded goals, the nought cases (2) ---
    # NOT A SUBSTITUTION. Total goals is a non-negative integer, so "the total
    # is anything BUT zero" and "over 0.5" are one proposition written two
    # ways. EXGOALS_1 upwards have no such equivalent - "not exactly one" is
    # 0, 2, 3... which no single line expresses - and BetKing sells the
    # OPPOSITE market, 9641, so mapping onto that sells the losing side.
    # These were hand-added once and destroyed by the next regeneration; they
    # are a rule in tools/gentail.js now.
    "EXGOALS_0": (160, 0.5, 12),
    "EXGOALS_FH_0": (161, 0.5, 12),
    # --- exactly N goals (6) ---
    "EXACT_1": (9641, 1, 74),
    "EXACT_2": (9641, 2, 74),
    "EXACT_3": (9641, 3, 74),
    "EXACT_4": (9641, 4, 74),
    "EXACT_5": (9641, 5, 74),
    "EXACT_6": (9641, 6, 74),
    # --- half most corners (3) ---
    "HALFCORNER_1": (9793, 0, 436),
    "HALFCORNER_2": (9793, 0, 438),
    "HALFCORNER_E": (9793, 0, 924),
    # --- penalty awarded (2) ---
    "PEN_N": (699, 0, 76),
    "PEN_Y": (699, 0, 74),
    # --- team corners (14) ---
    "CORNERS_A_OV_3.5": (10333, 3.5, 12),
    "CORNERS_A_OV_4.5": (10333, 4.5, 12),
    "CORNERS_A_OV_5.5": (10333, 5.5, 12),
    "CORNERS_A_OV_6.5": (10333, 6.5, 12),
    "CORNERS_A_UN_3.5": (10333, 3.5, 13),
    "CORNERS_A_UN_4.5": (10333, 4.5, 13),
    "CORNERS_A_UN_5.5": (10333, 5.5, 13),
    "CORNERS_A_UN_6.5": (10333, 6.5, 13),
    "CORNERS_H_OV_3.5": (10332, 3.5, 12),
    "CORNERS_H_OV_4.5": (10332, 4.5, 12),
    "CORNERS_H_OV_5.5": (10332, 5.5, 12),
    "CORNERS_H_UN_3.5": (10332, 3.5, 13),
    "CORNERS_H_UN_4.5": (10332, 4.5, 13),
    "CORNERS_H_UN_5.5": (10332, 5.5, 13),
    # --- total corners (14) ---
    "CORNERS_OV_10.5": (190, 10.5, 12),
    "CORNERS_OV_11.5": (190, 11.5, 12),
    "CORNERS_OV_12.5": (190, 12.5, 12),
    "CORNERS_OV_6.5": (190, 6.5, 12),
    "CORNERS_OV_7.5": (190, 7.5, 12),
    "CORNERS_OV_8.5": (190, 8.5, 12),
    "CORNERS_OV_9.5": (190, 9.5, 12),
    "CORNERS_UN_10.5": (190, 10.5, 13),
    "CORNERS_UN_11.5": (190, 11.5, 13),
    "CORNERS_UN_12.5": (190, 12.5, 13),
    "CORNERS_UN_6.5": (190, 6.5, 13),
    "CORNERS_UN_7.5": (190, 7.5, 13),
    "CORNERS_UN_8.5": (190, 8.5, 13),
    "CORNERS_UN_9.5": (190, 9.5, 13),
    # --- win either half (4) ---
    "WINHALF_A_N": (627, 0, 76),
    "WINHALF_A_Y": (627, 0, 74),
    "WINHALF_H_N": (628, 0, 76),
    "WINHALF_H_Y": (628, 0, 74),
}

# --- what BetKing does NOT sell, and why it is absent -----------------------
# An entry here is a market our other two books carry that BetKing does not, so
# a pasted code holding one can be read and split but never converted INTO
# BetKing. Written down with a reason apiece, because "verified absent across
# eight cards" and "nobody has looked" must not look the same in a year.
#
#   asian handicap, 48 of 64   Their 305 sells HALF-BALL lines only: -3.5 -2.5
#                              -1.5 -0.5 0.5 1.5 2.5 3.5 on every card checked.
#                              No quarter ball (-0.25, -0.75) and no whole ball.
#                              The whole-ball lines exist at 342, and that is a
#                              DIFFERENT BET - three-way, the draw its own
#                              outcome, no push - so mapping AH_1_-1 onto it
#                              would hand somebody a narrower bet than they
#                              placed. Left unmapped on purpose.
#   european handicap, 6       342 stops at three goals: -3 -2 -1 1 2 3. Our
#                              EH_0_4 and EH_0_5 have nowhere to go.
#   1st half handicap, 8       344 offers only -0.5 and 0.5.
#   2nd half handicap, 6       9335 offers -1.5 -0.5 0.5 1.5, so the whole-ball
#                              and zero lines are absent.
#   total corners, 4           190 runs 5.5 to 12.5; 13.5 and 14.5 are not sold.
#   team corners, 6            10332 runs 3.5-5.5 and 10333 2.5-6.5, and they
#                              are thin - the deepest line appeared on one card
#                              of eight.
#
# The 200 codes with no rule at all are a different thing again: correct score,
# exact goals, cards, margins, first goalscorer, the minute markets. BetKing
# prices most of those families - they are on the card - but each needs its own
# outcome-by-outcome reading, and a family guessed from its name is how the
# exact-goals top rung means "N or more" on one book and "exactly N" on the
# other. They stay unmapped until somebody reads them.

# --- the asymmetry, as data rather than a comment --------------------------
# A code our other books carry and this one does not is a leg that reads and
# splits and can NEVER convert into BetKing. The comments above say why family
# by family; this says it in a form a test can hold, so a code quietly added to
# one table cannot look the same as one deliberately left off another.
#
# Keyed by prefix, longest match wins. Every uncarried code must match exactly
# one entry - test_the_asymmetry_is_accounted_for walks our whole vocabulary
# and fails on anything that is neither carried nor explained here.
#
# "verified absent" means checked across eight deep cards on 19-20 Sep 2026.
# "not read yet" means nobody has sat down with their card - it is work, not a
# property of their catalogue, and the two must never look the same.
NOT_CARRIED = {
    "AH_1_": "verified absent: 305 sells half-ball lines only (-2.5 -1.5 -0.5 "
             "0.5 1.5 2.5 3.5, checked on twelve cards). Whole balls exist at "
             "342 and that is a DIFFERENT bet - three-way, draw its own "
             "outcome, no push - so mapping onto it narrows the punter's bet. "
             "Quarter balls are not sold at all. SEVEN LEGS of a real 39-leg "
             "SportyBet ticket were whole-ball Asian handicaps, so this is the "
             "single biggest thing BetKing cannot take.",
    "AH_2_": "verified absent: see AH_1_.",
    "FH_AH_": "verified absent: 344 offers -0.5 and 0.5 only.",
    "SH_AH_": "verified absent: 9335 offers -1.5 -0.5 0.5 1.5 only.",
    "EH_": "verified absent: 342 stops at three goals (-3 -2 -1 1 2 3).",
    "CORNERS_": "verified absent: 190 runs 5.5-12.5, 10332 3.5-5.5, 10333 "
                "2.5-6.5. The deepest team lines appeared on one card of "
                "eight, so they are thin as well as bounded.",
    "FH_HOME_": "verified absent: 10290 offers 0.5 and 1.5, not 2.5.",
    "FH_AWAY_": "verified absent: 10291 offers 0.5 and 1.5, not 2.5.",
    "SH_AWAY_": "verified absent: 10303 offers 0.5 and 1.5, not 2.5.",
    # THREE ENTRIES BELOW WERE WRONG UNTIL A REAL TICKET WAS READ, and all
    # three pointed at a market that is a DIFFERENT BET. That is the failure
    # this table exists to prevent, and it got into the table itself.
    "OVER_": "verified absent: whole-number totals. OVER_3 is 'over 3 goals' "
             "with a push on exactly three; BetKing's 160 sells only .5 lines "
             "(0.5-6.5) so there is no push line to cross to. 9641 'Total "
             "Goals(Exact) N' is a different bet again - exactly N, not over "
             "N. NINE LEGS of a real 39-leg ticket were these.",
    "UNDER_": "verified absent: see OVER_.",
    "EXGOALS_": "verified absent from 1 upwards, and the near-miss is an "
                "INVERSE. Our EXGOALS_1 is 'NOT exactly 1 goal'; their 9641 is "
                "'Total Goals(Exact) 1'. Mapping them together would sell "
                "somebody the exact opposite of their bet. The _0 cases DO "
                "cross and are carried above: 'not exactly zero' and 'over "
                "0.5' are the same proposition on a non-negative integer, so "
                "that is an equivalence rather than a substitution.",
    # MIX_ WAS HERE, AND IT WAS WRONG. I searched their MARKET names for "or"
    # and "Chance Mix Total Goals 1.5" does not contain it - the OR lives in
    # the OUTCOME labels ("2 or Over"). A punter's own code found it: 3T2NBQ,
    # Brighton v Arsenal, market 9648. Search outcomes, not names, and do not
    # write "verified absent" off a search that only looked at half the data.
    # EXACT_ IS CARRIED NOW, and the note that used to sit here called it
    # "correct score". It is not: our own mLabel says "Exactly N goals", and
    # their 9641 Total Goals(Exact) N is the same bet with the number as the
    # LINE. Third reason in this table written from a guess about what a family
    # name meant rather than from reading it. Only EXACT_0 is absent - their
    # 9641 starts at one, and nobody sells "exactly nought" under that name.
    "EXACT_0": "verified absent: their 9641 runs 1 to 7. Exactly nought goals "
               "is 0-0 and would live in a correct-score market, unread.",
    # The half versions are NOT the same market and must not inherit 9641's
    # mapping by looking similar. 9639 is named "1st Half - Total Goals 1" with
    # a single Yes outcome, which could be "exactly one" or "at least one" -
    # exactly the reading that has to be done rather than guessed.
    "EXACT_FH_": "not read yet: 9639 is on the card but its single Yes outcome "
                 "does not say whether it means exactly one or at least one.",
    "EXACT_SH_": "not read yet: 9640, same question as EXACT_FH_.",
    "BOUNDS_": "not read yet: one side's goals as a range, and the target is "
               "9619 Total Multigoal Home / 9620 Total Multigoal Away, which "
               "ARE per-team. I first wrote that their Multi Goal was "
               "total-only; that is 9616, and I had stopped looking. Read "
               "every band's outcome label before mapping - '33' means three "
               "or more in our vocabulary, and a band that means exactly "
               "three would be the same trap as EXACT_.",
    "TEAMGOALS_": "not read yet: team exact goals, 10286 and its home twin.",
    "CARD_": "not read yet: bookings markets.",
    "CORNRANGE_": "not read yet: corner bands.",
    "FIRSTGOAL_": "not read yet: 10331 First Corner / first scorer family.",
    "HIGHHALF_": "not read yet: highest scoring half. Crosses on the other two.",
    "MARGIN_": "not read yet: winning margin.",
    "EARLY_": "not read yet: 10978-10986 early-goal markets are on the card.",
    "BOTHHALVES_": "not read yet.",
    "GOALRANGE_": "not read yet: goal bands.",
    "HMC_": "not read yet: half most cards.",
    "DNB_": "carried - this entry exists so a prefix sweep cannot claim it.",
    # Two of the three cross and the third does not, which a family-shaped
    # reason would have hidden. reason_uncarried asks market_for first, so this
    # entry only ever speaks for the leg that is genuinely missing.
    "DC1UP_": "verified absent: their 10987 Double chance 1UP offers 1X and X2 "
              "and no 12 at all, on every card checked. The other two cross.",
    "FH_EH_": "not read yet: first-half three-way handicap. 344 is the Asian "
              "one; whether they sell a three-way half handicap is unchecked.",
    "SH_EH_": "not read yet: second-half three-way handicap, same question.",
    "FH_MIX_": "not read yet: first-half 1X2 & total. 10297 is on the card - "
               "but check the AND/OR distinction above before trusting it.",
    "FH_CARD_": "not read yet: first-half team bookings.",
    "FH_CARDUN_": "not read yet: the under side of first-half team bookings.",
}


def reason_uncarried(code):
    """Why BetKing does not carry one of our codes, or None if it does.

    Longest prefix wins, so FH_AH_ beats FH_ and AH_1_ beats AH_.
    """
    if market_for(code):
        return None
    best = None
    for prefix, why in NOT_CARRIED.items():
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, why)
    return best[1] if best else None


# Reverse lookup, DERIVED rather than typed twice - a second literal is a
# second thing to keep in step, and the pair would drift silently. Both tables,
# because a pasted code is resolved through exactly the same index.
# TWO NAMES FOR ONE BET, AND WHICH ONE COMES BACK. EXGOALS_0 ("the total is
# anything but zero") and OVER_0.5 are the same proposition on a non-negative
# integer, so they resolve to the same triple - the forward direction is happy
# either way, and the reverse has to choose. The MODELLED name wins: a reader
# should see "Goal in 1st half", not "Not exactly 0 goals in the first half",
# and both are true of the same leg. So the modelled table is written last and
# overwrites.
_BY_TRIPLE = {triple: code
              for code, triple in list(PASSTHROUGH_MAP.items()) +
              list(MARKET_MAP.items())}


def market_for(code):
    """The triple for one of our prediction codes, or None.

    None means REFUSE. There is deliberately no fallback market here: booking
    an unmapped code as something else returns a valid code for a bet the
    punter did not ask for, which is the one failure in this whole system that
    nothing downstream can see.

    BOTH TABLES, and every caller asks this rather than a table directly. The
    pass-through map is a mapping too: keyed on MARKET_MAP alone, every
    handicap and corner leg came back "not_mapped" however well BetKing prices
    it, so no converted slip carrying one could ever be booked here. That is
    the mistake bet9ja.py made on the other book and it is worth not repeating.
    """
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


def _sbv(value):
    """Their SpecialBetValue, as a float, however the feed spelled it."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# --- reading the feed -------------------------------------------------------

def _get_json(url, timeout=20, attempts=3):
    """One request, sequentially, with a long backoff.

    Sequential and patient for the reason the other two modules are: parallel
    requests from a datacentre IP get refused wholesale, and a refusal here is
    a throttle rather than a blip, so a fast retry just spends the next one.
    """
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout,
                             impersonate=IMPERSONATE)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s" % r.status_code
        except Exception as ex:                  # noqa: BLE001 - upstream
            last = str(ex)
        if i + 1 < attempts:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("betking GET failed: %s (%s)" % (url, last))


_GLOBALS = {"at": 0.0, "data": None}
_GLOBALS_LOCK = threading.Lock()
_GLOBALS_TTL = 3600


def global_variables(timeout=20):
    """Their coupon limits, which the booking endpoint requires in the payload.

    Without BetCouponGlobalVariable the POST is a 400 naming the field, so this
    is not optional decoration. Cached for an hour: it is a settings blob that
    changes about never, and fetching it per booking doubles the requests.
    """
    with _GLOBALS_LOCK:
        if _GLOBALS["data"] and time.time() - _GLOBALS["at"] < _GLOBALS_TTL:
            return _GLOBALS["data"]
    data = _get_json(GLOBALS_URL, timeout)
    with _GLOBALS_LOCK:
        _GLOBALS["data"] = data
        _GLOBALS["at"] = time.time()
    return data


def _items(payload):
    """Every match in one of their feed payloads.

    They wrap matches as AreaMatches[].Items[], and the per-event endpoint
    returns the SAME match many times over - one copy per market group, 183 of
    them for a deep fixture - so callers have to merge by ItemID rather than
    trust the first copy. Doing that here keeps the mistake in one place.
    """
    for area in (payload or {}).get("AreaMatches") or []:
        for item in area.get("Items") or []:
            if item.get("ItemID"):
                yield item


def _utc_date(stamp):
    """The calendar date of their stamp IN UTC, or "" if it is unreadable."""
    try:
        return datetime.datetime.fromisoformat(
            str(stamp)).astimezone(datetime.timezone.utc).date().isoformat()
    except Exception:                            # noqa: BLE001 - upstream text
        return str(stamp or "")[:10]


def _row(item):
    """The empty shell of one fixture, in the shape the other two books use."""
    return {
        "eventId": str(item.get("ItemID")),
        "slotId": str(item.get("ItemID")),
        # Their betslip carries the provider's id alongside their own; both go
        # on the wire, so both are kept.
        "eventCode": str(item.get("ExtProvIDItem") or ""),
        "teams": item.get("ItemName") or "",
        # An ISO string with an offset, which Date.parse handles as-is. Do not
        # "normalise" it - the site pairs on this and a rewritten stamp is a
        # new way to be wrong.
        "kickoff": item.get("ItemDate") or "",
        "league": item.get("TournamentName") or "",
        "country": item.get("CategoryName") or "",
        # UTC, LIKE THE OTHER THREE BOOKS - not the first ten characters of
        # their stamp. Their feed speaks UTC+2: a match at 23:30 UTC arrives as
        # "2026-09-27T01:30:00+02:00" and the slice made its date the 27th
        # while SportyBet, Bet9ja and Betpawa all called it the 26th. Nothing
        # on the site reads this field, which is exactly why it went unnoticed
        # - it is read by people, and it sent one straight to the wrong day.
        "startdate": _utc_date(item.get("ItemDate")),
        "odds": {},
        "raw": {},
        # What build_selection needs to make a leg, per code. Kept beside the
        # price rather than refetched, because the ids that identify a
        # selection (MatchOddsID, OddCollectionID) live on the odd itself.
        "sel": {},
    }


def _absorb(row, item):
    """Fold one copy of a match into its row, market by market."""
    for coll in item.get("OddsCollection") or []:
        otype = coll.get("OddsType") or {}
        mid = otype.get("OddsTypeID")
        coll_sbv = _sbv(coll.get("SpecialBetValue"))
        for mo in coll.get("MatchOdds") or []:
            attr = mo.get("OddAttribute") or {}
            # THE LINE LIVES ON THE OUTCOME, NOT ALWAYS ON THE COLLECTION.
            # Total Goals (160) sets both and they agree, which is why keying
            # on the collection worked for every modelled market. Total Corners
            # (190) and both handicaps (305, 342) set SpecialBetValue to 0 and
            # carry the line ONLY here - so a collection-keyed map folds every
            # corner line and every handicap line onto one entry and books
            # whichever it happened to see last. Read 19 Sep off five deep
            # cards. The collection is the fallback, not the source.
            sbv = _sbv(attr.get("SpecialValue")) if attr.get(
                "SpecialValue") is not None else coll_sbv
            code = _BY_TRIPLE.get((mid, sbv, attr.get("OddTypeID")))
            if not code:
                continue
            price = (mo.get("Outcome") or {}).get("OddOutcome")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            # A suspended market is left on the board with its price collapsed.
            # It is not bookable and would pay nothing, so it is not a price.
            if price <= 1.01:
                continue
            row["odds"][code] = price
            row["raw"][code] = str(price)
            row["sel"][code] = {
                # THE ID THAT MATTERS. See the module docstring.
                "SelectionId": mo.get("MatchOddsID"),
                "IDSelectionType": attr.get("OddTypeID"),
                "MarketId": coll.get("OddCollectionID"),
                "MarketTypeId": mid,
                "MarketName": otype.get("OddsTypeName") or "",
                "SelectionName": attr.get("OddName") or "",
                "OddValue": price,
                "IDGroup": coll.get("GroupNo"),
                "GamePlay": coll.get("Combinability"),
                "CompatibleMarkets": coll.get("CompatibleMarkets") or [],
            }


def _event_fields(item):
    """The fixture-level half of a betslip leg."""
    return {
        "IDSport": SOCCER,
        "SportName": "Football",
        "EventId": item.get("CategoryId"),
        "EventName": item.get("CategoryName") or "",
        "TournamentId": item.get("TournamentId"),
        "TournamentName": item.get("TournamentName") or "",
        "MatchId": item.get("ItemID"),
        "ProviderEventId": str(item.get("ExtProvIDItem") or ""),
        "MatchName": item.get("ItemName") or "",
        "EventDate": item.get("ItemDate") or "",
        "SmartCode": item.get("SmartBetCode"),
        "EventCategory": item.get("EventCategory") or "",
        "IncompatibleEvents": item.get("IncompatibleEvents") or [],
    }


def fetch_day(date, timeout=30):
    """Every football fixture kicking off on one date. ONE request.

    Their day endpoint carries the default markets only - 1X2, double chance,
    the match totals and both-to-score - which is enough for the board and not
    enough to book team goals. That is the same division of labour Bet9ja
    needs: list cheaply here, pull the deep card per leg at booking time.

    `date` is YYYY-MM-DD. Returns (rows, listed), where `listed` is THEIR OWN
    count of fixtures for that date - the outside opinion that tells a thin
    Tuesday apart from a throttled sweep. An empty dict on failure, because a
    bookmaker being unreachable must not take a page down.
    """
    url = "%s/api/feeds/prematch/GetEvents/%s/%s/0/0/%d" % (
        FEED, LANG, date, SOCCER)
    try:
        payload = _get_json(url, timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betking day %s failed: %s", date, ex)
        return {}, 0

    out = {}
    for item in _items(payload):
        eid = str(item.get("ItemID"))
        row = out.get(eid)
        if row is None:
            row = out[eid] = _row(item)
        _absorb(row, item)
    # A fixture we can price nothing on is not a fixture we can do anything
    # with, and carrying it makes the count lie about coverage.
    rows = {eid: row for eid, row in out.items() if row["odds"]}
    try:
        listed = int(payload.get("TotalNoOfItems") or 0)
    except (TypeError, ValueError):
        listed = 0
    return rows, listed


def all_fixtures(days=30, pause=0.45, today=None):
    """The sweep: the next eight days of football, one request per day.

    EIGHT, NOT THREE, AND THE DIFFERENCE IS THE WEEKEND. The board reaches
    about nine days out and its two biggest days by far are Saturday and
    Sunday - 146 and 102 fixtures on one measured board, against 8 to 37 on a
    weekday. A three-day sweep holds none of them until the Thursday, so the
    book would read "doesn't have any of these games" for the whole weekend
    slate, which is when slips actually get built.

    THIRTY, NOT EIGHT, AND THE DIFFERENCE IS EVERY PASTED CODE. A reader's
    betPawa code of 24 legs reached 12 October and BetKing could take two of
    them - not because BetKing lacks the games but because our sweep had never
    asked for them. Their own day feed answers 81 fixtures for 11 October and 7
    for the 16th, three and four weeks out. The board reaches nine days; a
    code somebody pastes reaches wherever its author built it, and that is the
    window the converter is judged on.

    Measured 14 Sep over ten days: 937 fixtures, 20.7MB, 11.6s, and their own
    count agreed exactly on every day.

    THIRTY IS WHERE THEIR CARD ACTUALLY ENDS, measured rather than chosen: 33
    fixtures on each of 17 and 18 October, 11 on the 13th, and nothing at all
    on the 21st or 25th - one or two a day after that. Twenty-one days was the
    first guess at this and it cut the 17th and 18th off, which is a BetKing
    reader's own code (H11FW9) pointing at games we were saying they did not
    have. That puts BetKing's coverage
    in the same range as the other two (Bet9ja 1222, SportyBet 1251) rather
    than at a quarter of it.

    Still eight requests against Bet9ja's per-league crawl of a hundred and
    seventy. The pause stays anyway - the block that hit the other two books
    was about request RATE from a datacentre, and being cheap is not the same
    as being polite.

    Returns (fixtures, stats). `stats["listed"]` is what BetKing says it has
    over the same window, so the caller can refuse a sweep that came back short
    instead of storing it and quietly shrinking the board.
    """
    import datetime
    base = today or datetime.date.today()
    out, listed, failed = {}, 0, []
    # ONE DAY PAST THE WINDOW, BECAUSE THEIR PAGES ARE UTC+2. A match at 23:30
    # UTC sits on their NEXT day's page, so a sweep that stops on the last day
    # of the window loses that day's late kickoffs entirely - and those are the
    # American ones, which is how a reader's Philadelphia leg came back as a
    # game BetKing supposedly did not have.
    for n in range(days + 1):
        day = (base + datetime.timedelta(days=n)).isoformat()
        rows, day_listed = fetch_day(day)
        if not rows:
            failed.append(day)
        out.update(rows)
        listed += day_listed
        log.info("betking %s: %d fixtures of %d listed", day, len(rows),
                 day_listed)
        if n + 1 < days:
            time.sleep(pause)
    return out, {"listed": listed, "days": days, "failed": failed}


def fetch_event(event_id, timeout=30):
    """Every market BetKing lists for one fixture - 206 on a big tie.

    This is what makes team goals and the first-half line bookable at all: the
    day feed does not carry them for any competition. The response repeats the
    match once per market group, which _items and _absorb fold back together.
    """
    url = "%s/api/feeds/prematch/event/%s/1/%s/0" % (FEED, LANG, event_id)
    try:
        payload = _get_json(url, timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betking event %s failed: %s", event_id, ex)
        return None

    row, fields = None, None
    for item in _items(payload):
        if str(item.get("ItemID")) != str(event_id):
            continue
        if row is None:
            row = _row(item)
            fields = _event_fields(item)
        _absorb(row, item)
    if row is None or not row["odds"]:
        return None
    row["event"] = fields
    return row


# --- booking ----------------------------------------------------------------

def build_selection(event, code):
    """One leg of a betslip, in the shape their coupon object wants.

    Every field is copied from what their own bundle sends. The price is the
    live one read back from the feed, not the caller's: they ignore it, but
    sending a stale number anyway would leave a wrong figure sitting in a
    coupon somebody may look at.
    """
    sel = (event.get("sel") or {}).get(code)
    if sel is None:
        raise KeyError("no %s on event %s" % (code, event.get("eventId")))
    fields = event.get("event")
    if not fields:
        raise KeyError("event %s was not fetched deeply" % event.get("eventId"))
    leg = dict(fields)
    leg.update(sel)
    # Not a banker, and compatible with the rest of the slip unless their own
    # feed said otherwise. Their client sends both on every leg.
    leg["Fixed"] = False
    leg["CompatibilityLevel"] = 1
    return leg


def _coupon(legs, gvars):
    """The client-side coupon object their Book endpoint takes.

    A booking carries no money - it is a shareable slip, not a bet - but their
    validator still wants a stake, so it gets their own minimum. CouponTypeId
    1 is a single, 2 a multiple, which is what a slip of more than one leg is.
    """
    total = 1.0
    for leg in legs:
        total *= float(leg["OddValue"])
    stake = float((gvars or {}).get("MinBetStake") or 100)
    return {
        "Odds": legs,
        "Groupings": [{"Grouping": len(legs), "Combinations": 1,
                       "Stake": stake}],
        "StakeGross": stake,
        "Stake": stake,
        "CouponTypeId": 1 if len(legs) == 1 else 2,
        "Language": LANG,
        "CurrencyId": 1,
        "TotalOdds": round(total, 10),
        "TotalCombinations": 1,
        "IsClientSideCoupon": True,
        "BetCouponGlobalVariable": gvars,
    }


def _transaction_id():
    """Their client's own shape: the tail of a millisecond clock plus noise.

    It is an idempotency key, not a secret - booking the same selections twice
    returns the same code either way.
    """
    import random
    return "%s%d" % (str(int(time.time() * 1000))[3:], random.randint(1, 99))


def _read_body(code, timeout=20):
    """Their whole answer for one booking code.

    One parser, two callers: generate_code wants the leg count to verify what
    it just minted, and read_coupon wants the rest of it. Parsing the response
    in both places is how the two would come to disagree about the same code.
    """
    return _get_json("%s/%s/%s" % (READ_URL, code, LANG), timeout)


def read_code(code, timeout=20):
    """Read a booking code back. Anonymous, like everything else here.

    Returns (available, legs) where `legs` is their own coupon rows. A code
    that exists but resolves to nothing comes back (0, []), which is exactly
    what a code booked with a selection id they do not recognise looks like.
    """
    body = _read_body(code, timeout)
    coupon = (body or {}).get("BookedCoupon") or {}
    return int(body.get("AvailableEventCount") or 0), (coupon.get("Odds") or [])


def read_coupon(code, timeout=20):
    """Return the legs behind a BetKing booking code, in OUR vocabulary.

    [{eventId, prediction, home, away, league, kickoff, odds}], where
    `prediction` is our market code, or None for a market we carry no mapping
    for. The same contract bet9ja.read_coupon answers on, so /api/slip and
    everything above it needs no third dialect.

    Their coupon rows carry the market as the same triple the feed does -
    MarketTypeId, Spread, IDSelectionType - so the decode is _BY_TRIPLE, which
    is derived from the forward table rather than a second map that could
    drift from it. Verified on a real booking, FR2D84: (160, 2.5, 12) came
    back as OVER_2.5 with the line intact in Spread.

    `AvailableEventCount` is NOT the leg count and must not be used as one: a
    coupon of four legs reads back with available=0 while its Odds array holds
    all four. It appears to be about what can still be loaded into a betslip,
    which is a different question from what the code contains.
    """
    try:
        body = _read_body(code, timeout)
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("betking coupon read failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    coupon = (body or {}).get("BookedCoupon") or {}
    rows = coupon.get("Odds") or []
    available = int(body.get("AvailableEventCount") or 0)
    # A REPRINT IS NOT A TRANSCRIPT, AND THIS BOOK SAYS SO OUT LOUD.
    # A leg disappears from the coupon the moment its fixture starts, so a code
    # read in the afternoon is shorter than the one somebody was handed in the
    # morning - measured on DT166R, booked with four legs and reading back with
    # one four hours later. Bet9ja does the same and tells us nothing; BetKing
    # names the casualties, so the reader can be told rather than shown a
    # quietly shorter slip and left to wonder.
    #
    # Two things it is NOT. The names are names only - no market, no selection -
    # so a dropped leg cannot be re-booked or even described beyond the fixture.
    # And the count decays too: FR2D84 was minted with four legs and now reports
    # three as its "original", so this is the best account available rather than
    # the truth. Both are why it is reported as what we READ, never as the slip.
    removed = [n for n in (body.get("RemovedEvents") or []) if n]
    booked = int(body.get("OriginalEventCount") or 0)

    if not rows:
        # A code they do not know answers 200 with BookedCoupon: null. So does
        # a code we minted against a selection id they did not recognise -
        # there is no way to tell those apart from here, and "not found" is the
        # honest answer to the reader either way.
        return {"error": "not found", "notFound": True}

    out = []
    for leg in rows:
        names = str(leg.get("MatchName") or "").split(" - ")
        try:
            odd = float(leg.get("OddValue"))
        except (TypeError, ValueError):
            odd = None
        triple = (leg.get("MarketTypeId"), _sbv(leg.get("Spread")),
                  leg.get("IDSelectionType"))
        out.append({
            "eventId": leg.get("MatchId"),
            "prediction": _BY_TRIPLE.get(triple),
            # What they called it, kept whether or not we mapped it: an
            # unmapped leg still has to be nameable on screen, and their own
            # words are better than a triple nobody can read.
            "raw": "%s/%s" % (leg.get("MarketName") or "",
                              leg.get("SelectionName") or ""),
            "home": names[0].strip() if names else "",
            "away": names[1].strip() if len(names) > 1 else "",
            "league": leg.get("TournamentName") or "",
            "kickoff": leg.get("EventDate") or "",
            "odds": odd,
        })
    return {"legs": out, "available": available,
            # What the reader is owed when their code has thinned: how many it
            # held, and which games have gone. Empty when nothing was dropped.
            "removed": removed, "booked": booked}


def generate_code(selections, timeout=30, verify=True):
    """Turn a list of {event, code} into a BetKing booking code.

    `verify` reads the code back before returning it, and that is not belt and
    braces: this endpoint accepts a selection id that does not exist and
    answers with an ordinary code. Without the read-back, a mapping mistake
    anywhere above would ship as a working feature that hands people empty
    slips, and nothing in the response would say so.
    """
    if not selections:
        return {"error": "no selections"}
    if len(selections) > BETSLIP_MAX:
        return {"error": "betking takes at most %d selections" % BETSLIP_MAX,
                "sent": len(selections)}
    # TWO LEGS ON ONE MATCH IS NOT A MULTIPLE HERE. Their client only combines
    # two selections from the same event when the first one's CompatibleMarkets
    # names the second's market (isCouponOddCompatible, and it returns false on
    # an empty list) - and every collection on every event checked comes back
    # with that list EMPTY. So a same-game pair is refused by them, and refused
    # the way everything is refused here: the coupon is accepted, a code comes
    # back, and it contains nothing. Measured on one fixture, 14 Sep: four legs
    # on four games booked (DT166R), the team-goals leg alone booked (N827WM),
    # and the two of them together came back empty (8J2DDW).
    # The route names the extra legs before it gets here; this is the backstop,
    # because the failure it prevents is silent.
    seen, dupes = set(), []
    for sel in selections:
        match = ((sel.get("event") or {}).get("event") or {}).get("MatchId")
        if match in seen:
            dupes.append(sel.get("code"))
        seen.add(match)
    if dupes:
        return {"error": "betking will not put two selections from one game on "
                         "a multiple (%s)" % ", ".join(str(d) for d in dupes),
                "sent": len(selections)}

    try:
        legs = [build_selection(s["event"], s["code"]) for s in selections]
    except (KeyError, TypeError, ValueError) as ex:
        return {"error": "could not build selection: %s" % ex}

    odds_total = 1.0
    for leg in legs:
        odds_total *= float(leg["OddValue"])

    try:
        gvars = global_variables(timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        return {"error": "could not read betking settings: %s" % ex}

    url = "%s/%s" % (BOOK_URL, _transaction_id())
    try:
        r = requests.post(
            url, data=json.dumps(_coupon(legs, gvars)),
            headers=_headers({"Content-Type": "application/json;charset=UTF-8"}),
            timeout=timeout, impersonate=IMPERSONATE)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betking booking failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    # 1 is their success. Anything else is a refusal, and their enum names it.
    if body.get("ResponseStatus") != 1 or not body.get("BookedCouponCode"):
        return {"error": json.dumps(body)[:400], "sent": len(legs)}

    code = body["BookedCouponCode"]
    if verify:
        try:
            available, rows = read_code(code, timeout)
        except Exception as ex:                  # noqa: BLE001 - upstream
            # The code may well be fine; we simply could not check. Say which.
            log.warning("betking read-back failed for %s: %s", code, ex)
            return {"code": code, "odds": round(odds_total, 2),
                    "legs": len(legs), "verified": False}
        if len(rows) != len(legs):
            return {"error": "betking accepted the slip and returned an empty "
                             "code (%d of %d legs resolved)"
                             % (len(rows), len(legs)),
                    "code": code, "sent": len(legs), "available": available}
        return {"code": code, "odds": round(odds_total, 2), "legs": len(legs),
                "verified": True}
    return {"code": code, "odds": round(odds_total, 2), "legs": len(legs)}
