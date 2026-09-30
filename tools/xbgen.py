"""Generate 1xBet's pass-through table from their own cards.

    python tools/xbgen.py tools/xbcat.json tools/xbmap.json

Nothing here is typed as an id. Every candidate is PROPOSED from the meaning of
our code - a period, one of THEIR group names, a pattern over THEIR outcome
name, and a line - and kept only if 1xBet priced exactly that on one of the
eight deep cards tools/xbharvest.py read. That is the rule that stopped 24
invented codes shipping on BetKing.

GROUP AND NAME, NOT NAME ALONE. Their names repeat across groups: "Handicap 1"
is both the two-way handicap (G2, whole and half lines) and the Asian one
(G2854, quarter lines); "W1" is both the 1X2 and the 1X2 (2UP). So a proposal
names the group too, and the line picks between the rest.

THE LINE IS P, ON EVERY OUTCOME. A whole line and the half line beside it are
different bets (one pushes) and are never folded together.

SIGNS ARE CHECKED AGAINST PRICES, INSIDE ONE CARD. A handicap side given more
start must shorten; a European handicap that hands the away side a goal must
lengthen the home win. A family that fails is dropped, not shipped.

The rejects are written out with a reason each; they become NOT_CARRIED.
"""
import collections
import json
import logging
import re
import sys

sys.path.insert(0, ".")
logging.disable(logging.CRITICAL)
import betking  # noqa: E402
import bet9ja  # noqa: E402
import betpawa  # noqa: E402
import onexbet  # noqa: E402
import server  # noqa: E402

CAT, OUT = sys.argv[1], sys.argv[2]
cat = json.load(open(CAT, encoding="utf8"))

name_of = {}                                      # (period, G, T) -> sample outcome name
for n in cat["names"]:
    name_of[(n["period"], n["G"], n["T"])] = n["market"] or ""
group_of = {(n["period"], n["G"]): n["group"] for n in cat["names"]}
priced = collections.defaultdict(set)             # (period, T) -> {P}
prices = collections.defaultdict(list)            # (card, period, T) -> [(P, C)]
for card, _gid, period, _G, T, P, C in cat["rows"]:
    p = onexbet._p(P) if P not in (None, 0) else None
    priced[(period, T)].add(p)
    prices[(card, period, T)].append((P, C))

PER = {"": "", "FH_": "1st half", "SH_": "2nd half"}


def f(v):
    return onexbet._p(v)


def find(period, group, pattern, p):
    """(period, T, P) for the one type in `group` whose outcome name matches
    `pattern` and that is priced at line `p`; else (None, reason)."""
    ts = sorted({T for (per, G, T), nm in name_of.items()
                 if per == period and group_of.get((per, G)) == group and re.fullmatch(pattern, nm)})
    if not ts:
        return None, f"verified absent: no {group!r} outcome like {pattern!r} in {period or 'full time'}"
    hits = [t for t in ts if p in priced[(period, t)]]
    if len(hits) != 1:
        lines = sorted({x for t in ts for x in priced[(period, t)] if x}, key=lambda s: float(s))
        return None, (f"verified absent: {group!r} in {period or 'full time'} sells lines "
                      f"{' '.join(lines) or 'none'}, not {p}")
    return (period, str(hits[0]), p), None


SIDE = {"1": "W1", "X": "X", "2": "W2"}


