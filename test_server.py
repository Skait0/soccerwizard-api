"""Tests for server.py - odds decode, multi-market merge, error paths, market-map
integrity, and the optional Redis cache layer. Zero external deps (stdlib unittest).

Run:  python -m unittest test_server -v
"""
import os, re, json, tempfile, unittest
os.environ.pop("SENTRY_DSN", None)          # keep Sentry a no-op

import server

# No test reaches SportyBet's live card. A test that wants one sets its own.
server._event_markets = lambda *a, **k: None


class MarketMapIntegrity(unittest.TestCase):
    def test_both_sides_present_for_two_way_markets(self):
        # de-vig/blend on the frontend needs both sides of each 2-way market
        for a, b in [("OVER_1.5", "UNDER_1.5"), ("OVER_2.5", "UNDER_2.5"), ("GG", "NG")]:
            self.assertIn(a, server.MARKET_MAP)
            self.assertIn(b, server.MARKET_MAP)

    def test_odds_lookup_covers_every_market(self):
        # every MARKET_MAP entry must be reverse-decodable from odds
        for code, m in server.MARKET_MAP.items():
            key = (str(m["marketId"]), str(m["outcomeId"]), m.get("specifier", "") or "")
            self.assertEqual(server._ODDS_LOOKUP.get(key), code)

    def test_fixture_markets_include_double_chance(self):
        # regression: double chance (market 10) must be fetched or 1X/X2 picks
        # fall back to estimated odds
        self.assertIn("10", server.FIXTURE_MARKET_IDS)

    def test_every_mapped_market_is_actually_fetched(self):
        # This replaces an exact-set assertion that went stale the moment the
        # first-half (68) and team-total (19, 20) markets were added, and then
        # sat red long enough to stop meaning anything. The invariant is what
        # was wanted: a market we can decode is no use if nothing asks for it,
        # and the symptom is silent - those picks quietly fall back to
        # estimated odds instead of real ones.
        need = {str(m["marketId"]) for m in server.MARKET_MAP.values()}
        missing = need - set(server.FIXTURE_MARKET_IDS)
        self.assertEqual(missing, set(),
                         "mapped but never fetched: %s" % sorted(missing))


class ExtractOdds(unittest.TestCase):
    def test_decodes_all_markets_and_skips_bad_values(self):
        event = {"markets": [
            {"id": "1", "specifier": "", "outcomes": [
                {"id": "1", "odds": "2.10"}, {"id": "2", "odds": "bad"}, {"id": "3", "odds": "3.00"}]},
            {"id": "18", "specifier": "total=2.5", "outcomes": [
                {"id": "12", "odds": "1.80"}, {"id": "13", "odds": "2.05"}]},
            {"id": "29", "specifier": "", "outcomes": [
                {"id": "74", "odds": "1.72"}, {"id": "76", "odds": None}]},
        ]}
        odds = server._extract_odds(event)
        self.assertEqual(odds["1"], 2.10)     # home
        self.assertEqual(odds["2"], 3.00)     # away (outcomeId 3)
        self.assertNotIn("X", odds)           # bad value skipped (outcomeId 2 = draw)
        self.assertEqual(odds["OVER_2.5"], 1.80)
        self.assertEqual(odds["UNDER_2.5"], 2.05)
        self.assertEqual(odds["GG"], 1.72)
        self.assertNotIn("NG", odds)          # None odds skipped


class MultiMarketMerge(unittest.TestCase):
    def _mock_get(self, market_outcomes):
        import re
        def fake_get(url, headers=None, impersonate=None, timeout=None):
            mid = re.search(r"marketId=(\d+)", url).group(1)
            page = int(re.search(r"pageNum=(\d+)", url).group(1))
            outs = market_outcomes.get(mid, [])
            class R:
                def json(self):
                    if page > 1 or not outs:
                        return {"bizCode": 10000, "data": {"tournaments": [], "events": []}}
                    market = {
                        "id": mid,
                        "specifier": outs[0][2],
                        "outcomes": [{"id": o[0], "odds": o[1]} for o in outs],
                    }
                    event = {
                        "eventId": "sr:match:1",
                        "homeTeamName": "A", "awayTeamName": "B",
                        "estimateStartTime": 1756000000000,
                        "markets": [market],
                    }
                    return {"bizCode": 10000, "data": {"tournaments": [{"events": [event]}]}}
            return R()
        return fake_get

    def test_merges_four_markets_onto_one_event(self):
        mo = {
            "1":  [("1", "2.10", ""), ("2", "3.40", ""), ("3", "3.00", "")],
            "10": [("9", "1.30", ""), ("10", "1.25", ""), ("11", "1.45", "")],
            "18": [("12", "1.80", "total=2.5"), ("13", "2.00", "total=2.5")],
            "29": [("74", "1.72", ""), ("76", "2.05", "")],
        }
        orig = server.requests.get
        server.requests.get = self._mock_get(mo)
        try:
            matches = server.fetch_sportybet_fixtures()
        finally:
            server.requests.get = orig
        self.assertEqual(len(matches), 1)                  # merged, not duplicated per market
        o = matches[0]["odds"]
        self.assertEqual(o["1"], 2.10); self.assertEqual(o["X"], 3.40); self.assertEqual(o["2"], 3.00)
        self.assertEqual(o["1X"], 1.30); self.assertEqual(o["X2"], 1.45)   # market 10
        self.assertEqual(o["GG"], 1.72)                                    # market 29


class BookingErrorPath(unittest.TestCase):
    def test_network_error_returns_error_dict_not_raise(self):
        def boom(*a, **k):
            raise server.RequestsError("simulated connection failure")
        orig = server.requests.post
        server.requests.post = boom
        try:
            res = server.generate_sportybet_code([{"eventId": "x", "marketId": "1", "outcomeId": "1"}])
        finally:
            server.requests.post = orig
        self.assertIsInstance(res, dict)
        self.assertIn("error", res)


class CacheLayer(unittest.TestCase):
    """The Redis layer is gated: with _redis None it must behave exactly like the
    old in-memory dict; with a (fake) Redis it round-trips through it."""

    def tearDown(self):
        server._redis = None  # never leak the fake client between tests

    def test_inmemory_roundtrip_when_no_redis(self):
        server._redis = None
        mem = {"at": 0, "data": None}
        self.assertIsNone(server._cache_get("x", mem))          # empty -> None
        server._cache_put("x", mem, [1, 2, 3])
        entry = server._cache_get("x", mem)
        self.assertEqual(entry["data"], [1, 2, 3])
        self.assertGreater(entry["at"], 0)
        self.assertEqual(mem["data"], [1, 2, 3])                # local dict updated

    def test_redis_roundtrip_shared_across_processes(self):
        # Minimal in-process fake of the redis commands we use.
        class FakeRedis:
            def __init__(self): self.kv = {}
            def get(self, k): return self.kv.get(k)
            def set(self, k, v, nx=False, ex=None):
                if nx and k in self.kv:
                    return None
                self.kv[k] = v
                return True
        server._redis = FakeRedis()
        mem = {"at": 0, "data": None}
        server._cache_put("fixtures", mem, [{"eventId": "1"}])
        # A DIFFERENT process/dict reads the same value from shared Redis:
        other = {"at": 0, "data": None}
        entry = server._cache_get("fixtures", other)
        self.assertEqual(entry["data"], [{"eventId": "1"}])


class FixtureLeagueLabel(unittest.TestCase):
    """Each fixture must carry its real competition.

    Flattening events out of `tournaments` used to drop the tournament, so
    every fixture arrived league-less and the consumer had to guess - which is
    how ordinary league games ended up labelled "England Cup" and a cup tie
    between two Premier League sides came out as the Premier League.
    """

    def _mock(self, payload_for_market):
        import re

        def fake_get(url, headers=None, impersonate=None, timeout=None):
            mid = re.search(r"marketId=(\d+)", url).group(1)
            page = int(re.search(r"pageNum=(\d+)", url).group(1))

            class R:
                def json(self):
                    if page > 1:
                        return {"bizCode": 10000, "data": {"tournaments": [], "events": []}}
                    return {"bizCode": 10000,
                            "data": payload_for_market.get(mid,
                                                           {"tournaments": [], "events": []})}
            return R()
        return fake_get

    def _run(self, payload_for_market):
        orig = server.requests.get
        server.requests.get = self._mock(payload_for_market)
        try:
            return server.fetch_sportybet_fixtures()
        finally:
            server.requests.get = orig

    @staticmethod
    def _event(eid="sr:match:1"):
        return {"eventId": eid, "homeTeamName": "A", "awayTeamName": "B",
                "estimateStartTime": 1756000000000, "markets": []}

    def _tournament(self, cat, name, eid="sr:match:1"):
        return {"tournaments": [{"category": {"name": cat}, "name": name,
                                 "events": [self._event(eid)]}]}

    def test_league_game_keeps_its_league(self):
        m = self._run({"1": self._tournament("England", "Premier League")})
        self.assertEqual(m[0]["league"], "England Premier League")

    def test_cup_tie_is_labelled_as_the_cup_not_the_league(self):
        m = self._run({"1": self._tournament("England", "FA Cup")})
        self.assertEqual(m[0]["league"], "England FA Cup")

    def test_event_without_a_tournament_gets_no_league_rather_than_a_guess(self):
        m = self._run({"1": {"tournaments": [], "events": [self._event()]}})
        self.assertEqual(m[0]["league"], "")

    def test_later_market_fills_a_league_the_first_one_lacked(self):
        # Market 1 lists the event loose (no tournament); market 10 names it.
        m = self._run({
            "1": {"tournaments": [], "events": [self._event()]},
            "10": self._tournament("Spain", "LaLiga"),
        })
        self.assertEqual(len(m), 1)                     # still merged, not duplicated
        self.assertEqual(m[0]["league"], "Spain LaLiga")

    def test_categoryless_tournament_falls_back_to_bare_name(self):
        m = self._run({"1": {"tournaments": [
            {"name": "Club Friendlies", "events": [self._event()]}]}})
        self.assertEqual(m[0]["league"], "Club Friendlies")


class LiveScoreIsNotTheFirstHalf(unittest.TestCase):
    """The live board published half-time scores for four months.

    SportyBet sends setScore as the running total and gameScore as the same
    thing split by period - Crystal Palace v Man City at 73 minutes carried
    setScore "1:3" and gameScore ["0:1","1:2"]. fetch_live_scores read
    gameScore[0] first, so it published the first half and never reached the
    setScore branch. 45 of the 71 live matches on the board were wrong when
    this was found, Bayern Munich among them, showing 1-0 at the 90th minute
    of a game that finished 4-1.

    It is not only cosmetic: the results sweep banks these as final scores, so
    a tip that landed is recorded as a loss.
    """

    def _one(self, **over):
        e = {"homeTeamName": "Crystal Palace", "awayTeamName": "Man City",
             "matchStatus": "H2", "playedSeconds": "73:29",
             "setScore": "1:3", "gameScore": ["0:1", "1:2"]}
        e.update(over)
        return {"bizCode": 10000,
                "data": [{"category": {"name": "England"}, "name": "Premier League",
                          "events": [e]}]}

    def _fetch(self, payload, pages=None):
        """Run fetch_live_scores against a stubbed SportyBet."""
        calls = {"n": 0}
        seq = pages if pages is not None else [payload]

        def fake_get(url, **kw):
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            class R:
                @staticmethod
                def json():
                    return seq[i]
            return R()

        real = server.requests.get
        server.requests.get = fake_get
        try:
            return server.fetch_live_scores(), calls["n"]
        finally:
            server.requests.get = real

    def test_second_half_goals_are_counted(self):
        out, _ = self._fetch(self._one())
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["homeScore"], out[0]["awayScore"]), (1, 3),
                         "must publish the running score, not the first half")

    def test_falls_back_to_summing_periods(self):
        # no setScore at all: add the halves up rather than taking one
        out, _ = self._fetch(self._one(setScore=None))
        self.assertEqual((out[0]["homeScore"], out[0]["awayScore"]), (1, 3))

    def test_goalless_first_half_is_not_reported_as_the_score(self):
        out, _ = self._fetch(self._one(setScore="1:0", gameScore=["0:0", "1:0"]))
        self.assertEqual((out[0]["homeScore"], out[0]["awayScore"]), (1, 0))

    def test_a_match_still_in_the_first_half_is_unaffected(self):
        out, _ = self._fetch(self._one(setScore="1:0", gameScore=["1:0"]))
        self.assertEqual((out[0]["homeScore"], out[0]["awayScore"]), (1, 0))


class LiveFeedDoesNotRepeatItself(unittest.TestCase):
    """liveOrPrematchEvents ignores pageNum.

    Pages 1 through 5 come back byte-identical, so looping them appended the
    same events five times: 400 entries for 80 matches, every client polling
    it every 30 seconds paying for four fifths of nothing.
    """

    def _page(self, event_id):
        return {"bizCode": 10000,
                "data": [{"category": {"name": "England"}, "name": "Premier League",
                          "events": [{"eventId": event_id,
                                      "homeTeamName": "A", "awayTeamName": "B",
                                      "matchStatus": "H2", "playedSeconds": "50:00",
                                      "setScore": "1:0", "gameScore": ["1:0"]}]}]}

    def test_identical_pages_are_fetched_once(self):
        t = LiveScoreIsNotTheFirstHalf()
        same = [self._page("sr:match:1")] * 5
        out, calls = t._fetch(None, pages=same)
        self.assertEqual(len(out), 1, "the same event must not be repeated")
        self.assertEqual(calls, 2, "stop after the first page that adds nothing")

    def test_real_paging_would_still_be_followed(self):
        t = LiveScoreIsNotTheFirstHalf()
        pages = [self._page("sr:match:%d" % i) for i in range(1, 4)]
        pages.append({"bizCode": 10000, "data": []})
        out, _ = t._fetch(None, pages=pages)
        self.assertEqual(len(out), 3, "distinct pages must all be kept")

