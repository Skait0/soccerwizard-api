"""MSport Nigeria: the seventh book.

Recon 22 Oct 2026, read out of their own bundle (static.msport.com/dist/js/main.061937ac.js,
msversion 1.201.1) and then measured from this PC.

THEIR IDS ARE SPORTRADAR'S, WRITTEN THE WAY SPORTYBET WRITES THEM. Events are `sr:match:N`
(the same number SportyBet uses: sr:match:74165976 is the same game with the same outcome ids
on both, 22 Oct), and markets, specifiers and outcomes are the plain UOF ids - market 18
`total=2.5` outcome 12 is over 2.5 on both books. So this book carries no market table of its
own: a code means what server.MARKET_MAP / PASSTHROUGH_MAP say it means, minus NOT_CARRIED.
The URLs and JSON field names are NOT SportyBet's.

READING AND BOOKING ARE BOTH PUBLIC. No login, token or signature: a share code is minted with
one POST and read back with one GET. Their own share link is https://www.msport.com/ng?code=X
(redirects to /ng/web?code=X with the code kept, checked 22 Oct).

Every answer is {"bizCode": 10000, "data": ..., "message": ...} with HTTP 200 even on errors,
so success is bizCode 10000 and nothing else. A code that does not exist answers bizCode 19000
"The code is invalid."
"""

import json
import logging
import re
import time
from collections import Counter

from curl_cffi import requests

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"
SITE = "https://www.msport.com"
API = SITE + "/api/ng"
LIST_URL = API + "/facts-center/query/frontend/sports-matches-list"
EVENT_URL = API + "/facts-center/query/frontend/match/detail"
SHARE_URL = API + "/orders/real-sports/order/share"
OPEN_URL = SITE + "/ng?code="
OK = 10000

# Their betslip says "There cannot be over 50 selections within a betslip" (bundle module
# 33726). Not yet measured against the server; the client limit is the honest cap until it is.
BETSLIP_MAX = 50

# The modelled markets' ids (server.MARKET_MAP), asked for on every list page so the sweep
# carries their prices without a per-event fetch. The list answers ~50 events a page.
SWEEP_MARKETS = (1, 10, 18, 19, 20, 29, 33, 34, 68)

