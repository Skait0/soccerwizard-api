"""Betway Nigeria: the sixth book, and the first that needs a login to book.

Recon 13-14 Oct 2026, read out of their own Nuxt bundle and then measured.

THEIR IDS ARE SPORTRADAR'S. The event id IS the Sportradar match id
(73835276 on Betway is sr:match:73835276 on SportyBet, same teams, same
kickoff), and every market and outcome id is the UOF id glued onto it:

    marketId  = {eventId}{uofMarket}{key=value~ for each specifier}
    outcomeId = {marketId}{uofOutcome}

so 1X2 home is "{eid}1" + "1" and over 2.5 is "{eid}18total=2.5~" + "12". The
numbers are SportyBet's numbers (server.MARKET_MAP), which is why this table is
written in SportyBet's terms - (market, specifier, outcome) with "|" between
specifiers - and why an outcome id can be BUILT rather than swept.

READING IS PUBLIC, BOOKING IS NOT. Fixtures, cards, prices and booking-code
reads need nothing. BookABet needs `Authorization: Bearer <betway-auth>` and
`X-Brand-Id`, from the owner's own account (owner's decision, 13 Oct 2026, ban
risk accepted). The token pair lives in Redis (sw:betway:auth / :refresh),
seeded once from the BETWAY_AUTH / BETWAY_REFRESH_TOKEN env vars, and is
renewed before it expires. Token values are never logged.

A CODE THAT DOES NOT EXIST answers 400 errorCode 6000359 "BookABetLimitExceeded".
Misnamed on their side; a real code read straight after it answers 200.
"""

import base64
import json
import logging
import os
import re
import threading
import time

from curl_cffi import requests

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"

SITE = "https://www.betway.com.ng"
FEED = SITE + "/sportsapi/br/v1"
UPCOMING_URL = FEED + "/BetBook/Upcoming/"
CARD_URL = FEED + "/FeedsMarket"
PRICES_URL = FEED + "/FeedsOutcome/emop/"
# Their bundle names two betting hosts (bet-api-sr and bet-api-sr02, behind a
# feature flag). Both now point at bet-api-sr, and the first live mint
# (BW79B45CD9, 14 Oct 2026) booked there from Railway.
BOOK_URL = SITE + "/appsynapse/bet-api-sr/v1/Betting/BookABet"
FIND_URL = SITE + "/appsynapse/bet-api-sr/v2/Betting/FindBookABet"
REFRESH_URL = SITE + "/appsynapse/auth/Users/refresh-token?nogeoredirect=1"
# config.betwayafrica.com/cron/appsettings/synapse/NG - public.
BRAND_ID = "f8a8d16a-d619-4b49-aa8c-f21211403c92"

NOT_FOUND_CODE = 6000359

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PAGE = 100
# MEASURED, 14 Oct 2026 (Railway probe, test codes, no stake): 30 and 40
# distinct games booked and read back whole (BW79BED4E7, BW79BED60F); 50
# answered 400 errorCode 10 "UnexpectedError" - their generic word for it.
# So the cap sits between 41 and 49 and 40 is the last size proven.
BETSLIP_MAX = 40
# The price endpoint takes outcome ids in the query string and answers an
# EMPTY BODY past roughly 8 KB of URL (measured 14 Oct: 270 ids, 8.7 KB).
# Six games of the modelled set is about 160 ids, 5 KB.
PRICE_BATCH_EVENTS = 6

# Their site renews when the JWT has under a day left (isTokenNearExpiry,
# 864e5 ms); cookies last seven days.
RENEW_BEFORE = 24 * 3600


def _headers(extra=None):
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": SITE + "/",
        "Origin": SITE,
    }
    if extra:
        h.update(extra)
    return h


# --- the markets we model ---------------------------------------------------
# code -> (uofMarket, specifier or None, uofOutcome). The same numbers as
# server.MARKET_MAP (SportyBet), pinned by test_betway, so the two books
# cannot quietly drift on what "OVER_2.5" means.
MARKET_MAP = {
    "1": ("1", None, "1"),
    "X": ("1", None, "2"),
    "2": ("1", None, "3"),
    "1X": ("10", None, "9"),
    "12": ("10", None, "10"),
    "X2": ("10", None, "11"),
    "OVER_0.5": ("18", "total=0.5", "12"),
    "OVER_1.5": ("18", "total=1.5", "12"),
    "OVER_2.5": ("18", "total=2.5", "12"),
    "OVER_3.5": ("18", "total=3.5", "12"),
    "UNDER_0.5": ("18", "total=0.5", "13"),
    "UNDER_1.5": ("18", "total=1.5", "13"),
    "UNDER_2.5": ("18", "total=2.5", "13"),
    "UNDER_3.5": ("18", "total=3.5", "13"),
    "UNDER_4.5": ("18", "total=4.5", "13"),
    # Win to Nil NO, home and away, full time (owner, 17 Oct 2026). Read off Roma v Genoa.
    "WTN_H_N": ("33", None, "76"),
    "WTN_A_N": ("34", None, "76"),
    "GG": ("29", None, "74"),
    "NG": ("29", None, "76"),
    "FH_OVER_0.5": ("68", "total=0.5", "12"),
    "FH_UNDER_0.5": ("68", "total=0.5", "13"),
    # 19 is the HOME side's total and 20 the AWAY side's, on Betway as on
    # SportyBet: read off Vissel Kobe v Jeonbuk on 14 Oct, where 19 is named
    # "Vissel Kobe Total" and 20 "Jeonbuk FC Total".
    "HOME_OVER_0.5": ("19", "total=0.5", "12"),
    "HOME_UNDER_0.5": ("19", "total=0.5", "13"),
    "HOME_OVER_1.5": ("19", "total=1.5", "12"),
    "HOME_UNDER_1.5": ("19", "total=1.5", "13"),
    "AWAY_OVER_0.5": ("20", "total=0.5", "12"),
    "AWAY_UNDER_0.5": ("20", "total=0.5", "13"),
    "AWAY_OVER_1.5": ("20", "total=1.5", "12"),
    "AWAY_UNDER_1.5": ("20", "total=1.5", "13"),
}