class BookingIsNoLongerRefusedOnOurOwnCache(unittest.TestCase):
    """THE PREMISE THIS CLASS WAS BUILT ON TURNED OUT TO BE FALSE.

    It read: half the card has no team-totals market, one unplaceable leg among
    forty loses all forty, and we hold every event's odds already so the answer
    is known here without asking SportyBet.

    We do not hold every event's odds. SportyBet's fixtures feed carries a
    PARTIAL market set per event - measured 15 Sep on the next day's card,
    Russian Premier League events with 1/X/2, double chance, GG and the
    first-half lines and no Over/Under at all, Swiss Super League events with
    every Over/Under line and no 1X2 at all. Both book perfectly well.

    A reader's own SportyBet code, JTEJA5, held four legs this route was
    refusing, on the very event ids we match - Lugano 1X, Thun 1X, Baltika
    OVER_1.5, Lokomotiv OVER_2.5 - every one returned as "no market there"
    under a message naming SportyBet, who had never been asked.

    So nothing is refused here any more. A leg our cache cannot price is
    counted as a `suspect`, reported, and sent: the bookmaker names what it
    will not take and the client drops exactly that."""

    def setUp(self):
        server._FIXTURES_CACHE.clear()
        server._FIXTURES_CACHE.update({"at": 9e9, "data": [
            {"eventId": "ev:good", "homeTeam": "A", "awayTeam": "B",
             "odds": {"OVER_1.5": 1.2, "GG": 1.7, "HOME_OVER_0.5": 1.11}},
            {"eventId": "ev:thin", "homeTeam": "C", "awayTeam": "D",
             "odds": {"OVER_1.5": 1.3}},          # no team-totals market
        ]})

    def tearDown(self):
        server._FIXTURES_CACHE.clear()

    def test_a_pick_we_cannot_price_is_counted_and_still_sent(self):
        bad, how = server._unbookable([
            {"eventId": "ev:good", "prediction": "OVER_1.5"},
            {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
        ])
        self.assertEqual(bad, [], "a missing price must not refuse the leg")
        self.assertEqual(how["suspect"], 1, "but it is still worth counting")
        self.assertEqual(how["suspect_markets"], ["HOME_OVER_0.5"])

    def test_a_fully_bookable_slip_is_left_alone(self):
        self.assertEqual(server._unbookable([
            {"eventId": "ev:good", "prediction": "OVER_1.5"},
            {"eventId": "ev:good", "prediction": "GG"},
            {"eventId": "ev:thin", "prediction": "OVER_1.5"},
        ])[0], [])

    def test_an_event_we_hold_no_prices_for_is_not_judged(self):
        # absent from the cache entirely - we cannot say, so we do not
        bad, how = server._unbookable([
            {"eventId": "ev:unknown", "prediction": "HOME_OVER_0.5"},
        ])
        self.assertEqual(bad, [])
        # ...and it says so, rather than the leg vanishing from the count.
        self.assertEqual(how["unknown"], 1)
        self.assertEqual(how["judged"], 0)

    def test_an_empty_cache_blocks_nothing(self):
        """A server that has just started holds no prices. Refusing every slip
        until the first refresh would be worse than the failure this prevents."""
        server._FIXTURES_CACHE.clear()
        bad, how = server._unbookable([
            {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
        ])
        self.assertEqual(bad, [])
        self.assertIsNone(how["cache_age_s"], "no cache means no age to report")

    def test_a_refusal_says_how_old_the_odds_it_judged_on_were(self):
        """The reason this returns a pair at all.

        The cache lives 45 minutes and the browser read its own copy at page
        load, so the two disagree about TIME rather than about markets: a
        market thinned since our last refresh still shows a price on their
        screen. 54 refusals in five days and nothing could say whether they
        were real gaps or our copy being stale, because the age was never
        recorded. Shortening the TTL is not the fix - a refresh is ~49
        sequential requests and this server has been blocked for less - so the
        age has to be measured before anything is built on a guess."""
        import time as _t
        server._FIXTURES_CACHE.update({"at": _t.time() - 1800, "data": [
            {"eventId": "ev:thin", "homeTeam": "C", "awayTeam": "D",
             "odds": {"OVER_1.5": 1.3}},
        ]})
        bad, how = server._unbookable([
            {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
            {"eventId": "ev:thin", "prediction": "OVER_1.5"},
            {"eventId": "ev:gone", "prediction": "1X"},
        ])
        self.assertEqual(bad, [], "nothing is refused on our cache any more")
        self.assertEqual(how["suspect"], 1, "the unpriced market is counted, not condemned")
        self.assertGreaterEqual(how["cache_age_s"], 1700)
        self.assertLessEqual(how["cache_age_s"], 1900)
        self.assertEqual(how["judged"], 2, "two legs on an event we hold")
        self.assertEqual(how["unknown"], 1, "and one we cannot judge at all")

    def test_the_route_sends_the_slip_instead_of_refusing_it(self):
        """It used to refuse here and name the leg. It cannot: the market may
        be on their card and absent from our copy of it, which is what JTEJA5
        showed. The slip goes, and SportyBet answers for its own board."""
        called = {"n": 0}
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: (
            called.__setitem__("n", called["n"] + 1) or {"code": "OK123"})
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                ]})
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("unbookable", r.get_json() or {})
            self.assertEqual(called["n"], 1, "the bookmaker must be the one asked")
        finally:
            server.generate_sportybet_code = real

    def test_the_refusal_carries_the_cache_age_to_sentry(self):
        """_unbookable can measure the age all it likes; if the ROUTE does not
        pass it on, Sentry still cannot tell a stale-cache refusal from a real
        gap and the whole exercise is decorative. Mutation-tested: deleting
        cache_age_s from the report call broke nothing until this existed."""
        seen = {}
        real_report, real_code = server.report, server.generate_sportybet_code
        server.report = lambda msg, level="warning", **ctx: seen.update(ctx)
        server.generate_sportybet_code = lambda *a, **k: {"code": "X"}
        try:
            with server.app.test_client() as c:
                c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                    {"eventId": "ev:gone", "prediction": "1X"},
                ]})
        finally:
            server.report, server.generate_sportybet_code = real_report, real_code
        self.assertIn("cache_age_s", seen, "the age must reach Sentry")
        self.assertIn("legs_judged", seen)
        self.assertIn("legs_unknown", seen)
        self.assertEqual(seen["legs_unknown"], 1, "the leg we hold no prices for")
        self.assertEqual(seen["suspect_legs"], 1,
                         "a leg we cannot price is reported even though it is sent")

    def test_an_unpriced_leg_is_reported_as_news_not_as_a_fault(self):
        """SportyBet's odds feed carries less than their card does, so a leg we
        cannot price is the ordinary case and most of these slips are accepted.
        Reported at warning, it sat in Sentry beside the refusals that actually
        cost a reader their code, and a warning stream where everything is a
        warning gets ignored wholesale."""
        seen = {}
        real_report, real_code = server.report, server.generate_sportybet_code
        server.report = lambda msg, level="warning", **ctx: seen.update(
            {"msg": msg, "level": level})
        server.generate_sportybet_code = lambda *a, **k: {"code": "X"}
        try:
            with server.app.test_client() as c:
                c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                ]})
        finally:
            server.report, server.generate_sportybet_code = real_report, real_code
        self.assertIn("cannot price", seen.get("msg", ""))
        self.assertEqual(seen.get("level"), "info",
                         "an expected gap in their feed is not a fault")

    def test_the_level_reaches_the_log_and_not_only_sentry(self):
        """report() logged at warning whatever it was told, so demoting a call
        changed Sentry and left Railway shouting the same line."""
        with self.assertLogs(server.log, level="INFO") as cap:
            server.report("routine thing", level="info", n=1)
        self.assertTrue(any(r.levelname == "INFO" for r in cap.records),
                        "the log line must carry the level it was given")
        with self.assertLogs(server.log, level="WARNING") as cap:
            server.report("bad thing", n=1)
        self.assertTrue(any(r.levelname == "WARNING" for r in cap.records),
                        "and warning must still be the default")

    def test_a_refusal_that_names_nothing_still_hands_back_the_suspects(self):
        """SportyBet's refusal is one sentence about the whole slip - "invalid
        event data, no market there" - and names no event and no market. The
        client can only drop legs that are named, so without this it showed
        "SportyBet wouldn't take this slip" over forty legs and no way to find
        the bad one. Reported 20 Sep by a reader who could not get a code and
        was told nothing about which pick was the problem.

        The suspects are weak evidence BEFORE asking, which is why nothing is
        refused on them. After a refusal they are the only evidence there is."""
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: {
            "error": "invalid event data, no market there", "sent": []}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                ]})
        finally:
            server.generate_sportybet_code = real
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual([(b["eventId"], b["prediction"]) for b in body["unbookable"]],
                         [("ev:thin", "HOME_OVER_0.5")],
                         "the leg our cache could not price is the one to offer")
        self.assertEqual(body["unbookable"][0]["reason"], "suspect",
                         "named as a suspicion, not as the bookmaker's word")

    def test_nothing_is_invented_when_every_leg_looks_fine(self):
        """A refusal we have no candidate for must stay a plain error. Offering
        the whole slip as unbookable would hand the client a retry with nothing
        left in it, and offering a leg at random is a guess with somebody
        else's bet on it."""
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: {
            "error": "invalid event data, no market there", "sent": []}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:good", "prediction": "GG"},
                ]})
        finally:
            server.generate_sportybet_code = real
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("unbookable", r.get_json() or {})

    def test_a_slip_of_nothing_but_suspects_is_not_emptied(self):
        """Every leg suspect means the suspicion explains nothing. Dropping all
        of them leaves no slip to retry, so the client would show its "can't
        take any of these" card for a refusal that may have had another cause
        entirely."""
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: {
            "error": "invalid event data, no market there", "sent": []}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                    {"eventId": "ev:thin", "prediction": "GG"},
                ]})
        finally:
            server.generate_sportybet_code = real
        body = r.get_json() or {}
        self.assertNotIn("unbookable", body)
        # BUT THE READER IS STILL OWED THE REASON. `unbookable` means "drop
        # these and the rest may book", which is meaningless when it covers the
        # whole slip - so it stays absent. The markets our copy of their card
        # has no price for are said separately, as information rather than as a
        # list to drop. Without this the page can only say "SportyBet wouldn't
        # take this slip" and name nothing at all, which is what a reader hit
        # on 21 Sep and what the live probe reproduced.
        self.assertTrue(body.get("suspectAll"),
                        "an all-suspect refusal must say so")
        self.assertEqual(sorted(body.get("suspectMarkets") or []),
                         ["GG", "HOME_OVER_0.5"],
                         "and name the markets it could not price")

    def test_a_refusal_with_no_suspects_at_all_says_nothing_extra(self):
        """Nothing invented stays nothing invented: a slip we have no doubt
        about gets the plain error, with neither field on it."""
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: {
            "error": "invalid event data, no market there", "sent": []}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:good", "prediction": "GG"},
                ]})
        finally:
            server.generate_sportybet_code = real
        body = r.get_json() or {}
        self.assertNotIn("unbookable", body)
        self.assertNotIn("suspectAll", body)
        self.assertNotIn("suspectMarkets", body)

class FailuresAreReported(unittest.TestCase):
    """A booking rejection is not an exception, so nothing raised and Sentry
    never saw one. report() makes them searchable events - and must never be
    able to break the booking it is reporting on."""

    def test_no_sentry_configured_is_a_silent_no_op(self):
        real = server._sentry
        server._sentry = None
        try:
            server.report("nothing should explode", legs=3)   # must not raise
        finally:
            server._sentry = real

    def test_it_tags_and_sends_when_sentry_is_present(self):
        sent = {}

        class Scope:
            def set_tag(self, k, v): sent.setdefault("tags", {})[k] = v
            def set_extra(self, k, v): sent.setdefault("extra", {})[k] = v

        class Ctx:
            def __enter__(self): return Scope()
            def __exit__(self, *a): return False

        class FakeSentry:
            @staticmethod
            def push_scope(): return Ctx()
            @staticmethod
            def capture_message(msg, level=None):
                sent["msg"] = msg; sent["level"] = level

        real = server._sentry
        server._sentry = FakeSentry
        try:
            server.report("booking: picks with no market at SportyBet",
                          bad_legs=2, total_legs=40, markets="HOME_OVER_0.5")
        finally:
            server._sentry = real

        self.assertEqual(sent["msg"], "booking: picks with no market at SportyBet")
        self.assertEqual(sent["level"], "warning")
        self.assertEqual(sent["tags"]["area"], "booking")
        self.assertEqual(sent["extra"]["bad_legs"], 2)
        self.assertEqual(sent["extra"]["markets"], "HOME_OVER_0.5")

    def test_a_broken_reporter_cannot_break_a_booking(self):
        class Exploding:
            @staticmethod
            def push_scope(): raise RuntimeError("sentry is down")

        real = server._sentry
        server._sentry = Exploding
        try:
            server.report("still fine", legs=1)   # swallowed, not raised
        finally:
            server._sentry = real

    def test_the_route_still_answers_when_reporting_explodes(self):
        """The whole point: reporting sits on the booking path."""
        class Exploding:
            @staticmethod
            def push_scope(): raise RuntimeError("sentry is down")

        server._FIXTURES_CACHE.clear()
        server._FIXTURES_CACHE.update({"at": 9e9, "data": [
            {"eventId": "ev:thin", "odds": {"OVER_1.5": 1.3}}]})
        real = server._sentry
        server._sentry = Exploding
        try:
            with server.app.test_client() as c:
                # An unmapped market: the one refusal this route still makes on
                # its own, and the path reporting sits on.
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:thin", "prediction": "NOT_A_MARKET"}]})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.get_json()["unbookable"],
                                 [{"eventId": "ev:thin", "prediction": "NOT_A_MARKET",
                                   "reason": "not_mapped"}])
        finally:
            server._sentry = real
            server._FIXTURES_CACHE.clear()