# SportyBet codes MSport does not sell, generated 22 Oct 2026 by keeping each SportyBet code
# only if its exact (market, outcome, specifier) was priced on one of MSport's eight deepest
# football cards that day (Aston Villa, Coventry, RB Leipzig, Ipswich, Cagliari, Liverpool,
# Chelsea, Dortmund - 9,825 priced triples). Every MODELLED code is carried. Most of what is
# missing is SportyBet's own shots markets (9005xx ids, 102 codes), corner and Asian handicap
# lines they did not list, and cards. "Not on eight deep cards" is the reason for each; a code
# that turns up on their card later is a regeneration, not a hand edit.
_NOT_SEEN = "not priced on MSport's eight deepest football cards, 22 Oct 2026"
NOT_CARRIED = {}
NOT_CARRIED.update({c: _NOT_SEEN for c in (
    "AH_1_-1", "AH_1_-2", "AH_1_-3", "AH_1_-3.5", "AH_1_-4", "AH_1_-4.5", "AH_1_0", "AH_1_1",
    "AH_1_2", "AH_1_3", "AH_1_3.5", "AH_1_4", "AH_1_4.5", "AH_1_5", "AH_2_-1", "AH_2_-2",
    "AH_2_-3", "AH_2_-3.5", "AH_2_-4", "AH_2_-4.5", "AH_2_0", "AH_2_1", "AH_2_2", "AH_2_3",
    "AH_2_3.5", "AH_2_4", "AH_2_4.5", "AH_2_5", "BOUNDS_A_0", "BOUNDS_A_1", "BOUNDS_A_11",
    "BOUNDS_A_12", "BOUNDS_A_13", "BOUNDS_A_2", "BOUNDS_A_22", "BOUNDS_A_23", "BOUNDS_A_33",
    "BOUNDS_H_0", "BOUNDS_H_1", "BOUNDS_H_11", "BOUNDS_H_12", "BOUNDS_H_13", "BOUNDS_H_2",
    "BOUNDS_H_22", "BOUNDS_H_23", "BOUNDS_H_33", "CARD_A_1", "CARD_A_2", "CARD_A_3", "CARD_A_4",
    "CARD_A_5", "CARD_A_6", "CARD_H_1", "CARD_H_2", "CARD_H_3", "CARD_H_4", "CARD_H_5", "CARD_H_6",
    "CORNERS_A_OV_0.5", "CORNERS_A_OV_1.5", "CORNERS_A_OV_2.5", "CORNERS_A_OV_3.5",
    "CORNERS_A_OV_4.5", "CORNERS_A_OV_5.5", "CORNERS_A_OV_6.5", "CORNERS_A_OV_7.5",
    "CORNERS_A_OV_8.5", "CORNERS_A_OV_9.5", "CORNERS_A_UN_0.5", "CORNERS_A_UN_1.5",
    "CORNERS_A_UN_2.5", "CORNERS_A_UN_3.5", "CORNERS_A_UN_4.5", "CORNERS_A_UN_5.5",
    "CORNERS_A_UN_6.5", "CORNERS_A_UN_7.5", "CORNERS_A_UN_8.5", "CORNERS_A_UN_9.5",
    "CORNERS_H_OV_0.5", "CORNERS_H_OV_1.5", "CORNERS_H_OV_2.5", "CORNERS_H_OV_3.5",
    "CORNERS_H_OV_4.5", "CORNERS_H_OV_5.5", "CORNERS_H_OV_6.5", "CORNERS_H_OV_7.5",
    "CORNERS_H_OV_8.5", "CORNERS_H_OV_9.5", "CORNERS_H_UN_0.5", "CORNERS_H_UN_1.5",
    "CORNERS_H_UN_2.5", "CORNERS_H_UN_3.5", "CORNERS_H_UN_4.5", "CORNERS_H_UN_5.5",
    "CORNERS_H_UN_6.5", "CORNERS_H_UN_7.5", "CORNERS_H_UN_8.5", "CORNERS_H_UN_9.5", "DC1UP_12",
    "DC1UP_1X", "DC1UP_X2", "EARLY_OV_10_1.5", "EARLY_OV_30_2.5", "EARLY_OV_50_3.5",
    "EARLY_UN_10_1.5", "EARLY_UN_30_2.5", "EARLY_UN_50_3.5", "EXACT_0", "EXACT_1", "EXACT_2",
    "EXACT_3", "EXACT_4", "EXACT_5", "EXACT_6", "EXGOALS_0", "EXGOALS_1", "EXGOALS_2", "EXGOALS_3",
    "EXGOALS_4", "EXGOALS_5", "EXGOALS_FH_0", "EXGOALS_FH_1", "EXGOALS_FH_2", "EXGOALS_FH_3",
    "FH_AH_1_-1", "FH_AH_1_-2", "FH_AH_1_0", "FH_AH_2_-1", "FH_AH_2_-2", "FH_AH_2_0",
    "FH_CARDUN_A_1", "FH_CARDUN_A_2", "FH_CARDUN_A_3", "FH_CARDUN_H_1", "FH_CARDUN_H_2",
    "FH_CARDUN_H_3", "FH_CARD_A_1", "FH_CARD_A_2", "FH_CARD_A_3", "FH_CARD_H_1", "FH_CARD_H_2",
    "FH_CARD_H_3", "GG2_N", "GG2_Y", "MIXGG_1", "MIXGG_2", "MIXGG_X", "MIXNG_1", "MIXNG_2",
    "MIXNG_X", "MIX_1_OV_2.5", "MIX_1_UN_2.5", "MIX_2_OV_2.5", "MIX_2_UN_2.5", "MIX_X_OV_2.5",
    "MIX_X_UN_2.5", "NR3_A", "NR3_ANY", "NR3_H", "OVER_2", "OVER_3", "R3_A", "R3_ANY", "R3_H",
    "SHOTS_A_OV_10.5", "SHOTS_A_OV_11.5", "SHOTS_A_OV_12.5", "SHOTS_A_OV_13.5", "SHOTS_A_OV_14.5",
    "SHOTS_A_OV_15.5", "SHOTS_A_OV_16.5", "SHOTS_A_OV_17.5", "SHOTS_A_OV_18.5", "SHOTS_A_OV_19.5",
    "SHOTS_A_OV_20.5", "SHOTS_A_OV_21.5", "SHOTS_A_OV_22.5", "SHOTS_A_OV_4.5", "SHOTS_A_OV_5.5",
    "SHOTS_A_OV_6.5", "SHOTS_A_OV_7.5", "SHOTS_A_OV_8.5", "SHOTS_A_OV_9.5", "SHOTS_A_UN_10.5",
    "SHOTS_A_UN_11.5", "SHOTS_A_UN_12.5", "SHOTS_A_UN_13.5", "SHOTS_A_UN_14.5", "SHOTS_A_UN_15.5",
    "SHOTS_A_UN_16.5", "SHOTS_A_UN_17.5", "SHOTS_A_UN_18.5", "SHOTS_A_UN_19.5", "SHOTS_A_UN_20.5",
    "SHOTS_A_UN_21.5", "SHOTS_A_UN_22.5", "SHOTS_A_UN_4.5", "SHOTS_A_UN_5.5", "SHOTS_A_UN_6.5",
    "SHOTS_A_UN_7.5", "SHOTS_A_UN_8.5", "SHOTS_A_UN_9.5", "SHOTS_H_OV_10.5", "SHOTS_H_OV_11.5",
    "SHOTS_H_OV_12.5", "SHOTS_H_OV_13.5", "SHOTS_H_OV_14.5", "SHOTS_H_OV_15.5", "SHOTS_H_OV_16.5",
    "SHOTS_H_OV_17.5", "SHOTS_H_OV_18.5", "SHOTS_H_OV_19.5", "SHOTS_H_OV_20.5", "SHOTS_H_OV_21.5",
    "SHOTS_H_OV_22.5", "SHOTS_H_OV_4.5", "SHOTS_H_OV_5.5", "SHOTS_H_OV_6.5", "SHOTS_H_OV_7.5",
    "SHOTS_H_OV_8.5", "SHOTS_H_OV_9.5", "SHOTS_H_UN_10.5", "SHOTS_H_UN_11.5", "SHOTS_H_UN_12.5",
    "SHOTS_H_UN_13.5", "SHOTS_H_UN_14.5", "SHOTS_H_UN_15.5", "SHOTS_H_UN_16.5", "SHOTS_H_UN_17.5",
    "SHOTS_H_UN_18.5", "SHOTS_H_UN_19.5", "SHOTS_H_UN_20.5", "SHOTS_H_UN_21.5", "SHOTS_H_UN_22.5",
    "SHOTS_H_UN_4.5", "SHOTS_H_UN_5.5", "SHOTS_H_UN_6.5", "SHOTS_H_UN_7.5", "SHOTS_H_UN_8.5",
    "SHOTS_H_UN_9.5", "SHOTS_OV_19.5", "SHOTS_OV_20.5", "SHOTS_OV_21.5", "SHOTS_OV_22.5",
    "SHOTS_OV_23.5", "SHOTS_OV_24.5", "SHOTS_OV_25.5", "SHOTS_OV_26.5", "SHOTS_OV_27.5",
    "SHOTS_OV_28.5", "SHOTS_OV_29.5", "SHOTS_OV_30.5", "SHOTS_OV_31.5", "SHOTS_UN_19.5",
    "SHOTS_UN_20.5", "SHOTS_UN_21.5", "SHOTS_UN_22.5", "SHOTS_UN_23.5", "SHOTS_UN_24.5",
    "SHOTS_UN_25.5", "SHOTS_UN_26.5", "SHOTS_UN_27.5", "SHOTS_UN_28.5", "SHOTS_UN_29.5",
    "SHOTS_UN_30.5", "SHOTS_UN_31.5", "SH_AH_1_-1", "SH_AH_1_-2", "SH_AH_1_0", "SH_AH_2_-1",
    "SH_AH_2_-2", "SH_AH_2_0", "UNDER_2", "UNDER_3", "UP1_1", "UP1_2", "UP1_X", "UP2_1", "UP2_2",
    "UP2_X",
)})

