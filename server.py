import os
import time
import json
import logging
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests
import bet9ja
from curl_cffi.requests import RequestsError

app = Flask(__name__)
CORS(app)

# Logs go to stdout/stderr, which Railway captures. Prefer log.* over print so
# messages carry a level + timestamp and exceptions carry a traceback.
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("soccerwizard")

# Error tracking (opt-in). Set SENTRY_DSN in Railway > Variables to enable; with
# it unset this is a complete no-op. Wrapped so a missing/broken SDK degrades to
# a warning instead of taking the app down on boot.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.0,  # errors only - no perf tracing overhead on the trial
            environment=os.environ.get("RAILWAY_ENVIRONMENT_NAME", "production"),
        )
        log.info("Sentry error tracking enabled")
        _sentry = sentry_sdk
    except Exception as ex:
        log.warning("SENTRY_DSN set but Sentry init failed (is sentry-sdk installed?): %s", ex)
        _sentry = None
else:
    _sentry = None


def report(message, level="warning", **context):
    """Log it, and send it to Sentry as a searchable event when one is set up.

    A booking rejection is not an exception, so nothing here ever raised and
    Sentry never saw one - the only record of a failed slip was the line the
    user read on their phone. Log lines are fine for reading after the fact but
    poor for noticing: nobody trawls Railway output to discover that a market
    started failing an hour ago.

    The tag is what makes it useful. Sentry groups by message, so every "no
    market" rejection lands in one issue with a count and a graph, rather than
    scattering. Context carries the detail - which markets, how many legs -
    without putting it in the title.

    A complete no-op with SENTRY_DSN unset, and it never raises: this sits on
    the booking path, and an error reporter that can break a booking is worse
    than no error reporter.
    """
    log.warning("%s | %s", message,
                " ".join("%s=%s" % (k, v) for k, v in sorted(context.items())))
    if not _sentry:
        return
    try:
        with _sentry.push_scope() as scope:
            scope.set_tag("area", "booking")
            for k, v in context.items():
                scope.set_extra(k, v)
            _sentry.capture_message(message, level=level)
    except Exception as ex:   # never let reporting break the thing it reports on
        log.warning("sentry capture failed: %s", ex)


# Prediction code -> SportyBet market/outcome (+ specifier for totals).
# Both sides of each two-way market are listed so the frontend can de-vig
# and blend (needs over AND under, GG AND NG).
MARKET_MAP = {
    "1":         {"marketId": "1",  "outcomeId": "1"},
    "X":         {"marketId": "1",  "outcomeId": "2"},
    "2":         {"marketId": "1",  "outcomeId": "3"},
    "1X":        {"marketId": "10", "outcomeId": "9"},
    "12":        {"marketId": "10", "outcomeId": "10"},
    "X2":        {"marketId": "10", "outcomeId": "11"},
    "OVER_1.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=1.5"},
    "UNDER_1.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=1.5"},
    "OVER_2.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=2.5"},
    "UNDER_2.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=2.5"},
    # Over 3.5 rides market 18, already fetched for Over 1.5 and Over 2.5, so
    # it costs no extra request - and the model has produced o35 all along.
    "OVER_3.5":  {"marketId": "18", "outcomeId": "12", "specifier": "total=3.5"},
    "UNDER_3.5": {"marketId": "18", "outcomeId": "13", "specifier": "total=3.5"},
    "GG":        {"marketId": "29", "outcomeId": "74"},
    "NG":        {"marketId": "29", "outcomeId": "76"},
    # First half, at least one goal. The model has predicted this all along
    # (fh_o05) but it was not bookable, so the site could only ever show it.
    # Market 68 carries total=0.5 on 199 of 200 upcoming events.
    "FH_OVER_0.5":  {"marketId": "68", "outcomeId": "12", "specifier": "total=0.5"},
    "FH_UNDER_0.5": {"marketId": "68", "outcomeId": "13", "specifier": "total=0.5"},
    # How many one side scores on its own. Market 19 is always the HOME
    # team's total and 20 the AWAY team's - verified across 200 events, where
    # 19's own description matched the home team 96 times and the away team
    # never, and 20 the reverse. Getting these round the wrong way would book
    # the opposing team's goals, so it was worth proving rather than assuming.
    "HOME_OVER_0.5":  {"marketId": "19", "outcomeId": "12", "specifier": "total=0.5"},
    "HOME_UNDER_0.5": {"marketId": "19", "outcomeId": "13", "specifier": "total=0.5"},
    "HOME_OVER_1.5":  {"marketId": "19", "outcomeId": "12", "specifier": "total=1.5"},
    "HOME_UNDER_1.5": {"marketId": "19", "outcomeId": "13", "specifier": "total=1.5"},
    "AWAY_OVER_0.5":  {"marketId": "20", "outcomeId": "12", "specifier": "total=0.5"},
    "AWAY_UNDER_0.5": {"marketId": "20", "outcomeId": "13", "specifier": "total=0.5"},
    "AWAY_OVER_1.5":  {"marketId": "20", "outcomeId": "12", "specifier": "total=1.5"},
    "AWAY_UNDER_1.5": {"marketId": "20", "outcomeId": "13", "specifier": "total=1.5"},
}