class Bet9jaFixturesRoute(unittest.TestCase):
    """The route serves the background sweep, and says so when it has none.

    An empty bag with success: true is exactly how the Bet9ja integration spent
    its first hour live: the sweep was getting a block page, every league came
    back empty, and the route reported a healthy fetch of nothing. A 503 is the
    honest answer and the one that shows up in monitoring.
    """

    def setUp(self):
        server._BET9JA_CACHE.clear()
        server._BET9JA_CACHE.update({"at": 0, "data": None})

    tearDown = setUp

    def test_no_sweep_yet_is_a_503_not_an_empty_success(self):
        with server.app.test_client() as c:
            r = c.get("/api/bet9ja/fixtures")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["success"])

    def test_it_serves_the_swept_bag(self):
        server._cache_put("bet9ja", server._BET9JA_CACHE,
                          {"1": {"teams": "A - B"}, "2": {"teams": "C - D"}})
        with server.app.test_client() as c:
            r = c.get("/api/bet9ja/fixtures")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["count"], 2)
        self.assertTrue(body["cached"])
        self.assertIn("ageSeconds", body)

    def test_the_route_never_sweeps_on_the_request_path(self):
        """Two minutes of work behind a web request is a timeout, not a page."""
        called = {"n": 0}
        real = server.bet9ja.all_fixtures
        server.bet9ja.all_fixtures = lambda *a, **k: called.__setitem__("n", 1)
        try:
            with server.app.test_client() as c:
                c.get("/api/bet9ja/fixtures")
        finally:
            server.bet9ja.all_fixtures = real
        self.assertEqual(called["n"], 0)

    def test_a_single_competition_is_still_fetchable_for_debugging(self):
        real = server.bet9ja.fetch_league
        server.bet9ja.fetch_league = lambda gid, **k: {"9": {"teams": "E - F"}}
        try:
            with server.app.test_client() as c:
                r = c.get("/api/bet9ja/fixtures?league=1348874")
        finally:
            server.bet9ja.fetch_league = real
        body = r.get_json()
        self.assertEqual(body["league"], 1348874)
        self.assertEqual(body["count"], 1)
        self.assertFalse(body["cached"])

    def test_a_junk_league_is_a_400(self):
        with server.app.test_client() as c:
            r = c.get("/api/bet9ja/fixtures?league=notanumber")
        self.assertEqual(r.status_code, 400)


class Bet9jaRefreshGuards(unittest.TestCase):
    """A sweep that comes back short must not replace a fuller one."""

    def setUp(self):
        server._BET9JA_CACHE.clear()
        server._BET9JA_CACHE.update({"at": 0, "data": None})

    tearDown = setUp

    def _sweep(self, fixtures, expected):
        real = server.bet9ja.all_fixtures
        server.bet9ja.all_fixtures = lambda *a, **k: (
            fixtures, {"expected": expected, "collected": len(fixtures),
                       "competitions": 170, "failed": [], "short": []})
        try:
            return server._refresh_bet9ja_once()
        finally:
            server.bet9ja.all_fixtures = real

    def test_a_full_sweep_is_stored(self):
        self.assertTrue(self._sweep({str(i): {} for i in range(100)}, 100))
        self.assertEqual(len(server._cache_get("bet9ja", server._BET9JA_CACHE)["data"]), 100)

    def test_far_under_their_own_count_is_refused(self):
        # Their catalogue is an outside opinion; a third of it is a block, not
        # a quiet day.
        self.assertFalse(self._sweep({str(i): {} for i in range(30)}, 100))
        self.assertIsNone(server._BET9JA_CACHE["data"])

    def test_nothing_at_all_is_refused(self):
        self.assertFalse(self._sweep({}, 100))

    def test_a_shrunken_sweep_does_not_replace_a_fuller_one(self):
        self._sweep({str(i): {} for i in range(100)}, 100)
        # Their count drops with it, so only the previous-copy guard can catch
        # this one.
        self.assertFalse(self._sweep({str(i): {} for i in range(50)}, 50))
        self.assertEqual(len(server._cache_get("bet9ja", server._BET9JA_CACHE)["data"]), 100)

    def test_a_raising_sweep_keeps_the_previous_copy(self):
        self._sweep({str(i): {} for i in range(100)}, 100)
        real = server.bet9ja.all_fixtures
        def boom(*a, **k):
            raise OSError("bet9ja down")
        server.bet9ja.all_fixtures = boom
        try:
            self.assertFalse(server._refresh_bet9ja_once())
        finally:
            server.bet9ja.all_fixtures = real
        self.assertEqual(len(server._cache_get("bet9ja", server._BET9JA_CACHE)["data"]), 100)


class Bet9jaRejectsLikeSportyBet(unittest.TestCase):
    """One bad leg must name every bad leg, not just itself.

    The route used to return on the first pick Bet9ja would not price. That
    told the caller about one leg out of forty and gave it nothing to retry
    with. The SportyBet route has answered with a named `unbookable` list since
    the "no market there" incident, and the client drops exactly those and
    retries - so both bookmakers answer in the same shape or that path only
    works for one of them.
    """

    def _post(self, priced):
        """priced: {eventId: [codes Bet9ja will take]}"""
        real_ev, real_gen = server.bet9ja.fetch_event, server.bet9ja.generate_code
        server.bet9ja.fetch_event = lambda eid: (
            {"eventId": eid, "raw": {c: "2.00" for c in priced.get(str(eid), [])}}
            if str(eid) in priced else None)
        server.bet9ja.generate_code = lambda sels: {"code": "B9CODE", "legs": len(sels)}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/bet9ja/booking-code", json={"selections": [
                    {"eventId": "1", "code": "1X"},
                    {"eventId": "2", "code": "HOME_OVER_0.5"},
                    {"eventId": "3", "code": "OVER_1.5"},
                ]})
            return r.status_code, r.get_json()
        finally:
            server.bet9ja.fetch_event, server.bet9ja.generate_code = real_ev, real_gen

    def test_every_unpriced_leg_is_named(self):
        code, body = self._post({"1": ["1X"]})   # 2 and 3 unbookable
        self.assertEqual(code, 400)
        self.assertFalse(body["success"])
        # The PAIR is the contract - dropUnbookable keys on
        # eventId + "|" + prediction - so that is what this pins. `reason`
        # was added beside it, and an exact-dict assertion turned an additive
        # field into a failure while saying nothing about whether the client
        # still works.
        self.assertEqual(
            [(b["eventId"], b["prediction"]) for b in body["unbookable"]],
            [("2", "HOME_OVER_0.5"), ("3", "OVER_1.5")])

    def test_the_shape_matches_the_sportybet_route(self):
        _code, body = self._post({"1": ["1X"]})
        for k in ("success", "message", "detail", "unbookable"):
            self.assertIn(k, body, k + " is missing, so dropUnbookable cannot read it")

    def test_an_event_bet9ja_does_not_carry_is_unbookable_not_a_crash(self):
        _code, body = self._post({})             # fetch_event returns None for all
        self.assertEqual(len(body["unbookable"]), 3)

    def test_a_fully_priced_slip_still_books(self):
        code, body = self._post({"1": ["1X"], "2": ["HOME_OVER_0.5"], "3": ["OVER_1.5"]})
        self.assertEqual(code, 200)
        self.assertEqual(body["code"], "B9CODE")


class Bet9jaTellsTheBugFromTheBookmaker(unittest.TestCase):
    """A leg can be unbookable for three reasons, and only one is our fault.

    They were logged under one message, so Sentry counted them together as a
    single issue. On 2 September that issue was raised to High priority and
    read as a regression, for a slip doing exactly the right thing: Bet9ja does
    not price team-to-score on Spartak Moscow v Rodina Moscow, and no code
    change conjures a price a bookmaker is not offering.

    Meanwhile the reason that IS a bug - a market missing from MARKET_MAP,
    which refuses that leg on every fixture forever - looks identical in the
    log. That one hid both-to-score being dropped from every Bet9ja slip while
    it was switched on by default.

    So the split is not tidying. It is the difference between a signal you act
    on and one you archive.
    """

    def _run(self, selections, priced, seen, prices=None):
        real_ev, real_gen = server.bet9ja.fetch_event, server.bet9ja.generate_code
        real_report = server.report
        # `prices` overrides the price of one "eventId|code" pair. Everything
        # else is 2.00, which is a market that is open and worth booking.
        server.bet9ja.fetch_event = lambda eid: (
            {"eventId": eid,
             "raw": {c: (prices or {}).get("%s|%s" % (eid, c), "2.00")
                     for c in priced.get(str(eid), [])}}
            if str(eid) in priced else None)
        server.bet9ja.generate_code = lambda sels: {"code": "B9CODE", "legs": len(sels)}
        server.report = lambda msg, level="warning", **ctx: seen.append((msg, level, ctx))
        try:
            with server.app.test_client() as c:
                r = c.post("/api/bet9ja/booking-code", json={"selections": selections})
            return r.status_code, r.get_json()
        finally:
            server.bet9ja.fetch_event, server.bet9ja.generate_code = real_ev, real_gen
            server.report = real_report

    # An unmapped market is the one worth waking up for.

    def test_an_unmapped_market_is_reported_as_its_own_bug(self):
        seen = []
        _c, body = self._run([{"eventId": "1", "code": "NOT_A_REAL_MARKET"}], {}, seen)
        msgs = [m for m, _l, _c2 in seen]
        self.assertIn("booking: Bet9ja market is not mapped", msgs)
        self.assertNotIn("booking: Bet9ja does not price this market on this fixture", msgs)
        self.assertEqual(body["unbookable"][0]["reason"], "not_mapped")

    def test_an_unmapped_market_costs_no_request(self):
        """It is knowable locally. Fetching an event to be told what MARKET_MAP
        already says is a request per bad leg, on the booking path."""
        calls = []
        real_ev = server.bet9ja.fetch_event
        server.bet9ja.fetch_event = lambda eid: calls.append(eid)
        try:
            with server.app.test_client() as c:
                c.post("/api/bet9ja/booking-code", json={"selections": [
                    {"eventId": "1", "code": "NOT_A_REAL_MARKET"}]})
        finally:
            server.bet9ja.fetch_event = real_ev
        self.assertEqual(calls, [], "the mapping is checked before the fetch")

    def test_the_unmapped_warning_names_the_market_so_it_can_be_added(self):
        seen = []
        self._run([{"eventId": "1", "code": "NOT_A_REAL_MARKET"}], {}, seen)
        ctx = [c for m, _l, c in seen if m == "booking: Bet9ja market is not mapped"][0]
        self.assertIn("NOT_A_REAL_MARKET", ctx["markets"])

    # A market Bet9ja simply does not price is not a fault.

    def test_an_unpriced_market_is_info_not_warning(self):
        seen = []
        _c, body = self._run(
            [{"eventId": "1", "code": "HOME_OVER_0.5"}], {"1": ["1X"]}, seen)
        hits = [(m, l) for m, l, _c2 in seen
                if m == "booking: Bet9ja does not price this market on this fixture"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "info",
                         "a bookmaker not offering a market is not our error")
        self.assertEqual(body["unbookable"][0]["reason"], "not_priced")

    def test_an_unpriced_market_never_raises_the_mapping_alarm(self):
        """The whole point. This is the case that was firing hourly and being
        read as the mapping bug coming back."""
        seen = []
        self._run([{"eventId": "1", "code": "HOME_OVER_0.5"}], {"1": ["1X"]}, seen)
        self.assertNotIn("booking: Bet9ja market is not mapped",
                         [m for m, _l, _c in seen])

    def test_a_missing_event_is_its_own_cause_again(self):
        seen = []
        _c, body = self._run([{"eventId": "9", "code": "1X"}], {}, seen)
        self.assertIn("booking: Bet9ja event would not load", [m for m, _l, _c in seen])
        self.assertEqual(body["unbookable"][0]["reason"], "event_gone")

    # A market priced at 1 is a market that is closed.

    def test_a_market_priced_at_one_is_refused_before_it_is_booked(self):
        """Bet9ja leaves a suspended market on the board with its price
        collapsed to 1 instead of removing it, so `code in raw` is true and the
        leg looks bookable. Sending it 502s their booking origin and takes the
        whole slip with it - no unbookable list, nothing to retry. Measured
        13 Sep 2026 on Elversberg v Bayern Munich, an hour after kick-off: that
        leg 502'd alone while its four companions booked."""
        seen = []
        _c, body = self._run([{"eventId": "1", "code": "OVER_1.5"}],
                             {"1": ["OVER_1.5"]}, seen,
                             prices={"1|OVER_1.5": "1"})
        self.assertEqual(body["unbookable"][0]["reason"], "suspended")
        hits = [(m, l) for m, l, _c2 in seen
                if m == "booking: Bet9ja has suspended this market"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "info",
                         "a closed market is how betting works, not a fault")

    def test_an_open_market_is_still_booked(self):
        """The guard must not eat the ordinary case."""
        _c, body = self._run([{"eventId": "1", "code": "OVER_1.5"}],
                             {"1": ["OVER_1.5"]}, [],
                             prices={"1|OVER_1.5": "1.11"})
        self.assertTrue(body.get("success"), body)

    def test_a_price_that_is_not_a_number_is_refused_rather_than_sent(self):
        """An unparseable price is not evidence that the market is open."""
        _c, body = self._run([{"eventId": "1", "code": "OVER_1.5"}],
                             {"1": ["OVER_1.5"]}, [],
                             prices={"1|OVER_1.5": "SUSP"})
        self.assertEqual(body["unbookable"][0]["reason"], "suspended")

    # Whatever the reason, the punter and the client see one list.

    def test_all_three_causes_still_come_back_as_unbookable(self):
        seen = []
        _c, body = self._run([
            {"eventId": "1", "code": "NOT_A_REAL_MARKET"},   # not mapped
            {"eventId": "2", "code": "HOME_OVER_0.5"},       # mapped, unpriced
            {"eventId": "9", "code": "1X"},                  # event gone
            {"eventId": "2", "code": "1X"},                  # fine
        ], {"2": ["1X"]}, seen)
        self.assertEqual(
            sorted(b["reason"] for b in body["unbookable"]),
            ["event_gone", "not_mapped", "not_priced"])
        # Three separate Sentry issues, not one mixed bag.
        self.assertEqual(len(set(m for m, _l, _c in seen)), 3)

    def test_the_retry_contract_survives_the_split(self):
        """dropUnbookable keys on eventId + "|" + prediction. If the split had
        renamed or dropped either, every Bet9ja retry would resend the same
        doomed slip and the loop would look like a bookmaker outage."""
        seen = []
        _c, body = self._run([
            {"eventId": "1", "code": "NOT_A_REAL_MARKET"},
            {"eventId": "2", "code": "HOME_OVER_0.5"},
        ], {"2": ["1X"]}, seen)
        for b in body["unbookable"]:
            self.assertIn("eventId", b)
            self.assertIn("prediction", b)

    def test_a_fully_priced_slip_reports_nothing_at_all(self):
        seen = []
        code, body = self._run([{"eventId": "1", "code": "1X"}], {"1": ["1X"]}, seen)
        self.assertEqual(code, 200)
        self.assertEqual(body["code"], "B9CODE")
        self.assertEqual(seen, [], "a clean booking must be silent")


