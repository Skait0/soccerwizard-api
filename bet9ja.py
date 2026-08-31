"""Bet9ja: odds and booking codes.

Sits beside the SportyBet code in server.py rather than replacing it. Bet9ja
rivals SportyBet for users in Nigeria, and a booking code is only useful to
somebody who holds an account with the bookmaker that issued it - so the site
needs both, not a better one.

It is the easier of the two integrations, which was not the expectation:

  odds      a plain GET; SportyBet needs curl_cffi faking a Chrome TLS
            fingerprint, this needs a Referer header and nothing else
  markets   self-describing keys ("S_DC_1X"); SportyBet needs a hand-built
            table mapping (marketId, outcomeId, specifier) triples
  booking   a form POST, anonymous, same as SportyBet's orders/share

Everything here was read off their own endpoints and their public bundle on
1 Sep 2026. Nothing is inferred - an earlier guess about how an odds key splits
turned out to be wrong (see parse_odds_key), which is exactly the sort of thing
that produces a rejected payload with nothing to explain it.
"""

import json
import logging

import requests

log = logging.getLogger(__name__)

BASE = "https://sports.bet9ja.com/desktop/feapi/PalimpsestAjax"
BOOK_URL = "https://apigw.bet9ja.com/sportsbook/placebet/BookABetV2"

# Their CDN and feed both refuse a request with no Referer.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _values(node):
    """Their collections arrive as a dict or a list depending on the group."""
    if isinstance(node, dict):
        return list(node.values())
    if isinstance(node, list):
        return node
    return []


def _items(node):
    """(key, value) pairs whichever shape a collection came back as. The key
    is the slot id, which the betslip needs, so a list falls back to its
    index."""
    if isinstance(node, dict):
        return list(node.items())
    if isinstance(node, list):
        return [(str(i), v) for i, v in enumerate(node)]
    return []


def _headers(extra=None):
    h = {"User-Agent": _UA, "Referer": "https://sports.bet9ja.com/",
         "Accept": "application/json, text/plain, */*"}
    if extra:
        h.update(extra)
    return h


# Which market group a code lives in. NOT a small integer, and the values are
# not guessable: a sweep of 1-8 finds only group 1 and would send you looking
# for a bug that is not there. 170 was found by clicking the Home/Away tab and
# reading the request the page made.
POPULAR = 1
HOME_AWAY = 170

# Our market codes -> (Bet9ja odds key, which market group carries it).
#
# The naming is not symmetrical: home-team-scores is S_HTS_Y but away is
# S_AWAYSCORE_Y. Anyone writing a tidy mirrored mapper gets caught by that.
MARKET_MAP = {
    "1":             ("S_1X2_1", POPULAR),
    "X":             ("S_1X2_X", POPULAR),
    "2":             ("S_1X2_2", POPULAR),
    "1X":            ("S_DC_1X", POPULAR),
    "12":            ("S_DC_12", POPULAR),
    "X2":            ("S_DC_X2", POPULAR),
    "OVER_1.5":      ("S_OU@1.5_O", POPULAR),
    "OVER_2.5":      ("S_OU@2.5_O", POPULAR),
    "OVER_3.5":      ("S_OU@3.5_O", POPULAR),
    "HOME_OVER_0.5": ("S_HTS_Y", HOME_AWAY),
    "AWAY_OVER_0.5": ("S_AWAYSCORE_Y", HOME_AWAY),
}

# The groups we actually need to fetch, derived rather than restated so adding
# a market cannot leave its group unfetched.
MARKET_GROUPS = sorted({g for _k, g in MARKET_MAP.values()})

_BY_KEY = {key: code for code, (key, _g) in MARKET_MAP.items()}


def parse_odds_key(key):
    """Split "S_OU@1.5_O" into the three fields a selection needs.

    Lifted from their bundle, not guessed. The first guess here was that the
    key split at the last underscore, giving a market of "S_OU@1.5". It does
    not: the handicap is pulled out into its own field and the market is just
    "S_OU". A selection built the wrong way is rejected with nothing useful in
    the response, so this is worth being exact about.

        S_DC_1X        -> ("S_DC",        "1X", None)
        S_OU@1.5_O     -> ("S_OU",        "O",  "1.5")
        S_HTS_Y        -> ("S_HTS",       "Y",  None)
        S_AWAYSCORE_Y  -> ("S_AWAYSCORE", "Y",  None)
    """
    parts = key.split("_")
    if len(parts) < 3:
        raise ValueError("not a Bet9ja odds key: %r" % (key,))
    market, aux = parts[1], None
    if "@" in market:
        market, aux = market.split("@", 1)
    return parts[0] + "_" + market, parts[2], aux


def fetch_events(league_id, group=POPULAR, timeout=15):
    """Every event in one league, for one market group.

    Returns {eventId: {...}} with the teams as a single "Home - Away" string,
    a UTC kickoff, and the odds keyed by market. Empty dict on any failure -
    this feeds a page, and a bookmaker being unreachable is not a reason to
    return a 500.
    """
    url = ("%s/GetEventsInCouponV2?SCHID=%s&DISP=0&MKEY=%s"
           % (BASE, league_id, group))
    try:
        r = requests.get(url, headers=_headers(), timeout=timeout)
        data = r.json()
    except Exception as ex:
        log.warning("bet9ja events %s/%s failed: %s", league_id, group, ex)
        return {}

    out = {}
    for grp in _values(data.get("D", {}).get("G", {})):
        # `E` comes back as a dict keyed by slot id on some groups and as a
        # plain list on others. Both carry the same event objects.
        for eid, ev in _items(grp.get("E")):
            odds = {}
            for key, val in (ev.get("O") or {}).items():
                code = _BY_KEY.get(key)
                if not code:
                    continue
                try:
                    odds[code] = float(val)
                except (TypeError, ValueError):
                    continue  # "-" and "" both appear for a suspended market
            if not odds:
                continue
            row = out.setdefault(str(ev.get("ID") or eid), {
                "eventId": ev.get("ID"),
                "slotId": eid,
                "teams": ev.get("DS") or "",
                "kickoff": ev.get("STARTDATEUTC") or "",
                "league": ev.get("GN") or grp.get("GN") or "",
                "startdate": (ev.get("STARTDATE") or "").replace("-", "/"),
                "odds": {},
            })
            row["odds"].update(odds)
    return out


