"""Generate Betway's pass-through table from their own cards.

    python tools/bwgen.py <cards> <out.json>

Betway's ids are Sportradar's UOF ids (see betway.py), the same numbers
SportyBet's tables already carry, so nothing is PROPOSED here at all: every
SportyBet pass-through code is its own candidate, and it is kept only if the
outcome it names is actually priced on one of the deepest Betway cards. Meaning
cannot drift between the two books because the provider's market definitions
are the same objects - but a code no deep card prices is NOT kept, because a
market in the dictionary is not a market on sale.

Read-only: talks to Betway's public feed, writes only <out.json>.
"""
import json
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")
import betway  # noqa: E402
import server  # noqa: E402

BIG = re.compile(r"premier league|laliga|la liga|serie a|bundesliga|ligue 1|"
                 r"champions league|europa league|eredivisie|primeira", re.I)


def family(code):
    """AH_1_-0.5 -> AH_, FH_CORNERS_OV_4.5 -> FH_CORNERS_OV_ ..."""
    m = re.match(r"^((?:[A-Z]+[0-9]?[A-Z]*_)+?)(?=[0-9XNY12+-]|[A-Z]+$|$)", code)
    return m.group(1) if m else code


def deepest(n):
    events = []
    for page in range(30):
        got, final = betway.fetch_page(page * betway.PAGE)
        events.extend(got)
        if final or not got:
            break
        time.sleep(0.45)
    now = time.time()
    pool = [e for e in events if not betway._is_fake(e)
            and int(e.get("expectedStartEpoch") or 0) > now + 3600]
    # Spread across the month, at most three per league: the board is sorted
    # by kickoff, so the first N big games are all one evening's AFC card.
    per, big = defaultdict(int), []
    for e in pool:
        lg = e.get("league") or ""
        if BIG.search(lg) and per[lg] < 3:
            per[lg] += 1
            big.append(e)
    big = big or pool
    cards = []
    for e in big[: n * 4]:
        body = betway._get_json(betway.CARD_URL, {
            "EventId": str(e["eventId"]), "CountryCode": "NG", "CultureCode": "en-US"})
        cards.append((len((body or {}).get("markets") or []), e, body))
        time.sleep(0.45)
    cards.sort(key=lambda c: -c[0])
    return cards[:n]


def candidate(code, t):
    """SportyBet's triple in Betway's spelling.

    Two differences, both of SPELLING, never of meaning:
    - SportyBet writes a variant market's outcome as "sr:exact_goals:6+:68";
      Betway writes "68" under the same variant specifier.
    - SportyBet runs a few markets under its own ids (60200 1UP, 60100 2UP,
      900300/900301 team corners). Betway carries the Sportradar originals:
      1601 with xup=1 / xup=2, and 167/168 with outcomes 12/13. Those are
      ALIASES, each checked on prices before it is believed (bwcheck below).
    """
    m, spec, o = str(t["marketId"]), t.get("specifier") or None, str(t["outcomeId"])
    if o.startswith("sr:"):
        o = o.rsplit(":", 1)[1]
    if m in ("60200", "60100"):                      # 1UP / 2UP
        return ("1601", "xup=1" if m == "60200" else "xup=2", o)
    if m in ("900300", "900301"):                    # team total corners
        return ("167" if m == "900300" else "168", spec, {"30": "12", "31": "13"}[o])
    return (m, spec, o)


def main(n, out_path):
    want = {code: candidate(code, t) for code, t in server.PASSTHROUGH_MAP.items()}
    cards = deepest(n)
    priced = defaultdict(int)              # code -> cards that price it
    lines = defaultdict(set)               # uofMarket -> specifiers seen
    for count, e, body in cards:
        eid = str(e["eventId"])
        prices = {p["outcomeId"]: p.get("priceDecimal") for p in body.get("prices") or []}
        seen = set()
        for o in body.get("outcomes") or []:
            t = betway.decode(eid, o.get("marketId"), o.get("outcomeId"))
            if t and prices.get(o["outcomeId"]):
                seen.add(t)
                lines[t[0]].add(t[1])
        for code, t in want.items():
            if t in seen:
                priced[code] += 1
        print(f"{count:4d} markets  {e['name']} | {e['league']}", file=sys.stderr)

    keep = {c: list(want[c]) for c in sorted(want) if priced[c]}
    # LINE VARIANTS: the market is on sale (its number is on these cards, in
    # the same specifier shape) and only this LINE was not offered on any of
    # them. Betway sets lines per game - one corner line per team, a few
    # totals - so a line missing from ten cards is a line not chosen for those
    # ten games, not a market they do not sell. Carried, and marked, because
    # booking re-reads the live price and names the leg not_priced if the line
    # is not up on that game. A market NUMBER never seen is never carried.
    for c in sorted(want):
        m, spec, _o = want[c]
        if c in keep or m not in lines or not spec:
            continue
        if re.fullmatch(r"(total|hcp)=[-0-9.:]+", spec) and \
                any(s and s.split("=")[0] == spec.split("=")[0] for s in lines[m]):
            keep[c] = list(want[c]) + ["line"]
    rejects = defaultdict(list)
    for c in sorted(want):
        if c not in keep:
            rejects[family(c)].append(c)
    out = {
        "cards": [{"eventId": str(e["eventId"]), "name": e["name"],
                   "league": e["league"], "markets": n} for n, e, _b in cards],
        "kept": keep,
        "rejected": {f: {"codes": cs,
                         "linesOnCard": sorted(str(s) for s in lines.get(want[cs[0]][0], ()))}
                     for f, cs in sorted(rejects.items())},
        "partlyKept": sorted({family(c) for c in keep} & set(rejects)),
    }
    with open(out_path, "w", encoding="utf8") as fh:
        json.dump(out, fh, indent=1)
    print(f"kept {len(keep)} of {len(want)}; {len(rejects)} families with rejects",
          file=sys.stderr)


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