# --- markets we carry but never model --------------------------------------
# GENERATED, NEVER TYPED - tools/bwgen.py, 14 Oct 2026, against the ten
# deepest cards on the board (Roma v Real Madrid at 330 markets down to
# Deportivo v Levante at 296). Every candidate is SportyBet's own triple in
# Betway's spelling, so meaning cannot drift: the provider's market
# definitions are the same objects. 157 were priced on those cards.
#
# Two spelling differences, handled in bwgen.candidate():
# - SportyBet writes a variant outcome "sr:exact_goals:6+:68"; Betway "68".
# - SportyBet's own promo ids map to Sportradar's originals: 1UP/2UP (60200/
#   60100) is 1601 with xup=1/xup=2 - checked on prices, 1UP shorter than 2UP
#   shorter than the plain 1X2 on six cards - and team corners (900300/1) are
#   167 (home, named after the home side on three cards) and 168 (away).
#
# "# line variant" rows (52): the market is on sale in the same specifier
# shape and only that LINE was not up on the ten cards. Betway sets lines per
# game (one corner line per team), so booking re-reads the live price and
# names the leg not_priced when the line is not up on that game.
PASSTHROUGH_MAP = {
    # --- BOTHHALVES_OV (2) ---
    "BOTHHALVES_OV_N": ("58", "total=1.5", "76"),
    "BOTHHALVES_OV_Y": ("58", "total=1.5", "74"),
    # --- BOTHHALVES_UN (2) ---
    "BOTHHALVES_UN_N": ("59", "total=1.5", "76"),
    "BOTHHALVES_UN_Y": ("59", "total=1.5", "74"),
    # --- CORNERS_A_OV (10) ---
    "CORNERS_A_OV_0.5": ("168", "total=0.5", "12"),  # line variant
    "CORNERS_A_OV_1.5": ("168", "total=1.5", "12"),  # line variant
    "CORNERS_A_OV_2.5": ("168", "total=2.5", "12"),  # line variant
    "CORNERS_A_OV_3.5": ("168", "total=3.5", "12"),  # line variant
    "CORNERS_A_OV_4.5": ("168", "total=4.5", "12"),
    "CORNERS_A_OV_5.5": ("168", "total=5.5", "12"),
    "CORNERS_A_OV_6.5": ("168", "total=6.5", "12"),
    "CORNERS_A_OV_7.5": ("168", "total=7.5", "12"),  # line variant
    "CORNERS_A_OV_8.5": ("168", "total=8.5", "12"),  # line variant
    "CORNERS_A_OV_9.5": ("168", "total=9.5", "12"),  # line variant
    # --- CORNERS_A_UN (10) ---
    "CORNERS_A_UN_0.5": ("168", "total=0.5", "13"),  # line variant
    "CORNERS_A_UN_1.5": ("168", "total=1.5", "13"),  # line variant
    "CORNERS_A_UN_2.5": ("168", "total=2.5", "13"),  # line variant
    "CORNERS_A_UN_3.5": ("168", "total=3.5", "13"),  # line variant
    "CORNERS_A_UN_4.5": ("168", "total=4.5", "13"),
    "CORNERS_A_UN_5.5": ("168", "total=5.5", "13"),
    "CORNERS_A_UN_6.5": ("168", "total=6.5", "13"),
    "CORNERS_A_UN_7.5": ("168", "total=7.5", "13"),  # line variant
    "CORNERS_A_UN_8.5": ("168", "total=8.5", "13"),  # line variant
    "CORNERS_A_UN_9.5": ("168", "total=9.5", "13"),  # line variant
    # --- CORNERS_H_OV (10) ---
    "CORNERS_H_OV_0.5": ("167", "total=0.5", "12"),  # line variant
    "CORNERS_H_OV_1.5": ("167", "total=1.5", "12"),  # line variant
    "CORNERS_H_OV_2.5": ("167", "total=2.5", "12"),  # line variant
    "CORNERS_H_OV_3.5": ("167", "total=3.5", "12"),
    "CORNERS_H_OV_4.5": ("167", "total=4.5", "12"),
    "CORNERS_H_OV_5.5": ("167", "total=5.5", "12"),  # line variant
    "CORNERS_H_OV_6.5": ("167", "total=6.5", "12"),  # line variant
    "CORNERS_H_OV_7.5": ("167", "total=7.5", "12"),  # line variant
    "CORNERS_H_OV_8.5": ("167", "total=8.5", "12"),  # line variant
    "CORNERS_H_OV_9.5": ("167", "total=9.5", "12"),  # line variant
    # --- CORNERS_H_UN (10) ---
    "CORNERS_H_UN_0.5": ("167", "total=0.5", "13"),  # line variant
    "CORNERS_H_UN_1.5": ("167", "total=1.5", "13"),  # line variant
    "CORNERS_H_UN_2.5": ("167", "total=2.5", "13"),  # line variant
    "CORNERS_H_UN_3.5": ("167", "total=3.5", "13"),
    "CORNERS_H_UN_4.5": ("167", "total=4.5", "13"),
    "CORNERS_H_UN_5.5": ("167", "total=5.5", "13"),  # line variant
    "CORNERS_H_UN_6.5": ("167", "total=6.5", "13"),  # line variant
    "CORNERS_H_UN_7.5": ("167", "total=7.5", "13"),  # line variant
    "CORNERS_H_UN_8.5": ("167", "total=8.5", "13"),  # line variant
    "CORNERS_H_UN_9.5": ("167", "total=9.5", "13"),  # line variant
    # --- CORNERS_OV (7) ---
    "CORNERS_OV_10.5": ("166", "total=10.5", "12"),
    "CORNERS_OV_11.5": ("166", "total=11.5", "12"),
    "CORNERS_OV_12.5": ("166", "total=12.5", "12"),  # line variant
    "CORNERS_OV_6.5": ("166", "total=6.5", "12"),  # line variant
    "CORNERS_OV_7.5": ("166", "total=7.5", "12"),  # line variant
    "CORNERS_OV_8.5": ("166", "total=8.5", "12"),
    "CORNERS_OV_9.5": ("166", "total=9.5", "12"),
    # --- CORNERS_UN (7) ---
    "CORNERS_UN_10.5": ("166", "total=10.5", "13"),
    "CORNERS_UN_11.5": ("166", "total=11.5", "13"),
    "CORNERS_UN_12.5": ("166", "total=12.5", "13"),  # line variant
    "CORNERS_UN_6.5": ("166", "total=6.5", "13"),  # line variant
    "CORNERS_UN_7.5": ("166", "total=7.5", "13"),  # line variant
    "CORNERS_UN_8.5": ("166", "total=8.5", "13"),
    "CORNERS_UN_9.5": ("166", "total=9.5", "13"),
    # --- DC2 (3) ---
    "DC2_12": ("85", None, "10"),
    "DC2_1X": ("85", None, "9"),
    "DC2_X2": ("85", None, "11"),
    # --- DNB (2) ---
    "DNB_1": ("11", None, "4"),
    "DNB_2": ("11", None, "5"),
    # --- EH (16) ---
    "EH_0_1_1": ("14", "hcp=0:1", "1711"),
    "EH_0_1_2": ("14", "hcp=0:1", "1713"),
    "EH_0_2_1": ("14", "hcp=0:2", "1711"),
    "EH_0_2_2": ("14", "hcp=0:2", "1713"),
    "EH_0_3_1": ("14", "hcp=0:3", "1711"),
    "EH_0_3_2": ("14", "hcp=0:3", "1713"),
    "EH_0_4_1": ("14", "hcp=0:4", "1711"),
    "EH_0_4_2": ("14", "hcp=0:4", "1713"),
    "EH_0_5_1": ("14", "hcp=0:5", "1711"),
    "EH_0_5_2": ("14", "hcp=0:5", "1713"),
    "EH_1_0_1": ("14", "hcp=1:0", "1711"),
    "EH_1_0_2": ("14", "hcp=1:0", "1713"),
    "EH_2_0_1": ("14", "hcp=2:0", "1711"),
    "EH_2_0_2": ("14", "hcp=2:0", "1713"),
    "EH_3_0_1": ("14", "hcp=3:0", "1711"),
    "EH_3_0_2": ("14", "hcp=3:0", "1713"),
    # --- EH_0_1 (1) ---
    "EH_0_1_X": ("14", "hcp=0:1", "1712"),
    # --- EH_0_2 (1) ---
    "EH_0_2_X": ("14", "hcp=0:2", "1712"),
    # --- EH_0_3 (1) ---
    "EH_0_3_X": ("14", "hcp=0:3", "1712"),
    # --- EH_0_4 (1) ---
    "EH_0_4_X": ("14", "hcp=0:4", "1712"),
    # --- EH_0_5 (1) ---
    "EH_0_5_X": ("14", "hcp=0:5", "1712"),
    # --- EH_1_0 (1) ---
    "EH_1_0_X": ("14", "hcp=1:0", "1712"),
    # --- EH_2_0 (1) ---
    "EH_2_0_X": ("14", "hcp=2:0", "1712"),
    # --- EH_3_0 (1) ---
    "EH_3_0_X": ("14", "hcp=3:0", "1712"),
    # --- EXACT (7) ---
    "EXACT_0": ("21", "variant=sr:exact_goals:6+", "68"),
    "EXACT_1": ("21", "variant=sr:exact_goals:6+", "69"),
    "EXACT_2": ("21", "variant=sr:exact_goals:6+", "70"),
    "EXACT_3": ("21", "variant=sr:exact_goals:6+", "71"),
    "EXACT_4": ("21", "variant=sr:exact_goals:6+", "72"),
    "EXACT_5": ("21", "variant=sr:exact_goals:6+", "73"),
    "EXACT_6": ("21", "variant=sr:exact_goals:6+", "74"),
    # --- EXACT_SH (3) ---
    "EXACT_SH_0": ("93", "variant=sr:exact_goals:2+", "85"),
    "EXACT_SH_1": ("93", "variant=sr:exact_goals:2+", "86"),
    "EXACT_SH_2": ("93", "variant=sr:exact_goals:2+", "87"),
    # --- FH_AH (12) ---
    "FH_AH_1_-0.5": ("66", "hcp=-0.5", "1714"),
    "FH_AH_1_-1": ("66", "hcp=-1", "1714"),  # line variant
    "FH_AH_1_-1.5": ("66", "hcp=-1.5", "1714"),
    "FH_AH_1_-2": ("66", "hcp=-2", "1714"),  # line variant
    "FH_AH_1_0": ("66", "hcp=0", "1714"),  # line variant
    "FH_AH_1_0.5": ("66", "hcp=0.5", "1714"),
    "FH_AH_2_-0.5": ("66", "hcp=-0.5", "1715"),
    "FH_AH_2_-1": ("66", "hcp=-1", "1715"),  # line variant
    "FH_AH_2_-1.5": ("66", "hcp=-1.5", "1715"),
    "FH_AH_2_-2": ("66", "hcp=-2", "1715"),  # line variant
    "FH_AH_2_0": ("66", "hcp=0", "1715"),  # line variant
    "FH_AH_2_0.5": ("66", "hcp=0.5", "1715"),
    # --- FH_AWAY_OVER (3) ---
    "FH_AWAY_OVER_0.5": ("70", "total=0.5", "12"),
    "FH_AWAY_OVER_1.5": ("70", "total=1.5", "12"),
    "FH_AWAY_OVER_2.5": ("70", "total=2.5", "12"),
    # --- FH_AWAY_UNDER (3) ---
    "FH_AWAY_UNDER_0.5": ("70", "total=0.5", "13"),
    "FH_AWAY_UNDER_1.5": ("70", "total=1.5", "13"),
    "FH_AWAY_UNDER_2.5": ("70", "total=2.5", "13"),
    # --- FH_EH (6) ---
    "FH_EH_0_1_1": ("65", "hcp=0:1", "1711"),
    "FH_EH_0_1_2": ("65", "hcp=0:1", "1713"),
    "FH_EH_0_2_1": ("65", "hcp=0:2", "1711"),
    "FH_EH_0_2_2": ("65", "hcp=0:2", "1713"),
    "FH_EH_1_0_1": ("65", "hcp=1:0", "1711"),
    "FH_EH_1_0_2": ("65", "hcp=1:0", "1713"),
    # --- FH_EH_0_1 (1) ---
    "FH_EH_0_1_X": ("65", "hcp=0:1", "1712"),
    # --- FH_EH_0_2 (1) ---
    "FH_EH_0_2_X": ("65", "hcp=0:2", "1712"),
    # --- FH_EH_1_0 (1) ---
    "FH_EH_1_0_X": ("65", "hcp=1:0", "1712"),
    # --- FH_HOME_OVER (3) ---
    "FH_HOME_OVER_0.5": ("69", "total=0.5", "12"),
    "FH_HOME_OVER_1.5": ("69", "total=1.5", "12"),
    "FH_HOME_OVER_2.5": ("69", "total=2.5", "12"),
    # --- FH_HOME_UNDER (3) ---
    "FH_HOME_UNDER_0.5": ("69", "total=0.5", "13"),
    "FH_HOME_UNDER_1.5": ("69", "total=1.5", "13"),
    "FH_HOME_UNDER_2.5": ("69", "total=2.5", "13"),
    # --- FH_MIX_1_OV (1) ---
    "FH_MIX_1_OV_1.5": ("79", "total=1.5", "796"),
    # --- FH_MIX_1_UN (1) ---
    "FH_MIX_1_UN_1.5": ("79", "total=1.5", "794"),
    # --- FH_MIX_2_OV (1) ---
    "FH_MIX_2_OV_1.5": ("79", "total=1.5", "804"),
    # --- FH_MIX_2_UN (1) ---
    "FH_MIX_2_UN_1.5": ("79", "total=1.5", "802"),
    # --- FH_MIX_X_OV (1) ---
    "FH_MIX_X_OV_1.5": ("79", "total=1.5", "800"),
    # --- FH_MIX_X_UN (1) ---
    "FH_MIX_X_UN_1.5": ("79", "total=1.5", "798"),
    # --- HIGHHALF (3) ---
    "HIGHHALF_1": ("52", None, "436"),
    "HIGHHALF_2": ("52", None, "438"),
    "HIGHHALF_E": ("52", None, "440"),
    # --- HIGHHALF_A (3) ---
    "HIGHHALF_A_1": ("54", None, "436"),
    "HIGHHALF_A_2": ("54", None, "438"),
    "HIGHHALF_A_E": ("54", None, "440"),
    # --- HIGHHALF_H (3) ---
    "HIGHHALF_H_1": ("53", None, "436"),
    "HIGHHALF_H_2": ("53", None, "438"),
    "HIGHHALF_H_E": ("53", None, "440"),
    # --- MIXGG (3) ---
    "MIXGG_1": ("860", None, "74"),
    "MIXGG_2": ("862", None, "74"),
    "MIXGG_X": ("861", None, "74"),
    # --- MIX_1_OV (1) ---
    "MIX_1_OV_2.5": ("854", "total=2.5", "74"),
    # --- MIX_1_UN (1) ---
    "MIX_1_UN_2.5": ("855", "total=2.5", "74"),
    # --- MIX_2_OV (1) ---
    "MIX_2_OV_2.5": ("858", "total=2.5", "74"),
    # --- MIX_2_UN (1) ---
    "MIX_2_UN_2.5": ("859", "total=2.5", "74"),
    # --- MIX_X_OV (1) ---
    "MIX_X_OV_2.5": ("856", "total=2.5", "74"),
    # --- MIX_X_UN (1) ---
    "MIX_X_UN_2.5": ("857", "total=2.5", "74"),
    # --- OVER (2) ---
    "OVER_2": ("18", "total=2", "12"),  # line variant
    "OVER_3": ("18", "total=3", "12"),  # line variant
    # --- SH_AH (12) ---
    "SH_AH_1_-0.5": ("88", "hcp=-0.5", "1714"),
    "SH_AH_1_-1": ("88", "hcp=-1", "1714"),  # line variant
    "SH_AH_1_-1.5": ("88", "hcp=-1.5", "1714"),
    "SH_AH_1_-2": ("88", "hcp=-2", "1714"),  # line variant
    "SH_AH_1_0": ("88", "hcp=0", "1714"),  # line variant
    "SH_AH_1_0.5": ("88", "hcp=0.5", "1714"),
    "SH_AH_2_-0.5": ("88", "hcp=-0.5", "1715"),
    "SH_AH_2_-1": ("88", "hcp=-1", "1715"),  # line variant
    "SH_AH_2_-1.5": ("88", "hcp=-1.5", "1715"),
    "SH_AH_2_-2": ("88", "hcp=-2", "1715"),  # line variant
    "SH_AH_2_0": ("88", "hcp=0", "1715"),  # line variant
    "SH_AH_2_0.5": ("88", "hcp=0.5", "1715"),
    # --- SH_AWAY_OVER (3) ---
    "SH_AWAY_OVER_0.5": ("92", "total=0.5", "12"),
    "SH_AWAY_OVER_1.5": ("92", "total=1.5", "12"),
    "SH_AWAY_OVER_2.5": ("92", "total=2.5", "12"),
    # --- SH_AWAY_UNDER (3) ---
    "SH_AWAY_UNDER_0.5": ("92", "total=0.5", "13"),
    "SH_AWAY_UNDER_1.5": ("92", "total=1.5", "13"),
    "SH_AWAY_UNDER_2.5": ("92", "total=2.5", "13"),
    # --- SH_EH (6) ---
    "SH_EH_0_1_1": ("87", "hcp=0:1", "1711"),
    "SH_EH_0_1_2": ("87", "hcp=0:1", "1713"),
    "SH_EH_0_2_1": ("87", "hcp=0:2", "1711"),
    "SH_EH_0_2_2": ("87", "hcp=0:2", "1713"),
    "SH_EH_1_0_1": ("87", "hcp=1:0", "1711"),
    "SH_EH_1_0_2": ("87", "hcp=1:0", "1713"),
    # --- SH_EH_0_1 (1) ---
    "SH_EH_0_1_X": ("87", "hcp=0:1", "1712"),
    # --- SH_EH_0_2 (1) ---
    "SH_EH_0_2_X": ("87", "hcp=0:2", "1712"),
    # --- SH_EH_1_0 (1) ---
    "SH_EH_1_0_X": ("87", "hcp=1:0", "1712"),
    # --- SH_HOME_OVER (3) ---
    "SH_HOME_OVER_0.5": ("91", "total=0.5", "12"),
    "SH_HOME_OVER_1.5": ("91", "total=1.5", "12"),
    "SH_HOME_OVER_2.5": ("91", "total=2.5", "12"),
    # --- SH_HOME_UNDER (3) ---
    "SH_HOME_UNDER_0.5": ("91", "total=0.5", "13"),
    "SH_HOME_UNDER_1.5": ("91", "total=1.5", "13"),
    "SH_HOME_UNDER_2.5": ("91", "total=2.5", "13"),
    # --- TEAMGOALS_A (4) ---
    "TEAMGOALS_A_0": ("24", "variant=sr:exact_goals:3+", "88"),
    "TEAMGOALS_A_1": ("24", "variant=sr:exact_goals:3+", "89"),
    "TEAMGOALS_A_2": ("24", "variant=sr:exact_goals:3+", "90"),
    "TEAMGOALS_A_3": ("24", "variant=sr:exact_goals:3+", "91"),
    # --- TEAMGOALS_H (4) ---
    "TEAMGOALS_H_0": ("23", "variant=sr:exact_goals:3+", "88"),
    "TEAMGOALS_H_1": ("23", "variant=sr:exact_goals:3+", "89"),
    "TEAMGOALS_H_2": ("23", "variant=sr:exact_goals:3+", "90"),
    "TEAMGOALS_H_3": ("23", "variant=sr:exact_goals:3+", "91"),
    # --- UNDER (2) ---
    "UNDER_2": ("18", "total=2", "13"),  # line variant
    "UNDER_3": ("18", "total=3", "13"),  # line variant
    # --- UP1 (3) ---
    "UP1_1": ("1601", "xup=1", "1"),
    "UP1_2": ("1601", "xup=1", "3"),
    "UP1_X": ("1601", "xup=1", "2"),
    # --- UP2 (3) ---
    "UP2_1": ("1601", "xup=2", "1"),
    "UP2_2": ("1601", "xup=2", "3"),
    "UP2_X": ("1601", "xup=2", "2"),
    # --- WINHALF_A (2) ---
    "WINHALF_A_N": ("51", None, "76"),
    "WINHALF_A_Y": ("51", None, "74"),
    # --- WINHALF_H (2) ---
    "WINHALF_H_N": ("50", None, "76"),
    "WINHALF_H_Y": ("50", None, "74"),
}

