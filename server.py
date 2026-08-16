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
    for page in range(1, 8):
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


def fetch_live_scores():
    url = f"https://{API_FOOTBALL_HOST}/v3/fixtures?live=all"
    headers = {"X-RapidAPI-Key": API_FOOTBALL_KEY, "X-RapidAPI-Host": API_FOOTBALL_HOST}
    r = requests.get(url, headers=headers, timeout=15)
    payload = r.json()
    errs = payload.get("errors")
    if errs:
        raise RuntimeError(f"API-Football: {errs}")
    out = []
    for item in payload.get("response", []):
        fx = item.get("fixture", {}) or {}
        status = fx.get("status") or {}
        league = item.get("league", {}) or {}
        teams = item.get("teams", {}) or {}
        goals = item.get("goals", {}) or {}
        home_name = (teams.get("home") or {}).get("name")
        away_name = (teams.get("away") or {}).get("name")
        home_goals, away_goals = [], []
        home_reds = away_reds = 0
        for ev in (item.get("events") or []):
            eteam = (ev.get("team") or {}).get("name")
            player = (ev.get("player") or {}).get("name")
            minute = (ev.get("time") or {}).get("elapsed")
            etype = ev.get("type")
            detail = ev.get("detail") or ""
            if etype == "Goal":
                rec = {"player": player, "minute": minute}
                if eteam == home_name:
                    home_goals.append(rec)
                elif eteam == away_name:
                    away_goals.append(rec)
            elif etype == "Card" and "Red" in detail:
                if eteam == home_name:
                    home_reds += 1
                elif eteam == away_name:
                    away_reds += 1
        lg = league.get("name") or ""
        if league.get("country") and league.get("country") != "World":
            lg = f"{league.get('country')} {lg}"
        out.append({
            "league": lg, "home": home_name, "away": away_name,
            "homeScore": goals.get("home"), "awayScore": goals.get("away"),
            "minute": status.get("elapsed"), "status": status.get("short"),
            "homeGoals": home_goals, "awayGoals": away_goals,
            "homeReds": home_reds, "awayReds": away_reds,
        })
    return out


@app.route('/api/livescores', methods=['GET'])
def get_livescores():
    now = time.time()
    if _LIVE_CACHE["data"] is not None and (now - _LIVE_CACHE["at"]) < _LIVE_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(_LIVE_CACHE["data"]), "matches": _LIVE_CACHE["data"]})
    try:
        matches = fetch_live_scores()
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
