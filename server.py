from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests

app = Flask(__name__)
CORS(app)  # Enables cross-origin requests for your frontend

# Expanded Market and Outcome Translation Dictionary for SoccerWizard
MARKET_MAP = {
    # 1X2 Full Time Results
    "1":        {"marketId": "1",  "outcomeId": "1"},  # Home Win
    "X":        {"marketId": "1",  "outcomeId": "2"},  # Draw
    "2":        {"marketId": "1",  "outcomeId": "3"},  # Away Win

    # Over / Under Goals
    "OVER_2.5": {"marketId": "18", "outcomeId": "over", "specifier": "total=2.5"},
    "OVER_1.5": {"marketId": "18", "outcomeId": "over", "specifier": "total=1.5"},

    # Both Teams To Score
    "GG":       {"marketId": "29", "outcomeId": "yes"},

    # Double Chance & Anybody Win (Market ID 10)
    "1X":       {"marketId": "10", "outcomeId": "1X"}, # Home or Draw
    "X2":       {"marketId": "10", "outcomeId": "X2"}, # Away or Draw
    "12":       {"marketId": "10", "outcomeId": "12"}  # Home or Away (Anybody Wins)
}

def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    payload = {"selections": selections_list}

    try:
        response = requests.post(
            url, 
            json=payload, 
            headers=headers, 
            impersonate="chrome120", 
            timeout=10
        )
        data = response.json()
        if data.get("bizCode") == 10000:
            return data.get("data", {}).get("shareCode")
        return None
    except Exception as e:
        print(f"API Internal Error: {e}")
        return None

@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    try:
        # Fetch live fixtures from SportyBet using curl_cffi with chrome120 impersonation
        region = "ng"
        url = f"https://www.sportybet.com/api/{region}/fixtures/upcoming"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.sportybet.com",
            "Referer": f"https://www.sportybet.com/{region}/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        response = requests.get(url, headers=headers, impersonate="chrome120", timeout=10)
        data = response.json()
        
        if data.get("bizCode") == 10000:
            raw_events = data.get("data", {}).get("events", [])
            formatted_matches = []
            for event in raw_events:
                formatted_matches.append({
                    "eventId": event.get("eventId"),
                    "homeTeam": event.get("homeTeamName"),
                    "awayTeam": event.get("awayTeamName"),
                    "startTime": event.get("estimateStartTime")
                })
            return jsonify({
                "success": True, 
                "matches": formatted_matches
            })
            
        return jsonify({
            "success": False, 
            "message": "Failed to fetch live events from SportyBet",
            "matches": []
        }), 500
        
    except Exception as e:
        print(f"Fixtures Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/generate-booking-code', methods=['POST'])
def api_generate_code():
    data = request.json or {}
    raw_selections = data.get("selections", [])

    formatted_selections = []
    for item in raw_selections:
        pred_key = item.get("prediction")
        mapping = MARKET_MAP.get(pred_key, MARKET_MAP["1"])
        
        formatted_selections.append({
            "eventId": item.get("eventId"),
            "marketId": mapping["marketId"],
            "outcomeId": mapping["outcomeId"],
            "specifier": mapping.get("specifier", None)
        })

    code = generate_sportybet_code(formatted_selections)

    if code:
        return jsonify({"success": True, "booking_code": code})
    else:
        return jsonify({"success": False, "message": "Failed to generate code. Ensure event IDs are currently active."}), 400

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "SoccerWizard API is running successfully!"})

if __name__ == '__main__':
    app.run(port=5000, debug=True)