# Every reason below was measured by tools/bwgen.py on the ten cards named
# above (14 Oct 2026) unless it says "not read yet". Longest prefix wins.
_TEN = "on ten deep cards (Champions League, Europa League, LaLiga, Serie A, Bundesliga), 14 Oct 2026"
# HALF-TIME LEGS (owner, 17 Oct 2026: "add the top four"). 2nd half over 0.5
# and under 2.5 were already carried above; these add the 1st half under 2.5
# and half-time 1X (home or draw at the break). Betway's ids are Sportradar's,
# the same numbers SportyBet's card showed on Tottenham v Coventry, 19 Oct 2026.
PASSTHROUGH_MAP["FH_UNDER_2.5"] = ("68", "total=2.5", "13")
PASSTHROUGH_MAP["FH_DC_1X"] = ("63", None, "9")

NOT_CARRIED = {
    "AH_": "verified absent at whole and quarter lines: no full-time Asian handicap "
           "(market 16) " + _TEN + ". Half lines ARE carried, on the three-way "
           "handicap that wins on exactly the same scores (AH_VIA_EH); whole and "
           "quarter lines push, so the three-way one is a different bet there.",
    "FH_AH_": "verified absent past the half balls: their first-half handicap "
              "(66) is a half-ball market " + _TEN + "; whole and zero lines push, "
              "so they are a different bet.",
    "SH_AH_": "verified absent past the half balls: see FH_AH_ (market 88).",
    "BOUNDS_": "verified absent: their team Multigoals rungs (1-2, 1-3, 2-3, 4+, "
               "no goal) are a different set from ours, and our 1-3 means one to "
               "three OR MORE.",
    "CARD_": "verified absent: they sell total bookings and sending-off markets "
             "only; no per-team booking count " + _TEN + ".",
    "FH_CARD_": "verified absent: see CARD_.",
    "FH_CARDUN_": "verified absent: see CARD_.",
    "CORNRANGE_": "verified absent: no corner-range market (169-171) " + _TEN + ".",
    "DC1UP_": "verified absent: they run 1UP on the 1X2 only, not on double chance.",
    "EARLY_": "verified absent: their early market is a 1X2 over the first ten "
              "minutes (105), not a goals line.",
    "EXACT_FH_": "verified absent: no first-half exact goals (71) " + _TEN + ".",
    "EXGOALS_": "verified absent: a bet that the total is anything BUT n is not sold.",
    "FIRSTGOAL_": "verified absent: no first-team-to-score market (8/62/84) " + _TEN +
                  "; their nearest is 1st Goal & 1X2, a different bet.",
    "GOALRANGE_": "verified absent in our rungs: their Goal Range (25) carries the "
                  "point_range:6+ variant (0-1, 2-3, 4-5, 6+), not our goal_range:7+ "
                  "(0-1, 2-3, 4-6, 7+). Same position, different rungs.",
    "HALFCORNER_": "verified absent: no half-with-most-corners market " + _TEN + ".",
    "HMC_": "verified absent: no half-with-most-bookings market " + _TEN + ".",
    "MARGIN_": "verified absent: no winning-margin market (15) " + _TEN + ".",
    "MIX_": "verified absent at 1.5 and 3.5: their 'X Or Over/Under' markets "
            "(854-859) are sold at 2.5 only " + _TEN + ".",
    "MIXNG_": "verified absent: they sell 'X Or Both Teams To Score' (860-862) "
              "and not the 'or not both' side.",
    "NR3_": "verified absent: SportyBet's own 3-in-a-row market, not a Sportradar one.",
    "R3_": "verified absent: SportyBet's own 3-in-a-row market, not a Sportradar one.",
    "PEN_": "verified absent: no penalty-awarded market " + _TEN + ".",
    "CORNERS_": "not read yet past 12.5: those lines come from Bet9ja and BetKing's "
                "tables, which SportyBet's (the generator's source) does not carry; "
                "Betway's total-corners lines ran 8.5 to 11.5 " + _TEN + ".",
    "SHOTS_": "verified absent: no team or match shots market " + _TEN +
              "; their shots markets are per player.",
    "SH_OVER_": "verified absent: no second-half match total (90) " + _TEN +
                "; they sell second-half TEAM totals (91/92).",
    "SH_UNDER_": "verified absent: see SH_OVER_.",
}


