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
# OURS, NOT THEIRS - not measured yet. Matches BetKing, the lowest real cap
# among the other books, until a probe reads Betway's.
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
# Empty until it is GENERATED: SportyBet's pass-through codes, each kept only
# if Betway prices its built outcome id on a deep card. Not typed by hand.
PASSTHROUGH_MAP = {}

NOT_CARRIED = {
    "": "not read yet: Betway's pass-through table has not been generated from "
        "their cards. Only the modelled markets are carried so far (14 Oct 2026).",
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


def market_for(code):
    """The triple for one of our codes, or None. NEVER a default."""
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


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
        code = _BY_TRIPLE.get(triple) if triple else None
        if not code or not o.get("isTradingActive", True):
            continue
        m = markets.get(o.get("marketId")) or {}
        if m.get("isSuspended") or m.get("isActive") is False:
            continue
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

    Prices are re-read live before booking: the swept price can be 45 minutes
    old, and whether Betway refuses a stale `value` is not measured yet.
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
