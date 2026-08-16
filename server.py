import os
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests

app = Flask(__name__)
CORS(app)

# ROTATE this key in RapidAPI and set API_FOOTBALL_KEY in Railway > Variables.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY",
                                  "1290ebaa69msh399e14f03021605p1d668fjsnc53f2fc2acbb")
API_FOOTBALL_HOST = "api-football-v1.p.rapidapi.com"

# marketId + numeric outcomeId (Betradar/SportyBet share format)
MARKET_MAP = {
    "1":        {"marketId": "1",  "outcomeId": "1"},
    "X":        {"marketId": "1",  "outcomeId": "2"},
    "2":        {"marketId": "1",  "outcomeId": "3"},
    "1X":       {"marketId": "10", "outcomeId": "9"},
    "12":       {"marketId": "10", "outcomeId": "10"},
    "X2":       {"marketId": "10", "outcomeId": "11"},
    "OVER_1.5": {"marketId": "18", "outcomeId": "12", "specifier": "total=1.5"},
    "OVER_2.5": {"marketId": "18", "outcomeId": "12", "specifier": "total=2.5"},
    "GG":       {"marketId": "29", "outcomeId": "74"},
}

_FIXTURES_CACHE = {"at": 0, "data": None}
_FIXTURES_TTL = 15 * 60
_LIVE_CACHE = {"at": 0, "data": None}
_LIVE_TTL = 30


def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        response = requests.post(url, json={"selections": selections_list},
                                 headers=headers, impersonate="chrome120", timeout=10)
        data = response.json()
        if data.get("bizCode") == 10000:
            return {"code": data.get("data", {}).get("shareCode")}
        return {"error": data.get("message") or data, "sent": selections_list}
    except Exception as e:
        return {"error": f"request failed: {e}", "sent": selections_list}


def fetch_sportybet_fixtures(region="ng"):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    matches = []
    for page in range(1, 21):
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
            })
    return matches


def _map_live_status(s):
    s = (s or "").upper()
    if s in ("FT", "AET", "PEN", "ENDED", "FINISHED"):
        return "FT"
    if s in ("HT", "HALFTIME", "PAUSE"):
        return "HT"
    return s or "LIVE"


def _extract_events(d):
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
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    matches = []
    for page in range(1, 6):
        url = (f"https://www.sportybet.com/api/{region}/factsCenter/liveOrPrematchEvents"
               f"?sportId=sr:sport:1&marketId=1&pageSize=100&pageNum={page}")
        r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
        data = r.json()
        if data.get("bizCode") != 10000:
            break
        pairs = _extract_events(data.get("data"))
        if not pairs:
            break
        for lg, e in pairs:
            if not isinstance(e, dict):
                continue
            status_raw = (e.get("matchStatus") or e.get("period")
                          or e.get("eventStatus") or e.get("playStatus") or "")
            # keep only in-play events
            gs = e.get("gameScore")
            ps = e.get("playedSeconds")
            is_live = bool(ps) or e.get("matchStatus") in ("H1", "H2", "HT", "ET", "P") \
                or isinstance(gs, list) and len(gs) > 0
            if not is_live:
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


def fetch_live_raw(region="ng"):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    url = (f"https://www.sportybet.com/api/{region}/factsCenter/liveOrPrematchEvents"
           f"?sportId=sr:sport:1&marketId=1&pageSize=20&pageNum=1")
    r = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
    payload = r.json()
    pairs = _extract_events(payload.get("data"))
    sample = None
    for lg, e in pairs:
        if isinstance(e, dict):
            sample = {k: e.get(k) for k in (
                "eventId", "homeTeamName", "awayTeamName", "homeScore", "awayScore",
                "setScore", "gameScore", "playedSeconds", "matchStatus", "period",
                "eventStatus", "playStatus", "remainingTimeInPeriod", "estimateStartTime")}
            sample["_league"] = lg
            break
    return {"bizCode": payload.get("bizCode"), "pairs": len(pairs), "sample": sample}


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    if request.args.get("debug") in ("1", "true"):
        try:
            return jsonify(fetch_live_raw())
        except Exception as ex:
            return jsonify({"debug_error": str(ex)}), 500
    now = time.time()
    if _LIVE_CACHE["data"] is not None and (now - _LIVE_CACHE["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(_LIVE_CACHE["data"]), "matches": _LIVE_CACHE["data"]})
    try:
        matches = fetch_live_scores()
        if matches:
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


@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "SoccerWizard API is running successfully!"})


if __name__ == '__main__':
    app.run(port=5000, debug=True)