def reason_uncarried(code):
    """Why Betway does not carry one of our codes, or None if it does."""
    if market_for(code):
        return None
    best = None
    for prefix, why in NOT_CARRIED.items():
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, why)
    return best[1] if best else None


# their key -> our code, derived; modelled table last so its names win.
_BY_TRIPLE = {triple: code
              for code, triple in list(PASSTHROUGH_MAP.items()) +
              list(MARKET_MAP.items())}


# --- half-line Asian handicaps, booked on the three-way handicap --------------
# Betway sells no Asian handicap (market 16 absent on ten deep cards), but a
# HALF-LINE Asian bet and one outcome of the three-way handicap (market 14) win
# on exactly the same scores - neither can push or draw:
#   home at line L  wins iff  diff + L > 0      iff  diff + (L + 0.5) > 0
#                   = 3-way "home" (1711) at net head start L + 0.5
#   away at line L  wins iff  diff + L < 0      iff  diff + (L - 0.5) < 0
#     (our AH_2_L is the away side at the HOME team's line L, as everywhere)
#                   = 3-way "away" (1713) at net head start L - 0.5
# and a net head start of 0 is the plain 1X2. Mapped at the owner's call
# (14 Oct 2026) after checking it on prices: 12 of 12 SportyBet Asian prices
# sat within 10% of Betway's three-way price for the same Sportradar match,
# Betway slightly shorter, on both sides and both signs. WHOLE and quarter
# lines push, so they are a different bet and stay unmapped (NOT_CARRIED AH_).
def _ah_via_eh(side, line):
    net = line + 0.5 if side == "1" else line - 0.5
    if net == 0:
        return ("1", None, "1" if side == "1" else "3")
    n = int(abs(net))
    hcp = "hcp={}:0".format(n) if net > 0 else "hcp=0:{}".format(n)
    return ("14", hcp, "1711" if side == "1" else "1713")