def propose(code):
    """-> (key, None) | (None, reason) | None when no rule covers the code."""
    m = re.fullmatch(r"(FH_|SH_)?(OVER|UNDER)_([\d.]+)", code)
    if m:
        return find(PER[m[1] or ""], "Total", rf"Total {m[2].title()} \(.*\)", f(m[3]))
    m = re.fullmatch(r"(FH_|SH_)?(HOME|AWAY)_(OVER|UNDER)_([\d.]+)", code)
    if m:
        n = "1" if m[2] == "HOME" else "2"
        return find(PER[m[1] or ""], f"Total {n}",
                    rf"Individual Total {n} {m[3].title()} \(.*\)", f(m[4]))
    m = re.fullmatch(r"(FH_|SH_)?AH_([12])_(-?[\d.]+)", code)
    if m:
        per = PER[m[1] or ""]
        # Whole and half lines live on "Handicap", quarters on "Asian Handicap";
        # both name the outcome "Handicap N (line)" with that side's OWN start.
        n = float(m[3])
        group = "Asian Handicap" if abs(n * 2 - round(n * 2)) > 1e-9 else "Handicap"
        # OUR AH_2_L IS THE HOME TEAM'S LINE; 1xBet's "Handicap 2 (P)" is the
        # away side's own, so the away code looks up P = -L. Mapped as P = L
        # until 30 Sep 2026 - the opposite bet - which the price-ladder check
        # could not see (it tests their ladder, not our meaning).
        own = n if m[2] == "1" else -n
        return find(per, group, rf"Handicap {m[2]} \(.*\)", f(own))
    m = re.fullmatch(r"(FH_|SH_)?EH_(\d+)_(\d+)_([12X])", code)
    if m:
        # Ours is a scoreline head start (EH_0_1 = away starts a goal up);
        # theirs writes it "(0:1)" and carries P = home start - away start.
        return find(PER[m[1] or ""], "European Handicap",
                    rf"European Handicap \(\d+:\d+\) {SIDE[m[4]]}", f(int(m[2]) - int(m[3])))
    m = re.fullmatch(r"CORNERS_(OV|UN)_([\d.]+)", code)
    if m:
        return find("Corners", "Total", rf"Total {'Over' if m[1] == 'OV' else 'Under'} \(.*\)", f(m[2]))
    m = re.fullmatch(r"CORNERS_([HA])_(OV|UN)_([\d.]+)", code)
    if m:
        n = "1" if m[1] == "H" else "2"
        return find("Corners", f"Total {n}",
                    rf"Individual Total {n} {'Over' if m[2] == 'OV' else 'Under'} \(.*\)", f(m[3]))
    # FIRST HALF ONLY. Our full-time MIX_ is an OR bet ("home or over 1.5");
    # FH_MIX_ is an AND bet ("draw and over 1.5 in the first half"), which is
    # what 1xBet sells. See REFUSED["MIX_"].
    m = re.fullmatch(r"(FH_)MIX_([12X])_(OV|UN)_([\d.]+)", code)
    if m:
        per = PER[m[1]]
        op = ">" if m[3] == "OV" else "<"
        if m[2] == "X":
            return find(per, "Draw + Total", rf"Draw And Total {op} \(.*\) - Yes", f(m[4]))
        return find(per, f"{m[2]}, Result + Total",
                    rf"Team {m[2]} To Win And Total {op} \(.*\) - Yes", f(m[4]))
    m = re.fullmatch(r"UP2_([12X])", code)
    if m:
        # Their 2UP draw is named plain "X"; the two wins carry "(2UP)".
        return find("", "1X2 (2UP)", "X" if m[1] == "X" else rf"{SIDE[m[1]]} \(2UP\)", None)
    m = re.fullmatch(r"FIRSTGOAL_(?:(FH|SH)_)?([12N])", code)
    if m:
        per = {"FH": "1st half", "SH": "2nd half"}.get(m[1], "")
        # "Next Goal" at 1 is the first goal of the period; "Neither Team To
        # Score" is an outcome of its own, so a goalless period is a loss for
        # both team outcomes, exactly like ours.
        pat = {"1": r"Team 1 To Score Next Goal \(.*\)", "2": r"Team 2 To Score Next Goal \(.*\)",
               "N": r"Neither Team To Score Next Goal \(.*\)"}[m[2]]
        return find(per, "Next Goal", pat, "1")
    m = re.fullmatch(r"WINHALF_([HA])_([YN])", code)
    if m:
        n = "1" if m[1] == "H" else "2"
        return find("", f"Team {n} To Win Either Half",
                    rf"Team {n} To Win At Least One Half - {'Yes' if m[2] == 'Y' else 'No'}", None)
    m = re.fullmatch(r"HIGHHALF_(?:([HA])_)?([12E])", code)
    if m:
        rel = {"1": ">", "E": "=", "2": "<"}[m[2]]
        if m[1]:
            n = "1" if m[1] == "H" else "2"
            return find("", f"Team {n} Scores In Halves", rf"Team {n} - 1st Half {rel} 2nd Half", None)
        return find("", "Scores In Each Half", rf"1st Half {rel} 2nd Half", None)
    m = re.fullmatch(r"EXACT_(?:(FH|SH)_)?(\d)", code)
    if m:
        per = {"FH": "1st half", "SH": "2nd half"}.get(m[1], "")
        n = int(m[2])
        # OUR TOP RUNG IS "N OR MORE": 6+ at full time, 3+ in the first half,
        # 2+ in the second. "N or more" is their 3-way "Over N-1"; "exactly
        # N" is their 3-way "Exactly N"; "none" in a half is "Total (0)".
        top = {"": 6, "1st half": 3, "2nd half": 2}[per]
        if n == top:
            return find(per, "3Way Total", r"Total Over .* \(3Way\)", f(n - 1))
        if n == 0 and per:
            return find(per, "Exact Number", r"Total \(0\) - Yes", None)
        return find(per, "3Way Total", r"Total Exactly .* \(3Way\)", f(n))
    for prefix, why in REFUSED.items():
        if code.startswith(prefix):
            return None, why
    return None


