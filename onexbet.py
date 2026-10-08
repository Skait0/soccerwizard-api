"""1xBet Nigeria: the fifth book.

Read-only recon came first (29 Sep 2026); everything below was measured against
1xbet.ng rather than carried over from the other four modules. See memory
onexbet-bookie-feasibility.md and the site repo's
docs/superpowers/specs/2026-09-29-onexbet-design.md.

CLOUDFLARE, NOT A LOGIN. Plain fetch gets a 403 "Just a moment" page. A
chrome-impersonating session that has loaded the football page once gets JSON.
Nothing here solves a challenge, and nothing should.

A LEG IS (PERIOD, TYPE, PARAM). Their card groups outcomes by G, but the type
T is unique inside a period, so G is not part of the key. The period matters
because halves and corners are separate GAMES with their own ids (the card's
SG list), and a leg booked against the main id is a different bet.

NO SPORTRADAR ID. Nothing on their card carries one, so the site pairs 1xBet
fixtures by names and kickoff only.
"""

import collections
import json
import logging
import re
import time
from datetime import datetime, timezone

from curl_cffi import requests

log = logging.getLogger(__name__)

SITE = "https://1xbet.ng"
API = SITE + "/service-api"
QS = "lng=en&tf=2200000&tz=1&country=159&partner=159"
PAUSE = 0.45
# THEIRS: 51 selections answers ErrorCode 157972, "the number of events in a
# coupon cannot exceed 50" (29 Sep 2026).
BETSLIP_MAX = 50
THROTTLED = 161627

# --- the markets we model ---------------------------------------------------
# code -> (period, T, P), all strings. Read off their card and named by their
# own coupon read-back (tools/xbharvest.py, 29 Sep 2026): G1 "1x2" W1/X/W2 is
# T1/2/3; G8 "Double Chance" is T4 1X, T5 12, T6 2X - NOT the order the names
# imply; G17 "Total" over/under is T9/T10 with the line in P; G15 "Total 1" is
# the home side's total (T11/12) and G62 "Total 2" the away side's (T13/14);
# G19 is both-teams-to-score, T180 yes / T181 no. First half is the "1st half"
# subgame, so FH_OVER_0.5 is that game's T9 at 0.5.
MARKET_MAP = {
    "1": ("", "1", None), "X": ("", "2", None), "2": ("", "3", None),
    "1X": ("", "4", None), "12": ("", "5", None), "X2": ("", "6", None),
    "OVER_1.5": ("", "9", "1.5"), "OVER_2.5": ("", "9", "2.5"), "OVER_3.5": ("", "9", "3.5"),
    "GG": ("", "180", None),
    "FH_OVER_0.5": ("1st half", "9", "0.5"),
    "HOME_OVER_0.5": ("", "11", "0.5"), "HOME_OVER_1.5": ("", "11", "1.5"),
    "AWAY_OVER_0.5": ("", "13", "0.5"), "AWAY_OVER_1.5": ("", "13", "1.5"),
    # The unders, to de-vig - the builders never offer these.
    "UNDER_1.5": ("", "10", "1.5"), "UNDER_2.5": ("", "10", "2.5"), "UNDER_3.5": ("", "10", "3.5"), "UNDER_4.5": ("", "10", "4.5"),
    "NG": ("", "181", None),
    "FH_UNDER_0.5": ("1st half", "10", "0.5"),
    "HOME_UNDER_0.5": ("", "12", "0.5"), "HOME_UNDER_1.5": ("", "12", "1.5"),
    "AWAY_UNDER_0.5": ("", "14", "0.5"), "AWAY_UNDER_1.5": ("", "14", "1.5"),
}

