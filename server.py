import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests

app = Flask(__name__)
CORS(app)

MARKET_MAP = {
    "1":        {"marketId": "1",  "outcomeId": "1"},
    "X":        {"marketId": "1",  "outcomeId": "2"},
    "2":        {"marketId": "1",  "outcomeId": "3"},
    "OVER_2.5": {"marketId": "18", "outcomeId": "over", "specifier": "total=2.5"},
    "OVER_1.5": {"marketId": "18", "outcomeId": "over", "specifier": "total=1.5"},
    "GG":       {"marketId": "29", "outcomeId": "yes"},
    "1X":       {"marketId": "10", "outcomeId": "1X"},
    "X2":       {"marketId": "10", "outcomeId": "X2"},
    "12":       {"marketId": "10", "outcomeId": "12"},
}

_FIXTURES_CACHE = {"at": 0, "data": None}
_FIXTURES_TTL = 15 * 60


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


@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    now = time.time()
    refresh = request.args.get("refresh") in ("1", "true")
    if not refresh and _FIXTURES_CACHE["data"] is not None \
            and (now - _FIXTURES_CACHE["at"]) < _FIXTURES_TTL:
        return jsonify({"success": True, "cached": True,
                        "count": len(_FIXTURES_CACHE["data"]),
                        "matches": _FIXTURES_CACHE["data"]})
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
                            "count": len(_FIXTURES_CACHE["data"]),
                            "matches": _FIXTURES_CACHE["data"]})
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
    return jsonify({"success": False,
                    "message": "SportyBet rejected the slip",
                    "detail": result.get("error"),
                    "sent": result.get("sent")}), 400


@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "SoccerWizard API is running successfully!"})


if __name__ == '__main__':
    app.run(port=5000, debug=True)