REFUSED = {
    "MIX_": "verified absent: ours is an OR bet (\"home OR over 1.5\"); 1xBet sells only "
            "\"Team 1 To Win And Total >\", the AND bet, which is narrower. Never map one onto the other.",
    "MIXGG_": "verified absent: ours is an OR bet (\"draw OR both score\"); 1xBet sells only "
              "\"X And Both Teams To Score\", the AND bet.",
    "MIXNG_": "verified absent: ours is an OR bet (\"draw OR not both score\"); 1xBet sells only "
              "the AND bet.",
    "DNB_": "verified absent: no draw-no-bet market, and no handicap line at 0 on a full-time "
            "card (their 0 lines exist on corners only).",
    "DC1UP_": "verified absent: 1xBet runs 2UP on the 1X2 (1X2 (2UP)), not 1UP on double chance.",
    "DC2_": "verified absent: no 2UP on double chance.",
    "UP1_": "verified absent: 1xBet runs 2UP only, never 1UP.",
    "MARGIN_": "verified absent: no winning-margin market on any of eight deep cards.",
    "CARD_": "verified absent: no bookings market on any of eight deep cards.",
    "FH_CARD": "verified absent: see CARD_.",
    "HMC_": "verified absent: see CARD_.",
    "PEN_": "verified absent: only 'Penalty In First 5 Minutes' is sold, not a match penalty market.",
    "SHOTS_": "verified absent: shots appear only under Players' stats, per player.",
    "CORNRANGE_": "verified absent: corner totals are over/under lines only, never ranges.",
    "HALFCORNER_": "verified absent: no half-versus-half corner bet.",
    "GOALRANGE_": "verified absent: 'Total From 2 To 3' exists on the halves only.",
    "EXGOALS_": "verified absent: a bet on the total being anything but n is not sold.",
    "EARLY_": "verified absent: their early market is 'Goal In First 5 Minutes', a different window "
              "from ours.",
    "BOTHHALVES_": "verified absent: 'Goals Scored In Both Halves' is about any goal in each half, not "
                   "each half clearing a line.",
    "BOUNDS_": "verified absent: per-team goal bounds are not sold at full time.",
    "TEAMGOALS_": "verified absent: per-team exact goals exist for the halves only, and ours are "
                  "full time.",
}


def sign_check():
    """Inside one card: a handicap side with more start is shorter; a European
    handicap giving the AWAY side a goal (P < 0) lengthens the home win."""
    agree = against = 0
    for (card, per, T), seen in prices.items():
        grp = next((group_of.get((per, G)) for (pp, G, TT) in name_of if pp == per and TT == T), None)
        if grp not in ("Handicap", "Asian Handicap"):
            continue
        seen = sorted((float(p), c) for p, c in seen if p is not None)
        for (p0, c0), (p1, c1) in zip(seen, seen[1:]):
            # AT THE FLOOR THE LADDER IS NOISE: +3 at 1.01 beside +3.5 at 1.04
            # (seven such pairs on 29 Sep, every one with both prices <= 1.05).
            # A sign error shows across the whole ladder, not at its tail.
            if p1 > p0 and max(c0, c1) > 1.06:
                agree += c1 < c0
                against += c1 >= c0
    eh = [0, 0]
    for (card, per, T), seen in prices.items():
        if per != "" or T != 424:
            continue
        plain = [c for p, c in prices.get((card, "", 1), [])]
        for p, c in seen:
            if plain and p is not None:
                ok = (c > plain[0]) if float(p) < 0 else (c < plain[0])
                eh[0 if ok else 1] += 1
    return (agree, against), tuple(eh)


def main():
    vocab = set()
    for m in (server, bet9ja, betking, betpawa):
        vocab |= set(m.MARKET_MAP) | set(m.PASSTHROUGH_MAP)
    (ah_ok, ah_bad), (eh_ok, eh_bad) = sign_check()
    out, rejects = {}, collections.defaultdict(list)
    for code in sorted(vocab):
        if code in onexbet.MARKET_MAP:
            continue
        got = propose(code)
        if got is None:
            rejects["no rule"].append(code)
            continue
        key, why = got
        if key is None:
            rejects[why].append(code)
            continue
        if re.match(r"(FH_|SH_)?AH_", code) and (ah_bad or not ah_ok):
            rejects[f"handicap sign did not verify ({ah_ok}:{ah_bad})"].append(code)
            continue
        if re.match(r"(FH_|SH_)?EH_", code) and (eh_bad or not eh_ok):
            rejects[f"european handicap sign did not verify ({eh_ok}:{eh_bad})"].append(code)
            continue
        out[code] = list(key)
    json.dump({"map": out, "rejects": rejects, "sign": {"ah": [ah_ok, ah_bad], "eh": [eh_ok, eh_bad]}},
              open(OUT, "w"), indent=1)
    print(f"vocabulary {len(vocab)}; mapped {len(out)}; AH sign {ah_ok}:{ah_bad}; "
          f"EH sign {eh_ok}:{eh_bad}; rejected {sum(len(v) for v in rejects.values())}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