_HEADERS = {
    "Accept": "application/json",
    # From their bundle. operid 2 is Nigeria (the bundle reads it from a translation key).
    "operid": "2", "clientid": "WEB", "platform": "WEB", "ApiLevel": "2",
    "Origin": SITE, "Referer": SITE + "/ng/web",
}

_FAKE = re.compile(r"\b(srl|esoccer|e-soccer|cyber|virtual|simulated)\b", re.I)


def _headers(extra=None):
    h = dict(_HEADERS)
    if extra:
        h.update(extra)
    return h


# --- the market vocabulary (SportyBet's, minus what they do not sell) -------------------

def _tables():
    import server                                   # late: server imports this module
    return server.MARKET_MAP, server.PASSTHROUGH_MAP


def market_for(code):
    """{marketId, outcomeId, specifier?} for a code this book sells, else None."""
    if code in NOT_CARRIED:
        return None
    mm, pt = _tables()
    return mm.get(code) or pt.get(code)


def reason_uncarried(code):
    return NOT_CARRIED.get(code)


_TRIPLES = {}


def _by_triple():
    """(marketId, outcomeId, specifier) -> our code, derived from the forward table."""
    if not _TRIPLES:
        mm, pt = _tables()
        for code, m in list(mm.items()) + list(pt.items()):
            if code in NOT_CARRIED:
                continue
            _TRIPLES.setdefault((str(m["marketId"]), str(m["outcomeId"]), m.get("specifier") or ""), code)
    return _TRIPLES


