import os
import time
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests
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
    except Exception as ex:
        log.warning("SENTRY_DSN set but Sentry init failed (is sentry-sdk installed?): %s", ex)


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
    "GG":        {"marketId": "29", "outcomeId": "74"},
    "NG":        {"marketId": "29", "outcomeId": "76"},
}

# Reverse lookup: (marketId, outcomeId, specifier) -> code, for reading odds.
_ODDS_LOOKUP = {}
for _code, _m in MARKET_MAP.items():
    _ODDS_LOOKUP[(str(_m["marketId"]), str(_m["outcomeId"]), _m.get("specifier", "") or "")] = _code

_FIXTURES_CACHE = {"at": 0, "data": None}
_FIXTURES_TTL = 15 * 60
_LIVE_CACHE = {"at": 0, "data": None}
_LIVE_TTL = 30

# --- Shared cache (opt-in) -------------------------------------------------
# With one process the in-memory dicts above are fine. Set REDIS_URL (add a
# Redis service on Railway) and the fixture/live caches + captured results move
# to Redis so multiple gunicorn workers / replicas share one copy instead of
# each keeping its own and each scraping SportyBet. Unset = unchanged behavior.
# Every Redis call falls back to the local dict on error, so a Redis blip can
# never take an endpoint down.
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis = None
if REDIS_URL:
    try:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True,
                                socket_connect_timeout=3, socket_timeout=3)
        _redis.ping()
        log.info("Redis enabled: cache + results shared across workers")
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

def _should_poll():
    """Whether THIS process should run the poll this tick. Without Redis, always
    (single process). With Redis, hold a short leader lock so only one worker
    scrapes SportyBet even under `gunicorn -w N` or multiple replicas."""
    if not _redis:
        return True
    try:
        pid = str(os.getpid())
        if _redis.set("sw:poll:leader", pid, nx=True, ex=POLL_SECS * 3):
            return True                              # became leader
        if _redis.get("sw:poll:leader") == pid:
            _redis.expire("sw:poll:leader", POLL_SECS * 3)
            return True                              # still leader, renew
        return False                                 # another worker leads
    except Exception as ex:
        log.warning("poll lock check failed, polling anyway: %s", ex)
        return True

# Markets pulled for each upcoming fixture, merged by eventId so the frontend
# gets every side it needs to de-vig, blend, and show real odds:
#   1  = 1X2 (home/draw/away)      10 = double chance (1X/12/X2)
#   18 = over/under totals          29 = both teams to score (GG/NG)
# Double chance is a default-enabled market and legOdd() reads its odds directly,
# so 10 must be fetched or those picks fall back to estimated odds.
FIXTURE_MARKET_IDS = ("1", "10", "18", "29")

# --- Results capture -------------------------------------------------------
# SportyBet gives no queryable history, so we accumulate final scores ourselves
# as games hit FT in the live feed. Stored in Redis when REDIS_URL is set (shared
# + survives redeploys); otherwise persisted to RESULTS_FILE - point that at a
# Railway VOLUME to survive redeploys, else it resets on each deploy.
RESULTS_FILE = os.environ.get("RESULTS_FILE", "/data/results.json")
_RESULTS = {}   # key "YYYY-MM-DD|home|away" -> {date,home,away,hg,ag,ts}

def _rkey(d, h, a):
    return d + "|" + (h or "").strip().lower() + "|" + (a or "").strip().lower()

def _load_results():
    global _RESULTS
    if _redis:
        try:
            v = _redis.get("sw:results")
            _RESULTS = json.loads(v) if v else {}
        except Exception as ex:
            log.warning("redis results load failed: %s", ex)
            _RESULTS = {}
        return
    try:
        with open(RESULTS_FILE, "r") as fh:
            _RESULTS = json.load(fh)
    except FileNotFoundError:
        _RESULTS = {}  # first run / no volume mounted yet - expected, not an error
    except (OSError, ValueError) as ex:
        # ValueError covers json.JSONDecodeError (corrupt file); OSError covers
        # permission/read failures. Start empty rather than crash on boot.
        log.warning("results load failed (%s): %s", RESULTS_FILE, ex)
        _RESULTS = {}

def _save_results():
    if _redis:
        try:
            _redis.set("sw:results", json.dumps(_RESULTS))
        except Exception as ex:
            log.warning("redis results save failed: %s", ex)
        return
    try:
        os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_RESULTS, fh)
        os.replace(tmp, RESULTS_FILE)
    except OSError as ex:
        log.warning("results save failed (%s): %s", RESULTS_FILE, ex)

def _capture_finals(matches):
    """Record any FT game with a known score. Called on every live poll."""
    changed = False
    import datetime
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    for m in matches or []:
        if m.get("status") != "FT":
            continue
        h, a = m.get("home"), m.get("away")
        hs, as_ = m.get("homeScore"), m.get("awayScore")
        if not h or not a or hs is None or as_ is None:
            continue
        k = _rkey(today, h, a)
        if k in _RESULTS:
            continue
        _RESULTS[k] = {"date": today, "home": h, "away": a,
                       "hg": int(hs), "ag": int(as_), "ts": int(time.time())}
        changed = True
    if changed:
        # keep last ~10 days only
        cut = int(time.time()) - 10 * 86400
        for k in [k for k, v in _RESULTS.items() if v.get("ts", 0) < cut]:
            _RESULTS.pop(k, None)
        _save_results()