# GENERATED, NEVER TYPED - tools/xbgen.py over tools/xbcat.json (the eight
# deepest cards on 29 Sep 2026: Roma-Real Madrid down to Aston Villa-Brentford,
# about 1,190 outcomes each). Every entry was proposed from our code's meaning
# in THEIR group and outcome names, and kept only because it is priced on one
# of those cards. Handicap signs verified against prices within a card: Asian
# 436 to 0 (seven pairs at the 1.01 floor set aside), European 28 to 0.
# Re-run both tools to change it; do not edit by hand.
PASSTHROUGH_MAP = {
    "AH_1_-0.25": ("", "3829", "-0.25"),
    "AH_1_-0.75": ("", "3829", "-0.75"),
    "AH_1_-1": ("", "7", "-1"),
    "AH_1_-1.25": ("", "3829", "-1.25"),
    "AH_1_-1.5": ("", "7", "-1.5"),
    "AH_1_-1.75": ("", "3829", "-1.75"),
    "AH_1_-2": ("", "7", "-2"),
    "AH_1_-2.25": ("", "3829", "-2.25"),
    "AH_1_-2.5": ("", "7", "-2.5"),
    "AH_1_-3": ("", "7", "-3"),
    "AH_1_-3.5": ("", "7", "-3.5"),
    "AH_1_0.25": ("", "3829", "0.25"),
    "AH_1_0.75": ("", "3829", "0.75"),
    "AH_1_1": ("", "7", "1"),
    "AH_1_1.25": ("", "3829", "1.25"),
    "AH_1_1.5": ("", "7", "1.5"),
    "AH_1_1.75": ("", "3829", "1.75"),
    "AH_1_2": ("", "7", "2"),
    "AH_1_2.25": ("", "3829", "2.25"),
    "AH_1_2.5": ("", "7", "2.5"),
    "AH_1_3": ("", "7", "3"),
    "AH_1_3.5": ("", "7", "3.5"),
    "AH_2_-0.25": ("", "3830", "0.25"),
    "AH_2_-0.75": ("", "3830", "0.75"),
    "AH_2_-1": ("", "8", "1"),
    "AH_2_-1.25": ("", "3830", "1.25"),
    "AH_2_-1.5": ("", "8", "1.5"),
    "AH_2_-1.75": ("", "3830", "1.75"),
    "AH_2_-2": ("", "8", "2"),
    "AH_2_-2.25": ("", "3830", "2.25"),
    "AH_2_-2.5": ("", "8", "2.5"),
    "AH_2_-3": ("", "8", "3"),
    "AH_2_-3.5": ("", "8", "3.5"),
    "AH_2_0.25": ("", "3830", "-0.25"),
    "AH_2_0.75": ("", "3830", "-0.75"),
    "AH_2_1": ("", "8", "-1"),
    "AH_2_1.25": ("", "3830", "-1.25"),
    "AH_2_1.5": ("", "8", "-1.5"),
    "AH_2_1.75": ("", "3830", "-1.75"),
    "AH_2_2": ("", "8", "-2"),
    "AH_2_2.25": ("", "3830", "-2.25"),
    "AH_2_2.5": ("", "8", "-2.5"),
    "AH_2_3": ("", "8", "-3"),
    "AH_2_3.5": ("", "8", "-3.5"),
    "CORNERS_A_OV_4.5": ("Corners", "13", "4.5"),
    "CORNERS_A_OV_5.5": ("Corners", "13", "5.5"),
    "CORNERS_A_UN_4.5": ("Corners", "14", "4.5"),
    "CORNERS_A_UN_5.5": ("Corners", "14", "5.5"),
    "CORNERS_H_OV_4.5": ("Corners", "11", "4.5"),
    "CORNERS_H_OV_5.5": ("Corners", "11", "5.5"),
    "CORNERS_H_UN_4.5": ("Corners", "12", "4.5"),
    "CORNERS_H_UN_5.5": ("Corners", "12", "5.5"),
    "CORNERS_OV_10.5": ("Corners", "9", "10.5"),
    "CORNERS_OV_11.5": ("Corners", "9", "11.5"),
    "CORNERS_OV_8.5": ("Corners", "9", "8.5"),
    "CORNERS_OV_9.5": ("Corners", "9", "9.5"),
    "CORNERS_UN_10.5": ("Corners", "10", "10.5"),
    "CORNERS_UN_11.5": ("Corners", "10", "11.5"),
    "CORNERS_UN_8.5": ("Corners", "10", "8.5"),
    "CORNERS_UN_9.5": ("Corners", "10", "9.5"),
    "EH_0_1_1": ("", "424", "-1"),
    "EH_0_1_2": ("", "426", "-1"),
    "EH_0_1_X": ("", "425", "-1"),
    "EH_0_2_1": ("", "424", "-2"),
    "EH_0_2_2": ("", "426", "-2"),
    "EH_0_2_X": ("", "425", "-2"),
    "EH_1_0_1": ("", "424", "1"),
    "EH_1_0_2": ("", "426", "1"),
    "EH_1_0_X": ("", "425", "1"),
    "EH_2_0_1": ("", "424", "2"),
    "EH_2_0_2": ("", "426", "2"),
    "EH_2_0_X": ("", "425", "2"),
    "EXACT_2": ("", "741", "2"),
    "EXACT_3": ("", "741", "3"),
    "EXACT_4": ("", "741", "4"),
    "EXACT_5": ("", "741", "5"),
    "EXACT_6": ("", "739", "5"),
    "EXACT_FH_0": ("1st half", "4548", None),
    "EXACT_FH_1": ("1st half", "741", "1"),
    "EXACT_FH_2": ("1st half", "741", "2"),
    "EXACT_FH_3": ("1st half", "739", "2"),
    "EXACT_SH_0": ("2nd half", "4548", None),
    "EXACT_SH_1": ("2nd half", "741", "1"),
    "EXACT_SH_2": ("2nd half", "739", "1"),
    "FH_AH_1_-1": ("1st half", "7", "-1"),
    "FH_AH_1_-1.5": ("1st half", "7", "-1.5"),
    "FH_AH_2_-1": ("1st half", "8", "1"),
    "FH_AH_2_-1.5": ("1st half", "8", "1.5"),
    "FH_AWAY_OVER_0.5": ("1st half", "13", "0.5"),
    "FH_AWAY_OVER_1.5": ("1st half", "13", "1.5"),
    "FH_AWAY_UNDER_0.5": ("1st half", "14", "0.5"),
    "FH_AWAY_UNDER_1.5": ("1st half", "14", "1.5"),
    "FH_EH_0_1_1": ("1st half", "424", "-1"),
    "FH_EH_0_1_2": ("1st half", "426", "-1"),
    "FH_EH_0_1_X": ("1st half", "425", "-1"),
    "FH_EH_1_0_1": ("1st half", "424", "1"),
    "FH_EH_1_0_2": ("1st half", "426", "1"),
    "FH_EH_1_0_X": ("1st half", "425", "1"),
    "FH_HOME_OVER_0.5": ("1st half", "11", "0.5"),
    "FH_HOME_OVER_1.5": ("1st half", "11", "1.5"),
    "FH_HOME_UNDER_0.5": ("1st half", "12", "0.5"),
    "FH_HOME_UNDER_1.5": ("1st half", "12", "1.5"),
    "FH_MIX_1_OV_1.5": ("1st half", "197", "1.5"),
    "FH_MIX_1_UN_1.5": ("1st half", "196", "1.5"),
    "FH_MIX_2_OV_1.5": ("1st half", "192", "1.5"),
    "FH_MIX_2_UN_1.5": ("1st half", "191", "1.5"),
    "FH_MIX_X_OV_1.5": ("1st half", "203", "1.5"),
    "FH_MIX_X_UN_1.5": ("1st half", "201", "1.5"),
    "FIRSTGOAL_1": ("", "388", "1"),
    "FIRSTGOAL_2": ("", "389", "1"),
    "FIRSTGOAL_FH_1": ("1st half", "388", "1"),
    "FIRSTGOAL_FH_2": ("1st half", "389", "1"),
    "FIRSTGOAL_FH_N": ("1st half", "390", "1"),
    "FIRSTGOAL_N": ("", "390", "1"),
    "FIRSTGOAL_SH_1": ("2nd half", "388", "1"),
    "FIRSTGOAL_SH_2": ("2nd half", "389", "1"),
    "FIRSTGOAL_SH_N": ("2nd half", "390", "1"),
    "HIGHHALF_1": ("", "188", None),
    "HIGHHALF_2": ("", "190", None),
    "HIGHHALF_A_1": ("", "2811", None),
    "HIGHHALF_A_2": ("", "2813", None),
    "HIGHHALF_A_E": ("", "2812", None),
    "HIGHHALF_E": ("", "189", None),
    "HIGHHALF_H_1": ("", "2808", None),
    "HIGHHALF_H_2": ("", "2810", None),
    "HIGHHALF_H_E": ("", "2809", None),
    "OVER_2": ("", "9", "2"),
    "OVER_3": ("", "9", "3"),
    "SH_AH_1_-1": ("2nd half", "7", "-1"),
    "SH_AH_1_-1.5": ("2nd half", "7", "-1.5"),
    "SH_AH_2_-1": ("2nd half", "8", "1"),
    "SH_AH_2_-1.5": ("2nd half", "8", "1.5"),
    "SH_AWAY_OVER_0.5": ("2nd half", "13", "0.5"),
    "SH_AWAY_OVER_1.5": ("2nd half", "13", "1.5"),
    "SH_AWAY_OVER_2.5": ("2nd half", "13", "2.5"),
    "SH_AWAY_UNDER_0.5": ("2nd half", "14", "0.5"),
    "SH_AWAY_UNDER_1.5": ("2nd half", "14", "1.5"),
    "SH_AWAY_UNDER_2.5": ("2nd half", "14", "2.5"),
    "SH_EH_0_1_1": ("2nd half", "424", "-1"),
    "SH_EH_0_1_2": ("2nd half", "426", "-1"),
    "SH_EH_0_1_X": ("2nd half", "425", "-1"),
    "SH_EH_0_2_X": ("2nd half", "425", "-2"),
    "SH_EH_1_0_1": ("2nd half", "424", "1"),
    "SH_EH_1_0_2": ("2nd half", "426", "1"),
    "SH_EH_1_0_X": ("2nd half", "425", "1"),
    "SH_HOME_OVER_0.5": ("2nd half", "11", "0.5"),
    "SH_HOME_OVER_1.5": ("2nd half", "11", "1.5"),
    "SH_HOME_OVER_2.5": ("2nd half", "11", "2.5"),
    "SH_HOME_UNDER_0.5": ("2nd half", "12", "0.5"),
    "SH_HOME_UNDER_1.5": ("2nd half", "12", "1.5"),
    "SH_HOME_UNDER_2.5": ("2nd half", "12", "2.5"),
    "SH_OVER_0.5": ("2nd half", "9", "0.5"),
    "SH_OVER_1.5": ("2nd half", "9", "1.5"),
    "SH_OVER_2.5": ("2nd half", "9", "2.5"),
    "SH_UNDER_0.5": ("2nd half", "10", "0.5"),
    "SH_UNDER_1.5": ("2nd half", "10", "1.5"),
    "SH_UNDER_2.5": ("2nd half", "10", "2.5"),
    "UNDER_2": ("", "10", "2"),
    "UNDER_3": ("", "10", "3"),
    "UP2_1": ("", "16684", None),
    "UP2_2": ("", "16686", None),
    "UP2_X": ("", "16685", None),
    "WINHALF_A_N": ("", "507", None),
    "WINHALF_A_Y": ("", "506", None),
    "WINHALF_H_N": ("", "505", None),
    "WINHALF_H_Y": ("", "504", None),
}

