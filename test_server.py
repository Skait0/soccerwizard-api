"""Tests for server.py - odds decode, multi-market merge, error paths, market-map
integrity, and the optional Redis cache layer. Zero external deps (stdlib unittest).

Run:  python -m unittest test_server -v
"""
import os, json, tempfile, unittest
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
        self.assertEqual(set(server.FIXTURE_MARKET_IDS), {"1", "10", "18", "29"})


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
