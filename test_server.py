"""Tests for server.py - odds decode, multi-market merge, error paths, market-map
integrity, and the optional Redis cache layer. Zero external deps (stdlib unittest).

Run:  python -m unittest test_server -v
"""
import os, re, json, tempfile, unittest
os.environ.pop("SENTRY_DSN", None)          # keep Sentry a no-op

import server


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

class BookingIsCheckedBeforeItIsSent(unittest.TestCase):
    """Half the card has no team-totals market, and one unplaceable leg among
    forty loses all forty. We hold every event's odds already, so the answer is
    known here without asking SportyBet - and naming the bad picks lets the
    caller drop exactly those instead of guessing."""

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

    def test_a_pick_with_no_market_is_named(self):
        bad = server._unbookable([
            {"eventId": "ev:good", "prediction": "OVER_1.5"},
            {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
        ])
        self.assertEqual(bad, [{"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"}])

    def test_a_fully_bookable_slip_is_left_alone(self):
        self.assertEqual(server._unbookable([
            {"eventId": "ev:good", "prediction": "OVER_1.5"},
            {"eventId": "ev:good", "prediction": "GG"},
            {"eventId": "ev:thin", "prediction": "OVER_1.5"},
        ]), [])

    def test_an_event_we_hold_no_prices_for_is_not_judged(self):
        # absent from the cache entirely - we cannot say, so we do not
        self.assertEqual(server._unbookable([
            {"eventId": "ev:unknown", "prediction": "HOME_OVER_0.5"},
        ]), [])

    def test_an_empty_cache_blocks_nothing(self):
        """A server that has just started holds no prices. Refusing every slip
        until the first refresh would be worse than the failure this prevents."""
        server._FIXTURES_CACHE.clear()
        self.assertEqual(server._unbookable([
            {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
        ]), [])

    def test_the_route_refuses_early_and_says_which(self):
        called = {"n": 0}
        real = server.generate_sportybet_code
        server.generate_sportybet_code = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        try:
            with server.app.test_client() as c:
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:good", "prediction": "OVER_1.5"},
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"},
                ]})
                self.assertEqual(r.status_code, 400)
                body = r.get_json()
                self.assertFalse(body["success"])
                self.assertEqual(body["unbookable"],
                                 [{"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"}])
                self.assertIn("no market there", body["detail"])
            self.assertEqual(called["n"], 0, "must not spend a call on a doomed slip")
        finally:
            server.generate_sportybet_code = real

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
                r = c.post("/api/generate-booking-code", json={"selections": [
                    {"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"}]})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.get_json()["unbookable"],
                                 [{"eventId": "ev:thin", "prediction": "HOME_OVER_0.5"}])
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

    def _run(self, selections, priced, seen):
        real_ev, real_gen = server.bet9ja.fetch_event, server.bet9ja.generate_code
        real_report = server.report
        server.bet9ja.fetch_event = lambda eid: (
            {"eventId": eid, "raw": {c: "2.00" for c in priced.get(str(eid), [])}}
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


class TheProcfileMustNotMultiplyTheSweeps(unittest.TestCase):
    """Why the worker count is pinned, enforced rather than just commented.

    Gunicorn imports the app once PER WORKER, and both refreshers start at
    import. So every extra worker starts another fixtures sweep - fifty-six
    sequential requests to SportyBet, kept sequential because doing them
    concurrently got every one refused from a Railway IP - and another Bet9ja
    sweep. Raising --workers to "make booking faster" would multiply the
    traffic at the endpoints that already refuse bursts.

    Request concurrency comes from THREADS instead. Before that the default
    single sync worker served one request at a time: five concurrent hits on
    the trivial / endpoint came back at 2.2, 4.5, 5.6 and 9.5 seconds and a
    sixth never did, which is what made booking feel slow when the booking
    call itself is one round trip.
    """

    def _procfile(self):
        with open(os.path.join(os.path.dirname(__file__), "Procfile")) as f:
            return f.read()

    def test_exactly_one_worker(self):
        self.assertIn("--workers 1", self._procfile(),
                      "each extra worker starts another SportyBet sweep")

    def test_concurrency_comes_from_threads(self):
        p = self._procfile()
        self.assertIn("--worker-class gthread", p)
        m = re.search(r"--threads (\d+)", p)
        self.assertIsNotNone(m, "gthread without --threads is still one at a time")
        self.assertGreater(int(m.group(1)), 1,
                           "one thread is the single-worker behaviour again")

    def test_the_refreshers_really_do_start_at_import(self):
        """The premise. If these ever move behind a guard that runs once per
        deploy rather than once per process, the --workers 1 rule can be
        revisited - and this test should be the thing that says so."""
        src = open(os.path.join(os.path.dirname(__file__), "server.py"),
                   encoding="utf-8").read()
        for call in ("_start_fixtures_thread()", "_start_bet9ja_thread()"):
            self.assertRegex(src, r"(?m)^" + re.escape(call),
                             call + " must be at module level for this rule to hold")

    def test_the_timeouts_survived_the_change(self):
        p = self._procfile()
        self.assertIn("--timeout 90", p)
        self.assertIn("--graceful-timeout 30", p,
                      "a restart must drain in-flight bookings, not cut them")