# Reverse lookup: (marketId, outcomeId, specifier) -> code, for reading odds.
_ODDS_LOOKUP = {}
for _code, _m in MARKET_MAP.items():
    _ODDS_LOOKUP[(str(_m["marketId"]), str(_m["outcomeId"]), _m.get("specifier", "") or "")] = _code

_FIXTURES_CACHE = {"at": 0, "data": None}
# Longer than it was. Every refresh is fifty-odd requests to SportyBet, and
# fixtures for the days ahead barely move between builds - so the useful
# thing to optimise is how rarely we ask, not how fast we ask.
_FIXTURES_TTL = 45 * 60
_LIVE_CACHE = {"at": 0, "data": None}
_LIVE_TTL = 30
# Bet9ja has no bulk endpoint: one request per competition, 170 of them, about
# two minutes. Same reasoning as above - the thing to optimise is how rarely we
# ask. Their fixtures move no faster than SportyBet's.
_BET9JA_CACHE = {"at": 0, "data": None}
_BET9JA_TTL = 45 * 60

# --- Shared cache (opt-in) -------------------------------------------------
# With one process the in-memory dicts above are fine. Set REDIS_URL (add a
# Redis service on Railway) and the fixture/live caches move to Redis so multiple
# gunicorn workers / replicas share one copy instead of each keeping its own and
# each scraping SportyBet. Unset = unchanged behavior. Every Redis call falls
# back to the local dict on error, so a Redis blip can never take an endpoint down.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis = None
if REDIS_URL:
    try:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True,
                                socket_connect_timeout=3, socket_timeout=3)
        _redis.ping()
        log.info("Redis enabled: fixture/live cache shared across workers")
    except Exception as ex:
        log.warning("REDIS_URL set but Redis unavailable; using in-memory cache: %s", ex)
        _redis = None

def _cache_get(name, mem):
    """Return the {'at','data'} entry for a cache. Prefer Redis when enabled,
    fall back to the process-local dict on any miss/error."""
    if _redis:
        try:
            v = _redis.get("sw:cache:" + name)
            if v:
                return json.loads(v)
        except Exception as ex:
            log.warning("redis get %s failed, using local: %s", name, ex)
    return mem if mem.get("data") is not None else None

def _cache_put(name, mem, data):
    """Store a cache entry. Always update the local dict (fallback + no-Redis
    path); mirror to Redis when enabled."""
    mem["at"] = time.time(); mem["data"] = data
    if _redis:
        try:
            _redis.set("sw:cache:" + name, json.dumps({"at": mem["at"], "data": data}))
        except Exception as ex:
            log.warning("redis set %s failed: %s", name, ex)

# Markets pulled for each upcoming fixture, merged by eventId so the frontend
# gets every side it needs to de-vig, blend, and show real odds:
#   1  = 1X2 (home/draw/away)      10 = double chance (1X/12/X2)
#   18 = over/under totals          29 = both teams to score (GG/NG)
#   68 = first-half over/under      (total=0.5 is the one the model predicts)
#   19 = home team goals             20 = away team goals
# Double chance is a default-enabled market and legOdd() reads its odds directly,
# so 10 must be fetched or those picks fall back to estimated odds. The same
# now applies to 68: a market the builder can select has to arrive with real
# odds, or every first-half leg is priced off an estimate.
FIXTURE_MARKET_IDS = ("1", "10", "18", "29", "68", "19", "20")


def _headers(region="ng"):
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    headers = dict(_headers(region)); headers["Content-Type"] = "application/json"
    try:
        response = requests.post(url, json={"selections": selections_list},
                                 headers=headers, impersonate="chrome120", timeout=10)
        data = response.json()
        if data.get("bizCode") == 10000:
            return {"code": data.get("data", {}).get("shareCode")}
        return {"error": data.get("message") or data, "sent": selections_list}
    except Exception as e:
        # Deliberately broad: this is a user-facing path and the route relies on
        # always getting a dict back (never a 500). Log so failures are visible.
        log.warning("booking request to SportyBet failed: %s", e)
        return {"error": f"request failed: {e}", "sent": selections_list}


def _extract_odds(event):
    """Return {code: odds_float} for the markets we care about."""
    odds = {}
    for mk in (event.get("markets") or []):
        mid = str(mk.get("id"))
        spec = mk.get("specifier") or ""
        for oc in (mk.get("outcomes") or []):
            oid = str(oc.get("id"))
            od = oc.get("odds")
            if od in (None, "", "-"):
                continue
            code = _ODDS_LOOKUP.get((mid, oid, spec))
            if code:
                try:
                    odds[code] = float(od)
                except (ValueError, TypeError):
                    pass  # non-numeric odds value - skip this outcome
    return odds