AH_VIA_EH = {}
for _side in ("1", "2"):
    for _tenths in range(-45, 50, 10):          # -4.5 .. 4.5 in steps of one
        _line = _tenths / 10
        _code = "AH_{}_{}".format(_side, ("%g" % _line))
        AH_VIA_EH[_code] = _ah_via_eh(_side, _line)


# triple -> the Asian codes it also books, so a card that prices the
# three-way outcome prices them too (fetch_event).
_ALIASES = {}
for _code, _t in AH_VIA_EH.items():
    _ALIASES.setdefault(_t, []).append(_code)


def market_for(code):
    """The triple for one of our codes, or None. NEVER a default.

    AH_VIA_EH is consulted but kept OUT of _BY_TRIPLE: its triples are the
    three-way handicap's own, so a code read back shows Betway's bet by
    Betway's name (EH_2_0_1) rather than ours. Same bet either way."""
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code) or AH_VIA_EH.get(code)


# --- ids --------------------------------------------------------------------

def _spec_part(spec):
    return "".join(s + "~" for s in spec.split("|")) if spec else ""


def market_id(event_id, triple):
    m, spec, _o = triple
    return "{}{}{}".format(event_id, m, _spec_part(spec))


def outcome_id(event_id, triple):
    return market_id(event_id, triple) + triple[2]