# The sweep reads the main card only. First-half prices come from the event
# fetch at booking time - a second card per fixture would double an 18 minute
# sweep for one market.
SWEEP_PERIODS = ("",)

# Every code any other book carries and this one does not, with the reason.
# Written from tools/xbmap.json's rejects; "verified absent" means eight deep
# cards were read for it. Longest prefix wins.
NOT_CARRIED = {
    'AH_': "carried, except where their card stops: whole and half lines +-1 to +-3.5 on 'Handicap' and quarter lines +-0.25 to +-2.25 on 'Asian Handicap'. Verified absent on eight deep cards: +-0.5, 0, +-2.75 and anything past +-3.5.",
    'BOTHHALVES_': "verified absent: 'Goals Scored In Both Halves' is about any goal in each half, not each half clearing a line.",
    'BOUNDS_': 'verified absent: per-team goal bounds are not sold at full time.',
    'CARD_': 'verified absent: no bookings market on any of eight deep cards.',
    'CORNERS_': 'carried, except: their total-corners lines run 8.5 to 11.5 (whole and half) - verified absent below 8.5 and above 11.5.',
    'CORNERS_A_': 'carried, except: see CORNERS_H_.',
    'CORNERS_H_': 'carried, except: their per-team corner lines are 4.5 to 5.5 only - verified absent elsewhere.',
    'CORNRANGE_': 'verified absent: corner totals are over/under lines only, never ranges.',
    'DC1UP_': 'verified absent: 1xBet runs 2UP on the 1X2 (1X2 (2UP)), not 1UP on double chance.',
    'DC2_': 'verified absent: no 2UP on double chance.',
    'DNB_': 'verified absent: no draw-no-bet market, and no handicap line at 0 on a full-time card (their 0 lines exist on corners only).',
    'EARLY_': "verified absent: their early market is 'Goal In First 5 Minutes', a different window from ours.",
    'EH_': 'carried at a head start of one or two goals. Three and more are not read yet: none appeared on the eight harvested cards, but a lopsided fixture sells them (Arsenal-Leeds carried -3 on 29 Sep) - re-harvest with one before mapping them.',
    'EXACT_': 'carried from 2 to 6+ through their 3-way total; verified absent: exactly 0 or exactly 1 at full time.',
    'EXGOALS_': 'verified absent: a bet on the total being anything but n is not sold.',
    'FH_AH_': 'carried at -1 and -1.5 for either side; verified absent: -0.5, 0, +0.5 and -2. Their first-half card sells +-1 and +-1.5 (and quarter lines no half code of ours uses).',
    'FH_AWAY_': 'carried, except: see FH_HOME_.',
    'FH_CARD': 'verified absent: see CARD_.',
    'FH_EH_': 'carried at a one-goal head start; verified absent at two in the first half.',
    'FH_HOME_': 'carried, except: first-half team totals stop at 2 - verified absent at 2.5.',
    'GOALRANGE_': "verified absent: 'Total From 2 To 3' exists on the halves only.",
    'HALFCORNER_': 'verified absent: no half-versus-half corner bet.',
    'HMC_': 'verified absent: see CARD_.',
    'MARGIN_': 'verified absent: no winning-margin market on any of eight deep cards.',
    'MIX_': 'verified absent: ours is an OR bet ("home OR over 1.5"); 1xBet sells only "Team 1 To Win And Total >", the AND bet, which is narrower. Never map one onto the other.',
    'MIXGG_': 'verified absent: ours is an OR bet ("draw OR both score"); 1xBet sells only "X And Both Teams To Score", the AND bet.',
    'MIXNG_': 'verified absent: ours is an OR bet ("draw OR not both score"); 1xBet sells only the AND bet.',
    'PEN_': "verified absent: only 'Penalty In First 5 Minutes' is sold, not a match penalty market.",
    'SHOTS_': "verified absent: shots appear only under Players' stats, per player.",
    'SH_AH_': 'carried at -1 and -1.5 for either side; verified absent: -0.5, 0, +0.5 and -2. Their second-half card sells +-1 and +-1.5 (and quarter lines no half code of ours uses).',
    'SH_EH_': 'carried at a one-goal head start; verified absent at two on every outcome in the second half.',
    'TEAMGOALS_': 'verified absent: per-team exact goals exist for the halves only, and ours are full time.',
    'UP1_': 'verified absent: 1xBet runs 2UP only, never 1UP.',
}


