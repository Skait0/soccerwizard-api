"""Bet9ja: odds and booking codes.

Sits beside the SportyBet code in server.py rather than replacing it. Bet9ja
rivals SportyBet for users in Nigeria, and a booking code is only useful to
somebody who holds an account with the bookmaker that issued it - so the site
needs both, not a better one.

It is the easier of the two integrations, which was not the expectation:

  odds      a GET with a Referer; no auth, no cookie, no token. It does need
            curl_cffi's Chrome TLS fingerprint, same as SportyBet, but only
            from a datacentre - see the import below
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
import time

# curl_cffi, not requests, and the reason is not style.
#
# Plain requests works perfectly from a laptop and gets a block page from a
# datacentre - Bet9ja answered Railway with HTML, which surfaced as "Expecting
# value: line 1 column 1" and an empty fixture list on a route that still
# reported success. Nothing in local testing can catch that: the whole
# difference is which IP asks. SportyBet needed the same treatment, which is
# why curl_cffi was already a dependency here.
#
# IMPERSONATE goes on every call. Sending it only on the ones that seemed to
# need it is how you end up debugging this twice.
from curl_cffi import requests
from urllib.parse import quote

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"

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
    # Four markets the site offers and this map did not carry, so the booking
    # route refused them on every fixture: it rejects a leg whose code is absent
    # from here, however well Bet9ja prices it. Both-to-score is ON BY DEFAULT
    # in the builder, so most Bet9ja slips have been quietly losing that leg.
    #
    # Keys read off GetEvent?EVENTID=, which returns 771 of them for one game.
    # The group is only meaningful to the coupon route; these are unreachable by
    # GID either way and come from fetch_event, which is what booking uses. They
    # are filed under groups already fetched so MARKET_GROUPS does not grow and
    # the sweep does not get slower.
    "GG":            ("S_GGNG_Y", POPULAR),
    "FH_OVER_0.5":   ("S_OU1T@0.5_O", POPULAR),
    "HOME_OVER_1.5": ("S_HAOU@1.5_OH", HOME_AWAY),
    "AWAY_OVER_1.5": ("S_HAOU@1.5_OA", HOME_AWAY),
    # The other side of each line. Nobody bets these here - the builders offer
    # only the over - but server.py maps them for SportyBet and uses them to
    # DE-VIG: over and under together give the bookmaker's own implied
    # probability with the margin taken out, which is what blends into ours.
    # Bet9ja was blend:false purely because these were missing, so its readers
    # got model prices with no market check at all. Same nine SportyBet has,
    # all verified present on a live event.
    "UNDER_1.5":      ("S_OU@1.5_U", POPULAR),
    "UNDER_2.5":      ("S_OU@2.5_U", POPULAR),
    "UNDER_3.5":      ("S_OU@3.5_U", POPULAR),
    "NG":             ("S_GGNG_N", POPULAR),
    "FH_UNDER_0.5":   ("S_OU1T@0.5_U", POPULAR),
    "HOME_UNDER_0.5": ("S_HTS_N", HOME_AWAY),
    "AWAY_UNDER_0.5": ("S_AWAYSCORE_N", HOME_AWAY),
    "HOME_UNDER_1.5": ("S_HAOU@1.5_UH", HOME_AWAY),
    "AWAY_UNDER_1.5": ("S_HAOU@1.5_UA", HOME_AWAY),
}

# The groups we actually need to fetch, derived rather than restated so adding
# a market cannot leave its group unfetched.

# --- markets we move but do not model ------------------------------------
# A CONVERTER NEEDS IDENTITY, NOT A PREDICTION. Everything above is a market
# the model has an opinion about, which is why it is priced, graded and in the
# published record. These are not: they exist so a slip somebody else built can
# be read, re-cut and moved to the other book without us pretending to rate it.
# Nothing here is ever produced by tipCode, so no board, no record and no
# calibration changes by their being here.
#
# 1UP and 2UP are SportyBet's early-payout promotion and Bet9ja's alike: the
# bet pays as soon as the side goes one (or two) goals ahead, whatever the
# final score. Both books run it, which is how it can be converted at all - it
# was the family I guessed would NOT port, and seven real codes said otherwise.
# Verified against a live event before being written down: S_1X21_11 = 1.62,
# S_1X22_12 = 2.31 on Leeds v Newcastle, 13 Sep 2026.
#
# Their sign is not their outcome. S_1X21 spells home/draw/away as 11/X1/21 and
# S_1X22 as 12/X2/22 - the trailing digit is which promotion it is, not which
# side - and reading that pair the obvious way books the wrong team.
PASSTHROUGH_MAP = {
    "UP1_1": ("S_1X21_11", 1), "UP1_X": ("S_1X21_X1", 1), "UP1_2": ("S_1X21_21", 1),
    "UP2_1": ("S_1X22_12", 1), "UP2_X": ("S_1X22_X2", 1), "UP2_2": ("S_1X22_22", 1),
}
# 1X2-or-Over/Under. One market with six outcomes here, where SportyBet has six
# markets with a Yes/No - and the lines are the problem, not the outcomes: this
# book sells 1.5 and 3.5, that one sells 2.5, and nothing overlaps. Checked
# across four live events, 23 keys each at 1.5 and 3.5 and none at 2.5.
# Their own label for XorUn reads "Under Or Under {HND} Goals", which is a typo
# in THEIR dictionary - the key says XorUn and the bet is draw-or-under.
_MIX_SIGNS = (("MIX_1_OV", "1orOv"), ("MIX_1_UN", "1orUn"),
              ("MIX_X_OV", "XorOv"), ("MIX_X_UN", "XorUn"),
              ("MIX_2_OV", "2orOv"), ("MIX_2_UN", "2orUn"))
for _line in ("1.5", "3.5"):
    for _code, _sign in _MIX_SIGNS:
        PASSTHROUGH_MAP["%s_%s" % (_code, _line)] = (
            "S_CHANCEMIXOU@%s_%s" % (_line, _sign), 1)

# THE 2.5 LINE IS A DIFFERENT MARKET ENTIRELY, and missing it is what made this
# family look unconvertible. There are four chance-mix families here, not one:
# S_CHANCEMIXOU ("1X2 or Over/Under") runs 1.5 and 3.5, and S_CHANCEMIXGGOU
# ("Chance Mix") runs 2.5 - the same six selections under another key. A regex
# anchored on CHANCEMIXOU@ could not see it, so four sampled events reported
# "no 2.5 anywhere" and the conclusion drawn from that - that the two books
# share no line and the family can never convert - was wrong. One real code
# from a punter, 5RHG763, disproved it.
# 2.5 is exactly what SportyBet sells on 854-859, so those legs cross with
# nothing changed at all.
for _code, _sign in _MIX_SIGNS:
    PASSTHROUGH_MAP["%s_2.5" % _code] = ("S_CHANCEMIXGGOU@2.5_%s" % _sign, 1)

# 1X2 or GG/NG - SportyBet's 860-862 against their S_CHANCEMIX. No line on
# either side, so nothing to reconcile.
for _code, _sign in (("MIXGG_1", "1orGG"), ("MIXGG_X", "XorGG"),
                     ("MIXGG_2", "2orGG"), ("MIXNG_1", "1orNG"),
                     ("MIXNG_X", "XorNG"), ("MIXNG_2", "2orNG")):
    PASSTHROUGH_MAP[_code] = ("S_CHANCEMIX_%s" % _sign, 1)

# Team cards, against SportyBet's 800060. "N or more" there is "over N-0.5"
# here, which is the same bet said two ways. Their home side runs to 3.5 and
# the away side stops at 2.5, so anything above that has no pair and is left
# named rather than guessed at.
for _n, _line in ((1, "0.5"), (2, "1.5"), (3, "2.5"), (4, "3.5")):
    PASSTHROUGH_MAP["CARD_H_%d" % _n] = ("S_OUBOOKHOME@%s_O" % _line, 1)
for _n, _line in ((1, "0.5"), (2, "1.5"), (3, "2.5")):
    PASSTHROUGH_MAP["CARD_A_%d" % _n] = ("S_OUBOOKAWAY@%s_O" % _line, 1)

# Corners for the match, against SportyBet's 166. Their card runs 7.5 to 14.5
# and SportyBet's 6.5 to 12.5, so the shared middle is mapped and the ends are
# left alone - a line one book does not sell is not a line to invent.
for _line in ("7.5", "8.5", "9.5", "10.5", "11.5", "12.5", "13.5", "14.5"):
    PASSTHROUGH_MAP["CORNERS_OV_%s" % _line] = ("S_OUCORNERS@%s_O" % _line, 1)
    PASSTHROUGH_MAP["CORNERS_UN_%s" % _line] = ("S_OUCORNERS@%s_U" % _line, 1)
# Not merged into MARKET_MAP for the same reason as its SportyBet twin: that
# table is what the sweep fetches and what the board prices. These are fetched
# per event at book time, where the full card comes back anyway.
def market_for(code):
    """The odds key and group for a market code, modelled or pass-through."""
    return MARKET_MAP.get(code) or PASSTHROUGH_MAP.get(code)


MARKET_GROUPS = sorted({g for _k, g in MARKET_MAP.values()})

_BY_KEY = {key: code for code, (key, _g) in
           list(MARKET_MAP.items()) + list(PASSTHROUGH_MAP.items())}


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


def _get_json(url, timeout=15, attempts=3):
    """GET and decode, retrying a dropped connection but not a bad reply.

    Under a steady sweep their feed closes a connection mid-handshake roughly
    once in eighty requests. Unretried that is an empty competition, which is
    indistinguishable from a competition with no fixtures - ten events went
    missing from a sweep exactly this way and the run still reported no
    failures.

    Only the request is retried. A reply that arrives and does not parse is a
    block page, and asking again from the same IP gets the same block page.
    """
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout,
                             impersonate=IMPERSONATE)
        except Exception as ex:                      # noqa: BLE001 - transport
            last = ex
            if i + 1 < attempts:
                time.sleep(0.4 * (i + 1))
            continue
        return r.json()
    raise last


def fetch_events(league_id, group=POPULAR, timeout=15, by_group=True):
    """Every event in one league, for one market group.

    `by_group` picks which of their two endpoints to use, and it matters:

      GetEventsInGroup?GROUPID=   any competition they carry, keyed by the GID
                                  that GetSports lists - all 75 countries
      GetEventsInCouponV2?SCHID=  only the 14 "popular coupons" for soccer

    The coupon route was found first and covers the majors, but a SCHID exists
    only for a league they have chosen to feature. Fetching by GID needs no
    lookup table and reaches everything, which matters when the board carries
    47 leagues and their popular list has 14.

    Returns {eventId: {...}}. Empty dict on any failure - this feeds a page,
    and a bookmaker being unreachable is not a reason to return a 500.
    """
    if by_group:
        url = ("%s/GetEventsInGroup?GROUPID=%s&DISP=0&MKEY=%s"
               % (BASE, league_id, group))
    else:
        url = ("%s/GetEventsInCouponV2?SCHID=%s&DISP=0&MKEY=%s"
               % (BASE, league_id, group))
    try:
        data = _get_json(url, timeout)
    except Exception as ex:
        log.warning("bet9ja events %s/%s failed: %s", league_id, group, ex)
        return {}

    # The two endpoints wrap the group differently: the coupon one nests it
    # under D.G keyed by GID, the group one IS the group.
    node = data.get("D") or {}
    groups = _values(node.get("G")) if node.get("G") else ([node] if node.get("E") else [])

    out = {}
    for grp in groups:
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
                # Their betslip wants this, and it is not EXTID: a real slip
                # carries "3070" where EXTID is an eight-digit provider id.
                "eventCode": str(ev.get("C") or ""),
                "teams": ev.get("DS") or "",
                "kickoff": ev.get("STARTDATEUTC") or "",
                "league": ev.get("GN") or grp.get("GN") or "",
                # The country. Their slip sends it as SG.
                "country": grp.get("SG") or ev.get("SG") or "",
                "startdate": (ev.get("STARTDATE") or "").replace("-", "/"),
                "odds": {},
                # The prices exactly as the feed gave them. A booking sends
                # odds as STRINGS - "4.25", not 4.25 - so the float above is
                # for our own arithmetic and this is what goes on the wire.
                "raw": {},
            })
            row["odds"].update(odds)
            for key, val in (ev.get("O") or {}).items():
                code = _BY_KEY.get(key)
                if code and code in odds:
                    row["raw"][code] = str(val)
    return out


def leagues(timeout=20):
    """Every soccer competition Bet9ja carries.

    {gid: {"league", "country", "events", "next"}} - their whole catalogue in
    one call, 75 countries, so fetch_league can be pointed at anything without
    a lookup table. Named by GID, which is what GetEventsInGroup wants.

    `events` is their own count for the competition and `next` its earliest
    kick-off. Both come free with the catalogue and both are worth having:
    the count is the only independent check that a sweep collected what was
    there, and the date lets one skip a competition whose season has not
    started without spending a request to find that out.
    """
    try:
        pal = (_get_json("%s/GetSports?SPORTID=1&DISP=0" % BASE, timeout)
               .get("D", {}).get("PAL", {}))
    except Exception as ex:
        log.warning("bet9ja league list failed: %s", ex)
        return {}

    soccer = pal.get("1") or {}
    out = {}
    for country in _values(soccer.get("SG")):
        name = country.get("SG_DESC") or ""
        for gid, grp in _items(country.get("G")):
            try:
                num = int(grp.get("NUM") or 0)
            except (TypeError, ValueError):
                num = 0
            out[str(gid)] = {"league": grp.get("G_DESC") or "", "country": name,
                             "events": num, "next": grp.get("D") or ""}
    return out


def all_fixtures(timeout=15, pause=0.05, catalogue=None):
    """Every soccer event Bet9ja is pricing, across every competition.

    The site matches a fixture to a bookmaker event on team names and kick-off
    time, never on league, so what it needs is one flat bag - not 47 lookups
    against a league mapping we would then have to maintain by hand.

    There is no bulk endpoint. GetEventsInGroup takes one GROUPID - a
    comma-separated list returns an empty group - so this is one request per
    competition, 171 of them and about eighty seconds. That is fine in a
    background refresher and hopeless in a request handler, which is why
    nothing calls this from a route.

    What comes back is 1X2, double chance and the over/under lines. NOT team
    goals: that route ignores MKEY (see fetch_league). Team over 0.5 is one of
    the site's four default markets, so a Bet9ja price for it is unavailable
    here and is read per event at booking time instead. Matching does not care
    - it pairs on names and kick-off - and an unpriced leg still books.

    Returns (fixtures, stats). `stats` carries their own event count beside
    ours, because the failure worth catching here is not an exception - it is a
    sweep that returns 300 events instead of 1,150 and looks entirely healthy.
    """
    cat = catalogue if catalogue is not None else leagues(timeout=timeout)
    stats = {"competitions": len(cat), "expected": 0, "collected": 0,
             "failed": [], "short": []}
    if not cat:
        log.warning("bet9ja sweep: no catalogue, nothing to fetch")
        return {}, stats

    out = {}
    for gid, meta in cat.items():
        stats["expected"] += meta.get("events") or 0
        try:
            rows = fetch_league(gid, timeout=timeout)
        except Exception as ex:                      # noqa: BLE001 - one league
            # One competition failing is not a reason to lose the other 170.
            log.warning("bet9ja sweep: %s (%s) failed: %s",
                        gid, meta.get("league"), ex)
            stats["failed"].append(gid)
            continue
        # "Nothing came back" and "they are not pricing anything here" look
        # identical from the return value, and only one of them is a problem.
        # The catalogue said how many events this competition has, so compare.
        want = meta.get("events") or 0
        if len(rows) < want:
            stats["short"].append({"gid": gid, "league": meta.get("league"),
                                   "want": want, "got": len(rows)})
        for eid, row in rows.items():
            # The catalogue names the competition better than the event does,
            # and the site shows this.
            row.setdefault("league", meta.get("league") or "")
            if not row.get("country"):
                row["country"] = meta.get("country") or ""
            row["groupId"] = str(gid)
            out[eid] = row
        if pause:
            # Not politeness for its own sake: a tight loop over their feed
            # started closing connections mid-handshake while this was being
            # measured.
            time.sleep(pause)

    stats["collected"] = len(out)
    return out, stats


def fetch_league(league_id, timeout=15, by_group=True):
    """One league across every market group that route actually serves.

    The coupon route honours MKEY, so the markets we offer are split across
    two groups there and both have to be asked for and merged.

    GetEventsInGroup does NOT. MKEY=1 and MKEY=170 return byte-for-byte the
    same twenty odds keys, so asking twice is the same request twice - it cost
    half of a 155-second sweep before this was measured. It also means team
    goals are simply not reachable by GID: they come from fetch_event, which
    is what the booking route uses anyway.
    """
    groups = MARKET_GROUPS if not by_group else [POPULAR]
    merged = {}
    for group in groups:
        for eid, row in fetch_events(league_id, group, timeout, by_group).items():
            if eid in merged:
                # Both maps, or a market from the second group is priced for
                # our own arithmetic and then missing when the slip is built.
                merged[eid]["odds"].update(row["odds"])
                merged[eid]["raw"].update(row["raw"])
            else:
                merged[eid] = row
    return merged


def selection_id(event_id, odds_key):
    """The key their betslip uses for a selection: eventId, "$", odds key.

    Not guessable and not the feed's slot number, which is what an earlier pass
    used. Both EVS and ODDS are keyed by this, and getting it wrong is why the
    API accepted the format and then answered 500 with an empty body: the slip
    was well-formed and referred to selections that did not resolve.
    """
    return "%s$%s" % (event_id, odds_key)


def fetch_event(event_id, timeout=15):
    """Every market Bet9ja lists for one fixture - about 1,300 of them.

    This is what makes full coverage possible. The two list endpoints each fall
    short on their own: GetEventsInGroup reaches all 172 competitions but
    ignores MKEY and only ever returns the default markets, while the coupon
    route honours MKEY and has team goals but exists only for the 14 leagues
    they feature. Asking about one event gets everything, for any league.

    So: list fixtures cheaply per league, and pull the full book only for the
    handful actually going on a slip.
    """
    url = "%s/GetEvent?EVENTID=%s&DISP=0" % (BASE, event_id)
    try:
        ev = _get_json(url, timeout).get("D") or {}
    except Exception as ex:
        log.warning("bet9ja event %s failed: %s", event_id, ex)
        return None
    if not ev.get("ID"):
        return None

    odds, raw = {}, {}
    for key, val in (ev.get("O") or {}).items():
        code = _BY_KEY.get(key)
        if not code:
            continue
        try:
            odds[code] = float(val)
        except (TypeError, ValueError):
            continue
        raw[code] = str(val)
    return {
        "eventId": ev.get("ID"),
        "slotId": str(ev.get("ID")),
        "eventCode": str(ev.get("C") or ""),
        "teams": ev.get("DS") or "",
        "kickoff": ev.get("STARTDATEUTC") or "",
        "league": ev.get("GN") or "",
        "country": ev.get("SG") or "",
        "startdate": (ev.get("STARTDATE") or "").replace("-", "/"),
        "odds": odds,
        "raw": raw,
    }


def build_selection(event, code):
    """One leg of a betslip, in the shape their EVS map wants.

    Every field here was matched against a real booking rather than inferred.
    The ones that are not obvious:

      sid         the FULL odds key ("S_1X2_2"), not the market half of it
      market      the human label ("1X2"), which is the middle of the key
      oddValue    a string, exactly as the feed gave it
      eventCode   the event's C field, not EXTID
      sportName   empty - their own client sends ""
    """
    key, _group = market_for(code)
    _sid_unused, sign, aux = parse_odds_key(key)
    raw = (event.get("raw") or {}).get(code)
    if raw is None:
        raise KeyError("no %s on event %s" % (code, event.get("eventId")))
    market = key.split("_")[1].split("@")[0]
    return {
        "id": selection_id(event["eventId"], key),
        "eventId": event["eventId"],
        "eventCode": event.get("eventCode") or "",
        # Their own UI rewrites the "A - B" the feed gives into "A v B" before
        # sending it, so this matches what their site would have posted.
        "eventName": (event.get("teams") or "").replace(" - ", " v "),
        "market": market,
        "sid": key,
        "sign": sign,
        "GN": event.get("league") or "",
        "leagueName": event.get("league") or "",
        "SG": event.get("country") or "",
        "startdate": event.get("startdate") or "",
        "oddValue": raw,
        "hnd": aux or "",
        "sportName": "",
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
    # ODDS maps selection id -> the odd as a STRING, keyed identically to EVS.
    # Their own code fills it (`ODDS[sel] = odd`) before pushing the bet.
    odds_by_sel = {sid: leg["oddValue"] for sid, leg in evs.items()}

    betslip = {
        "BETS": [{
            # Integers, not the tab names. A real slip sends 0 for both.
            "BSTYPE": 0,
            "TAB": 0,
            # From their builder: a multiple is (NUMLINES=count, COMB=1,
            # TYPE=count); a single is (1, 1, 1).
            "NUMLINES": n,
            "COMB": 1,
            "TYPE": n,
            # A booking carries no money - it is a shareable slip, not a bet.
            "STAKE": 0, "POTWINMIN": 0, "POTWINMAX": 0,
            "BONUSMIN": 0, "BONUSMAX": 0,
            # Not rounded. A real slip sends the full product (49.9375 for
            # 4.25 x 11.75), and rounding it is a mismatch their validator can
            # see.
            "ODDMIN": odds_total, "ODDMAX": odds_total,
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
                          timeout=timeout, impersonate=IMPERSONATE)
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
    """Pull the booking number out of a successful response.

    It is `data[0].RIS` - seven characters, the same shape as the codes their
    own site shows and as the ones tipsters post. Not COUPONCODE, which sits
    beside it and is an internal UUID: sending somebody that would give them a
    code Bet9ja will not load.

    Success is `status: 1` with an empty error. A refusal comes back with
    status -1 and a message, and must not be mistaken for a code.
    """
    if not isinstance(body, dict):
        return None
    if body.get("status") != 1:
        return None
    rows = body.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        ris = row.get("RIS")
        if isinstance(ris, (str, int)) and str(ris).strip():
            return str(ris).strip()
    return None


# --- reading a booking code back ------------------------------------------
# THE HOST IS NOT THE ONE THAT ISSUES THE CODE. Booking posts to
# apigw.bet9ja.com; reading a code back is served by coupon.bet9ja.com, which
# is a third hostname their site talks to and neither of the two this module
# already knew about. Found by loading sports.bet9ja.com/?bookABetCode=<code>
# and reading the network log rather than by guessing paths, after
# GetBookABet and LoadBookABet came back 404 on both known hosts.
COUPON_URL = ("https://coupon.bet9ja.com/desktop/feapi/CouponAjax/"
              "GetBookABetCouponV2?couponCode=%s&type=TYPE_DESKTOP_REPRINT")

# ours <- theirs, for turning a leg we did not build back into a market we know
_ODDS_KEY_TO_CODE = {v[0]: k for k, v in
                     list(MARKET_MAP.items()) + list(PASSTHROUGH_MAP.items())}


def read_coupon(code, timeout=15):
    """Return the legs behind a Bet9ja booking code.

    [{eventId, prediction, home, away, league, kickoff, odds}], where
    `prediction` is OUR market code, or None for a market we do not model.

    A reprint is not a transcript. Bet9ja drops events from a coupon on its own
    schedule, so a code read days later can come back shorter than it was
    booked - measured on 5R9ZZNB, minted with five legs and reading back with
    three - and nothing in the response says how many there were. Anything
    built on this has to show the punter what was read rather than claim it is
    the whole slip.
    """
    try:
        r = requests.get(COUPON_URL % quote(str(code)), headers=_headers(),
                         timeout=timeout, impersonate=IMPERSONATE)
        body = r.json()
    except Exception as ex:                      # noqa: BLE001 - user-facing
        log.warning("bet9ja coupon read failed: %s", ex)
        return {"error": "request failed: %s" % ex}

    if (body or {}).get("R") != "OK":
        # Their answer for a code that does not exist is a 200 with R=ERROR.
        return {"error": "not found", "notFound": True}

    out = []
    for key, leg in ((body.get("D") or {}).get("O") or {}).items():
        eid, _, sid = str(key).partition("$")
        names = str(leg.get("E_NAME") or "").split(" - ")
        try:
            odd = float(leg.get("V"))
        except (TypeError, ValueError):
            odd = None
        out.append({
            "eventId": int(eid) if eid.isdigit() else eid,
            "prediction": _ODDS_KEY_TO_CODE.get(sid),
            "raw": sid,
            "home": names[0].strip() if names else "",
            "away": names[1].strip() if len(names) > 1 else "",
            "league": leg.get("GN") or "",
            "kickoff": leg.get("STARTDATEUTC") or "",
            "odds": odd,
        })
    return {"legs": out}