class TheServerMustNotMultiplyTheSweeps(unittest.TestCase):
    """Why the worker count is pinned, checked in the file that is actually read.

    First attempt put this in the Procfile. Railway does not run the Procfile -
    the start command is set on the service - so the container came up
    `Using worker: sync` thirty-six seconds after the commit that supposedly
    changed it, and the before/after timings around it were noise. Gunicorn
    loads gunicorn.conf.py from the working directory whatever the command
    line says, so that is where the settings live and what these assert.

    The rule itself: gunicorn imports the app once PER WORKER and both
    refreshers start at import, so every extra worker starts another
    fifty-six-request SportyBet sweep and another Bet9ja sweep - at endpoints
    that already refuse bursts from datacentre IPs. Raising the worker count
    to make booking feel faster is how a slowness problem becomes an outage.
    Request concurrency comes from threads instead; the work is all I/O.
    """

    def _conf(self):
        path = os.path.join(os.path.dirname(__file__), "gunicorn.conf.py")
        ns = {}
        with open(path, encoding="utf-8") as f:
            exec(f.read(), ns)
        return ns

    def test_exactly_one_worker(self):
        self.assertEqual(self._conf()["workers"], 1,
                         "each extra worker starts another SportyBet sweep")

    def test_concurrency_comes_from_threads(self):
        c = self._conf()
        self.assertEqual(c["worker_class"], "gthread")
        self.assertGreater(c["threads"], 1,
                           "one thread is the single sync worker again")

    def test_the_timeouts_are_stated_here_too(self):
        """So they survive if the service's start command is ever simplified
        to a bare `gunicorn server:app`."""
        c = self._conf()
        self.assertEqual(c["timeout"], 90)
        self.assertEqual(c["graceful_timeout"], 30,
                         "a restart must drain in-flight bookings, not cut them")

    def test_the_refreshers_really_do_start_at_import(self):
        """The premise of the one-worker rule. If these ever move behind a
        guard that runs once per deploy rather than once per process, the rule
        can be revisited - and this test should be what says so."""
        src = open(os.path.join(os.path.dirname(__file__), "server.py"),
                   encoding="utf-8").read()
        for call in ("_start_fixtures_thread()", "_start_bet9ja_thread()"):
            self.assertRegex(src, r"(?m)^" + re.escape(call),
                             call + " must be at module level for this rule to hold")

    def test_the_procfile_does_not_contradict_the_config(self):
        """The Procfile is not read by Railway, but it is the first place a
        person looks. It must not say something different from the truth."""
        with open(os.path.join(os.path.dirname(__file__), "Procfile")) as f:
            proc = f.read()
        c = self._conf()
        if "--workers" in proc:
            m = re.search(r"--workers (\d+)", proc)
            self.assertEqual(int(m.group(1)), c["workers"],
                             "Procfile and gunicorn.conf.py disagree on workers")
        if "--worker-class" in proc:
            self.assertIn(c["worker_class"], proc,
                          "Procfile and gunicorn.conf.py disagree on worker class")


class ReadASlipBack(unittest.TestCase):
    """Both books hand a code back, which is what the splitter and the
    converter rest on. The shapes are theirs; the normalisation is ours, and it
    is the part that can quietly lie - a leg dropped here is a game the punter
    had on their slip and will not see on ours."""

    SPORTY_OK = {"bizCode": 10000, "data": {"ticket": {"selections": [
        {"eventId": "sr:match:1", "marketId": "18", "outcomeId": "12",
         "specifier": "total=1.5"},
        {"eventId": "sr:match:9", "marketId": "18", "outcomeId": "12",
         "specifier": "total=1.5"},
    ]}}}

    def _sporty(self, payload, cached=None):
        real_get = server.requests.get
        real_cache = server._FIXTURES_CACHE.copy()
        class R:
            def json(self_inner): return payload
        server.requests.get = lambda *a, **k: R()
        if cached is not None:
            server._FIXTURES_CACHE.update({"at": server.time.time(), "data": cached})
        try:
            with server.app.test_client() as c:
                r = c.get("/api/slip?book=sporty&code=ABC123")
            return r.status_code, r.get_json()
        finally:
            server.requests.get = real_get
            server._FIXTURES_CACHE.clear()
            server._FIXTURES_CACHE.update(real_cache)

    def test_a_slip_comes_back_in_our_own_market_codes(self):
        """Their (marketId, outcomeId, specifier) triple is meaningless to the
        rest of this project. _ODDS_LOOKUP already maps it and is exercised on
        every odds refresh, so the read borrows the mapping rather than
        growing a second one that can drift from it."""
        _c, body = self._sporty(self.SPORTY_OK)
        self.assertTrue(body["success"])
        self.assertEqual([l["prediction"] for l in body["legs"]],
                         ["OVER_1.5", "OVER_1.5"])

    def test_a_leg_we_cannot_name_is_still_returned(self):
        """Their read carries no team names, so names come from the fixtures
        cache. A game we do not carry is a fact the caller has to see - dropping
        it would hand back a shorter slip than the punter booked and say
        nothing about it."""
        _c, body = self._sporty(self.SPORTY_OK, cached=[
            {"eventId": "sr:match:1", "homeTeam": "Arsenal", "awayTeam": "Spurs",
             "league": "England Premier League", "odds": {"OVER_1.5": 1.2}}])
        self.assertEqual(body["read"], 2)
        self.assertEqual(body["legs"][0]["home"], "Arsenal")
        self.assertEqual(body["legs"][1]["home"], "",
                         "an unknown event keeps its place in the slip")

    def test_the_count_is_what_was_read_not_what_was_booked(self):
        """A reprint is not a transcript: Bet9ja drops events from a coupon on
        its own schedule and says nothing about how many there were. `read` is
        named for what it is so nothing downstream can imply otherwise."""
        _c, body = self._sporty(self.SPORTY_OK)
        self.assertEqual(body["read"], len(body["legs"]))

    def test_a_code_with_nothing_behind_it_is_a_404(self):
        code, body = self._sporty({"bizCode": 19000, "message": "Invalid"})
        self.assertEqual(code, 404)
        self.assertTrue(body["notFound"])

    def test_a_code_that_is_not_a_code_never_reaches_the_bookmaker(self):
        """It is a request made in somebody else's name against a third party,
        so the shape is checked here rather than by them."""
        sent = []
        real_get = server.requests.get
        server.requests.get = lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError("sent"))
        try:
            with server.app.test_client() as c:
                for bad in ("", "!!", "a" * 40, "../../etc/passwd"):
                    r = c.get("/api/slip?book=sporty&code=" + bad)
                    self.assertEqual(r.status_code, 400, bad)
        finally:
            server.requests.get = real_get
        self.assertEqual(sent, [])

    def test_an_unknown_bookmaker_is_refused(self):
        with server.app.test_client() as c:
            r = c.get("/api/slip?book=acme&code=ABC123")
        self.assertEqual(r.status_code, 400)

    def test_bet9ja_legs_carry_their_own_names(self):
        """Their read names the teams, so unlike SportyBet's it needs no cache
        to be useful."""
        real = server.bet9ja.read_coupon
        server.bet9ja.read_coupon = lambda code, **k: {"legs": [
            {"eventId": 1, "prediction": "OVER_1.5", "raw": "S_OU@1.5_O",
             "home": "PSV", "away": "Sparta Rotterdam", "league": "Eredivisie",
             "kickoff": "2026-09-13T18:00:00Z", "odds": 1.04}]}
        try:
            with server.app.test_client() as c:
                body = c.get("/api/slip?book=bet9ja&code=ABC123").get_json()
        finally:
            server.bet9ja.read_coupon = real
        self.assertEqual(body["legs"][0]["home"], "PSV")
        self.assertEqual(body["book"], "bet9ja")


class MarketsWeMoveButDoNotModel(unittest.TestCase):
    """1UP and 2UP are both books' early-payout promotion: the bet pays as soon
    as the side goes one (or two) ahead, whatever the final score. We have no
    opinion about them and never will - they exist so a slip somebody else
    built can be read, re-cut and moved without us pretending to rate it.

    Both were verified against live events before being written down, and both
    booked: Bet9ja 5RH8VCV and SportyBet ZVD0MB, 13 Sep 2026."""

    def test_the_promotion_markets_reach_both_books(self):
        for code in ("UP1_1", "UP1_X", "UP1_2", "UP2_1", "UP2_X", "UP2_2"):
            self.assertIsNotNone(server.market_for(code), code + " has no SportyBet id")
            self.assertIsNotNone(server.bet9ja.market_for(code), code + " has no Bet9ja key")

    def test_a_promotion_leg_decodes_back_to_its_own_code(self):
        """The read has to round-trip, or a converted slip loses the leg it
        came in on."""
        for code in ("UP1_1", "UP1_X", "UP1_2", "UP2_1", "UP2_X", "UP2_2"):
            m = server.market_for(code)
            key = (str(m["marketId"]), str(m["outcomeId"]), m.get("specifier") or "")
            self.assertEqual(server._ODDS_LOOKUP.get(key), code,
                             code + " does not decode back from " + str(key))

    def test_1UP_and_2UP_are_not_the_same_market(self):
        """1UP is 60200 and 2UP is 60100 - the LOWER id is the later
        promotion. Reading that pair the obvious way books a different bet."""
        self.assertEqual(server.market_for("UP1_1")["marketId"], 60200)
        self.assertEqual(server.market_for("UP2_1")["marketId"], 60100)

    def test_the_sign_is_not_the_side_on_bet9ja(self):
        """S_1X21 spells home/draw/away as 11/X1/21 and S_1X22 as 12/X2/22:
        the trailing digit is WHICH PROMOTION, not which side. Reading it the
        obvious way books the wrong team."""
        self.assertEqual(server.bet9ja.market_for("UP1_1")[0], "S_1X21_11")
        self.assertEqual(server.bet9ja.market_for("UP1_2")[0], "S_1X21_21")
        self.assertEqual(server.bet9ja.market_for("UP2_1")[0], "S_1X22_12")
        self.assertEqual(server.bet9ja.market_for("UP2_2")[0], "S_1X22_22")

    def test_nothing_we_predict_was_disturbed(self):
        """The 24 modelled markets keep their ids. This table sits beside them
        and a collision would silently repoint a market the record is built
        on."""
        for code, m in (("1", ("1", "1")), ("OVER_1.5", ("18", "12")),
                        ("GG", ("29", "74"))):
            got = server.MARKET_MAP[code]
            self.assertEqual((str(got["marketId"]), str(got["outcomeId"])), m)
        self.assertNotIn("UP2_1", server.MARKET_MAP,
                         "a pass-through market must never join the swept table")
        self.assertNotIn("UP2_1", server.bet9ja.MARKET_MAP)

    def test_the_promotions_carry_no_specifier(self):
        """A stray specifier would be sent as a line and refused."""
        for code in ("UP1_1", "UP2_2"):
            self.assertEqual(server.market_for(code).get("specifier"), "")