# --- reading ----------------------------------------------------------------------------

def _call(method, url, params=None, body=None, timeout=30, attempts=3, pause=1.5):
    last = None
    for i in range(attempts):
        try:
            if method == "GET":
                r = requests.get(url, params=params, headers=_headers(), impersonate=IMPERSONATE, timeout=timeout)
            else:
                r = requests.post(url, params=params, data=json.dumps(body),
                                  headers=_headers({"Content-Type": "application/json"}),
                                  impersonate=IMPERSONATE, timeout=timeout)
            return r.json()
        except Exception as ex:                      # noqa: BLE001 - upstream
            last = ex
            time.sleep(pause * (i + 1))
    raise last


def _iso(ms):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return ""


def _row(event):
    """One fixture, in the shape every other book's sweep answers with."""
    eid = str(event.get("eventId") or "")
    kickoff = _iso(event.get("startTime"))
    return {
        "eventId": eid,
        "slotId": eid,
        "eventCode": "",
        # Their event id IS Sportradar's, prefix and all - the same string SportyBet uses.
        "srId": eid[len("sr:match:"):] if eid.startswith("sr:match:") else "",
        "teams": "{} - {}".format(event.get("homeTeam") or "", event.get("awayTeam") or ""),
        "kickoff": kickoff,
        "league": event.get("tournament") or "",
        "country": event.get("category") or "",
        "startdate": kickoff[:10],
        "odds": {},
        "raw": {},
        "sel": {},
    }