def fetch_sportybet_fixtures(region="ng"):
    """Fetch upcoming events and merge odds across the markets we bet on.

    The pcUpcomingEvents endpoint returns each event's `markets` array filtered
    to the marketId requested, so a single-market fetch (the old behaviour) only
    ever yielded 1X2 odds - OVER/UNDER and GG/NG never arrived and the frontend
    had nothing to de-vig or blend for those. We now fetch each market and merge
    odds by eventId. Event metadata (teams, kickoff) is taken from whichever
    market first surfaces the event.

    Cost: ~3x the requests, paid only on a cache miss (TTL {}m). Partial failure
    (one market down) still returns the odds we did get; total failure raises so
    the caller can serve stale.
    """.format(_FIXTURES_TTL // 60)
    headers = _headers(region)
    by_event = {}   # eventId -> merged match dict
    order = []      # preserve first-seen order
    errors = 0

    # Sequential, deliberately, after parallel took the endpoint down.
    # Eight markets by seven pages is fifty-six round trips, and issuing them
    # concurrently from Railway got every single one refused - SportyBet is
    # tolerant of a steady caller and not of a burst from a datacentre IP.
    # Locally, on a residential connection, the same code ran in 6.6s, which
    # is exactly the kind of difference that only shows up in production.
    #
    # So: one at a time, with a deadline instead of a worker timeout deciding
    # when to stop. Whatever has arrived by then is returned and cached;
    # partial data serves the site, a killed worker serves nothing. The
    # gunicorn timeout sits well beyond this so the deadline is always what
    # ends the fetch.
    # Seven markets by seven pages is forty-nine round trips at roughly a
    # second each, plus the pause between them - a fifty-five second budget
    # cut the last markets off entirely and cached a partial feed, which is
    # why team totals had no real odds after the first successful fetch.
    # Gunicorn allows ninety, so this leaves headroom and still ends the
    # fetch itself rather than letting a killed worker do it.
    # Generous, because nobody is waiting on this any more - it runs on a
    # background thread, not inside a visitor's request.
    # Raised from 240 after measuring what it was actually costing.
    #
    # Coverage of the published feed fell almost exactly in fetch order:
    # 1X2 100%, double chance 98%, totals 90%, GG 86%, then team totals 75%
    # and 70%. Splitting the feed by position made the cause plain - across
    # the first tenth of events every market sat at 98%, and across the last
    # tenth 1X2 was still 100% while away totals had collapsed to 36%. Missing
    # odds were clustered in the last pages of the last markets, which is the
    # shape of a fetch running out of time, not of a bookmaker declining to
    # price a game.
    #
    # That cost real money: a leg with no odds cannot be booked, and one
    # unbookable leg rejects the whole slip. The site was reporting "SportyBet
    # wouldn't take this slip" for markets SportyBet was in fact offering.
    #
    # Nothing waits on this. It runs on a background thread every forty-five
    # minutes, so even a full fifteen-minute fetch is a third of the cycle.
    # The old budget was the constraint; there was never a reason for it to be
    # this tight.
    deadline = time.time() + 900

    for market_id in FIXTURE_MARKET_IDS:
        # Seven pages was seven hundred events, and SportyBet currently lists
        # sixteen hundred across seventeen. Everything past the seventh page
        # simply did not exist as far as this site was concerned - which is
        # why a National League tie on page eight showed as unavailable while
        # SportyBet was plainly offering it, and why the cup ties were thin.
        # Empty pages break the loop below, so a market with less to say
        # still costs only one wasted request rather than thirteen.
        for page in range(1, 21):
            if time.time() > deadline:
                # Reported, not just logged. This truncates the feed and the
                # damage shows up far away - as an unbookable slip - so it has
                # to be visible somewhere other than a log nobody trawls.
                report("fixtures fetch hit its deadline",
                       level="warning", market=market_id, page=page,
                       events=len(by_event),
                       markets_done=FIXTURE_MARKET_IDS.index(market_id),
                       markets_total=len(FIXTURE_MARKET_IDS))
                log.warning("fixtures fetch hit its deadline at market %s page %s; "
                            "returning %d events", market_id, page, len(by_event))
                break
            url = (f"https://www.sportybet.com/api/{region}/factsCenter/pcUpcomingEvents"
                   f"?sportId=sr:sport:1&marketId={market_id}&pageSize=100&pageNum={page}")
            # One retry before abandoning a market. A single refused request
            # used to zero every market it touched, which is how one bad
            # minute turned into an empty feed.
            data = None
            for attempt in (1, 2):
                try:
                    r = requests.get(url, headers=headers, impersonate="chrome120", timeout=12)
                    data = r.json()
                    break
                except (RequestsError, ValueError) as ex:
                    errors += 1
                    log.warning("fixtures fetch failed (market %s page %s, try %s): %s",
                                market_id, page, attempt, ex)
                    if attempt == 1:
                        # Longer than it looks like it needs to be. These
                        # failures are a throttle, not a blip, and coming
                        # straight back just spends the second try on the
                        # same refusal.
                        time.sleep(4)
            if data is None:
                break  # give up on this market, move to the next
            # A real pause between pages, not a token one. At roughly one and
            # a half requests a second SportyBet started refusing partway
            # through - and because a refused first page abandons the whole
            # market, that silently dropped the team-total odds from the feed.
            # Slower here costs nothing: this runs on a background thread every
            # forty-five minutes, and nobody is waiting for it.
            time.sleep(0.45)
            if data.get("bizCode") != 10000:
                break
            d = data.get("data", {}) or {}
            # Keep each event paired with its tournament. Flattening the events
            # out of `tournaments` used to throw the competition away, which left
            # every fixture league-less and forced the consumer to guess - so a
            # cup tie between two Premier League sides came out as the Premier
            # League, and ordinary league games came out as "England Cup".
            # Same "{category} {name}" shape the livescores feed uses.
            events = []
            for t in (d.get("tournaments") or []):
                cat = ((t.get("category") or {}).get("name")) or t.get("categoryName") or ""
                nm = t.get("name") or ""
                lg = (f"{cat} {nm}").strip() if cat else nm
                for e in (t.get("events") or []):
                    events.append((lg, e))
            for e in (d.get("events") or []):
                events.append(("", e))
            if not events:
                break
            for lg, e in events:
                eid = e.get("eventId")
                if not eid:
                    continue
                m = by_event.get(eid)
                if m is None:
                    m = {
                        "eventId": eid,
                        "homeTeam": e.get("homeTeamName"),
                        "awayTeam": e.get("awayTeamName"),
                        "startTime": e.get("estimateStartTime"),
                        "league": lg,
                        "odds": {},
                    }
                    by_event[eid] = m
                    order.append(eid)
                elif lg and not m.get("league"):
                    # A later market can surface a tournament the first one
                    # listed loose under `events`, so fill the gap if we can.
                    m["league"] = lg
                # Merge this market's odds into whatever we already have.
                m["odds"].update(_extract_odds(e))
    if not by_event and errors:
        # Total failure - let the route serve stale rather than cache an empty set.
        raise RuntimeError("all SportyBet fixture fetches failed")

    # Per-market coverage, so a thin feed is a number rather than a guess.
    # A market well below the 1X2 count means its later pages did not arrive,
    # and every event it is missing is a leg that cannot be booked.
    total = len(by_event)
    priced = {}
    for m in by_event.values():
        for code in m["odds"]:
            priced[code] = priced.get(code, 0) + 1
    thin = sorted(
        ((c, n) for c, n in priced.items() if total and n < total * 0.9),
        key=lambda x: x[1])
    log.info("fixtures: %d events, %d markets priced, %d errors",
             total, len(priced), errors)
    if thin:
        log.warning("fixtures: thin coverage on %s",
                    ", ".join(f"{c} {n}/{total}" for c, n in thin[:8]))
    return [by_event[eid] for eid in order]


def _map_live_status(s):
    s = (s or "").upper()
    if s in ("FT", "AET", "PEN", "ENDED", "FINISHED"):
        return "FT"
    if s in ("HT", "HALFTIME", "PAUSE"):
        return "HT"
    return s or "LIVE"


def _extract_live_events(d):
    pairs = []
    tours = []
    if isinstance(d, list):
        tours = d
    elif isinstance(d, dict):
        tours = d.get("tournaments") or []
        if not tours and d.get("events"):
            return [("", e) for e in d["events"]]
    for t in tours:
        if not isinstance(t, dict):
            continue
        cat = ((t.get("category") or {}).get("name")) or t.get("categoryName") or ""
        nm = t.get("name") or ""
        lg = (f"{cat} {nm}").strip() if cat else nm
        evs = t.get("events")
        if evs:
            for e in evs:
                pairs.append((lg, e))
        else:
            pairs.append((lg, t))
    return pairs


def fetch_live_scores(region="ng"):
    headers = _headers(region)
    matches = []
    # liveOrPrematchEvents ignores pageNum: pages 1 through 5 come back with
    # byte-identical event lists. Looping them appended the same 71 events five
    # times, so the feed served 400 entries for 80 matches and every client
    # polling it every 30 seconds paid for four fifths of nothing. The loop
    # stays in case the endpoint ever grows real paging, but it now stops the
    # moment a page brings nothing new.
    seen_ids = set()
    for page in range(1, 6):
        url = (f"https://www.sportybet.com/api/{region}/factsCenter/liveOrPrematchEvents"
               f"?sportId=sr:sport:1&marketId=1&pageSize=100&pageNum={page}")
        r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
        data = r.json()
        if data.get("bizCode") != 10000:
            break
        pairs = _extract_live_events(data.get("data"))
        if not pairs:
            break
        fresh = []
        for lg, e in pairs:
            if not isinstance(e, dict):
                continue
            # eventId when there is one, otherwise the pairing and its
            # competition - an event with no id must still not arrive twice.
            key = e.get("eventId") or "%s|%s|%s" % (
                e.get("homeTeamName"), e.get("awayTeamName"), lg)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            fresh.append((lg, e))
        if not fresh:
            break
        pairs = fresh
        for lg, e in pairs:
            if not isinstance(e, dict):
                continue
            status_raw = (e.get("matchStatus") or e.get("period")
                          or e.get("eventStatus") or e.get("playStatus") or "")
            gs = e.get("gameScore")
            ps = e.get("playedSeconds")
            _st = _map_live_status(status_raw)
            is_live = bool(ps) or e.get("matchStatus") in ("H1", "H2", "HT", "ET", "P") \
                or (isinstance(gs, list) and len(gs) > 0)
            # Ended games have no clock, so they'd be dropped - but the results
            # capture needs them. Keep FT too.
            if not is_live and _st != "FT":
                continue
            hs = e.get("homeScore")
            aw = e.get("awayScore")
            ss = e.get("setScore")
            # setScore is the running total. gameScore is the same thing split
            # by period - Crystal Palace v Man City at 73 minutes carried
            # setScore "1:3" and gameScore ["0:1","1:2"], which sums to it.
            #
            # This used to read gameScore[0] first, so it published the FIRST
            # HALF as the live score and never reached the setScore branch
            # below, because hs and aw were no longer None by then. Every match
            # that scored in the second half was reported wrong: 45 of the 71
            # live games on the board when this was found, Bayern Munich among
            # them, showing 1-0 at the 90th minute of a game that finished 4-1.
            # Downstream that is worse than a wrong number on a screen - the
            # results sweep banks these as final scores, so a tip that landed
            # gets recorded as a loss.
            if (hs is None or aw is None) and isinstance(ss, str) and ":" in ss:
                try:
                    p = ss.split(":"); hs = int(p[0]); aw = int(p[1])
                except (ValueError, IndexError):
                    pass
            # Only if setScore is missing: add the periods up rather than
            # taking one of them.
            if (hs is None or aw is None) and isinstance(gs, list) and gs:
                th = ta = 0
                ok = False
                for part in gs:
                    if not isinstance(part, str) or ":" not in part:
                        continue
                    try:
                        a, b = part.split(":"); th += int(a); ta += int(b); ok = True
                    except (ValueError, IndexError):
                        ok = False
                        break
                if ok:
                    hs, aw = th, ta
            minute = None
            if isinstance(ps, str):
                if ":" in ps:
                    try:
                        minute = int(ps.split(":")[0])
                    except (ValueError, IndexError):
                        pass
                elif ps.isdigit():
                    minute = int(ps) // 60
            elif isinstance(ps, (int, float)):
                minute = int(ps) // 60
            matches.append({
                "league": lg or "",
                "home": e.get("homeTeamName"),
                "away": e.get("awayTeamName"),
                "homeScore": hs, "awayScore": aw, "minute": minute,
                "status": _map_live_status(status_raw),
                "homeGoals": [], "awayGoals": [], "homeReds": 0, "awayReds": 0,
            })
    return matches


# --- background refresher -------------------------------------------------
# Seven markets by seven pages is forty-nine round trips to SportyBet. That
# never belonged inside a visitor's request: done serially it outran the
# worker timeout and the worker was killed before it could fall back to stale
# data, and done concurrently the burst got this server refused outright.
# Either way the endpoint answered 500 and the cache could never refresh.
#
# So the fetch runs on its own thread and the route only ever reads what that
# thread has stored. Nobody waits for SportyBet, a slow or refused fetch costs
# a stale answer rather than an error, and the request path cannot time out
# because it does no network work at all.
_REFRESH_LOCK = threading.Lock()

def _refresh_fixtures_once():
    """Replace the stored feed only with something at least as complete.

    A throttled pass does not fail outright, it comes back short. SportyBet
    starts refusing partway through, a refused page abandons the rest of that
    market, and what returns is a smaller feed that looks perfectly valid.
    Storing it would drop fixtures and whole markets from the site while every
    health check still read green, which is the worst kind of failure: quiet,
    and indistinguishable from a thin day.
    """
    try:
        matches = fetch_sportybet_fixtures()
        if not matches:
            log.warning("fixtures refresh returned nothing; keeping previous copy")
            return False
        prev = _cache_get("fixtures", _FIXTURES_CACHE)
        prev_n = len((prev or {}).get("data") or [])
        # A fifth down is weather: a card genuinely thins out overnight. Much
        # beyond that on a feed this size is the throttle, not the day.
        if prev_n and len(matches) < prev_n * 0.8:
            log.warning("fixtures refresh returned %d against %d stored, looks "
                        "truncated; keeping the fuller copy", len(matches), prev_n)
            return False
        _cache_put("fixtures", _FIXTURES_CACHE, matches)
        log.info("fixtures refreshed: %d events", len(matches))
        return True
    except Exception as ex:
        log.warning("fixtures refresh failed, keeping previous copy: %s", ex)
    return False

def _fixtures_loop():
    # With a shared cache the copy in Redis outlives this process, so a
    # redeploy usually starts with data that is minutes old. Refetching it
    # straight away would spend forty-nine requests to replace something we
    # already have - and every one of those is a request that got this server
    # refused once. Wait out whatever is left of its life instead.
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _FIXTURES_TTL:
            wait = _FIXTURES_TTL - age
            log.info("fixtures cache is %ds old; first refresh in %ds",
                     int(age), int(wait))
            time.sleep(wait)
    while True:
        ok = _refresh_fixtures_once()
        # Retry sooner after a failure than after a success, but never so
        # soon that a refused IP gets hammered back into refusing.
        time.sleep(_FIXTURES_TTL if ok else 300)

def _start_fixtures_thread():
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_fixtures_loop, name="fixtures-refresh", daemon=True)
    t.start()
    log.info("fixtures refresher started (every %dm)", _FIXTURES_TTL // 60)

_start_fixtures_thread()


# --- Bet9ja fixtures -------------------------------------------------------
# The same pattern as above and for the same reasons, with one addition: Bet9ja
# publishes its own event count per competition, so a sweep can be checked
# against an outside opinion rather than only against the last one we stored.
# That matters here more than it does for SportyBet. Bet9ja answers a datacentre
# with a block page rather than an error, which is how these routes spent the
# first hour of their life reporting a successful fetch of nothing.
_BET9JA_LOCK = threading.Lock()

def _refresh_bet9ja_once():
    try:
        fixtures, stats = bet9ja.all_fixtures()
    except Exception as ex:                          # noqa: BLE001 - background
        log.warning("bet9ja refresh failed, keeping previous copy: %s", ex)
        return False

    expected = stats.get("expected") or 0
    got = len(fixtures)
    if not fixtures:
        log.warning("bet9ja refresh returned nothing; keeping previous copy")
        return False
    # Their own catalogue said how many events exist. Coming back well under
    # that is a throttled or blocked sweep, not a thin day, and storing it
    # would quietly shrink the board.
    if expected and got < expected * 0.9:
        log.warning("bet9ja refresh collected %d of %d they list; keeping "
                    "previous copy (failed=%d short=%d)", got, expected,
                    len(stats.get("failed") or []), len(stats.get("short") or []))
        return False
    prev = _cache_get("bet9ja", _BET9JA_CACHE)
    prev_n = len((prev or {}).get("data") or {})
    if prev_n and got < prev_n * 0.8:
        log.warning("bet9ja refresh returned %d against %d stored, looks "
                    "truncated; keeping the fuller copy", got, prev_n)
        return False

    _cache_put("bet9ja", _BET9JA_CACHE, fixtures)
    log.info("bet9ja refreshed: %d events of %d listed, %d competitions, "
             "%d failed", got, expected, stats.get("competitions"),
             len(stats.get("failed") or []))
    for s in (stats.get("short") or [])[:5]:
        log.info("bet9ja short: %s wanted %s got %s",
                 s.get("league"), s.get("want"), s.get("got"))
    return True

def _bet9ja_loop():
    entry = _cache_get("bet9ja", _BET9JA_CACHE)
    if entry and entry.get("data"):
        age = time.time() - entry["at"]
        if age < _BET9JA_TTL:
            time.sleep(_BET9JA_TTL - age)
    while True:
        ok = _refresh_bet9ja_once()
        time.sleep(_BET9JA_TTL if ok else 300)

def _start_bet9ja_thread():
    if not _BET9JA_LOCK.acquire(blocking=False):
        return
    t = threading.Thread(target=_bet9ja_loop, name="bet9ja-refresh", daemon=True)
    t.start()
    log.info("bet9ja refresher started (every %dm)", _BET9JA_TTL // 60)

_start_bet9ja_thread()


@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if entry and entry.get("data"):
        age = int(time.time() - entry["at"])
        return jsonify({"success": True, "cached": True, "ageSeconds": age,
                        "stale": age > _FIXTURES_TTL,
                        "count": len(entry["data"]), "matches": entry["data"]})
    # Nothing stored yet - the refresher is on its first pass. 503 rather than
    # 500 so callers treat it as "not ready", and the CDN in front does not
    # store it as the answer.
    return jsonify({"success": False, "warming": True,
                    "error": "fixtures not loaded yet", "matches": []}), 503


# --- Bet9ja (odds only, for now) -------------------------------------------
# Bet9ja rivals SportyBet for users in Nigeria, and a booking code is only any
# use to somebody who holds an account with the bookmaker that issued it - so
# this is a second source alongside, not a replacement.
#
# Both halves are verified end to end: a slip built here was booked through
# Bet9ja and the code loaded on their own site with the right three selections.
# See bet9ja.py for the fields that had to be exact.
@app.route('/api/bet9ja/fixtures', methods=['GET'])
def get_bet9ja_fixtures():
    """Every Bet9ja event, served from the background sweep.

    No `league` argument: the site pairs a fixture to a bookmaker event on team
    names and kick-off time, never on competition, so what it wants is one flat
    bag. Matching by league would mean maintaining a mapping from our 47
    leagues to their GIDs by hand, and buying nothing with it.

    `?league={gid}` still fetches a single competition live, which is for
    debugging one league rather than for the site.
    """
    gid = request.args.get("league")
    if gid:
        try:
            events = bet9ja.fetch_league(int(gid))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "league must be a number"}), 400
        except Exception as ex:                  # noqa: BLE001 - user-facing path
            report("bet9ja fixtures failed", league=gid, error=str(ex))
            return jsonify({"success": False, "error": str(ex), "matches": {}}), 502
        return jsonify({"success": True, "league": int(gid), "cached": False,
                        "count": len(events), "matches": events})

    entry = _cache_get("bet9ja", _BET9JA_CACHE)
    data = (entry or {}).get("data")
    if not data:
        # The sweep takes two minutes, so doing it here would time out. Say so
        # rather than returning an empty bag with success: true - that lie is
        # what made this integration's first outage invisible.
        return jsonify({"success": False, "count": 0, "matches": {},
                        "error": "bet9ja fixtures not loaded yet"}), 503
    return jsonify({"success": True, "cached": True,
                    "ageSeconds": int(time.time() - entry["at"]),
                    "count": len(data), "matches": data})