_SPEC_OUT_RE = re.compile(r"^((?:[a-z]+=[^~]*~)*)(\d+)$")


def decode(event_id, mkt_id, out_id):
    """(uofMarket, specifier, uofOutcome) from their ids, or None.

    The market NUMBER comes from the market id and the SPECIFIER from the
    outcome id. The concatenation has no separator, so "{eid}1" + "1" and
    "{eid}11" + "" only part when the market id says where the market ends.
    And the specifier cannot come from the market id: on [Total Goals] every
    line's outcome hangs off the bare "{eid}18" while its own id carries
    "total=2.5~" (14 Oct 2026) - reading the market id dropped every total.
    """
    eid, mkt_id, out_id = str(event_id), str(mkt_id), str(out_id)
    if not mkt_id.startswith(eid):
        return None
    m = re.match(r"\d+", mkt_id[len(eid):])
    if not m or not out_id.startswith(eid + m.group()):
        return None
    got = _SPEC_OUT_RE.match(out_id[len(eid) + len(m.group()):])
    if not got:
        return None
    spec = "|".join(s for s in got.group(1).split("~") if s) or None
    return (m.group(), spec, got.group(2))


# --- reading ----------------------------------------------------------------

def _get_json(url, params=None, timeout=30, attempts=3, pause=1.5):
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


# Simulated games share real names (skill: SRL / eSoccer / cyber / virtual).
_FAKE = re.compile(r"\b(srl|esoccer|e-soccer|cyber|virtual|simulated)\b", re.I)


def _is_fake(event):
    return (event.get("regionId") == "esoccer"
            or bool(_FAKE.search(" ".join(str(event.get(k) or "") for k in
                                          ("name", "league", "region")))))


def _iso(epoch):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError):
        return ""


def _row(event):
    """The empty shell of one fixture, in the shape the other books use."""
    eid = str(event.get("eventId"))
    kickoff = _iso(event.get("expectedStartEpoch"))
    home, away = event.get("homeTeam") or "", event.get("awayTeam") or ""
    return {
        "eventId": eid,
        "slotId": eid,
        "eventCode": "",
        # Their event id is Sportradar's match id (verified 14 Oct 2026).
        "srId": eid if re.fullmatch(r"\d{8,9}", eid) else None,
        "teams": "{} - {}".format(home, away) if home and away
                 else (event.get("name") or "").replace(" vs. ", " - "),
        "kickoff": kickoff,
        "league": event.get("league") or "",
        "country": event.get("region") or "",
        "startdate": kickoff[:10],
        "odds": {},
        "raw": {},
        "sel": {},
    }


def _put(row, code, oid, mid, odd, market_name="", selection_name=""):
    try:
        odd = float(odd)
    except (TypeError, ValueError):
        return
    if odd <= 1.01:                              # suspended / collapsed
        return
    row["odds"][code] = odd
    row["raw"][code] = str(odd)
    row["sel"][code] = {"outcomeId": oid, "marketId": mid, "odds": odd,
                        "marketName": market_name,
                        "selectionName": selection_name}


def fetch_page(skip=0, timeout=40):
    """One page of upcoming football: (events, isFinalPage)."""
    body = _get_json(UPCOMING_URL, {
        "countryCode": "NG", "sportId": "soccer", "Skip": int(skip),
        "Take": PAGE, "cultureCode": "en-US", "isEsport": "false",
        "boostedOnly": "false", "marketTypes": "[Win/Draw/Win]"}, timeout)
    return (body or {}).get("events") or [], bool((body or {}).get("isFinalPage"))


def fetch_prices(outcome_ids, timeout=30):
    """{outcomeId: (price, marketId)} for outcome ids built with outcome_id().

    An id they do not sell is simply absent from the answer, which is how a
    market that does not exist on a fixture is told from one that does.

    THE MARKET ID COMES FROM THEM, NOT FROM market_id(). Match totals hang off
    a bare "{eid}18" ([Total Goals]) while team and half totals carry the
    specifier ("{eid}19total=0.5~") - 14 Oct 2026 - so the id their betslip
    would send is the one this answer names.
    """
    ids = list(outcome_ids)
    if not ids:
        return {}
    params = [("CountryCode", "NG"), ("CultureCode", "en-US")] + \
             [("OutcomeIds", i) for i in ids]
    last = None
    for n in range(3):
        try:
            r = requests.get(PRICES_URL, params=params, headers=_headers(),
                             timeout=timeout, impersonate=IMPERSONATE)
            body = r.json()
            break
        except Exception as ex:                  # noqa: BLE001 - upstream
            last = ex
            time.sleep(1.5 * (n + 1))
    else:
        raise last
    mkt = {str(o.get("outcomeId")): str(o.get("marketId") or "")
           for o in (body or {}).get("outcomes") or []
           if o.get("isTradingActive", True)}
    return {str(p.get("outcomeId")): (p.get("priceDecimal"), mkt[str(p.get("outcomeId"))])
            for p in (body or {}).get("prices") or []
            if str(p.get("outcomeId")) in mkt}


