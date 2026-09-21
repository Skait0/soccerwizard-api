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

# The market type ids the sweep asks for. Derived from the table rather than
# typed, so a market added above is swept without a second edit - the drift
# between those two lists is exactly how a book ends up unable to book a market
# it can price.
SWEEP_MARKETS = sorted({m for m, _line, _out in MARKET_MAP.values()})

# their key -> our code, derived rather than typed for the same reason.
_BY_TRIPLE = {triple: code for code, triple in MARKET_MAP.items()}


def market_for(code):
    """The one place that answers "do we carry this market on Betpawa?".

    Returns the triple or None. NEVER a default: an unmapped market that falls
    back to something plausible books a bet nobody asked for, and the book
    answers success because the selection it received was real.
    """
    return MARKET_MAP.get(code)


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