@app.route('/api/bet9ja/booking-code', methods=['POST'])
def api_bet9ja_code():
    """Turn a set of picks into a Bet9ja booking code.

    Body: {"selections": [{"league": 492, "eventId": "825683591",
                           "code": "1X"}, ...]}

    Odds are re-read from the live feed rather than trusted from the caller: a
    price the site showed a minute ago may have moved, and Bet9ja rejects a
    slip whose odds do not match theirs.
    """
    data = request.get_json(silent=True) or {}
    picks = data.get("selections") or []
    if not picks:
        return jsonify({"success": False, "error": "no selections"}), 400

    # One fetch per SELECTION, against the per-event endpoint. That is the only
    # way to get every market for any league: the league listings either miss
    # team goals or miss most of the competitions. A slip is a handful of legs,
    # so a request each is cheap, and the odds are read fresh at book time
    # anyway because Bet9ja rejects a slip whose prices have moved.
    # Every bad leg, not the first one.
    #
    # This used to return on the first pick Bet9ja would not price, which told
    # the caller about one leg out of forty and gave it nothing to retry with:
    # drop that leg, resend, discover the next one, forty round trips. The
    # SportyBet route has answered with a named `unbookable` list since the
    # "no market there" incident and the client already knows how to drop
    # exactly those and try again. Same shape here, so one client path serves
    # both bookmakers.
    resolved, bad = [], []
    try:
        for p in picks:
            ev = bet9ja.fetch_event(p.get("eventId"))
            if not ev or p.get("code") not in (ev.get("raw") or {}):
                bad.append({"eventId": p.get("eventId"), "prediction": p.get("code")})
                continue
            resolved.append({"event": ev, "code": p.get("code")})
    except Exception as ex:                      # noqa: BLE001 - user-facing path
        report("bet9ja odds fetch failed", error=str(ex))
        return jsonify({"success": False, "error": str(ex)}), 502

    if bad:
        report("booking: picks with no market at Bet9ja",
               bad_legs=len(bad), total_legs=len(picks),
               markets=", ".join(sorted({str(b["prediction"]) for b in bad})),
               events=", ".join(sorted({str(b["eventId"]) for b in bad})[:10]))
        return jsonify({
            "success": False,
            "message": "Bet9ja rejected the slip",
            "detail": "no market there for %d of %d picks" % (len(bad), len(picks)),
            "unbookable": bad,
        }), 400

    out = bet9ja.generate_code(resolved)
    if out.get("code"):
        return jsonify({"success": True, **out})
    report("bet9ja booking refused", legs=len(resolved), detail=str(out.get("error"))[:300])
    return jsonify({"success": False, **out}), 502


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    now = time.time()
    entry = _cache_get("live", _LIVE_CACHE)
    if entry and (now - entry["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(entry["data"]), "matches": entry["data"]})
    try:
        matches = fetch_live_scores()
        _cache_put("live", _LIVE_CACHE, matches)
        return jsonify({"success": True, "cached": False,
                        "count": len(matches), "matches": matches})
    except Exception as ex:
        # Broad by design - degrade to stale rather than 500. See get_fixtures.
        entry = _cache_get("live", _LIVE_CACHE)
        if entry:
            log.warning("livescores fetch failed, serving stale: %s", ex)
            return jsonify({"success": True, "cached": True, "stale": True,
                            "count": len(entry["data"]), "matches": entry["data"]})
        log.exception("livescores fetch failed and no cache to fall back on")
        return jsonify({"success": False, "error": str(ex), "matches": []}), 500


def _unbookable(raw_selections):
    """Which of these picks SportyBet has no market for.

    Roughly half the card carries no team-totals market at all - 888 of 1797
    fixtures on the day this was written - and asking to book one comes back
    "invalid event data, no market there", which takes the whole slip down.
    One unplaceable leg among forty loses all forty.

    We already hold every event's odds in the fixtures cache, so the answer is
    known here without asking SportyBet. Checking costs nothing and turns a
    flat rejection into a list of exactly which picks are the problem.

    Silent when the cache is empty: no prices is not the same as prices that
    exclude a market, and refusing a slip because this server has just started
    would be worse than the failure it prevents.
    """
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    rows = (entry or {}).get("data") or []
    if not rows:
        return []
    odds_by_event = {}
    for m in rows:
        if isinstance(m, dict) and m.get("eventId"):
            odds_by_event[m["eventId"]] = m.get("odds") or {}
    bad = []
    for item in raw_selections:
        ev, pred = item.get("eventId"), item.get("prediction")
        prices = odds_by_event.get(ev)
        if prices is None:          # event not in the cache - cannot judge it
            continue
        price = prices.get(pred)
        if not price or price <= 1.01:
            bad.append({"eventId": ev, "prediction": pred})
    return bad


@app.route('/api/generate-booking-code', methods=['POST'])
def api_generate_code():
    data = request.json or {}
    raw_selections = data.get("selections", [])

    bad = _unbookable(raw_selections)
    if bad:
        # Named rather than counted, so the caller can drop exactly these and
        # retry instead of guessing which leg broke it.
        report("booking: picks with no market at SportyBet",
               bad_legs=len(bad), total_legs=len(raw_selections),
               markets=", ".join(sorted({b["prediction"] for b in bad})),
               events=", ".join(sorted({b["eventId"] for b in bad})[:10]))
        return jsonify({
            "success": False,
            "message": "SportyBet rejected the slip",
            "detail": "no market there for %d of %d picks" % (len(bad), len(raw_selections)),
            "unbookable": bad,
        }), 400

    formatted_selections = []
    for item in raw_selections:
        mapping = MARKET_MAP.get(item.get("prediction"), MARKET_MAP["1"])
        formatted_selections.append({
            "eventId": item.get("eventId"),
            "marketId": mapping["marketId"],
            "outcomeId": mapping["outcomeId"],
            "specifier": mapping.get("specifier", "")
        })
    result = generate_sportybet_code(formatted_selections)
    if result.get("code"):
        return jsonify({"success": True, "booking_code": result["code"]})

    # Got past our own check and SportyBet still said no. That is the case
    # worth seeing: it means the cache disagreed with them, or something else
    # is wrong, and until now it was thrown away silently.
    # Got past our own check and SportyBet still said no: our cache and theirs
    # disagree, which is the one worth being told about rather than reading later.
    report("booking: SportyBet rejected a slip that passed validation",
           reason=str(result.get("error"))[:200], legs=len(raw_selections),
           markets=",".join(sorted({(i.get("prediction") or "?") for i in raw_selections})))
    return jsonify({"success": False, "message": "SportyBet rejected the slip",
                    "detail": result.get("error"), "sent": result.get("sent")}), 400


@app.route('/', methods=['GET'])
def home():
    """Also reports where the cache lives, so adding Redis can be confirmed
    from a browser rather than by trawling deploy logs. If this says
    "memory" after REDIS_URL is set, the variable did not take."""
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    live = _cache_get("live", _LIVE_CACHE)
    redis_ok = False
    if _redis:
        try:
            _redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
    return jsonify({
        "status": "SoccerWizard API is running successfully!",
        "cache": "redis" if redis_ok else "memory",
        "redisConfigured": bool(REDIS_URL),
        # Same reason as redisConfigured: after setting SENTRY_DSN this says
        # whether the variable actually took, without reading deploy logs.
        # "configured" is the DSN being present; "active" is the SDK having
        # started, which is the one that matters and can differ.
        "sentryConfigured": bool(SENTRY_DSN),
        "sentryActive": bool(_sentry),
        "fixtures": {
            "count": len((entry or {}).get("data") or []),
            "ageSeconds": int(time.time() - entry["at"]) if entry else None,
        },
        "livescores": {
            "count": len((live or {}).get("data") or []),
            "ageSeconds": int(time.time() - live["at"]) if live else None,
        },
        "markets": list(FIXTURE_MARKET_IDS),
    })


if __name__ == '__main__':
    app.run(port=5000, debug=True)