def _price_rows(rows, timeout=30):
    """Fill each row's modelled markets from the price endpoint, in batches."""
    for i in range(0, len(rows), PRICE_BATCH_EVENTS):
        batch = rows[i:i + PRICE_BATCH_EVENTS]
        want = {}
        for row in batch:
            for code, triple in MARKET_MAP.items():
                want[outcome_id(row["eventId"], triple)] = (row, code, triple)
        got = fetch_prices(want, timeout)
        for oid, (odd, mid) in got.items():
            hit = want.get(oid)
            if hit:
                row, code, _triple = hit
                _put(row, code, oid, mid, odd)
        yield len(batch)


def all_fixtures(pages=30, pause=0.45, timeout=40, deadline=20 * 60):
    """The sweep: list every upcoming game, then price the modelled markets.

    Two passes because the list endpoint fills only the one market it is asked
    for, and patchily past 1X2 (14 Oct: totals on 5 of 20). The second pass
    builds each game's outcome ids and asks the price endpoint for them in
    batches - one request per six games.
    """
    started = time.time()
    events, read, failed = [], 0, []
    for n in range(pages):
        try:
            got, final = fetch_page(n * PAGE, timeout)
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("betway page %d failed: %s", n, ex)
            failed.append(n)
            break
        read += 1
        events.extend(got)
        if final or not got:
            break
        time.sleep(pause)

    now = time.time()
    rows = []
    for e in events:
        if _is_fake(e) or e.get("isLive") or e.get("isOutright"):
            continue
        try:
            if int(e.get("expectedStartEpoch") or 0) <= now:
                continue
        except (TypeError, ValueError):
            continue
        rows.append(_row(e))

    priced, skipped = 0, 0
    try:
        for done in _price_rows(rows, timeout):
            priced += done
            if time.time() - started > deadline:
                skipped = len(rows) - priced
                log.warning("betway sweep hit its deadline, %d games unpriced",
                            skipped)
                break
            time.sleep(pause)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betway pricing stopped after %d games: %s", priced, ex)
        skipped = len(rows) - priced

    out = {r["eventId"]: r for r in rows if r["odds"]}
    log.info("betway swept %d fixtures (%d listed, %d pages)",
             len(out), len(events), read)
    return out, {"pages": read, "failed": failed, "listed": len(events),
                 "skipped": skipped}