def _p(v):
    """A param as the one string every table keys on: 2.5 -> "2.5", 1.0 -> "1"."""
    if v is None:
        return None
    return format(float(v), "g")


def _key(period, t, p):
    return (period or "", str(t), _p(p))


def _by_key():
    # Derived, never typed: the modelled table last so its names win.
    return {_key(*v): code
            for code, v in list(PASSTHROUGH_MAP.items()) + list(MARKET_MAP.items())}


def market_for(code):
    """The one place that answers "do we carry this market on 1xBet?".

    NEVER a default: an unmapped market that falls back to something plausible
    books a bet nobody asked for, and the book answers success.
    """
    if not code:
        return None
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


def code_for(period, t, p):
    return _by_key().get(_key(period, t, p))


def reason_uncarried(code):
    """Why 1xBet does not carry one of our codes, or None if it does. Longest prefix wins."""
    if market_for(code):
        return None
    best = None
    for prefix, why in NOT_CARRIED.items():
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, why)
    return best[1] if best else None


_session_obj = None


def _session():
    """One warmed session. The football page sets the cookies the API wants."""
    global _session_obj
    if _session_obj is None:
        s = requests.Session(impersonate="chrome")
        s.get(SITE + "/en/line/football", timeout=30)
        _session_obj = s
    return _session_obj


