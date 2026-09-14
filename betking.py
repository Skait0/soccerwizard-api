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

# Reverse lookup, DERIVED rather than typed twice - a second literal is a
# second thing to keep in step, and the pair would drift silently. Both tables,
# because a pasted code is resolved through exactly the same index.
_BY_TRIPLE = {triple: code
              for code, triple in list(MARKET_MAP.items()) +
              list(PASSTHROUGH_MAP.items())}


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
        "startdate": (item.get("ItemDate") or "")[:10],
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


def all_fixtures(days=8, pause=0.45, today=None):
    """The sweep: the next eight days of football, one request per day.

    EIGHT, NOT THREE, AND THE DIFFERENCE IS THE WEEKEND. The board reaches
    about nine days out and its two biggest days by far are Saturday and
    Sunday - 146 and 102 fixtures on one measured board, against 8 to 37 on a
    weekday. A three-day sweep holds none of them until the Thursday, so the
    book would read "doesn't have any of these games" for the whole weekend
    slate, which is when slips actually get built.

    Measured 14 Sep over ten days: 937 fixtures, 20.7MB, 11.6s, and their own
    count agreed exactly on every day. Day eight onwards is a handful of
    fixtures, so this stops where the board does. That puts BetKing's coverage
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
    for n in range(days):
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


def read_code(code, timeout=20):
    """Read a booking code back. Anonymous, like everything else here.

    Returns (available, legs) where `legs` is their own coupon rows. A code
    that exists but resolves to nothing comes back (0, []), which is exactly
    what a code booked with a selection id they do not recognise looks like.
    """
    url = "%s/%s/%s" % (READ_URL, code, LANG)
    body = _get_json(url, timeout)
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
        available, rows = read_code(code, timeout)
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("betking coupon read failed: %s", ex)
        return {"error": "request failed: %s" % ex}

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
    return {"legs": out, "available": available}


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