_load_results()


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
    for market_id in FIXTURE_MARKET_IDS:
        for page in range(1, 8):  # up to ~700 events per market
            url = (f"https://www.sportybet.com/api/{region}/factsCenter/pcUpcomingEvents"
                   f"?sportId=sr:sport:1&marketId={market_id}&pageSize=100&pageNum={page}")
            try:
                r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
                data = r.json()
            except (RequestsError, ValueError) as ex:
                errors += 1
                log.warning("fixtures fetch failed (market %s page %s): %s", market_id, page, ex)
                break  # give up on this market, move to the next
            if data.get("bizCode") != 10000:
                break
            d = data.get("data", {}) or {}
            events = []
            for t in (d.get("tournaments") or []):
                events.extend(t.get("events") or [])
            events.extend(d.get("events") or [])
            if not events:
                break
            for e in events:
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
                        "odds": {},
                    }
                    by_event[eid] = m
                    order.append(eid)
                # Merge this market's odds into whatever we already have.
                m["odds"].update(_extract_odds(e))
    if not by_event and errors:
        # Total failure - let the route serve stale rather than cache an empty set.
        raise RuntimeError("all SportyBet fixture fetches failed")
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
            if (hs is None or aw is None) and isinstance(gs, list) and gs:
                sc = gs[0]
                if isinstance(sc, str) and ":" in sc:
                    try:
                        p = sc.split(":"); hs = int(p[0]); aw = int(p[1])
                    except (ValueError, IndexError):
                        pass
            if (hs is None or aw is None) and isinstance(ss, str) and ":" in ss:
                try:
                    p = ss.split(":"); hs = int(p[0]); aw = int(p[1])
                except (ValueError, IndexError):
                    pass
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


@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    now = time.time()
    refresh = request.args.get("refresh") in ("1", "true")
    entry = _cache_get("fixtures", _FIXTURES_CACHE)
    if not refresh and entry and (now - entry["at"]) < _FIXTURES_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(entry["data"]), "matches": entry["data"]})
    try:
        matches = fetch_sportybet_fixtures()
        if matches:
            _cache_put("fixtures", _FIXTURES_CACHE, matches)
        return jsonify({"success": True, "cached": False,
                        "count": len(matches), "matches": matches})
    except Exception as ex:
        # Broad by design: a fetch failure should degrade to stale data, never
        # 500 the visitor. Log which path we took so Railway shows the cause.
        entry = _cache_get("fixtures", _FIXTURES_CACHE)
        if entry:
            log.warning("fixtures fetch failed, serving stale: %s", ex)
            return jsonify({"success": True, "cached": True, "stale": True,
                            "count": len(entry["data"]), "matches": entry["data"]})
        log.exception("fixtures fetch failed and no cache to fall back on")
        return jsonify({"success": False, "error": str(ex), "matches": []}), 500


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    now = time.time()
    entry = _cache_get("live", _LIVE_CACHE)
    if entry and (now - entry["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(entry["data"]), "matches": entry["data"]})
    try:
        matches = fetch_live_scores()
        try:
            _capture_finals(matches)
        except Exception:
            # Best-effort persistence - must never break serving live scores.
            log.exception("capture_finals failed")
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


@app.route('/api/generate-booking-code', methods=['POST'])
def api_generate_code():
    data = request.json or {}
    raw_selections = data.get("selections", [])
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
    return jsonify({"success": False, "message": "SportyBet rejected the slip",
                    "detail": result.get("error"), "sent": result.get("sent")}), 400


@app.route('/api/results', methods=['GET'])
def get_results():
    # Optional ?days=N (default 7). Returns finals captured from the live feed.
    try:
        days = int(request.args.get("days", "7"))
    except (ValueError, TypeError):
        days = 7  # bad query param - fall back to default
    if _redis:
        _load_results()  # only the leader worker captures; refresh from shared store
    cut = int(time.time()) - max(1, days) * 86400
    out = [v for v in _RESULTS.values() if v.get("ts", 0) >= cut]
    out.sort(key=lambda v: v.get("ts", 0), reverse=True)
    return jsonify({"success": True, "count": len(out), "results": out})


@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "SoccerWizard API is running successfully!"})


# --- Background capture ----------------------------------------------------
# Polling only inside the request handler means finals are caught only when a
# visitor happens to load the page. A daemon thread polls independently every
# POLL_SECS so FT games are recorded within seconds of ending, traffic or not.
import threading
POLL_SECS = int(os.environ.get("CAPTURE_POLL_SECS", "30"))

def _capture_loop():
    while True:
        try:
            if _should_poll():   # with Redis, only the leader worker scrapes
                ms = fetch_live_scores()
                _capture_finals(ms)
                _cache_put("live", _LIVE_CACHE, ms)
        except Exception:
            # Broad by design: a daemon thread that lets any exception escape
            # dies silently and stops capturing finals. Log the traceback and
            # keep looping.
            log.exception("capture loop iteration failed")
        time.sleep(POLL_SECS)

_poller_started = False
def _start_poller():
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    threading.Thread(target=_capture_loop, daemon=True).start()

# DISABLE_POLLER lets tests (and one-off script imports) load this module without
# spawning the background network thread.
if not os.environ.get("DISABLE_POLLER"):
    _start_poller()


if __name__ == '__main__':
    app.run(port=5000, debug=True)