class ThePreflightOnlyJudgesWhatItCanSee(unittest.TestCase):
    """The fixtures sweep fetches the 24 markets MARKET_MAP names and nothing
    else, so a pass-through pick has no price in that cache whether SportyBet
    sells it or not. Judging one anyway refused every converted leg as "no
    market there" before the slip ever reached the bookmaker.

    Found by converting a real Bet9ja code on the live site - Le Mans +0.5,
    a line SportyBet prices perfectly well - not by reading this function."""

    def _judge(self, picks, cached):
        real = server._FIXTURES_CACHE.copy()
        server._FIXTURES_CACHE.update({"at": server.time.time(), "data": cached})
        try:
            return server._unbookable(picks)
        finally:
            server._FIXTURES_CACHE.clear()
            server._FIXTURES_CACHE.update(real)

    CACHE = [{"eventId": "e1", "odds": {"OVER_1.5": 1.3}}]

    def test_a_pass_through_market_is_never_refused_here(self):
        for code in ("AH_1_0.5", "CORNERS_OV_8.5", "UP2_1", "CARD_H_3", "MIXGG_1"):
            bad, meta = self._judge(
                [{"eventId": "e1", "prediction": code}], self.CACHE)
            self.assertEqual(bad, [], code + " was refused before being sent")
            self.assertEqual(meta["unknown"], 1, code + " should count as unknown")

    def test_a_modelled_market_is_still_judged(self):
        """The guard must not blind the check it was built for: half the card
        carries no team-totals market, and one unplaceable leg among forty
        loses all forty."""
        bad, how = self._judge(
            [{"eventId": "e1", "prediction": "HOME_OVER_1.5"}], self.CACHE)
        self.assertEqual(bad, [], "our cache cannot condemn a market any more")
        self.assertEqual(how["suspect"], 1, "but it is still counted and reported")

    def test_a_priced_modelled_market_passes(self):
        bad, _m = self._judge(
            [{"eventId": "e1", "prediction": "OVER_1.5"}], self.CACHE)
        self.assertEqual(bad, [])

    def test_sportybet_quotes_no_quarter_handicaps(self):
        """Measured across 355 events: halves and wholes from -4.5 to 5, and
        no quarter lines at all. Generating them made codes that could never
        be booked here."""
        self.assertIsNone(server.market_for("AH_1_-0.25"))
        self.assertIsNotNone(server.market_for("AH_1_-0.5"))
        self.assertIsNotNone(server.bet9ja.market_for("AH_1_-0.25"),
                             "Bet9ja does quote quarters - those split there")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Bet9jaPassThroughBooks(unittest.TestCase):
    """A pass-through market is mapped, and the booking route has to agree.

    The route gated every leg on MARKET_MAP, which names the 24 markets we
    model and nothing else, so a handicap, a corner line, a team card or a 2UP
    came back "not_mapped" however well Bet9ja prices it. Nothing converted
    from SportyBet carrying one could ever be booked here. Found on the live
    site the same afternoon the mirror-image bug was fixed on the other book.
    """

    def _post(self, selections, raw):
        real_ev, real_gen = server.bet9ja.fetch_event, server.bet9ja.generate_code
        server.bet9ja.fetch_event = lambda eid: {"eventId": eid, "raw": raw}
        server.bet9ja.generate_code = lambda sels: {"code": "B9CODE", "legs": len(sels)}
        try:
            with server.app.test_client() as c:
                r = c.post("/api/bet9ja/booking-code",
                           json={"selections": selections})
            return r.status_code, r.get_json()
        finally:
            server.bet9ja.fetch_event, server.bet9ja.generate_code = real_ev, real_gen

    def test_a_pass_through_leg_is_not_refused_before_it_is_sent(self):
        code, body = self._post([{"eventId": "1", "code": "AH_1_-0.5"}],
                                {"AH_1_-0.5": "1.85"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("success"))

    def test_a_market_in_neither_table_is_still_not_mapped(self):
        _c, body = self._post([{"eventId": "1", "code": "NOT_A_REAL_MARKET"}],
                              {"AH_1_-0.5": "1.85"})
        self.assertEqual(body["unbookable"][0]["reason"], "not_mapped")

    def test_a_pass_through_market_the_fixture_lacks_is_still_named(self):
        _c, body = self._post([{"eventId": "1", "code": "CORNERS_OV_9.5"}],
                              {"AH_1_-0.5": "1.85"})
        self.assertEqual(body["unbookable"][0]["reason"], "not_priced")


class PassThroughParity(unittest.TestCase):
    """The two pass-through tables, pinned against each other.

    A code in one book's table and not the other's is a leg that reads and
    splits on that book and can never cross to the other. Every one of those
    below is deliberate and has a reason written beside the table it belongs
    to - but nothing recorded WHICH they were, so a line quietly added to one
    side looked exactly like a line deliberately left off the other. This test
    is that record: it fails when the asymmetry changes, and the fix is either
    to map the missing side or to move the code into the list here with the
    reason.
    """

    # SportyBet quotes it, Bet9ja does not.
    SPORTY_ONLY = {
        # Whole Over/Under lines. Their card carries 0.5 to 5.5 in halves and
        # nothing whole; converting one changes the bet, which the site offers
        # out loud rather than doing quietly.
        "OVER_2", "OVER_3", "UNDER_2", "UNDER_3",
        # Handicap lines past +-3. SportyBet runs -4.5 to 5, Bet9ja -3 to 3.
        "AH_1_-4.5", "AH_1_-4", "AH_1_-3.5", "AH_1_3.5", "AH_1_4", "AH_1_4.5",
        "AH_1_5",
        "AH_2_-4.5", "AH_2_-4", "AH_2_-3.5", "AH_2_3.5", "AH_2_4", "AH_2_4.5",
        "AH_2_5",
        # Team cards above what Bet9ja's card reaches: their home side stops at
        # 3.5 (4+) and their away side at 2.5 (3+).
        "CARD_H_5", "CARD_H_6", "CARD_A_4", "CARD_A_5", "CARD_A_6",
        # Corners: SportyBet 6.5 to 12.5, Bet9ja 7.5 to 14.5.
        "CORNERS_OV_6.5", "CORNERS_UN_6.5",
    }

    # Bet9ja quotes it, SportyBet does not.
    BET9JA_ONLY = {
        # Quarter handicaps. Measured across 355 SportyBet events: halves and
        # wholes only, no quarter line anywhere on their card.
        "AH_1_-2.75", "AH_1_-2.25", "AH_1_-1.75", "AH_1_-1.25", "AH_1_-0.75",
        "AH_1_-0.25", "AH_1_0.25", "AH_1_0.75", "AH_1_1.25", "AH_1_1.75",
        "AH_1_2.25", "AH_1_2.75",
        "AH_2_-2.75", "AH_2_-2.25", "AH_2_-1.75", "AH_2_-1.25", "AH_2_-0.75",
        "AH_2_-0.25", "AH_2_0.25", "AH_2_0.75", "AH_2_1.25", "AH_2_1.75",
        "AH_2_2.25", "AH_2_2.75",
        # The top of their corner card.
        "CORNERS_OV_13.5", "CORNERS_OV_14.5", "CORNERS_UN_13.5",
        "CORNERS_UN_14.5",
        # 1X2-or-Over/Under at the lines they sell. SportyBet sells only 2.5,
        # which both tables carry, so only 1.5 and 3.5 are stuck here.
        "MIX_1_OV_1.5", "MIX_1_OV_3.5", "MIX_1_UN_1.5", "MIX_1_UN_3.5",
        "MIX_2_OV_1.5", "MIX_2_OV_3.5", "MIX_2_UN_1.5", "MIX_2_UN_3.5",
        "MIX_X_OV_1.5", "MIX_X_OV_3.5", "MIX_X_UN_1.5", "MIX_X_UN_3.5",
        # Penalty AWARDED. SportyBet sells penalty SCORED (800123) and only the
        # Yes side of it; a saved penalty settles the two opposite ways, so
        # there is nothing here to translate into.
        "PEN_Y", "PEN_N",
        # Which half carries the most bookings, and the same question about
        # corners. Read off SportyBet's catalogue on 14 Sep 2026: their
        # Bookings group is 81 markets on a featured event and their Corners
        # group 48, and neither carries a most-in-a-half market.
        "HMC_1", "HMC_2", "HMC_E",
        "HALFCORNER_1", "HALFCORNER_2", "HALFCORNER_E",
    }

    # SportyBet quotes it, Bet9ja does not - added 14 Sep from a real code.
    SPORTY_ONLY_14SEP = {
        # Double chance with the 1UP promotion. Bet9ja runs 1UP on 1X2 only.
        "DC1UP_1X", "DC1UP_12", "DC1UP_X2",
        # One side's corners USED to be here ("Bet9ja sells team corners
        # nowhere on their card", 14 Sep). Wrong by 25 Sep: their live card
        # carries S_CORNERSHOMEOU / S_CORNERSAWAYOU on every MLS game checked,
        # so both books map 0.5-9.5 and the family is no longer one-sided.
        # Goals inside the first N minutes. No equivalent on their card either.
        "EARLY_OV_10_1.5", "EARLY_UN_10_1.5", "EARLY_OV_30_2.5", "EARLY_UN_30_2.5",
        "EARLY_OV_50_3.5", "EARLY_UN_50_3.5",
        # Excluded number of goals, match and first half. No Bet9ja equivalent.
        "EXGOALS_0", "EXGOALS_1", "EXGOALS_2", "EXGOALS_3", "EXGOALS_4", "EXGOALS_5",
        "EXGOALS_FH_0", "EXGOALS_FH_1", "EXGOALS_FH_2", "EXGOALS_FH_3",
        # Goal bounds - one side's goals as a range. Theirs sells exact team
        # totals, not ranges, so these read and split here and cross nowhere.
        "BOUNDS_H_0", "BOUNDS_H_1", "BOUNDS_H_2", "BOUNDS_H_11", "BOUNDS_H_12",
        "BOUNDS_H_13", "BOUNDS_H_22", "BOUNDS_H_23", "BOUNDS_H_33",
        "BOUNDS_A_0", "BOUNDS_A_1", "BOUNDS_A_2", "BOUNDS_A_11", "BOUNDS_A_12",
        "BOUNDS_A_13", "BOUNDS_A_22", "BOUNDS_A_23", "BOUNDS_A_33",
    }

    # Taken from SportyBet's own `favourite` list on 14 Sep rather than from a
    # reader's complaint.
    #
    # THIS LIST SHRANK THE SAME DAY, AND THE REASON IT EXISTED WAS WRONG. It
    # was first written as "Bet9ja sells none of these shapes: no first-goal
    # market, no exact-goals variants, no winning margin, no goal range, no
    # per-half team totals" - asserted from having mapped one book, without
    # opening the other. Bet9ja sells four of those five. Reading their
    # dictionary and then checking the 1,531 priced keys on a real event
    # crossed 34 of these codes; what stayed is below, each with the reason it
    # stayed.
    SPORTY_ONLY_CATALOGUE = {
        # The top rung of every exact-goals family. SportyBet's is "or more",
        # Bet9ja's is that number exactly, so these two look like a pair and
        # are not the same bet. Converting would narrow somebody's bet
        # silently, which is worse than splitting it.
        "EXACT_6", "EXACT_FH_3",
        # Exact goals, nil. Their card starts at 1.
        "EXACT_0",
        # Second-half exact goals. Their S_EG1 is the first half only.
        "EXACT_SH_0", "EXACT_SH_1", "EXACT_SH_2",
        # "Not both halves over/under 1.5". Bet9ja sells the two halves as one
        # combined outcome, so the YES side crosses and the NO side is four of
        # their outcomes rather than one.
        "BOTHHALVES_OV_N", "BOTHHALVES_UN_N",
        # First goal within a half. S_1STSCORE2T is in their dictionary and was
        # priced on none of four Premier League events carrying 1,100-1,500
        # keys each, and they list no first-half equivalent at all.
        "FIRSTGOAL_FH_1", "FIRSTGOAL_FH_2", "FIRSTGOAL_FH_N",
        "FIRSTGOAL_SH_1", "FIRSTGOAL_SH_2", "FIRSTGOAL_SH_N",
        # Goal range. Their Multi Goal bands are 1-2 through 3-6; ours are
        # 0-1, 2-3, 4-6 and 7+. Not one band is shared.
        "GOALRANGE_0_1", "GOALRANGE_2_3", "GOALRANGE_4_6", "GOALRANGE_7",
        # One side's goals inside one half. S_OUHOME1T and its three siblings
        # are listed in their dictionary and priced on none of those same four
        # events. A key format that cannot be read off a real event is a
        # guess, and a guessed key books the wrong bet rather than failing.
        "FH_AWAY_OVER_0.5", "FH_AWAY_OVER_1.5", "FH_AWAY_OVER_2.5",
        "FH_AWAY_UNDER_0.5", "FH_AWAY_UNDER_1.5", "FH_AWAY_UNDER_2.5",
        "FH_HOME_OVER_0.5", "FH_HOME_OVER_1.5", "FH_HOME_OVER_2.5",
        "FH_HOME_UNDER_0.5", "FH_HOME_UNDER_1.5", "FH_HOME_UNDER_2.5",
        "SH_AWAY_OVER_0.5", "SH_AWAY_OVER_1.5", "SH_AWAY_OVER_2.5",
        "SH_AWAY_UNDER_0.5", "SH_AWAY_UNDER_1.5", "SH_AWAY_UNDER_2.5",
        "SH_HOME_OVER_0.5", "SH_HOME_OVER_1.5", "SH_HOME_OVER_2.5",
        "SH_HOME_UNDER_0.5", "SH_HOME_UNDER_1.5", "SH_HOME_UNDER_2.5",
    }

    # The sibling tranche, 14 Sep: half-versions and per-team versions of
    # markets already carried. Bet9ja's dictionary names a counterpart for
    # EVERY family here and prices not one of them on five Premier League
    # events, so all of it reads and splits rather than converting. The one
    # sibling that DOES cross - first-half 1X2 & over/under - is absent from
    # this list for exactly that reason.
    SPORTY_ONLY_SIBLINGS = {
        # European handicap, the match and each half. Bet9ja names one
        # (S_1X2HND1T/2T) and prices it on none of five events.
        "EH_0_1_1", "EH_0_1_2", "EH_0_1_X", "EH_0_2_1", "EH_0_2_2",
        "EH_0_2_X", "EH_0_3_1", "EH_0_3_2", "EH_0_3_X", "EH_0_4_1",
        "EH_0_4_2", "EH_0_4_X", "EH_0_5_1", "EH_0_5_2", "EH_0_5_X",
        "EH_1_0_1", "EH_1_0_2", "EH_1_0_X", "EH_2_0_1", "EH_2_0_2",
        "EH_2_0_X", "EH_3_0_1", "EH_3_0_2", "EH_3_0_X", "FH_EH_0_1_1",
        "FH_EH_0_1_2", "FH_EH_0_1_X", "FH_EH_0_2_1", "FH_EH_0_2_2",
        "FH_EH_0_2_X", "FH_EH_1_0_1", "FH_EH_1_0_2", "FH_EH_1_0_X",
        "SH_EH_0_1_1", "SH_EH_0_1_2", "SH_EH_0_1_X", "SH_EH_0_2_1",
        "SH_EH_0_2_2", "SH_EH_0_2_X", "SH_EH_1_0_1", "SH_EH_1_0_2",
        "SH_EH_1_0_X",
        # Asian handicap inside one half. S_12HND1T, S_12HND2T and S_AHH are all
        # listed over there and none of them are ever priced.
        "FH_AH_1_-0.5", "FH_AH_1_-1", "FH_AH_1_-1.5", "FH_AH_1_-2",
        "FH_AH_1_0", "FH_AH_1_0.5", "FH_AH_2_-0.5", "FH_AH_2_-1",
        "FH_AH_2_-1.5", "FH_AH_2_-2", "FH_AH_2_0", "FH_AH_2_0.5",
        "SH_AH_1_-0.5", "SH_AH_1_-1", "SH_AH_1_-1.5", "SH_AH_1_-2",
        "SH_AH_1_0", "SH_AH_1_0.5", "SH_AH_2_-0.5", "SH_AH_2_-1",
        "SH_AH_2_-1.5", "SH_AH_2_-2", "SH_AH_2_0", "SH_AH_2_0.5",
        # Corner range, the match and each side. S_MULTIC, S_MULTICH and S_MULTICA
        # carry our exact bands - 0-8/9-11/12+ and 0-2/3-4/5-6/7+ - and are
        # priced on none of five events.
        "CORNRANGE_0_8", "CORNRANGE_12", "CORNRANGE_9_11", "CORNRANGE_A_0_2",
        "CORNRANGE_A_3_4", "CORNRANGE_A_5_6", "CORNRANGE_A_7",
        "CORNRANGE_H_0_2", "CORNRANGE_H_3_4", "CORNRANGE_H_5_6",
        "CORNRANGE_H_7",
        # One side's bookings in the first half. S_CARDSHOME1T and S_CARDSAWAY1T,
        # listed and never sold.
        "FH_CARDUN_A_1", "FH_CARDUN_A_2", "FH_CARDUN_A_3", "FH_CARDUN_H_1",
        "FH_CARDUN_H_2", "FH_CARDUN_H_3", "FH_CARD_A_1", "FH_CARD_A_2",
        "FH_CARD_A_3", "FH_CARD_H_1", "FH_CARD_H_2", "FH_CARD_H_3",
    }

    # MIXNG_1/X/2 WAS IN THIS LIST AND IS NOT ANY MORE. It looked like a gap
    # because SportyBet does not use the word: their name for no-goal is "Any
    # Clean Sheet", markets 863/864/865, read off their catalogue on 14 Sep
    # 2026. At least one clean sheet is exactly "not both teams scored", so it
    # is the same bet under another name and the family now crosses whole.

    def test_the_top_rung_of_exact_goals_never_crosses(self):
        """"6 or more" and "exactly 6" are not the same bet.

        SportyBet's exact-goals families top out at "or more" - EXACT_6 is six
        or more, EXACT_FH_3 is three or more. Bet9ja's S_EXACTGOAL runs 1 to 6
        and its 6 means six exactly; S_EG1 runs 0 to 4 and its 3 means three
        exactly. Mapping the two tops together would quietly hand somebody a
        narrower bet than the one they placed, and a converted leg that loses
        on a 7-goal game is indistinguishable from us having booked the wrong
        market - which is exactly what `market_for(pred) or MARKET_MAP["1"]`
        used to do.

        The rungs below the top ARE the same bet on both books and do cross.
        """
        for code in ("EXACT_6", "EXACT_FH_3"):
            self.assertIn(code, server.PASSTHROUGH_MAP,
                          "%s should still be bookable on SportyBet" % code)
            self.assertIsNone(server.bet9ja.market_for(code),
                              "%s must not cross: their top rung is exact, "
                              "ours is 'or more'" % code)
        for code in ("EXACT_1", "EXACT_5", "EXACT_FH_0", "EXACT_FH_2"):
            self.assertIsNotNone(server.bet9ja.market_for(code),
                                 "%s is the same bet on both books" % code)

    def test_a_team_total_of_three_does_cross_because_both_mean_three_plus(self):
        """The same shape as above, with the opposite answer.

        Bet9ja's exact team goals are 0, 1, 2 and "3+", which is what
        SportyBet's is too. So the top rung crosses here where it cannot for
        the match total - the rule is what the number MEANS, never where it
        sits in the list.
        """
        for side in ("H", "A"):
            for n in ("0", "1", "2", "3"):
                code = "TEAMGOALS_%s_%s" % (side, n)
                self.assertIsNotNone(server.bet9ja.market_for(code), code)
        self.assertEqual(server.bet9ja.market_for("TEAMGOALS_H_3")[0], "S_GOALSHOME_3+")

    def test_no_bet9ja_key_was_invented_for_a_market_they_never_price(self):
        """Half-team totals are listed in their dictionary and never sold.

        S_OUHOME1T and its three siblings appear in TRANS on every event and
        were priced on none of four Premier League events carrying 1,100-1,500
        keys each. The key format therefore cannot be READ, only guessed - and
        a guessed key does not fail loudly, it books whatever it happens to
        hit. Nothing here may point at one.
        """
        guessed = [c for c in ("FH_HOME_OVER_0.5", "FH_AWAY_UNDER_1.5",
                               "SH_HOME_OVER_0.5", "SH_AWAY_UNDER_1.5",
                               "FIRSTGOAL_SH_1")
                   if server.bet9ja.market_for(c)]
        self.assertEqual(guessed, [],
                         "a key was invented for a market Bet9ja never prices")

    def test_highest_scoring_half_keeps_the_ids_read_off_the_catalogue(self):
        """52/53/54 and 436/438/440, not 1/2/3.

        Every other three-way market on this book uses small consecutive
        outcome ids, so these look like a mistake and are not: they were read
        off SportyBet's own catalogue on 14 Sep 2026 and were identical on
        three events. Rounding them to 1/2/3 would book a different half.
        """
        for code, mkt in (("HIGHHALF_", 52), ("HIGHHALF_H_", 53),
                          ("HIGHHALF_A_", 54)):
            for sfx, out in (("1", 436), ("2", 438), ("E", 440)):
                got = server.PASSTHROUGH_MAP[code + sfx]
                self.assertEqual(got["marketId"], mkt, code + sfx)
                self.assertEqual(got["outcomeId"], out, code + sfx)

    def test_the_asymmetry_is_the_recorded_one(self):
        s, b = set(server.PASSTHROUGH_MAP), set(server.bet9ja.PASSTHROUGH_MAP)
        self.assertEqual(s - b, self.SPORTY_ONLY | self.SPORTY_ONLY_14SEP
                         | self.SPORTY_ONLY_CATALOGUE | self.SPORTY_ONLY_SIBLINGS)
        self.assertEqual(b - s, self.BET9JA_ONLY)

    def test_every_shared_code_resolves_on_both_books(self):
        """A code in both tables has to be bookable on both, not merely
        present: market_for is what the booking routes ask."""
        for code in set(server.PASSTHROUGH_MAP) & set(server.bet9ja.PASSTHROUGH_MAP):
            self.assertIsNotNone(server.market_for(code), code)
            self.assertIsNotNone(server.bet9ja.market_for(code), code)

    def test_no_pass_through_code_collides_with_a_modelled_one(self):
        """market_for reads MARKET_MAP first, so a duplicated key would be
        silently shadowed on one book and not the other."""
        for mod, table in ((server, server.PASSTHROUGH_MAP),
                           (server.bet9ja, server.bet9ja.PASSTHROUGH_MAP)):
            self.assertEqual(set(mod.MARKET_MAP) & set(table), set())


class SportyBetNeverSubstitutesAMarket(unittest.TestCase):
    """A market this server cannot map is refused, never swapped for another.

    The selection used to be built as `market_for(pred) or MARKET_MAP["1"]`, so
    an unknown code became HOME WIN - marketId 1, outcomeId 1, no specifier.
    SportyBet accepted it and returned a booking code, so nothing looked wrong
    at any point: the punter asked for one bet, got a code, and held a
    different one. Found by sending a Bet9ja-only market to this route on
    purpose and reading the code back.
    """

    def _post(self, selections):
        real_gen, real_report = server.generate_sportybet_code, server.report
        sent = []
        server.generate_sportybet_code = lambda sels, region="ng": (
            sent.append(sels) or {"code": "SBCODE"})
        server.report = lambda msg, level="warning", **ctx: None
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code",
                           json={"selections": selections})
            return r.status_code, r.get_json(), sent
        finally:
            server.generate_sportybet_code, server.report = real_gen, real_report

    def test_an_unknown_market_is_refused_rather_than_booked_as_a_home_win(self):
        code, body, sent = self._post(
            [{"eventId": "sr:match:1", "prediction": "TOTAL_NONSENSE_MARKET"}])
        self.assertEqual(code, 400, body)
        self.assertEqual(body["unbookable"][0]["reason"], "not_mapped")
        self.assertEqual(sent, [], "nothing may be sent to the bookmaker")

    def test_a_market_only_the_other_book_sells_is_refused_here(self):
        """Bet9ja carries the 1.5 rung of 1X2-or-Over/Under; this book does
        not, and the difference must not be papered over."""
        code, body, sent = self._post(
            [{"eventId": "sr:match:1", "prediction": "MIX_1_OV_1.5"}])
        self.assertEqual(code, 400)
        self.assertEqual(body["unbookable"][0]["prediction"], "MIX_1_OV_1.5")
        self.assertEqual(sent, [])

    def test_a_pass_through_market_this_book_does_sell_still_books(self):
        real_unb = server._unbookable
        server._unbookable = lambda sel: ([], {"cache_age_s": 1, "judged": 0, "unknown": 1})
        try:
            code, body, sent = self._post(
                [{"eventId": "sr:match:1", "prediction": "AH_1_-0.5"}])
        finally:
            server._unbookable = real_unb
        self.assertEqual(code, 200, body)
        self.assertEqual(sent[0][0]["marketId"], 16, "the handicap, not a home win")