def _absorb(row, markets):
    """Fold a card's markets into the row, outcome by outcome."""
    trip = _by_triple()
    for m in markets or []:
        if m.get("status") not in (None, 0):          # 0 = open; anything else cannot be booked
            continue
        mid, spec = str(m.get("id")), m.get("specifiers") or ""
        for o in m.get("outcomes") or []:
            code = trip.get((mid, str(o.get("id")), spec))
            if not code or o.get("isActive") == 0:
                continue
            try:
                odd = float(o.get("odds"))
            except (TypeError, ValueError):
                continue
            if odd <= 1.01:
                continue
            row["odds"][code] = odd
            row["raw"][code] = str(o.get("odds"))
            row["sel"][code] = {"marketId": int(m["id"]), "specifier": spec,
                                "outcomeId": str(o.get("id")), "odds": odd}


def fetch_page(last_event_id=None, timeout=40):
    """One page (~50 events) of the football list, with the modelled markets priced."""
    # `marketIds` (a comma list) only works WITHOUT `marketId`: sent together, marketId wins and
    # the page carries 1X2 alone (22 Oct 2026).
    params = {"sportId": "sr:sport:1", "displayStyle": "FULL",
              "marketIds": ",".join(str(m) for m in SWEEP_MARKETS)}
    if last_event_id:
        params["lastEventId"] = last_event_id
    body = _call("POST", LIST_URL, params=params, body=[], timeout=timeout)
    if (body or {}).get("bizCode") != OK:
        raise RuntimeError("msport list: {}".format((body or {}).get("message")))
    data = body.get("data") or {}
    events = [e for t in data.get("tournaments") or [] for e in t.get("events") or []]
    return events, data.get("lastEventId")


def all_fixtures(pages=60, pause=0.45, timeout=40, deadline=15 * 60):
    """The sweep: the whole football list by cursor (37 pages, 1,805 games on 22 Oct)."""
    t0, rows, seen, last, n = time.time(), [], set(), None, 0
    while n < pages and time.time() - t0 < deadline:
        events, nxt = fetch_page(last, timeout)
        n += 1
        for e in events:
            eid = e.get("eventId")
            name = "{} {} {}".format(e.get("homeTeam"), e.get("awayTeam"), e.get("tournament"))
            if not eid or eid in seen or _FAKE.search(name):
                continue
            seen.add(eid)
            row = _row(e)
            _absorb(row, e.get("markets"))
            if row["odds"]:
                rows.append(row)
        if not nxt or not events:
            break
        last = nxt
        time.sleep(pause)
    return rows, {"pages": n, "fixtures": len(rows), "seconds": round(time.time() - t0, 1)}


def fetch_event(event_id, timeout=30):
    """The full card of one game (195 markets on a UCL tie): booking reads it fresh."""
    try:
        body = _call("GET", EVENT_URL, params={"eventId": event_id}, timeout=timeout)
    except Exception as ex:                          # noqa: BLE001 - upstream
        log.warning("msport event %s failed: %s", event_id, ex)
        return None
    if (body or {}).get("bizCode") != OK:
        return None
    data = body.get("data") or {}
    row = _row(dict(data, eventId=data.get("eventId") or event_id))
    _absorb(row, data.get("markets"))
    return row if row["odds"] else None


# --- booking ----------------------------------------------------------------------------

def build_selection(event, code):
    """One leg: {eventId, marketId, specifier, outcomeId} - their four fields, verbatim."""
    m = market_for(code)
    if m is None:
        raise KeyError("msport does not carry {}".format(code))
    return {"eventId": event["eventId"], "marketId": int(m["marketId"]),
            "specifier": m.get("specifier") or "", "outcomeId": str(m["outcomeId"])}


