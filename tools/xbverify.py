"""Book every carried code on a real fixture and read it back.

    python tools/xbverify.py <gameId>

For each code priced on the fixture: mint a one-leg code, read it back, and
print code | their group/market name | decoded prediction. A decoded
prediction that is not the code sent is a mapping bug; so is a market name
that does not say the bet in our code - read those by eye. Free codes only;
nothing is placed.
"""
import logging
import sys
import time

sys.path.insert(0, ".")
logging.disable(logging.CRITICAL)
import onexbet  # noqa: E402

ev = onexbet.fetch_event(sys.argv[1])
assert ev, "event would not load"
print(ev["teams"], ev["kickoff"], len(ev["odds"]), "codes priced")
bad = 0
for code in sorted(ev["odds"]):
    out = onexbet.generate_code([{"event": ev, "code": code}])
    time.sleep(1)
    if out.get("error"):
        print(f"{code:24} ERROR {out['error']}")
        bad += 1
        continue
    leg = onexbet.read_coupon(out["code"])["legs"][0]
    ok = leg["prediction"] == code
    bad += not ok
    print(f"{code:24} {'ok ' if ok else 'BAD'} {leg['raw']:55} -> {leg['prediction']}")
    time.sleep(1)
print(f"{bad} bad of {len(ev['odds'])}")
