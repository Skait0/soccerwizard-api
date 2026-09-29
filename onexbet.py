"""1xBet Nigeria: the fifth book.

Read-only recon came first (29 Sep 2026); everything below was measured against
1xbet.ng rather than carried over from the other four modules. See memory
onexbet-bookie-feasibility.md and the site repo's
docs/superpowers/specs/2026-09-29-onexbet-design.md.

CLOUDFLARE, NOT A LOGIN. Plain fetch gets a 403 "Just a moment" page. A
chrome-impersonating session that has loaded the football page once gets JSON.
Nothing here solves a challenge, and nothing should.

A LEG IS (PERIOD, TYPE, PARAM). Their card groups outcomes by G, but the type
T is unique inside a period, so G is not part of the key. The period matters
because halves and corners are separate GAMES with their own ids (the card's
SG list), and a leg booked against the main id is a different bet.

NO SPORTRADAR ID. Nothing on their card carries one, so the site pairs 1xBet
fixtures by names and kickoff only.
"""

import logging
import re
import time
from datetime import datetime, timezone

from curl_cffi import requests

log = logging.getLogger(__name__)

SITE = "https://1xbet.ng"
API = SITE + "/service-api"
QS = "lng=en&tf=2200000&tz=1&country=159&partner=159"
PAUSE = 0.45
# THEIRS: 51 selections answers ErrorCode 157972, "the number of events in a
# coupon cannot exceed 50" (29 Sep 2026).
BETSLIP_MAX = 50

# Replaced by the real table in the next task.
MARKET_MAP = {"1": ("", "1", None), "X": ("", "2", None), "2": ("", "3", None)}
PASSTHROUGH_MAP = {}


def _p(v):
    """A param as the one string every table keys on: 2.5 -> "2.5", 1.0 -> "1"."""
    if v is None:
        return None
    return format(float(v), "g")


def _key(period, t, p):
    return (period or "", str(t), _p(p))


def _by_key():
    # Derived, never typed: the modelled table last so its names win.
    return {_key(*v): code
            for code, v in list(PASSTHROUGH_MAP.items()) + list(MARKET_MAP.items())}


_session_obj = None


def _session():
    """One warmed session. The football page sets the cookies the API wants."""
    global _session_obj
    if _session_obj is None:
        s = requests.Session(impersonate="chrome")
        s.get(SITE + "/en/line/football", timeout=30)
        _session_obj = s
    return _session_obj


def _reset_session():
    global _session_obj
    _session_obj = None


def _get_json(path, attempts=3):
    """One GET, retried with a long backoff: these are throttles, not blips."""
    last = None
    for n in range(attempts):
        try:
            r = _session().get(API + path, timeout=40)
            return r.json()
        except Exception as ex:                  # noqa: BLE001 - upstream
            last = ex
            _reset_session()
            if n + 1 < attempts:
                time.sleep(3 * (n + 1))
    raise last


# Their board lists simulated and duplicate competitions beside the real ones,
# under the same club names. "Alternative Matches" is 1,141 of 2,859 listed
# games (29 Sep 2026): re-listings of the same fixtures with other lines, so a
# converter could otherwise pair a leg with the wrong copy.
_VIRTUAL = re.compile(r"alternative|srl|simulated|e-?sports?|cyber|virtual|fantasy|"
                      r"statistic|special|team vs|vs player|short football|\dx\d", re.I)


def _is_virtual(league):
    return bool(_VIRTUAL.search(league or ""))


def _iso(unix):
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(game):
    """The empty shell of one fixture, in the shape the other four books use."""
    kickoff = _iso(game["S"]) if game.get("S") else ""
    sub = {}
    for sg in game.get("SG") or []:
        period = "|".join(x for x in (sg.get("TG") or "", sg.get("PN") or "") if x)
        if period and period not in sub:
            sub[period] = int(sg["I"])
    return {
        "eventId": str(game["I"]),
        "slotId": str(game["I"]),
        "eventCode": "",
        "srId": None,
        "teams": "{} - {}".format(game.get("O1") or "", game.get("O2") or ""),
        "kickoff": kickoff,
        "league": game.get("L") or "",
        "country": game.get("CN") or "",
        "startdate": kickoff[:10],
        "odds": {}, "raw": {}, "sel": {}, "sub": sub,
    }


def _absorb(row, card, period=""):
    """Fold one card's outcomes into its row. `period` names the subgame the
    card belongs to ("" for full time)."""
    by_key = _by_key()
    gid = int(card["I"])
    for grp in card.get("GE") or []:
        for rows in grp.get("E") or []:
            for e in rows:
                code = by_key.get(_key(period, e.get("T"), e.get("P")))
                if not code:
                    continue
                try:
                    coef = float(e.get("C"))
                except (TypeError, ValueError):
                    continue
                # A blocked outcome stays on the card with its price collapsed.
                if coef <= 1.01 or e.get("B"):
                    continue
                row["odds"][code] = coef
                row["raw"][code] = str(coef)
                row["sel"][code] = {"gameId": gid, "T": int(e["T"]),
                                    "P": e.get("P"), "coef": coef}


def fetch_card(game_id):
    try:
        v = _get_json(f"/LineFeed/GetGameZip?id={int(game_id)}&isSubGames=true&GroupEvents=true"
                      f"&countevents=500&grMode=4&marketType=1&{QS}")
    except Exception as ex:                      # noqa: BLE001 - upstream
        log.warning("1xbet card %s failed: %s", game_id, ex)
        return None
    return (v or {}).get("Value") or None


def all_fixtures(deadline_s=2100):
    """The sweep: every real competition, nearest kick-offs first.

    Their game list carries NO prices, so every fixture costs one card request.
    Measured 29 Sep 2026: about 1,600 real games, 0.22s a card plus our 0.45s
    pause, so roughly 18 minutes a sweep. The deadline sits inside the 45 minute
    refresh. A sweep that hits it keeps the nearest dates (the ones readers
    book) and reports how many it skipped, so a cut-short sweep never reads as
    a quiet day.
    """
    t0 = time.monotonic()
    champs = (_get_json(f"/LineFeed/GetChampsZip?sport=1&{QS}") or {}).get("Value") or []
    listing = []
    for c in champs:
        if _is_virtual(c.get("L")):
            continue
        time.sleep(PAUSE)
        try:
            games = ((_get_json(f"/LineFeed/GetChampZip?champ={int(c['LI'])}&{QS}") or {})
                     .get("Value") or {}).get("G") or []
        except Exception as ex:                  # noqa: BLE001 - upstream
            log.warning("1xbet champ %s failed: %s", c.get("LI"), ex)
            continue
        for g in games:
            g.setdefault("L", c.get("L"))
            listing.append(g)
    listing.sort(key=lambda g: g.get("S") or 0)

    out, read, skipped = {}, 0, 0
    for g in listing:
        if time.monotonic() - t0 > deadline_s:
            skipped += 1
            continue
        time.sleep(PAUSE)
        card = fetch_card(g["I"])
        if not card:
            continue
        read += 1
        card.setdefault("L", g.get("L"))
        row = _row(card)
        _absorb(row, card)
        if row["odds"]:
            out[row["eventId"]] = row
    log.info("1xbet swept %d fixtures (%d cards read, %d skipped at the deadline)",
             len(out), read, skipped)
    return out, {"listed": len(listing), "read": read, "skipped": skipped}