def _reset_session():
    global _session_obj
    _session_obj = None


def _get_json(path, attempts=3):
    """One GET, retried with a long backoff: these are throttles, not blips."""
    last = None
    for n in range(attempts):
        try:
            r = _session().get(API + path, timeout=40)
            return r.json()
        except Exception as ex:                  # noqa: BLE001 - upstream
            last = ex
            _reset_session()
            if n + 1 < attempts:
                time.sleep(3 * (n + 1))
    raise last


# Their board lists simulated and duplicate competitions beside the real ones,
# under the same club names. "Alternative Matches" is 1,141 of 2,859 listed
# games (29 Sep 2026): re-listings of the same fixtures with other lines, so a
# converter could otherwise pair a leg with the wrong copy.
_VIRTUAL = re.compile(r"alternative|srl|simulated|e-?sports?|cyber|virtual|fantasy|"
                      r"statistic|special|team vs|vs player|short football|\dx\d", re.I)


def _is_virtual(league):
    return bool(_VIRTUAL.search(league or ""))


def _iso(unix):
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(game):
    """The empty shell of one fixture, in the shape the other four books use."""
    kickoff = _iso(game["S"]) if game.get("S") else ""
    sub = {}
    for sg in game.get("SG") or []:
        period = "|".join(x for x in (sg.get("TG") or "", sg.get("PN") or "") if x)
        if period and period not in sub:
            sub[period] = int(sg["I"])
    return {
        "eventId": str(game["I"]),
        "slotId": str(game["I"]),
        "eventCode": "",
        "srId": None,
        "teams": "{} - {}".format(game.get("O1") or "", game.get("O2") or ""),
        "kickoff": kickoff,
        "league": game.get("L") or "",
        "country": game.get("CN") or "",
        "startdate": kickoff[:10],
        "odds": {}, "raw": {}, "sel": {}, "sub": sub,
    }