def fetch_event(event_id, timeout=30):
    """One fixture's full card, every mapped code priced. None if gone."""
    try:
        body = _get_json(CARD_URL, {"EventId": str(event_id),
                                    "CountryCode": "NG",
                                    "CultureCode": "en-US"}, timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betway card %s failed: %s", event_id, ex)
        return None
    markets = {m.get("marketId"): m for m in (body or {}).get("markets") or []}
    prices = {p.get("outcomeId"): p.get("priceDecimal")
              for p in (body or {}).get("prices") or []}
    outcomes = (body or {}).get("outcomes") or []
    if not outcomes:
        return None
    row = _row({"eventId": event_id})
    for o in outcomes:
        triple = decode(event_id, o.get("marketId"), o.get("outcomeId"))
        codes = ([_BY_TRIPLE[triple]] if triple in _BY_TRIPLE else []) +             _ALIASES.get(triple, [])
        if not codes or not o.get("isTradingActive", True):
            continue
        m = markets.get(o.get("marketId")) or {}
        if m.get("isSuspended") or m.get("isActive") is False:
            continue
        for code in codes:
            _put(row, code, o["outcomeId"], o["marketId"],
                 prices.get(o["outcomeId"]), m.get("displayName") or "",
                 o.get("displayName") or o.get("name") or "")
    return row if row["odds"] else None


# --- the session ------------------------------------------------------------
# Redis when REDIS_URL is set (Railway), a module dict otherwise. The env vars
# are only the seed: a renewal writes the new pair here, and the env copy is
# assumed dead from then on.
_AUTH_KEY, _REFRESH_KEY = "sw:betway:auth", "sw:betway:refresh"
_mem = {}
_store = None
_session_lock = threading.Lock()


def _redis():
    global _store
    if _store is None:
        url = os.environ.get("REDIS_URL", "").strip()
        if url:
            try:
                import redis
                _store = redis.from_url(url, decode_responses=True,
                                        socket_timeout=5)
                _store.ping()
            except Exception as ex:              # noqa: BLE001 - optional
                log.warning("betway: Redis unavailable, tokens in memory: %s",
                            type(ex).__name__)
                _store = False
        else:
            _store = False
    return _store or None


def _get(key, env):
    r = _redis()
    if r:
        try:
            v = r.get(key)
            if v:
                return v
        except Exception as ex:                  # noqa: BLE001
            log.warning("betway: token read failed: %s", type(ex).__name__)
    return _mem.get(key) or os.environ.get(env, "").strip() or None


def _set(key, value):
    _mem[key] = value
    r = _redis()
    if r:
        r.set(key, value)


def tokens():
    return _get(_AUTH_KEY, "BETWAY_AUTH"), _get(_REFRESH_KEY, "BETWAY_REFRESH_TOKEN")


def token_expiry(jwt):
    """The JWT's exp as epoch seconds, or None. Read, never verified."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:                            # noqa: BLE001
        return None


def renew(timeout=20):
    """Swap the refresh token for a new pair and store it. True on success.

    Measured 14 Oct: the refresh token came back unchanged, but the stored
    pair is whatever they return, so a change on their side costs nothing.
    """
    with _session_lock:
        _auth, ref = tokens()
        if not ref:
            log.error("betway: no refresh token stored")
            return False
        try:
            r = requests.post(REFRESH_URL, data=json.dumps({"refresh_token": ref}),
                              headers=_headers({"Content-Type": "application/json"}),
                              timeout=timeout, impersonate=IMPERSONATE)
            body = r.json() if r.status_code == 200 else {}
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("betway renew failed: %s", type(ex).__name__)
            return False
        access, new_ref = body.get("access_token"), body.get("refresh_token")
        if not access:
            log.error("betway renew refused: HTTP %s", r.status_code)
            return False
        _set(_AUTH_KEY, access)
        if new_ref:
            _set(_REFRESH_KEY, new_ref)
        return True


def session_state():
    """{ok, secondsLeft} for the health check - no token values."""
    auth, ref = tokens()
    exp = token_expiry(auth) if auth else None
    left = int(exp - time.time()) if exp else None
    return {"ok": bool(auth and ref and left and left > 0),
            "secondsLeft": left, "hasRefresh": bool(ref)}


def keep_alive():
    """Renew when under a day is left. Returns session_state() after."""
    st = session_state()
    if st["secondsLeft"] is None or st["secondsLeft"] < RENEW_BEFORE:
        renew()
        st = session_state()
    return st


# --- booking ----------------------------------------------------------------

def build_selection(event, code):
    """One leg as their BookABet expects it. KeyError when not priced."""
    sel = (event.get("sel") or {}).get(code)
    if sel is None:
        raise KeyError("no {} on event {}".format(code, event.get("eventId")))
    return {"outcomeId": sel["outcomeId"], "eventId": int(event["eventId"]),
            "marketId": sel["marketId"], "value": sel["odds"],
            "selected": True, "payment": 1, "canPlaceSportsBonusBet": False,
            "isSportBonusAllowed": False, "meetsMinSportsBonusOdds": False,
            "isStartingPrice": False}


def read_coupon(code, timeout=20):
    """The legs behind a Betway code, in OUR vocabulary - same contract as the
    other books: [{eventId, srId, prediction, raw, home, away, league,
    kickoff, odds}]."""
    try:
        r = requests.post(FIND_URL, data=json.dumps(
            {"countryCode": "NG", "bookingCode": code, "cultureCode": "en-US"}),
            headers=_headers({"Content-Type": "application/json"}),
            timeout=timeout, impersonate=IMPERSONATE)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("betway coupon read failed: %s", ex)
        return {"error": f"request failed: {ex}"}
    if r.status_code != 200:
        if (body or {}).get("errorCode") == NOT_FOUND_CODE:
            return {"error": "not found", "notFound": True}
        return {"error": "betway answered {}: {}".format(
            r.status_code, json.dumps(body)[:200])}
    sels = (body or {}).get("selections") or []
    if not sels:
        return {"error": "not found", "notFound": True}
    out = []
    for s in sels:
        ev = s.get("sportEvent") or {}
        o = s.get("outcome") or {}
        eid = str(ev.get("eventId") or o.get("eventId") or "")
        triple = decode(eid, s.get("marketId"), s.get("outcomeId"))
        price = (s.get("price") or {}).get("priceDecimal")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        out.append({
            "eventId": eid,
            "srId": eid if re.fullmatch(r"\d{8,9}", eid) else None,
            "prediction": _BY_TRIPLE.get(triple) if triple else None,
            "raw": "{}/{}".format(s.get("marketName") or "",
                                  o.get("displayName") or o.get("name") or ""),
            "home": ev.get("homeTeam") or "",
            "away": ev.get("awayTeam") or "",
            "league": ev.get("league") or "",
            "kickoff": _iso(ev.get("expectedStartEpoch")),
            "odds": price,
            "outcomeId": s.get("outcomeId"),
        })
    return {"legs": out, "available": len(out), "removed": [],
            "booked": len(out)}


def _book(payload, timeout):
    auth, _ref = tokens()
    if not auth:
        return None, {"error": "betway session missing"}
    r = requests.post(BOOK_URL, data=json.dumps(payload), headers=_headers({
        "Content-Type": "application/json", "Authorization": "Bearer " + auth,
        "X-Brand-Id": BRAND_ID}), timeout=timeout, impersonate=IMPERSONATE)
    try:
        body = r.json()
    except Exception:                            # noqa: BLE001
        body = {}
    return r.status_code, body


def generate_code(selections, timeout=30, verify=True):
    """Turn [{event, code}] into a Betway booking code, read back leg by leg.

    THEY DO NOT CHECK THE PRICE WE SEND (measured 14 Oct 2026): a leg sent at
    99.0 against a live 1.89 booked, and the read-back carried 1.89 - the code
    is repriced when it is read, like BetKing. Prices are still re-read live
    first, because the live read is also how a market that has CLOSED since the
    sweep is caught before the slip goes out.

    SAME GAME IS REFUSED, AND THAT IS VERIFIED: a home win and over 1.5 on one
    game booked and read back both legs (BW79BED3C4), and Betway's own betslip
    then flagged them "There are conflicting selections in your Betslip, please
    revise" (owner's screenshot, 14 Oct 2026). Accepted, not placeable - the
    1xBet shape - so server.py names the second leg same_game and drops it.

    A 401 renews the session once and retries.
    """
    if not selections:
        return {"error": "no selections"}
    if len(selections) > BETSLIP_MAX:
        return {"error": f"betway slips are capped at {BETSLIP_MAX:d} selections here",
                "sent": len(selections)}
    try:
        legs = [build_selection(s["event"], s["code"]) for s in selections]
    except (KeyError, TypeError, ValueError) as ex:
        return {"error": f"could not build selection: {ex}"}
    try:
        live = fetch_prices([leg["outcomeId"] for leg in legs], timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betway live price read failed: %s", ex)
        live = None
    if live is not None:
        gone = [s for s, leg in zip(selections, legs) if leg["outcomeId"] not in live]
        if gone:
            return {"error": "betway no longer prices {} of {} legs".format(
                len(gone), len(legs)),
                "unbookable": [{"eventId": s["event"]["eventId"],
                                "prediction": s["code"], "reason": "not_priced"}
                               for s in gone]}
        for leg in legs:
            leg["value"], leg["marketId"] = live[leg["outcomeId"]]

    payload = {"cultureCode": "en-US", "countryCode": "NG",
               "isSingleBet": False, "outcomes": legs}
    try:
        status, body = _book(payload, timeout)
        if status == 401 and renew():
            status, body = _book(payload, timeout)
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("betway booking failed: %s", ex)
        return {"error": f"request failed: {ex}"}
    if status is None:
        return body
    code = (body or {}).get("bookingCode")
    if not code:
        return {"error": "betway answered {}: {}".format(
            status, json.dumps(body)[:300]), "sent": len(legs)}

    odds_total = 1.0
    for leg in legs:
        odds_total *= float(leg["value"])
    out = {"code": code, "odds": round(odds_total, 2), "legs": len(legs)}
    if not verify:
        return out
    got = read_coupon(code, timeout)
    if got.get("error"):
        return {**out, "verified": False}
    # Every leg, not the count (skill: 1xBet dropped one of 30 silently).
    back = {leg.get("outcomeId") for leg in got.get("legs") or []}
    missing = [s for s, leg in zip(selections, legs) if leg["outcomeId"] not in back]
    if missing:
        return {"error": "betway accepted the slip and returned a code holding "
                         f"{len(legs) - len(missing)} of {len(legs)} legs",
                "code": code, "sent": len(legs),
                "unbookable": [{"eventId": s["event"]["eventId"],
                                "prediction": s["code"], "reason": "not_priced"}
                               for s in missing]}
    return {**out, "verified": True}
