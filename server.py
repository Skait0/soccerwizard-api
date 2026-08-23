import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests

app = Flask(__name__)
CORS(app)

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

# --- Results capture -------------------------------------------------------
# SportyBet gives no queryable history, so we accumulate final scores ourselves
# as games hit FT in the live feed. Persisted to RESULTS_FILE. Set that env var
# to a path on a Railway VOLUME so it survives redeploys; without a volume the
# file lives in ephemeral storage and resets on each deploy.
import os, json
RESULTS_FILE = os.environ.get("RESULTS_FILE", "/data/results.json")
_RESULTS = {}   # key "YYYY-MM-DD|home|away" -> {date,home,away,hg,ag,ts}

def _rkey(d, h, a):
    return d + "|" + (h or "").strip().lower() + "|" + (a or "").strip().lower()

def _load_results():
    global _RESULTS
    try:
        with open(RESULTS_FILE, "r") as fh:
            _RESULTS = json.load(fh)
    except Exception:
        _RESULTS = {}

def _save_results():
    try:
        os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_RESULTS, fh)
        os.replace(tmp, RESULTS_FILE)
    except Exception as ex:
        print(f"results save failed: {ex}")

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
                except Exception:
                    pass
    return odds


def fetch_sportybet_fixtures(region="ng"):
    headers = _headers(region)
    matches = []
    for page in range(1, 8):  # up to ~700 events
        url = (f"https://www.sportybet.com/api/{region}/factsCenter/pcUpcomingEvents"
               f"?sportId=sr:sport:1&marketId=1&pageSize=100&pageNum={page}")
        r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
        data = r.json()
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
            matches.append({
                "eventId": e.get("eventId"),
                "homeTeam": e.get("homeTeamName"),
                "awayTeam": e.get("awayTeamName"),
                "startTime": e.get("estimateStartTime"),
                "odds": _extract_odds(e),
            })
    return matches


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
                    except Exception:
                        pass
            if (hs is None or aw is None) and isinstance(ss, str) and ":" in ss:
                try:
                    p = ss.split(":"); hs = int(p[0]); aw = int(p[1])
                except Exception:
                    pass
            minute = None
            if isinstance(ps, str):
                if ":" in ps:
                    try:
                        minute = int(ps.split(":")[0])
                    except Exception:
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
    if not refresh and _FIXTURES_CACHE["data"] is not None \
            and (now - _FIXTURES_CACHE["at"]) < _FIXTURES_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(_FIXTURES_CACHE["data"]), "matches": _FIXTURES_CACHE["data"]})
    try:
        matches = fetch_sportybet_fixtures()
        if matches:
            _FIXTURES_CACHE["data"] = matches
            _FIXTURES_CACHE["at"] = now
        return jsonify({"success": True, "cached": False,
                        "count": len(matches), "matches": matches})
    except Exception as ex:
        if _FIXTURES_CACHE["data"] is not None:
            return jsonify({"success": True, "cached": True, "stale": True,
                            "count": len(_FIXTURES_CACHE["data"]), "matches": _FIXTURES_CACHE["data"]})
        return jsonify({"success": False, "error": str(ex), "matches": []}), 500


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    now = time.time()
    if _LIVE_CACHE["data"] is not None and (now - _LIVE_CACHE["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(_LIVE_CACHE["data"]), "matches": _LIVE_CACHE["data"]})
    try:
        matches = fetch_live_scores()
        try:
            _capture_finals(matches)
        except Exception as _ce:
            print(f"capture finals failed: {_ce}")
        _LIVE_CACHE["data"] = matches
        _LIVE_CACHE["at"] = now
        return jsonify({"success": True, "cached": False,
                        "count": len(matches), "matches": matches})
    except Exception as ex:
        print(f"Livescores Error: {ex}")
        if _LIVE_CACHE["data"]:
            return jsonify({"success": True, "cached": True, "stale": True,
                            "count": len(_LIVE_CACHE["data"]), "matches": _LIVE_CACHE["data"]})
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
    except Exception:
        days = 7
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
            ms = fetch_live_scores()
            _capture_finals(ms)
            _LIVE_CACHE["data"] = ms
            _LIVE_CACHE["at"] = time.time()
        except Exception as ex:
            print(f"capture loop: {ex}")
        time.sleep(POLL_SECS)

_poller_started = False
def _start_poller():
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    threading.Thread(target=_capture_loop, daemon=True).start()

_start_poller()


if __name__ == '__main__':
    app.run(port=5000, debug=True)