def _absorb(row, card, period=""):
    """Fold one card's outcomes into its row. `period` names the subgame the
    card belongs to ("" for full time)."""
    by_key = _by_key()
    gid = int(card["I"])
    for grp in card.get("GE") or []:
        for rows in grp.get("E") or []:
            for e in rows:
                code = by_key.get(_key(period, e.get("T"), e.get("P")))
                if not code:
                    continue
                try:
                    coef = float(e.get("C"))
                except (TypeError, ValueError):
                    continue
                # A blocked outcome stays on the card with its price collapsed.
                if coef <= 1.01 or e.get("B"):
                    continue
                row["odds"][code] = coef
                row["raw"][code] = str(coef)
                row["sel"][code] = {"gameId": gid, "T": int(e["T"]),
                                    "P": e.get("P"), "coef": coef}


def fetch_card(game_id):
    try:
        v = _get_json(f"/LineFeed/GetGameZip?id={int(game_id)}&isSubGames=true&GroupEvents=true"
                      f"&countevents=500&grMode=4&marketType=1&{QS}")
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("1xbet card %s failed: %s", game_id, ex)
        return None
    return (v or {}).get("Value") or None


def all_fixtures(deadline_s=2100):
    """The sweep: every real competition, nearest kick-offs first.

    Their game list carries NO prices, so every fixture costs one card request.
    Measured 29 Sep 2026: about 1,600 real games, 0.22s a card plus our 0.45s
    pause, so roughly 18 minutes a sweep. The deadline sits inside the 45 minute
    refresh. A sweep that hits it keeps the nearest dates (the ones readers
    book) and reports how many it skipped, so a cut-short sweep never reads as
    a quiet day.
    """
    t0 = time.monotonic()
    champs = (_get_json(f"/LineFeed/GetChampsZip?sport=1&{QS}") or {}).get("Value") or []
    listing = []
    for c in champs:
        if _is_virtual(c.get("L")):
            continue
        time.sleep(PAUSE)
        try:
            games = ((_get_json(f"/LineFeed/GetChampZip?champ={int(c['LI'])}&{QS}") or {})
                     .get("Value") or {}).get("G") or []
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("1xbet champ %s failed: %s", c.get("LI"), ex)
            continue
        for g in games:
            g.setdefault("L", c.get("L"))
            listing.append(g)
    listing.sort(key=lambda g: g.get("S") or 0)

    out, read, skipped = {}, 0, 0
    for g in listing:
        if time.monotonic() - t0 > deadline_s:
            skipped += 1
            continue
        time.sleep(PAUSE)
        card = fetch_card(g["I"])
        if not card:
            continue
        read += 1
        card.setdefault("L", g.get("L"))
        row = _row(card)
        _absorb(row, card)
        if row["odds"]:
            out[row["eventId"]] = row
    log.info("1xbet swept %d fixtures (%d cards read, %d skipped at the deadline)",
             len(out), read, skipped)
    return out, {"listed": len(listing), "read": read, "skipped": skipped}