def read_coupon(code, timeout=20):
    """The legs behind an MSport code, in OUR vocabulary - the contract every book answers on:
    {legs: [{eventId, prediction, raw, home, away, league, kickoff, odds}], available, booked}.
    A leg whose game has started is dropped from `bettableBetSlip`; originalSelectionCount
    says how many the code held."""
    try:
        body = _call("GET", "{}/{}".format(SHARE_URL, code), timeout=timeout, attempts=2)
    except Exception as ex:                          # noqa: BLE001 - user-facing
        log.warning("msport coupon read failed: %s", ex)
        return {"error": "request failed: {}".format(ex)}
    if (body or {}).get("bizCode") != OK:
        return {"error": "not found", "notFound": True, "message": (body or {}).get("message")}
    data = body.get("data") or {}
    trip, out = _by_triple(), []
    for leg in data.get("bettableBetSlip") or []:
        ev, mk, oc = leg.get("event") or {}, leg.get("market") or {}, leg.get("outcome") or {}
        try:
            odds = float(oc.get("odds"))
        except (TypeError, ValueError):
            odds = None
        eid = str(ev.get("eventId") or "")
        out.append({
            "eventId": eid,
            # Their event id is Sportradar's, so every leg pairs across books by id.
            "srId": eid[len("sr:match:"):] if eid.startswith("sr:match:") else None,
            "prediction": trip.get((str(mk.get("id")), str(oc.get("id")), mk.get("specifiers") or "")),
            "raw": "{}/{}".format(mk.get("description") or "", oc.get("description") or ""),
            "home": ev.get("homeTeam") or "", "away": ev.get("awayTeam") or "",
            "league": ev.get("tournament") or "", "kickoff": _iso(ev.get("startTime")),
            "odds": odds,
        })
    try:
        booked = int(data.get("originalSelectionCount") or 0)
    except (TypeError, ValueError):
        booked = 0
    return {"legs": out, "available": len(out), "removed": [], "booked": booked or len(out)}


def read_code(code, timeout=20):
    got = read_coupon(code, timeout)
    if got.get("error"):
        return 0, []
    return got.get("booked") or 0, got.get("legs") or []


def generate_code(selections, timeout=30, verify=True):
    """Turn [{event, code}] into an MSport share code, then read it back leg by leg.

    Same-game legs are accepted here (Over 2.5 and GG on one match booked and read back,
    22 Oct). The read-back compares the MULTISET of (event, code) sent against what decodes
    back, and names any missing leg as not_priced so the client drops exactly that one."""
    if not selections:
        return {"error": "no selections"}
    if len(selections) > BETSLIP_MAX:
        return {"error": "msport slips are capped at {:d} selections here".format(BETSLIP_MAX),
                "sent": len(selections)}
    try:
        legs = [build_selection(s["event"], s["code"]) for s in selections]
    except (KeyError, TypeError, ValueError) as ex:
        return {"error": "could not build selection: {}".format(ex)}
    odds_total = 1.0
    for s in selections:
        odds_total *= float(((s["event"].get("sel") or {}).get(s["code"]) or {}).get("odds") or 1.0)
    try:
        body = _call("POST", SHARE_URL, body={"selections": legs}, timeout=timeout, attempts=1)
    except Exception as ex:                          # noqa: BLE001 - upstream
        log.warning("msport booking failed: %s", ex)
        return {"error": "request failed: {}".format(ex)}
    code = ((body or {}).get("data") or {}).get("shareCode") if (body or {}).get("bizCode") == OK else None
    if not code:
        return {"error": (body or {}).get("message") or json.dumps(body)[:400], "sent": len(legs)}
    if not verify:
        return {"code": code, "odds": round(odds_total, 2), "legs": len(legs)}
    try:
        _, back = read_code(code, timeout)
    except Exception as ex:                          # noqa: BLE001 - upstream
        log.warning("msport read-back failed for %s: %s", code, ex)
        return {"code": code, "odds": round(odds_total, 2), "legs": len(legs), "verified": False}
    sent = Counter((s["event"]["eventId"], s["code"]) for s in selections)
    got = Counter((l["eventId"], l["prediction"]) for l in back)
    missing = list((sent - got).elements())
    if missing:
        return {"error": "msport accepted the slip and returned a code holding "
                         "{} of {} legs".format(len(back), len(legs)),
                "code": code, "sent": len(legs), "available": len(back),
                "not_priced": [{"eventId": e, "prediction": c} for e, c in missing]}
    return {"code": code, "odds": round(odds_total, 2), "legs": len(legs), "verified": True}
