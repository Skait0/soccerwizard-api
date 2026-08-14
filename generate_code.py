from curl_cffi import requests

def generate_sportybet_code(selections_list, region="ng"):
    url = f"https://www.sportybet.com/api/{region}/orders/share"
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.sportybet.com",
        "Referer": f"https://www.sportybet.com/{region}/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    payload = {
        "selections": selections_list
    }

    print(f"Connecting to SportyBet {region.upper()}...")

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
        else:
            print(f"[SportyBet API Response]: {data.get('message')}")
            return None

    except Exception as e:
        print(f"[Error]: {e}")
        return None


# ==========================================
# TEST RUN WITH ID 39704
# ==========================================
active_match_id = "sr:match:67015332" 

sample_selections = [
    {
        "eventId": active_match_id, 
        "marketId": "1",    # Market 1 = Match Winner (1X2)
        "outcomeId": "1",   # Outcome 1 = Home Win
        "specifier": None
    }
]

# Function call
code = generate_sportybet_code(sample_selections, region="ng")

if code:
    print("\n==========================================")
    print(f" SUCCESS! YOUR LIVE BOOKING CODE IS: {code}")
    print("==========================================")