class ACodeFromARealPunter(unittest.TestCase):
    """HCVKA1, read on 14 Sep: thirty-one legs, nine unreadable.

    The reader's words were "we carry some games but the converter says we
    dont". Two separate causes in one code, and neither was the booking:

      nine legs      markets we had never mapped - 60110 double chance with the
                     1UP promotion, 60180 goals inside the first N minutes, and
                     900300 one side's corners at a line other than 7.5
      five legs      games that had KICKED OFF. SportyBet drops a fixture from
                     its upcoming list the moment it starts and our sweep
                     follows, so the names were gone from the cache by the time
                     the code was read.
    """

    def test_the_markets_that_code_carried_are_all_mapped_now(self):
        for raw, code in (
            ("60110/11/", "DC1UP_X2"),
            ("60180/12/minsnr=10|total=1.5", "EARLY_OV_10_1.5"),
            ("60180/12/minsnr=30|total=2.5", "EARLY_OV_30_2.5"),
            ("900300/30/total=3.5", "CORNERS_H_OV_3.5"),
        ):
            m = server.PASSTHROUGH_MAP[code]
            got = "%s/%s/%s" % (m["marketId"], m["outcomeId"], m["specifier"])
            self.assertEqual(got, raw, code)


    def test_pv5cll_the_three_it_could_not_read(self):
        """A second reader's code, thirty-nine legs, three unreadable. All of
        them use the outcome id as a VALUE rather than as an index, which
        nothing else in these tables does."""
        for raw, code in (
            ("450004/1/", "EXGOALS_1"),          # not exactly one goal
            ("810002/1/", "EXGOALS_FH_1"),       # not exactly one in the half
            ("450003/23/", "BOUNDS_A_23"),       # away side score two to three+
        ):
            m = server.PASSTHROUGH_MAP[code]
            got = "%s/%s/%s" % (m["marketId"], m["outcomeId"], m["specifier"])
            self.assertEqual(got, raw, code)

    def test_goal_bounds_ids_are_ranges_not_counts(self):
        """11 is "exactly one" and 12 is "one to two" - reading them as numbers
        in sequence would book a different range."""
        self.assertEqual(server.PASSTHROUGH_MAP["BOUNDS_A_11"]["outcomeId"], 11)
        self.assertEqual(server.PASSTHROUGH_MAP["BOUNDS_A_12"]["outcomeId"], 12)
        self.assertEqual(server.PASSTHROUGH_MAP["BOUNDS_A_33"]["outcomeId"], 33)
        self.assertEqual(server.PASSTHROUGH_MAP["BOUNDS_H_0"]["marketId"], 450002)
        self.assertEqual(server.PASSTHROUGH_MAP["BOUNDS_A_0"]["marketId"], 450003)

    def test_the_1up_double_chance_ids_are_not_in_sign_order(self):
        """9 is Home or Draw, 10 is Home or AWAY, 11 is Draw or Away - the same
        trap market 85 carries. Reading the pair the obvious way books 12 as
        1X, which is a different bet on somebody else's money."""
        self.assertEqual(server.PASSTHROUGH_MAP["DC1UP_1X"]["outcomeId"], 9)
        self.assertEqual(server.PASSTHROUGH_MAP["DC1UP_12"]["outcomeId"], 10)
        self.assertEqual(server.PASSTHROUGH_MAP["DC1UP_X2"]["outcomeId"], 11)

    def test_the_early_goals_specifier_keeps_both_numbers(self):
        """`minsnr=10|total=1.5` is over 1.5 goals in the first ten minutes.
        Dropping the minsnr half books a full-match line instead."""
        for code in ("EARLY_OV_10_1.5", "EARLY_OV_30_2.5", "EARLY_UN_50_3.5"):
            spec = server.PASSTHROUGH_MAP[code]["specifier"]
            self.assertIn("minsnr=", spec, code)
            self.assertIn("total=", spec, code)

    def test_a_game_off_the_board_is_named_by_asking_the_bookmaker(self):
        """The cache holds upcoming matches only. Rather than shrug, the read
        asks SportyBet for that one event - which still answers for a game in
        play, and says so."""
        calls = []

        def fake(event_id, region="ng"):
            calls.append(event_id)
            return {"homeTeam": "FC Inter Turku", "awayTeam": "Vaasan Palloseura",
                    "league": "Finland Veikkausliiga", "status": "H1"}

        real = server._sporty_event_name
        server._sporty_event_name = fake
        try:
            self.assertTrue(callable(server._sporty_event_name))
            got = server._sporty_event_name("sr:match:74299674")
        finally:
            server._sporty_event_name = real
        self.assertEqual(got["homeTeam"], "FC Inter Turku")
        self.assertEqual(got["status"], "H1")
        self.assertEqual(calls, ["sr:match:74299674"])

    def test_the_lookup_is_capped_and_cached(self):
        """One request per unnamed leg, and never the same leg twice - a code
        can carry forty of them."""
        src = open(os.path.join(os.path.dirname(__file__), "server.py"),
                   encoding="utf-8").read()
        self.assertIn("_EVENT_NAME_CACHE", src)
        self.assertIn("_EVENT_NAME_MAX", src)
        body = src[src.index("def _sporty_event_name("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("if event_id in _EVENT_NAME_CACHE", body)
        self.assertIn("timeout=6", body)


class BetKingAnswersInTheSameShape(unittest.TestCase):
    """The third book has to speak the client's existing language.

    `dropUnbookable` keys on eventId + "|" + prediction and knows nothing about
    which bookmaker refused. If this route answers in a different shape, the
    retry path works for two books out of three - which is the kind of thing
    that looks fine until somebody's slip dies.
    """

    def _post(self, priced, picks=None, gen=None):
        """priced: {eventId: [codes BetKing prices on that fixture]}"""
        real_ev = server.betking.fetch_event
        real_gen = server.betking.generate_code
        calls = []

        def fake_event(eid):
            calls.append(str(eid))
            if str(eid) not in priced:
                return None
            codes = priced[str(eid)]
            return {"eventId": str(eid),
                    "odds": {c: 2.0 for c in codes},
                    "raw": {c: "2.0" for c in codes},
                    "sel": {c: {"SelectionId": 1} for c in codes},
                    "event": {"MatchId": int(eid)}}

        server.betking.fetch_event = fake_event
        server.betking.generate_code = gen or (
            lambda sels: {"code": "BKCODE", "legs": len(sels), "verified": True})
        try:
            with server.app.test_client() as c:
                r = c.post("/api/betking/booking-code", json={"selections":
                    picks or [
                        {"eventId": "1", "code": "1X"},
                        {"eventId": "2", "code": "HOME_OVER_0.5"},
                        {"eventId": "3", "code": "OVER_1.5"},
                    ]})
            return r.status_code, r.get_json(), calls
        finally:
            server.betking.fetch_event = real_ev
            server.betking.generate_code = real_gen

    def test_every_unbookable_leg_is_named_with_the_same_keys(self):
        code, body, _ = self._post({"1": ["1X"]})
        self.assertEqual(code, 400)
        self.assertEqual(
            [(b["eventId"], b["prediction"]) for b in body["unbookable"]],
            [("2", "HOME_OVER_0.5"), ("3", "OVER_1.5")])
        for k in ("success", "message", "detail", "unbookable"):
            self.assertIn(k, body, k + " is missing, so dropUnbookable cannot read it")

    def test_a_fully_priced_slip_books(self):
        code, body, _ = self._post(
            {"1": ["1X"], "2": ["HOME_OVER_0.5"], "3": ["OVER_1.5"]})
        self.assertEqual(code, 200)
        self.assertEqual(body["code"], "BKCODE")

    def test_an_unmapped_market_costs_no_request_at_all(self):
        """It is already known locally, and it is refused - never substituted."""
        _code, body, calls = self._post(
            {"9": ["1"]}, picks=[{"eventId": "9", "code": "CORNERS_OVER_9.5"}])
        self.assertEqual(body["unbookable"][0]["reason"], "not_mapped")
        self.assertEqual(calls, [])

    def test_two_legs_on_one_game_cost_one_fetch(self):
        """Their deep card is a megabyte; fetching it twice for a double is
        a request nobody needed."""
        _code, _body, calls = self._post(
            {"7": ["1", "OVER_1.5"]},
            picks=[{"eventId": "7", "code": "1"},
                   {"eventId": "7", "code": "OVER_1.5"}])
        self.assertEqual(calls, ["7"])

    def test_the_cap_is_forty_and_nothing_is_fetched_past_it(self):
        picks = [{"eventId": str(i), "code": "1"} for i in range(41)]
        _code, body, calls = self._post({}, picks=picks)
        self.assertIn("40", body["error"])
        self.assertEqual(calls, [])

    def test_a_second_leg_on_one_game_is_named_and_the_rest_still_book(self):
        """BetKing takes one selection per match on a multiple, and a coupon
        carrying two comes back as a code with nothing in it. So the extra leg
        is named like any other unbookable one and the client drops exactly
        that leg rather than losing the slip."""
        code, body, _ = self._post(
            {"1": ["1", "OVER_1.5"], "2": ["1"]},
            picks=[{"eventId": "1", "code": "1"},
                   {"eventId": "2", "code": "1"},
                   {"eventId": "1", "code": "OVER_1.5"}])
        self.assertEqual(code, 400)
        self.assertEqual([(b["eventId"], b["prediction"], b["reason"])
                          for b in body["unbookable"]],
                         [("1", "OVER_1.5", "same_game")])
        # And the sentence says what actually happened. "No market there" is a
        # plain untruth here - the market is priced, it is the second leg that
        # is refused.
        self.assertIn("one selection per game", body["detail"])

    def test_the_first_leg_on_a_game_is_the_one_kept(self):
        _code, body, _ = self._post(
            {"7": ["1", "X", "2"]},
            picks=[{"eventId": "7", "code": "1"},
                   {"eventId": "7", "code": "X"},
                   {"eventId": "7", "code": "2"}])
        self.assertEqual([b["prediction"] for b in body["unbookable"]],
                         ["X", "2"])

    def test_an_empty_code_is_reported_as_our_bug_not_their_refusal(self):
        """BetKing accepts a selection id it does not know and answers with an
        ordinary code. generate_code catches that; the route must not then
        hand the empty code back as a success."""
        code, body, _ = self._post(
            {"1": ["1X"]}, picks=[{"eventId": "1", "code": "1X"}],
            gen=lambda sels: {"error": "betking accepted the slip and returned "
                                       "an empty code (0 of 1 legs resolved)",
                              "code": "QP1GZZ", "sent": 1})
        self.assertEqual(code, 502)
        self.assertFalse(body["success"])


class ReadingABetKingCodeThroughTheRoute(unittest.TestCase):
    """The converter reads every book through /api/slip, so the route has to
    name BetKing rather than let it fall through.

    Added because a mutation deleting the branch broke nothing: read_coupon was
    tested directly and the ROUTE was not, so an unnamed book would have been
    read against SportyBet and answered as though it were SportyBet's.
    """

    LEGS = [{"eventId": 1005309147, "prediction": "OVER_2.5",
             "home": "Leeds", "away": "Newcastle", "league": "Premier League",
             "kickoff": "2026-09-14T21:00:00+02:00", "odds": 1.71}]

    def _get(self, book, betking_out=None, sporty_out=None):
        real_bk = server.betking.read_coupon
        real_sb = server.read_sporty_share
        server.betking.read_coupon = lambda c, **k: (
            betking_out if betking_out is not None else {"legs": self.LEGS})
        server.read_sporty_share = lambda c, **k: (
            sporty_out if sporty_out is not None else {"legs": []})
        try:
            with server.app.test_client() as c:
                r = c.get("/api/slip?book=%s&code=FR2D84" % book)
                return r.status_code, r.get_json()
        finally:
            server.betking.read_coupon = real_bk
            server.read_sporty_share = real_sb

    def test_a_betking_code_is_read_by_betking(self):
        code, body = self._get("betking")
        self.assertEqual(code, 200)
        self.assertEqual(body["book"], "betking")
        self.assertEqual(body["legs"][0]["prediction"], "OVER_2.5")

    def test_it_is_not_read_against_sportybet(self):
        # The failure this guards: the branch is gone, betking falls through to
        # the else, and SportyBet's answer is returned labelled "betking".
        _code, body = self._get("betking", sporty_out={"legs": [
            {"eventId": "x", "prediction": "1", "home": "Wrong", "away": "Book",
             "league": "", "kickoff": "", "odds": 2.0}]})
        self.assertEqual(body["legs"][0]["home"], "Leeds")

    def test_a_code_they_do_not_know_is_a_404(self):
        code, body = self._get("betking",
                               betking_out={"error": "not found", "notFound": True})
        self.assertEqual(code, 404)
        self.assertTrue(body["notFound"])

    def test_an_unknown_book_is_still_refused(self):
        code, body = self._get("bogus")
        self.assertEqual(code, 400)
        self.assertIn("unknown bookmaker", body["error"])


class TheRouteFeedsBetKingWhatBetKingReads(unittest.TestCase):
    """A call-site assertion, added because three bugs have now shipped past a
    green suite that built its own inputs.

    Every test above hands `generate_code` a hand-made event. This one drives
    the REAL module: the route's dict is passed to the real build_selection, so
    if the route ever checks one field and the module reads another, this
    fails and the mocked tests do not.
    """

    def test_a_route_shaped_event_builds_a_real_leg(self):
        import test_betking
        payload = test_betking._payload([test_betking.ONE_X_TWO])
        real_get = server.betking._get_json
        server.betking._get_json = lambda *a, **k: payload
        try:
            event = server.betking.fetch_event(1005309147)
        finally:
            server.betking._get_json = real_get

        sent = []
        real_gen = server.betking.generate_code
        real_ev = server.betking.fetch_event
        server.betking.fetch_event = lambda eid: event

        def capture(sels):
            # The route promises {event, code}; the module reads event["sel"]
            # and event["event"]. Build the leg for real rather than asserting
            # on the dict's shape.
            sent.extend(server.betking.build_selection(s["event"], s["code"])
                        for s in sels)
            return {"code": "BKCODE", "legs": len(sels)}

        server.betking.generate_code = capture
        try:
            with server.app.test_client() as c:
                r = c.post("/api/betking/booking-code",
                           json={"selections": [{"eventId": "1005309147",
                                                 "code": "1"}]})
        finally:
            server.betking.generate_code = real_gen
            server.betking.fetch_event = real_ev

        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["SelectionId"], 2339553331)
        self.assertEqual(sent[0]["MatchId"], 1005309147)



class WhenSportyBetWillNotSayWhichLeg(unittest.TestCase):
    """Their refusal is one sentence about the whole slip.

    "invalid event data, no market there" names no event and no market, and a
    reader was shown it again on 22 Sep over a slip they could not correct.
    Our own cache cannot answer for them - it is partial by design and 45
    minutes old - so the slip is bisected back at their booking endpoint and
    the legs are named by SportyBet.

    Measured live the same day: 199 of the day's upcoming events carry no
    team-totals price at all, and asking for one is refused exactly this way.
    """

    @staticmethod
    def _legs(n):
        return [{"eventId": "sr:match:%d" % i, "marketId": "1",
                 "outcomeId": "1", "specifier": ""} for i in range(n)]

    def _run(self, bad_ix, legs=8):
        """Stand in for their endpoint: any subset holding a bad leg is refused."""
        sent = []
        picks = self._legs(legs)

        def fake(subset, region="ng"):
            sent.append(len(subset))
            ids = {x["eventId"] for x in subset}
            if any(picks[i]["eventId"] in ids for i in bad_ix):
                return {"error": "invalid event data, no market there"}
            return {"code": "OK%d" % len(sent)}

        old = server.generate_sportybet_code
        server.generate_sportybet_code = fake
        try:
            return picks, server._probe_refusal(picks), sent
        finally:
            server.generate_sportybet_code = old

    def test_one_bad_leg_among_eight_is_named(self):
        picks, (found, how), sent = self._run([5])
        self.assertEqual(found, [5])
        self.assertFalse(how["ran_out"])
        # Bisection, not a sweep: eight legs must not cost eight calls.
        self.assertLess(how["calls"], 8, "this is walking the slip, not halving it")

    def test_two_bad_legs_are_both_named(self):
        picks, (found, how), sent = self._run([1, 6])
        self.assertEqual(sorted(found), [1, 6])

    def test_a_slip_that_is_fine_leg_by_leg_names_nothing(self):
        """Every leg books alone and the slip does not.

        That is a statement about the COMBINATION, and telling the reader to
        remove "a leg" would be advice about a problem they do not have. The
        route answers `combination: true` instead of inventing a culprit.
        """
        picks = self._legs(4)
        calls = {"n": 0}

        def fake(subset, region="ng"):
            calls["n"] += 1
            return ({"error": "no"} if len(subset) == len(picks)
                    else {"code": "OK"})

        old = server.generate_sportybet_code
        server.generate_sportybet_code = fake
        try:
            found, how = server._probe_refusal(picks)
        finally:
            server.generate_sportybet_code = old
        self.assertEqual(found, [])
        self.assertFalse(how["ran_out"])

    def test_the_probe_is_bounded_and_says_when_it_ran_out(self):
        """It runs while somebody waits. A partial answer must never read as a
        complete one - `ran_out` is what stops the route claiming the slip is
        fine when it simply stopped asking."""
        picks = self._legs(64)

        def fake(subset, region="ng"):
            return {"error": "no"}                # everything is refused

        old = server.generate_sportybet_code
        server.generate_sportybet_code = fake
        try:
            found, how = server._probe_refusal(picks)
        finally:
            server.generate_sportybet_code = old
        self.assertLessEqual(how["calls"], server.PROBE_MAX_CALLS)
        self.assertTrue(how["ran_out"])

    def test_two_bad_legs_in_a_29_leg_slip_are_both_found(self):
        """The 24 Sep report, at its real size. Twelve calls ran out on this
        and named nothing, so the reader was sent round on a guess."""
        for bad in ([4, 10], [0, 28], [13, 14], [2, 17]):
            picks, (found, how), sent = self._run(bad, legs=29)
            self.assertEqual(sorted(found), bad, bad)
            self.assertFalse(how["ran_out"], bad)
            self.assertNotIn(29, sent, "the whole slip is already known refused")

    def test_every_named_leg_is_refused_alone_even_when_the_fault_is_a_pair(self):
        """Property over random slips. Faults are single legs AND pairs that
        are refused only together. Whatever the probe names must be a leg
        SportyBet refuses by itself - inference is never sent as their word -
        and, within budget, every single-leg fault is named."""
        import random
        rnd = random.Random(7)
        checked = 0
        for _ in range(600):
            n = rnd.randint(2, 40)
            singles = set(rnd.sample(range(n), rnd.randint(0, min(4, n))))
            rest = [i for i in range(n) if i not in singles]
            pairs = [tuple(rnd.sample(rest, 2))] if len(rest) >= 2 and rnd.random() < 0.5 else []
            picks = self._legs(n)
            ix = {p["eventId"]: i for i, p in enumerate(picks)}

            def refused(subset):
                s = {ix[x["eventId"]] for x in subset}
                return bool(s & singles) or any(a in s and b in s for a, b in pairs)

            if not refused(picks):
                continue
            checked += 1

            def fake(subset, region="ng"):
                return {"error": "no"} if refused(subset) else {"code": "OK"}

            old = server.generate_sportybet_code
            server.generate_sportybet_code = fake
            try:
                found, how = server._probe_refusal(picks)
            finally:
                server.generate_sportybet_code = old
            case = "n=%d singles=%s pairs=%s" % (n, sorted(singles), pairs)
            for i in found:
                self.assertTrue(refused([picks[i]]), "named leg %d books alone: %s" % (i, case))
            if not how["ran_out"]:
                self.assertEqual(sorted(found), sorted(singles), case)
        self.assertGreater(checked, 300)

    def test_doubted_legs_are_searched_first(self):
        """Legs our cache has no price for are asked about first, so when they
        are the fault the search closes on them in fewer calls."""
        picks = self._legs(29)
        bad = {4, 10}

        def run(first):
            n = {"c": 0}

            def fake(subset, region="ng"):
                n["c"] += 1
                ids = {x["eventId"] for x in subset}
                return ({"error": "no"} if any(picks[i]["eventId"] in ids for i in bad)
                        else {"code": "OK"})
            old = server.generate_sportybet_code
            server.generate_sportybet_code = fake
            try:
                found, how = server._probe_refusal(picks, first=first)
            finally:
                server.generate_sportybet_code = old
            return sorted(found), n["c"]

        plain, plain_calls = run(())
        hinted, hinted_calls = run([4, 10])
        self.assertEqual(plain, [4, 10])
        self.assertEqual(hinted, [4, 10])
        self.assertLess(hinted_calls, plain_calls)

    def test_the_route_names_both_legs_of_a_29_leg_slip(self):
        """End to end through the route: the refusal comes back naming exactly
        the two legs SportyBet will not take, as `refused_alone`."""
        legs = [{"eventId": "sr:match:%d" % i, "prediction": "1X"} for i in range(29)]
        legs[4]["prediction"] = "OVER_1.5"
        legs[10]["prediction"] = "AWAY_OVER_0.5"
        bad = {("sr:match:4", server.market_for("OVER_1.5")["marketId"]),
               ("sr:match:10", server.market_for("AWAY_OVER_0.5")["marketId"])}

        def fake(subset, region="ng"):
            if any((x["eventId"], x["marketId"]) in bad for x in subset):
                return {"error": "invalid event data, no market there", "sent": subset}
            return {"code": "OK"}

        old_gen, old_ver = server.generate_sportybet_code, server._verify_sporty_code
        server.generate_sportybet_code = fake
        server._verify_sporty_code = lambda code, sel: (True, [])
        server._FIXTURES_CACHE.clear()
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": legs})
        finally:
            server.generate_sportybet_code, server._verify_sporty_code = old_gen, old_ver
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(sorted((b["eventId"], b["prediction"]) for b in body["unbookable"]),
                         [("sr:match:10", "AWAY_OVER_0.5"), ("sr:match:4", "OVER_1.5")])
        self.assertTrue(all(b["reason"] == "refused_alone" for b in body["unbookable"]))


class TheCodeSportyBetHandsBackIsReadBack(unittest.TestCase):
    """They mint an ordinary code for a slip they only partly understood.

    Measured 22 Sep on the live endpoint: five legs sent with one unknown event
    id among them came back as share code holding FOUR, with no error and no
    mention of the leg that vanished. That is the BetKing failure, on the book
    this site was built around and the only one whose codes were never checked.
    """

    SENT = [{"eventId": "sr:match:1", "prediction": "1X"},
            {"eventId": "sr:match:2", "prediction": "1X"},
            {"eventId": "sr:match:3", "prediction": "OVER_1.5"}]

    def _with_read(self, reply):
        old = server.read_sporty_share
        server.read_sporty_share = lambda code, **kw: reply
        try:
            return server._verify_sporty_code("ABC123", self.SENT)
        finally:
            server.read_sporty_share = old

    def test_a_short_code_names_the_leg_that_vanished(self):
        ok, missing = self._with_read({"legs": [
            {"eventId": "sr:match:1"}, {"eventId": "sr:match:2"}]})
        self.assertFalse(ok)
        self.assertEqual([m["eventId"] for m in missing], ["sr:match:3"])

    def test_a_complete_code_passes(self):
        ok, missing = self._with_read({"legs": [
            {"eventId": "sr:match:1"}, {"eventId": "sr:match:2"},
            {"eventId": "sr:match:3"}]})
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_a_code_we_cannot_read_is_not_called_wrong(self):
        """Unprovable is not the same as wrong. Their read endpoint being down,
        or a code too fresh to resolve, must not refuse a booking that may be
        perfectly good - that would be worse than the failure this guards."""
        for reply in ({"error": "not found"}, {"legs": []}, None, "junk"):
            ok, missing = self._with_read(reply)
            self.assertTrue(ok, repr(reply))
            self.assertEqual(missing, [])

    def test_a_read_that_raises_is_not_called_wrong_either(self):
        old = server.read_sporty_share

        def boom(code, **kw):
            raise RuntimeError("their endpoint fell over")

        server.read_sporty_share = boom
        try:
            ok, missing = server._verify_sporty_code("ABC123", self.SENT)
        finally:
            server.read_sporty_share = old
        self.assertTrue(ok)
        self.assertEqual(missing, [])

class EveryMarketTheBuilderOffersCarriesARealPrice(unittest.TestCase):
    """The rule the chance-mix family was breaking, written down so it cannot
    break again quietly.

    server.py has said it for a while: "a market the builder can select has to
    arrive with real odds, or every leg is priced off an estimate". Double
    chance and the first-half line were each added to the sweep for exactly
    that reason. The chance-mix markets - Draw or over 2.5, Result or over 2.5,
    Draw or GG, Result or GG - never were, so they were the only markets on the
    site priced from the model rather than from the book.

    It shows up as combo odds being wildly high. Those legs are short (real
    BetKing prices, read back: 1.05, 1.07, 1.10, 1.11, 1.21), so a target takes
    thirty or forty of them, and a few percent of error per leg compounds:
    1.08^40 is about twenty-one times the payout. Every other market looked
    right because every other market carried the bookmaker's own number.
    """

    # What the builder can put on a slip. A new chip here means a new id in
    # FIXTURE_MARKET_IDS, and this test is where that is remembered.
    BUILDER_MARKETS = [
        "1", "X", "2", "1X", "X2", "12",
        "OVER_1.5", "OVER_2.5", "OVER_3.5", "GG", "NG", "FH_OVER_0.5",
        "HOME_OVER_0.5", "HOME_OVER_1.5", "AWAY_OVER_0.5", "AWAY_OVER_1.5",
        "MIX_1_OV_2.5", "MIX_X_OV_2.5", "MIX_2_OV_2.5",
        "MIXGG_1", "MIXGG_X", "MIXGG_2",
        # The corners chip, 23 Sep 2026: over and under 7.5-10.5, the range
        # every book sells. Swept for availability as much as price - the
        # site offers a corners line only where this feed quotes it.
        "CORNERS_OV_7.5", "CORNERS_UN_7.5", "CORNERS_OV_8.5", "CORNERS_UN_8.5",
        "CORNERS_OV_9.5", "CORNERS_UN_9.5", "CORNERS_OV_10.5", "CORNERS_UN_10.5",
        # Total shots, overs only on the site, 23 Sep 2026.
        "SHOTS_OV_19.5", "SHOTS_OV_24.5", "SHOTS_OV_31.5",
    ]

    def test_corner_outcomes_come_back_as_our_codes(self):
        """The site gates every corners leg on this feed carrying its code,
        so a corners outcome the merge cannot name is a chip that builds
        nothing, silently."""
        for line in ("7.5", "8.5", "9.5", "10.5"):
            for side in ("OV", "UN"):
                code = "CORNERS_%s_%s" % (side, line)
                ids = server.market_for(code)
                key = (str(ids["marketId"]), str(ids["outcomeId"]), ids.get("specifier", "") or "")
                self.assertEqual(server._ODDS_LOOKUP.get(key), code)

    def test_every_offered_market_is_swept(self):
        missing = []
        for code in self.BUILDER_MARKETS:
            ids = server.market_for(code)
            self.assertIsNotNone(ids, "%s is offered but maps to nothing" % code)
            if str(ids["marketId"]) not in server.FIXTURE_MARKET_IDS:
                missing.append("%s (market %s)" % (code, ids["marketId"]))
        self.assertEqual(missing, [],
                         "offered with no real price, so priced off the model: "
                         + ", ".join(missing))

    def test_the_odds_extractor_can_name_the_new_markets(self):
        """Fetching an id buys nothing if the merge cannot turn its outcomes
        back into our code. _ODDS_LOOKUP is built from both tables, and the
        chance-mix codes live in the pass-through one."""
        for code in ("MIX_1_OV_2.5", "MIX_X_OV_2.5", "MIX_2_OV_2.5",
                     "MIXGG_1", "MIXGG_X", "MIXGG_2"):
            ids = server.market_for(code)
            key = (str(ids["marketId"]), str(ids["outcomeId"]), ids.get("specifier", "") or "")
            self.assertEqual(server._ODDS_LOOKUP.get(key), code,
                             "a fetched %s outcome would be dropped on the floor" % code)

    def test_win_a_half_is_a_known_gap(self):
        """WIN A HALF IS STILL ESTIMATED, and this is the record of it rather
        than a silent omission. Markets 50 and 51, one pass each, and the sweep
        has just doubled from seven ids to thirteen against a host that has
        refused us before - so it is a separate decision, not a drive-by. If it
        is ever added, this test should be deleted and the two codes moved into
        BUILDER_MARKETS above."""
        for code in ("WINHALF_H_Y", "WINHALF_A_Y"):
            ids = server.market_for(code)
            self.assertNotIn(str(ids["marketId"]), server.FIXTURE_MARKET_IDS,
                             "win-a-half is swept now - move it into BUILDER_MARKETS")


class TeamCornersBuildable(unittest.TestCase):
    """25 Sep 2026: the site's Team corners chip builds these, so both books
    must book them and the sweep must quote them."""

    def test_sporty_uses_30_31_not_the_totals_12_13(self):
        for side, mid in (("H", 900300), ("A", 900301)):
            for line in ("1.5", "2.5", "3.5", "4.5", "5.5", "6.5"):
                ov = server.PASSTHROUGH_MAP["CORNERS_%s_OV_%s" % (side, line)]
                un = server.PASSTHROUGH_MAP["CORNERS_%s_UN_%s" % (side, line)]
                self.assertEqual((ov["marketId"], ov["outcomeId"], ov["specifier"]), (mid, 30, "total=%s" % line))
                self.assertEqual((un["marketId"], un["outcomeId"]), (mid, 31))

    def test_bet9ja_keys_name_the_side(self):
        m = server.bet9ja.PASSTHROUGH_MAP
        self.assertEqual(m["CORNERS_H_OV_4.5"][0], "S_CORNERSHOMEOU@4.5_HCO")
        self.assertEqual(m["CORNERS_H_UN_4.5"][0], "S_CORNERSHOMEOU@4.5_HCU")
        self.assertEqual(m["CORNERS_A_OV_2.5"][0], "S_CORNERSAWAYOU@2.5_ACO")
        self.assertEqual(m["CORNERS_A_UN_2.5"][0], "S_CORNERSAWAYOU@2.5_ACU")

    def test_the_sweep_asks_for_both_sides_after_everything_else(self):
        ids = server.FIXTURE_MARKET_IDS
        self.assertEqual(ids[-2:], ("900300", "900301"))

    def test_a_swept_outcome_comes_back_as_its_code(self):
        ev = {"markets": [{"id": 900301, "specifier": "total=2.5",
                           "outcomes": [{"id": "30", "odds": "1.41"}, {"id": "31", "odds": "2.85"}]}]}
        self.assertEqual(server._extract_odds(ev), {"CORNERS_A_OV_2.5": 1.41, "CORNERS_A_UN_2.5": 2.85})


class LiveCardAfterRefusal(unittest.TestCase):
    """_live_verdicts: after SportyBet refuses, their live card names the dead
    legs - and silence is never a verdict."""

    CARD = {"status": 0, "markets": {
        ("18", "12", "total=2.5"): (0, 1),                   # goals over 2.5 open
        ("900394", "12", "total=27.5"): (0, 1),              # shots re-lined to 27.5
        ("900394", "12", "total=28.5"): (0, 1),
        ("166", "12", "total=9.5"): (1, 1),                  # corners 9.5 suspended
    }, "odds": {("900394", "12", "total=27.5"): 1.85}}

    def setUp(self):
        self.real = server._event_markets
        server._event_markets = lambda ev, region="ng": {
            "ev:live": self.CARD, "ev:gone": None,
            "ev:ko": {"status": 1, "markets": {}},
            "ev:blank": {"status": 0, "markets": {}},
        }.get(ev)

    def tearDown(self):
        server._event_markets = self.real

    def verdicts(self, legs):
        return server._live_verdicts([{"eventId": e, "prediction": p} for e, p in legs])

    def test_an_open_leg_is_left_alone(self):
        self.assertEqual(self.verdicts([("ev:live", "OVER_2.5")]), [])

    def test_a_moved_line_names_the_current_one(self):
        self.assertEqual(self.verdicts([("ev:live", "SHOTS_OV_25.5")]),
                         [{"i": 0, "reason": "line_moved", "now": "SHOTS_OV_27.5", "odds": 1.85}],
                         "the nearest open line of the same market and side, at its live price")

    def test_a_suspended_market_is_closed_not_moved(self):
        self.assertEqual(self.verdicts([("ev:live", "CORNERS_OV_9.5")]),
                         [{"i": 0, "reason": "closed"}])

    def test_a_game_that_kicked_off_says_so(self):
        self.assertEqual(self.verdicts([("ev:ko", "OVER_2.5")]),
                         [{"i": 0, "reason": "started"}])

    def test_no_card_or_an_empty_one_names_nothing(self):
        """JTEJA5: a leg is never condemned on missing evidence."""
        self.assertEqual(self.verdicts([("ev:gone", "OVER_2.5"), ("ev:blank", "OVER_2.5")]), [])

    def test_the_route_names_dead_legs_from_the_card_before_probing(self):
        real_code, real_probe = server.generate_sportybet_code, server._probe_refusal
        server.generate_sportybet_code = lambda *a, **k: {
            "error": "invalid event data, no market there", "sent": []}
        server._probe_refusal = lambda *a, **k: self.fail("the card answered; no probe")
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:live", "prediction": "OVER_2.5"},
                    {"eventId": "ev:live", "prediction": "SHOTS_OV_25.5"},
                ]})
        finally:
            server.generate_sportybet_code, server._probe_refusal = real_code, real_probe
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["unbookable"], [{"eventId": "ev:live",
            "prediction": "SHOTS_OV_25.5", "reason": "line_moved", "now": "SHOTS_OV_27.5"}],
            "the refusal names the line; the price travels with the pre-booking check")


    def test_the_pre_booking_check_reads_only_corners_and_shots(self):
        """Goals legs are 1.4% stale and not worth a request; these are the
        lines SportyBet re-lines. The page asks before it books."""
        seen = []
        real = server._event_markets
        server._event_markets = lambda ev, region="ng": (seen.append(ev), self.CARD)[1]
        try:
            with server.app.test_client() as c:
                r = c.post("/api/sporty/live-check", json={"selections": [
                    {"eventId": "ev:a", "prediction": "OVER_2.5"},
                    {"eventId": "ev:b", "prediction": "SHOTS_OV_25.5"},
                ]})
        finally:
            server._event_markets = real
        self.assertEqual(seen, ["ev:b"], "the goals leg costs no request")
        self.assertEqual(r.get_json()["verdicts"], [{"eventId": "ev:b",
            "prediction": "SHOTS_OV_25.5", "reason": "line_moved",
            "now": "SHOTS_OV_27.5", "odds": 1.85}])

    def test_the_pre_booking_check_reads_at_most_eight_games(self):
        seen = []
        real = server._event_markets
        server._event_markets = lambda ev, region="ng": (seen.append(ev), None)[1]
        try:
            with server.app.test_client() as c:
                c.post("/api/sporty/live-check", json={"selections": [
                    {"eventId": "ev:%d" % i, "prediction": "CORNERS_OV_9.5"} for i in range(12)]})
        finally:
            server._event_markets = real
        self.assertEqual(len(seen), server.LINE_CHECK_EVENTS)
