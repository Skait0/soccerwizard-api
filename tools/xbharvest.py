"""Harvest 1xBet's catalogue from their eight deepest cards.

    python tools/xbharvest.py tools/xbcat.json

Their card carries ids only (group G, type T, param P) - no names. The names
exist in exactly one place: a coupon read-back, which says "Total Over (2.5)".
So one sample of every (period, G, T) is minted into a free code and read
back, fifty at a time. Nothing is placed.

A coupon holding one sample they will not take fails WHOLE (ErrorCode 161627,
"call failed, try later"), so a refused chunk is halved until the bad sample
is alone. 29 Sep 2026: 11,110 priced rows, 394 (period, G, T), 384 named.

Output: {"cards": [...], "rows": [[card, gameId, period, G, T, P, C]],
         "names": [{period, G, T, market, group, groupId, gameType, param}]}
"""
import json
import sys
import time

from curl_cffi import requests

OUT = sys.argv[1]
B = "https://1xbet.ng"
Q = "&lng=en&tf=2200000&tz=1&country=159&partner=159"
H = {"Content-Type": "application/json", "Origin": B, "Referer": B + "/en/line/football"}
s = requests.Session(impersonate="chrome")
s.get(B + "/en/line/football", timeout=30)


def get(path):
    time.sleep(0.45)
    return s.get(B + path, timeout=40).json()


def card(gid):
    return get(f"/service-api/LineFeed/GetGameZip?id={gid}&lng=en&isSubGames=true"
               "&GroupEvents=true&countevents=500&grMode=4&partner=159&country=159&marketType=1")["Value"]


BIG = ["England. Premier League", "Spain. La Liga", "Italy. Serie A",
       "Germany. Bundesliga", "France. Ligue 1", "UEFA Champions League"]
champs = get("/service-api/LineFeed/GetChampsZip?sport=1" + Q)["Value"]
now = time.time()
cands = []
for c in champs:
    if c.get("L") not in BIG:
        continue
    for g in get(f"/service-api/LineFeed/GetChampZip?champ={c['LI']}" + Q)["Value"].get("G") or []:
        if g["S"] > now + 6 * 3600:
            cands.append((g["I"], c["L"], g["O1"], g["O2"]))
sized = []
for gid, lg, o1, o2 in cands[:40]:
    v = card(gid)
    sized.append((v.get("EC") or 0, gid, f"{o1} - {o2} ({lg})", v))
sized.sort(key=lambda x: -x[0])
deep = sized[:8]
print("deepest:", [(n, name) for n, _g, name, _v in deep], file=sys.stderr)

rows = []   # [cardIdx, gameId, period, G, T, P, C]
for ci, (_n, gid, _name, v) in enumerate(deep):
    games = [(gid, "", v)]
    for sg in v.get("SG") or []:
        period = "|".join(x for x in (sg.get("TG") or "", sg.get("PN") or "") if x)
        games.append((sg["I"], period, card(sg["I"])))
    for g_id, period, cv in games:
        for grp in cv.get("GE") or []:
            for r in grp["E"]:
                for e in r:
                    rows.append([ci, g_id, period, grp["G"], e["T"], e.get("P"), e["C"]])

samples = {}
for _ci, g_id, period, G, T, P, C in rows:
    samples.setdefault((period, G, T), (g_id, T, P, C))
keys = list(samples)
names = {}


def ev(g_id, T, P, C):
    return {"GameId": g_id, "Type": T, "Coef": C, "Param": P or 0, "PV": None, "PlayerId": 0,
            "Kind": 3, "InstrumentId": 0, "Seconds": 0, "Price": 0, "Expired": 0, "PlayersDuel": []}


def name_chunk(chunk):
    body = {"notWait": True, "CheckCf": 1, "partner": 159, "AntiExpressCoef": 1, "Summ": 0,
            "Vid": 1, "Events": [ev(*samples[k]) for k in chunk]}
    r = s.post(B + "/service-api/LiveBet/Open/SaveCoupon", data=json.dumps(body), headers=H, timeout=40).json()
    time.sleep(0.8)
    if not r.get("Value"):
        # One bad sample sinks the whole coupon with 161627 - halve and retry.
        if len(chunk) > 1:
            h = len(chunk) // 2
            name_chunk(chunk[:h])
            name_chunk(chunk[h:])
        return
    rb = s.post(B + "/service-api/LiveBet/Open/GetCoupon",
                data=json.dumps({"Guid": r["Value"], "Lng": "en", "partner": 159}), headers=H, timeout=40).json()
    time.sleep(0.8)
    for e in (rb.get("Value") or {}).get("Events") or []:
        for k in chunk:
            if samples[k][0] == e["GameId"] and k[2] == e["Type"]:
                names[k] = {"market": e.get("MarketName"), "group": e.get("GroupName"),
                            "groupId": e.get("GroupId"), "period": e.get("PeriodName"),
                            "gameType": e.get("GameType"), "param": e.get("Param")}


for i in range(0, len(keys), 25):
    name_chunk(keys[i:i + 25])

json.dump({"cards": [{"gameId": gid, "name": name, "events": n} for n, gid, name, _v in deep],
           "rows": rows,
           "names": [dict(v, period=k[0], G=k[1], T=k[2]) for k, v in names.items()]},
          open(OUT, "w", encoding="utf8"))
print(f"rows {len(rows)} distinct (period,G,T) {len(keys)} named {len(names)}", file=sys.stderr)