# --- booking ----------------------------------------------------------------

def _post(path, body, timeout=30):
    r = _session().post(API + path, data=json.dumps(body), timeout=timeout, headers={
        "Content-Type": "application/json", "Origin": SITE,
        "Referer": SITE + "/en/line/football"})
    return r.json()


def _periods_needed():
    return sorted({v[0] for v in list(MARKET_MAP.values()) + list(PASSTHROUGH_MAP.values())} - {""})


def periods_for(codes):
    """The periods a set of our codes lives on, full time first."""
    got = {(market_for(c) or ("",))[0] for c in codes} - {""}
    return [""] + sorted(got)


def fetch_event(event_id, periods=None):
    """The markets we carry on one fixture, read fresh for booking.

    ONLY THE PERIODS ASKED FOR. Reading all three subgames with a pause
    before each cost ~2.2s a game, and past six legs the site's 15s proxy
    timed out while a code was minted behind it (review, 29 Sep 2026). A
    full-time slip reads the main card and nothing else; `None` still means
    every period we carry, for tools/xbverify.py.
    """
    card = fetch_card(event_id)
    if not card:
        return None
    row = _row(card)
    _absorb(row, card)
    wanted = _periods_needed() if periods is None else [p for p in periods if p]
    for period in wanted:
        gid = row["sub"].get(period)
        if not gid:
            continue
        time.sleep(PAUSE)
        sub = fetch_card(gid)
        if sub:
            _absorb(row, sub, period=period)
    return row if row["odds"] else None


def build_selection(event, code):
    """One leg, in their Events shape. The game id is the SUBGAME's for a half
    or corners market - booking it against the main game is a different bet."""
    sel = (event.get("sel") or {}).get(code)
    if sel is None:
        raise KeyError("no {} on event {}".format(code, event.get("eventId")))
    p = sel.get("P")
    return {"GameId": int(sel["gameId"]), "Type": int(sel["T"]), "Coef": float(sel["coef"]),
            "Param": float(p) if p is not None else 0, "PV": None, "PlayerId": 0,
            "Kind": 3, "InstrumentId": 0, "Seconds": 0, "Price": 0, "Expired": 0,
            "PlayersDuel": []}


def _period_of(ev):
    """The subgame a read-back leg belongs to, in the same words as the card's SG.
    Read-back carries it as GameType ("Corners") and PeriodName ("1st half"),
    seen on code XXXGS, 29 Sep 2026."""
    return "|".join(x for x in (ev.get("GameType") or "", ev.get("PeriodName") or "") if x)


def read_coupon(code):
    """The legs behind a 1xBet booking code, in OUR vocabulary - the contract
    bet9ja/betking/betpawa.read_coupon answer on, so /api/slip needs no fifth dialect.

    `eventId` is the MAIN game, so a first-half leg pairs with the same board
    fixture as a full-time one. `odds` is their LIVE price: the read-back
    reprices every leg whatever was sent.
    """
    try:
        body = _post("/LiveBet/Open/GetCoupon", {"Guid": code, "Lng": "en", "partner": 159})
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("1xbet coupon read failed: %s", ex)
        return {"error": f"request failed: {ex}"}
    if not isinstance(body, dict) or not body.get("Success"):
        # 100849 "Incorrect code"; 159271 "events ... have finished". Either
        # way there is no slip to show.
        return {"error": "not found", "notFound": True}
    out = []
    for e in (body.get("Value") or {}).get("Events") or []:
        try:
            odds = float(e.get("Coef"))
        except (TypeError, ValueError):
            odds = None
        start = e.get("Start")
        out.append({
            "eventId": str(e.get("MainGameId") or e.get("GameId")),
            # A single-row market reads back Param 0, where the table keys it None.
            "prediction": code_for(_period_of(e), e.get("Type"), e.get("Param") or None),
            "raw": "{}/{}".format(e.get("GroupName") or "", e.get("MarketName") or ""),
            "home": e.get("Opp1Eng") or e.get("Opp1") or "",
            "away": e.get("Opp2Eng") or e.get("Opp2") or "",
            "league": e.get("ChampNameEng") or e.get("Liga") or "",
            "kickoff": _iso(start) if start else "",
            "odds": odds,
        })
    if not out:
        return {"error": "not found", "notFound": True}
    return {"legs": out, "available": len(out), "removed": [], "booked": len(out)}