def fetch_league(league_id, timeout=15):
    """One league across every market group we care about, merged.

    Two requests rather than one, because the markets we offer are split
    across groups and there is no group that carries them all.
    """
    merged = {}
    for group in MARKET_GROUPS:
        for eid, row in fetch_events(league_id, group, timeout).items():
            if eid in merged:
                merged[eid]["odds"].update(row["odds"])
            else:
                merged[eid] = row
    return merged


def build_selection(event, code):
    """One leg of a betslip, in the shape their EVS map wants."""
    key, _group = MARKET_MAP[code]
    sid, sign, aux = parse_odds_key(key)
    odd = event["odds"].get(code)
    if odd is None:
        raise KeyError("no %s on event %s" % (code, event.get("eventId")))
    return {
        "id": event["slotId"],
        "eventId": event["eventId"],
        "eventCode": event.get("eventCode"),
        # Their own UI rewrites the "A - B" the feed gives into "A v B" before
        # sending it, so this matches what their site would have posted.
        "eventName": (event.get("teams") or "").replace(" - ", " v "),
        "sid": sid,
        "sign": sign,
        "hnd": aux or "",
        "oddValue": odd,
        "startdate": event.get("startdate") or "",
        "GN": event.get("league") or "",
        "leagueName": event.get("league") or "",
        "sportName": "Soccer",
        # Their validator rejects a slip without this and names it in the
        # response: {"code":100,"message":"checkformatbetsliperror",
        # "data":{"LIVE":"not defined"}}. We only ever book fixtures that have
        # not kicked off, so it is always 0.
        "LIVE": 0,
        "isLive": False,
    }


def generate_code(selections, timeout=15):
    """Turn a list of {event, code} into a Bet9ja booking code.

    Anonymous, exactly like SportyBet's. Booking is not placing: it produces a
    shareable code, takes no stake and needs no account.
    """
    if not selections:
        return {"error": "no selections"}

    evs, odds_total = {}, 1.0
    try:
        for sel in selections:
            leg = build_selection(sel["event"], sel["code"])
            evs[leg["id"]] = leg
            odds_total *= float(leg["oddValue"])
    except (KeyError, ValueError) as ex:
        return {"error": "could not build selection: %s" % ex}

    n = len(evs)
    # ODDS is not decoration: it maps selection id -> odd, and their own code
    # fills it (`c.ODDS[sel] = odd`) before pushing the bet. Sending it empty
    # is what produced `checkformatbetsliperror`, whose `data` names "LIVE" and
    # sends you looking in the wrong place entirely.
    odds_by_sel = {sid: leg["oddValue"] for sid, leg in evs.items()}

    # Argument order taken from their builder rather than assumed. For a
    # multiple they call it with (NUMLINES=count, COMB=1, TYPE=count); for a
    # single, (1, 1, 1). An accumulator is one bet carrying every selection.
    tab = "multiples" if n > 1 else "singles"
    betslip = {
        "BETS": [{
            "BSTYPE": tab,
            "TAB": tab,
            "NUMLINES": n if n > 1 else 1,
            "COMB": 1,
            "TYPE": n if n > 1 else 1,
            # A booking carries no money - it is a shareable slip, not a bet.
            # Their own UI books with zeros in all of these.
            "STAKE": 0, "POTWINMIN": 0, "POTWINMAX": 0,
            "BONUSMIN": 0, "BONUSMAX": 0,
            "ODDMIN": round(odds_total, 2), "ODDMAX": round(odds_total, 2),
            "ODDS": odds_by_sel,
            "FIXED": {},
        }],
        "EVS": evs,
        # Their builder's last act before returning the slip.
        "IMPERSONIZE": 0,
    }

    try:
        r = requests.post(BOOK_URL, data={"BETSLIP": json.dumps(betslip)},
                          headers=_headers({
                              "Content-Type": "application/x-www-form-urlencoded"}),
                          timeout=timeout)
        body = r.json()
    except Exception as ex:
        log.warning("bet9ja booking failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    code = _find_code(body)
    if code:
        return {"code": code, "odds": round(odds_total, 2), "legs": n}
    return {"error": body if isinstance(body, (str, int)) else json.dumps(body)[:400],
            "sent": len(evs)}


def _find_code(body):
    """Pull the booking number out, wherever they put it.

    Deliberately not pinned to one path. Their response shape is undocumented
    and the one thing worth being robust about is finding a code that is
    plainly there.
    """
    if not isinstance(body, dict):
        return None
    for path in (("D", "BOOKINGNUMBER"), ("D", "CODE"), ("data", "code"),
                 ("BOOKINGNUMBER",), ("CODE",)):
        cur = body
        for step in path:
            if not isinstance(cur, dict) or step not in cur:
                cur = None
                break
            cur = cur[step]
        if isinstance(cur, (str, int)) and str(cur).strip():
            return str(cur).strip()
    return None