def read_code(code):
    got = read_coupon(code)
    if got.get("error"):
        return 0, []
    return got["available"], got["legs"]


def generate_code(selections):
    """Turn [{event, code}] into a 1xBet booking code, and check it."""
    if not selections:
        return {"error": "no selections"}
    if len(selections) > BETSLIP_MAX:
        return {"error": f"1xbet slips hold at most {BETSLIP_MAX:d} selections",
                "sent": len(selections)}
    # ONE LEG PER GAME, keyed on the MAIN game. Their save accepts a same-game
    # pair, but their betslip marks both legs "Incompatible event" and the
    # punter cannot place it (seen in the browser, 29 Sep 2026). A first-half
    # leg is on the same game as a full-time one.
    seen, dupes = set(), []
    for s in selections:
        eid = str((s.get("event") or {}).get("eventId") or "")
        if eid in seen:
            dupes.append(s.get("code"))
        seen.add(eid)
    if dupes:
        return {"error": "1xbet takes one selection per game on a multiple ({})".format(
                    ", ".join(str(d) for d in dupes)), "sent": len(selections)}
    try:
        events = [build_selection(s["event"], s["code"]) for s in selections]
    except (KeyError, TypeError, ValueError) as ex:
        return {"error": f"could not build selection: {ex}"}

    body = {"notWait": True, "CheckCf": 1, "partner": 159, "AntiExpressCoef": 1,
            "Summ": 0, "Vid": 1, "Events": events}
    # THEIR THROTTLE ANSWERS 161627, "call failed, try later". 12 of 155
    # back-to-back one-leg mints got it on 29 Sep and 11 of those booked
    # cleanly four seconds later - so it is waited out, twice, with a long
    # backoff. (The same code also answers a bogus game id, which a retry
    # cannot fix; that one comes back as the error after the last try.)
    for attempt in range(3):
        try:
            saved = _post("/LiveBet/Open/SaveCoupon", body)
        except Exception as ex:                  # noqa: BLE001 - upstream
            return {"error": f"request failed: {ex}"}
        saved = saved if isinstance(saved, dict) else {}
        if saved.get("ErrorCode") != THROTTLED or attempt == 2:
            break
        time.sleep(4 * (attempt + 1))
    code = saved.get("Value") if saved.get("Success") else None
    if not code:
        return {"error": str(saved.get("Error") or saved)[:400],
                "errorCode": saved.get("ErrorCode"), "sent": len(events)}

    # READ IT BACK, ALWAYS. A bogus type mints a code that reads back empty, and
    # 30 legs came back as 29 with no error (29 Sep 2026).
    got = read_coupon(code)
    if got.get("error") and not got.get("notFound"):
        # OUR read failed (network), not their slip: the code stands, unchecked.
        # Reporting it as "0 of N legs" blamed a slip nobody had looked at.
        log.warning("1xbet read-back failed for %s: %s", code, got.get("error"))
        return {"code": code, "odds": None, "legs": len(events), "verified": False}
    legs = got.get("legs") or []
    # EVERY LEG, NOT JUST THE COUNT. A leg booked against the wrong game, or
    # one they re-lined, keeps the count and is still a different slip. Each
    # leg read back must decode to the code sent on the event it was sent for;
    # the ones that do not are named so the client drops exactly those.
    sent = [(str(s["event"].get("eventId")), s["code"]) for s in selections]
    back = collections.Counter((str(l["eventId"]), l["prediction"]) for l in legs)
    missing = []
    for key in sent:
        if back[key] > 0:
            back[key] -= 1
        else:
            missing.append({"eventId": key[0], "prediction": key[1]})
    if missing or len(legs) != len(events):
        return {"error": f"1xbet accepted the slip and returned a code holding "
                         f"{len(events) - len(missing)} of {len(events)} legs as sent",
                "code": code, "sent": len(events), "available": len(legs),
                "missing": missing}
    # A leg they read back with no price makes the total unknown - counting it
    # as 1.0 understated what the slip pays.
    total = 1.0
    for leg in legs:
        if not leg.get("odds"):
            total = None
            break
        total *= leg["odds"]
    return {"code": code, "odds": round(total, 2) if total else None,
            "legs": len(events), "verified": True}
