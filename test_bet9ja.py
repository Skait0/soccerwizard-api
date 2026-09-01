"""Tests for bet9ja.py - the odds decode, the key parser, and the shapes their
API rejects. Zero external deps (stdlib unittest), no network.

Run:  python -m unittest test_bet9ja -v
"""
import json
import re
import unittest
from unittest import mock

import bet9ja


class ParseOddsKey(unittest.TestCase):
    """The one thing worth being exact about.

    A first pass assumed the key split at the LAST underscore, which makes
    S_OU@1.5_O into a market of "S_OU@1.5". It does not. Their bundle pulls the
    handicap into its own field and the market is just "S_OU". A selection built
    the wrong way is rejected with nothing in the response that points at why,
    so this is the difference between a working integration and a long evening.
    """

    def test_plain_market(self):
        self.assertEqual(bet9ja.parse_odds_key("S_DC_1X"), ("S_DC", "1X", None))
        self.assertEqual(bet9ja.parse_odds_key("S_1X2_2"), ("S_1X2", "2", None))

    def test_handicap_is_split_out_not_left_in_the_market(self):
        self.assertEqual(bet9ja.parse_odds_key("S_OU@1.5_O"), ("S_OU", "O", "1.5"))
        self.assertEqual(bet9ja.parse_odds_key("S_OU@2.5_U"), ("S_OU", "U", "2.5"))

    def test_the_asymmetric_team_markets(self):
        # Home is S_HTS, away is S_AWAYSCORE. Not a typo on their side and not
        # something a mirrored mapper would ever produce.
        self.assertEqual(bet9ja.parse_odds_key("S_HTS_Y"), ("S_HTS", "Y", None))
        self.assertEqual(bet9ja.parse_odds_key("S_AWAYSCORE_Y"),
                         ("S_AWAYSCORE", "Y", None))

    def test_a_key_that_is_not_one_raises(self):
        for bad in ("", "S_DC", "nonsense"):
            with self.assertRaises(ValueError):
                bet9ja.parse_odds_key(bad)


class MarketMap(unittest.TestCase):
    def test_every_default_market_is_covered(self):
        # The site's four default markets. If any of these is missing, Bet9ja
        # cannot build the slip the slider and wizard actually produce.
        for code in ("1X", "X2", "1", "2", "OVER_1.5",
                     "HOME_OVER_0.5", "AWAY_OVER_0.5"):
            self.assertIn(code, bet9ja.MARKET_MAP, code + " is not mapped")

    def test_every_mapped_key_parses(self):
        for code, (key, group) in bet9ja.MARKET_MAP.items():
            sid, sign, _aux = bet9ja.parse_odds_key(key)
            self.assertTrue(sid.startswith("S_"), code)
            self.assertTrue(sign, code)
            self.assertIn(group, (bet9ja.POPULAR, bet9ja.HOME_AWAY), code)

    def test_the_reverse_lookup_has_no_collisions(self):
        keys = [k for k, _g in bet9ja.MARKET_MAP.values()]
        self.assertEqual(len(keys), len(set(keys)),
                         "two of our codes map to the same Bet9ja key")

    def test_groups_are_derived_not_restated(self):
        # Adding a market in a new group must not leave that group unfetched.
        for _code, (_key, group) in bet9ja.MARKET_MAP.items():
            self.assertIn(group, bet9ja.MARKET_GROUPS)

    def test_home_away_markets_are_in_the_home_away_group(self):
        # MKEY is not a small integer and is not guessable: a sweep of 1-8 finds
        # only group 1. Team goals live in 170, found by watching the tab click.
        self.assertEqual(bet9ja.MARKET_MAP["HOME_OVER_0.5"][1], bet9ja.HOME_AWAY)
        self.assertEqual(bet9ja.MARKET_MAP["AWAY_OVER_0.5"][1], bet9ja.HOME_AWAY)
        self.assertEqual(bet9ja.MARKET_MAP["1X"][1], bet9ja.POPULAR)


def _feed(events, dict_shape=True):
    """A GetEventsInCouponV2 response."""
    E = events if dict_shape else list(events.values())
    return {"R": "OK", "D": {"G": {"170880": {"GID": 170880, "GN": "Premier League",
                                              "E": E}}}}


def _event(eid="825683591", odds=None):
    return {"ID": eid, "DS": "Ipswich Town - Liverpool", "GN": "Premier League",
            "STARTDATEUTC": "2026-09-04T19:00:00Z",
            "STARTDATE": "2026-09-04 19:00:00", "EXTID": "72221244",
            "O": odds if odds is not None else {"S_DC_1X": "2.40", "S_OU@1.5_O": "1.16"}}


class FetchEvents(unittest.TestCase):
    def _run(self, payload):
        r = mock.Mock(); r.json.return_value = payload
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            return bet9ja.fetch_events(492)

    def test_decodes_the_markets_we_offer(self):
        out = self._run(_feed({"0": _event()}))
        row = list(out.values())[0]
        self.assertEqual(row["teams"], "Ipswich Town - Liverpool")
        self.assertEqual(row["kickoff"], "2026-09-04T19:00:00Z")
        self.assertEqual(row["odds"], {"1X": 2.40, "OVER_1.5": 1.16})

    def test_events_may_arrive_as_a_list(self):
        # `E` is a dict on some groups and a list on others. Both carry the same
        # objects, and assuming a dict raised AttributeError on the first real
        # call this module ever made.
        out = self._run(_feed({"0": _event()}, dict_shape=False))
        self.assertEqual(len(out), 1)

    def test_markets_we_do_not_offer_are_ignored(self):
        out = self._run(_feed({"0": _event(odds={"S_CORNERS_O": "1.5",
                                                 "S_DC_1X": "2.40"})}))
        self.assertEqual(list(out.values())[0]["odds"], {"1X": 2.40})

    def test_suspended_prices_are_skipped_not_crashed_on(self):
        # A suspended market comes back as "-" or "".
        out = self._run(_feed({"0": _event(odds={"S_DC_1X": "-", "S_OU@1.5_O": ""})}))
        self.assertEqual(out, {}, "an event with no usable price should be dropped")

    def test_an_event_with_no_market_of_ours_is_dropped(self):
        out = self._run(_feed({"0": _event(odds={"S_CORNERS_O": "1.5"})}))
        self.assertEqual(out, {})

    def test_a_dead_feed_returns_empty_rather_than_raising(self):
        # This feeds a page. A bookmaker being unreachable is not a 500.
        with mock.patch.object(bet9ja.requests, "get",
                               side_effect=OSError("connection reset")):
            self.assertEqual(bet9ja.fetch_events(492), {})

    def test_junk_json_returns_empty(self):
        r = mock.Mock(); r.json.side_effect = ValueError("not json")
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            self.assertEqual(bet9ja.fetch_events(492), {})


class FetchLeagueMerges(unittest.TestCase):
    def test_market_groups_are_merged_onto_one_event(self):
        """The markets we offer are split across groups, and no single group
        carries them all - so two requests, merged by event."""
        popular = _feed({"0": _event(odds={"S_DC_1X": "2.40"})})
        home_away = _feed({"0": _event(odds={"S_HTS_Y": "1.38",
                                             "S_AWAYSCORE_Y": "1.07"})})
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            r = mock.Mock()
            r.json.return_value = home_away if "MKEY=170" in url else popular
            return r

        with mock.patch.object(bet9ja.requests, "get", side_effect=fake_get):
            out = bet9ja.fetch_league(492)

        self.assertEqual(len(calls), len(bet9ja.MARKET_GROUPS))
        row = list(out.values())[0]
        self.assertEqual(row["odds"],
                         {"1X": 2.40, "HOME_OVER_0.5": 1.38, "AWAY_OVER_0.5": 1.07})

    def test_the_raw_prices_are_merged_too_not_just_the_floats(self):
        """Caught by the first live booking, not by a test.

        `odds` is for our arithmetic, `raw` is what goes on the wire. Merging
        only the first meant a market from the second group priced fine and
        then raised KeyError the moment a slip tried to use it - so team goals,
        which live in the Home/Away group, could never be booked.
        """
        popular = _feed({"0": _event(odds={"S_DC_1X": "2.40"})})
        home_away = _feed({"0": _event(odds={"S_HTS_Y": "1.38"})})

        def fake_get(url, **kw):
            r = mock.Mock()
            r.json.return_value = home_away if "MKEY=170" in url else popular
            return r

        with mock.patch.object(bet9ja.requests, "get", side_effect=fake_get):
            row = list(bet9ja.fetch_league(492).values())[0]
        self.assertEqual(set(row["raw"]), set(row["odds"]),
                         "every priced market must also have its wire value")
        self.assertEqual(row["raw"]["HOME_OVER_0.5"], "1.38")


class Leagues(unittest.TestCase):
    def test_flattens_countries_into_gid_keyed_competitions(self):
        payload = {"D": {"PAL": {"1": {"S_DESC": "Soccer", "SG": {
            "11058": {"SG_DESC": "England", "G": {
                "170880": {"G_DESC": "Premier League"},
                "170881": {"G_DESC": "Championship"}}},
            "11060": {"SG_DESC": "Sweden", "G": {
                "1348874": {"G_DESC": "Allsvenskan"}}}}}}}}
        r = mock.Mock(); r.json.return_value = payload
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            out = bet9ja.leagues()
        self.assertEqual(out["170880"], {"league": "Premier League", "country": "England"})
        self.assertEqual(out["1348874"], {"league": "Allsvenskan", "country": "Sweden"})

    def test_a_dead_catalogue_is_empty_not_an_exception(self):
        with mock.patch.object(bet9ja.requests, "get", side_effect=OSError("nope")):
            self.assertEqual(bet9ja.leagues(), {})


class FetchEvent(unittest.TestCase):
    """The per-event book is what makes every league bookable.

    Neither listing endpoint is enough alone: GetEventsInGroup reaches all 172
    competitions but ignores MKEY and only returns the default markets, while
    the coupon route honours MKEY and has team goals but exists for just the 14
    featured leagues. Asking about a single event returns roughly 1,300 markets
    and works anywhere.
    """

    def _run(self, payload):
        r = mock.Mock(); r.json.return_value = payload
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            return bet9ja.fetch_event("825683591")

    def test_reads_the_markets_we_offer_out_of_the_full_book(self):
        out = self._run({"D": {"ID": "825683591", "DS": "Ipswich Town - Liverpool",
                               "C": "1079", "GN": "Premier League", "SG": "England",
                               "STARTDATE": "2026-09-04 19:00:00",
                               "STARTDATEUTC": "2026-09-04T19:00:00Z",
                               "O": {"S_DC_1X": "2.4", "S_HTS_Y": "1.38",
                                     "S_CORNERS_O": "1.5"}}})
        self.assertEqual(out["odds"], {"1X": 2.4, "HOME_OVER_0.5": 1.38})
        self.assertEqual(out["raw"]["HOME_OVER_0.5"], "1.38",
                         "the wire value must be the string the feed gave")
        self.assertEqual(out["eventCode"], "1079")
        self.assertEqual(out["country"], "England")

    def test_an_unknown_event_is_none_not_a_half_built_dict(self):
        self.assertIsNone(self._run({"D": {}}))

    def test_a_dead_endpoint_is_none(self):
        with mock.patch.object(bet9ja.requests, "get", side_effect=OSError("nope")):
            self.assertIsNone(bet9ja.fetch_event("1"))


class BuildSelection(unittest.TestCase):
    def setUp(self):
        r = mock.Mock(); r.json.return_value = _feed({"7": _event()})
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            self.ev = list(bet9ja.fetch_events(492).values())[0]

    def test_carries_the_fields_their_slip_wants(self):
        """Matched field by field against a real booking, not inferred.

        Six of these were wrong on the first pass: sid was the split market
        rather than the whole key, oddValue was a float, the id was the feed's
        slot number, and market, SG and eventCode were missing entirely.
        """
        leg = bet9ja.build_selection(self.ev, "1X")
        self.assertEqual(leg["sid"], "S_DC_1X", "sid is the full odds key")
        self.assertEqual(leg["sign"], "1X")
        self.assertEqual(leg["market"], "DC", "the human market label")
        self.assertEqual(leg["hnd"], "")
        self.assertEqual(leg["oddValue"], "2.40", "odds go on the wire as strings")
        self.assertEqual(leg["eventId"], "825683591")
        self.assertEqual(leg["sportName"], "", "their own client sends empty")

    def test_the_selection_id_is_event_and_key(self):
        """The field that cost the most.

        Both EVS and ODDS are keyed by "{eventId}${oddsKey}". Using the feed's
        slot number instead produced a slip that passed format validation and
        then failed to resolve - a 500 with an empty body and nothing to say
        why.
        """
        leg = bet9ja.build_selection(self.ev, "1X")
        self.assertEqual(leg["id"], "825683591$S_DC_1X")
        self.assertEqual(leg["id"], bet9ja.selection_id("825683591", "S_DC_1X"))

    def test_a_handicap_goes_to_hnd_not_into_the_market(self):
        leg = bet9ja.build_selection(self.ev, "OVER_1.5")
        self.assertEqual((leg["sid"], leg["sign"], leg["hnd"], leg["market"]),
                         ("S_OU@1.5_O", "O", "1.5", "OU"))

    def test_the_name_is_rewritten_the_way_their_own_ui_does(self):
        # The feed says "A - B"; their client posts "A v B".
        self.assertEqual(bet9ja.build_selection(self.ev, "1X")["eventName"],
                         "Ipswich Town v Liverpool")

    def test_asking_for_a_market_this_event_lacks_raises(self):
        with self.assertRaises(KeyError):
            bet9ja.build_selection(self.ev, "AWAY_OVER_0.5")


class GenerateCode(unittest.TestCase):
    def setUp(self):
        r = mock.Mock(); r.json.return_value = _feed({"7": _event()})
        with mock.patch.object(bet9ja.requests, "get", return_value=r):
            self.ev = list(bet9ja.fetch_events(492).values())[0]
        self.picks = [{"event": self.ev, "code": "1X"}]

    def _posted(self, response):
        seen = {}

        def fake_post(url, data=None, headers=None, timeout=None, **kw):
            seen["url"] = url
            seen["slip"] = json.loads(data["BETSLIP"])
            r = mock.Mock(); r.json.return_value = response
            return r

        with mock.patch.object(bet9ja.requests, "post", side_effect=fake_post):
            out = bet9ja.generate_code(self.picks)
        return seen, out

    def test_odds_is_populated_not_empty(self):
        """The bug that cost the most time.

        Their builder does `ODDS[selection] = odd` before pushing the bet.
        Sending ODDS as {} produced `checkformatbetsliperror`, whose `data`
        field names "LIVE" and points at nothing relevant.
        """
        seen, _ = self._posted({"D": {"BOOKINGNUMBER": "5PTJJVX"}})
        bet = seen["slip"]["BETS"][0]
        self.assertTrue(bet["ODDS"], "ODDS must map selection id -> odd")
        self.assertEqual(set(bet["ODDS"]), set(seen["slip"]["EVS"]),
                         "every selection in EVS needs its odd in ODDS")

    def test_the_slip_carries_impersonize(self):
        # The last thing their builder sets before returning the slip.
        seen, _ = self._posted({"D": {"BOOKINGNUMBER": "X"}})
        self.assertIn("IMPERSONIZE", seen["slip"])

    def test_a_single_is_shaped_as_a_single(self):
        seen, _ = self._posted({"D": {"BOOKINGNUMBER": "X"}})
        bet = seen["slip"]["BETS"][0]
        # Integers, not the tab names. A real slip sends 0 for both.
        self.assertEqual((bet["BSTYPE"], bet["TAB"]), (0, 0))
        self.assertEqual((bet["NUMLINES"], bet["COMB"], bet["TYPE"]), (1, 1, 1))

    def test_an_accumulator_is_one_bet_carrying_every_leg(self):
        ev2 = dict(self.ev); ev2["slotId"] = "8"; ev2["eventId"] = "999"
        seen = {}

        def fake_post(url, data=None, headers=None, timeout=None, **kw):
            seen["slip"] = json.loads(data["BETSLIP"])
            r = mock.Mock(); r.json.return_value = {"D": {"BOOKINGNUMBER": "X"}}
            return r

        with mock.patch.object(bet9ja.requests, "post", side_effect=fake_post):
            bet9ja.generate_code([{"event": self.ev, "code": "1X"},
                                  {"event": ev2, "code": "OVER_1.5"}])
        self.assertEqual(len(seen["slip"]["BETS"]), 1, "an accumulator is one bet")
        bet = seen["slip"]["BETS"][0]
        self.assertEqual(bet["TAB"], 0)
        self.assertEqual((bet["NUMLINES"], bet["COMB"], bet["TYPE"]), (2, 1, 2))

    def test_a_booking_carries_no_money(self):
        # It is a shareable slip, not a bet. Their own UI books with zeros.
        seen, _ = self._posted({"D": {"BOOKINGNUMBER": "X"}})
        bet = seen["slip"]["BETS"][0]
        for field in ("STAKE", "POTWINMIN", "POTWINMAX", "BONUSMIN", "BONUSMAX"):
            self.assertEqual(bet[field], 0, field)

    def test_the_code_is_read_from_ris(self):
        """It is data[0].RIS, seven characters, the same shape their own site
        shows. Not COUPONCODE, which sits beside it and is an internal UUID -
        handing somebody that gives them a code Bet9ja will not load."""
        body = {"status": 1, "error": {"code": 0, "message": ""},
                "data": [{"RIS": "5PTL8WL",
                          "COUPONCODE": "043528ed-ced1-44b7-ac05-0f21b89d03f0",
                          "STATUS": 1}]}
        _seen, out = self._posted(body)
        self.assertEqual(out["code"], "5PTL8WL")
        self.assertNotIn("043528ed", str(out), "never hand back the internal id")

    def test_a_failed_status_is_not_mined_for_a_code(self):
        body = {"status": -1, "error": {"code": 100, "message": "checkformatbetsliperror"},
                "data": [{"RIS": "IGNORE"}]}
        _seen, out = self._posted(body)
        self.assertNotIn("code", out)

    def test_a_refusal_is_reported_not_swallowed(self):
        _seen, out = self._posted(
            {"status": -1, "error": {"code": 100, "message": "checkformatbetsliperror"}})
        self.assertNotIn("code", out)
        self.assertIn("checkformatbetsliperror", out["error"])

    def test_no_selections_is_an_error_not_a_request(self):
        with mock.patch.object(bet9ja.requests, "post") as post:
            self.assertIn("error", bet9ja.generate_code([]))
            post.assert_not_called()

    def test_a_dead_endpoint_returns_an_error_rather_than_raising(self):
        with mock.patch.object(bet9ja.requests, "post",
                               side_effect=OSError("connection reset")):
            self.assertIn("error", bet9ja.generate_code(self.picks))


class TheContainerCanActuallyRunThis(unittest.TestCase):
    """Both halves of the outage on 1 Sep 2026, which cost 42 minutes.

    bet9ja.py imported requests, requirements.txt did not list it, and the
    module was on my machine as someone else's transitive dependency. Every
    test passed. Every live booking worked. Gunicorn could not import
    server.py and the whole API - live scores, odds, both bookmakers - was
    down from the first Bet9ja deploy.

    Then, with the import fixed, the routes came back reporting success and
    returning nothing, because Bet9ja serves a datacentre IP an HTML block
    page. Only curl_cffi's TLS fingerprint gets JSON. Neither failure is
    reachable from a laptop, so both need asserting rather than testing.
    """

    def _imports(self, path):
        """Top-level `import x` / `from x import` names in one module."""
        import ast
        with open(path, encoding="utf8") as fh:
            tree = ast.parse(fh.read())
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
        return out

    def test_every_third_party_import_is_declared(self):
        import sys, os
        with open("requirements.txt", encoding="utf8") as fh:
            declared = {re.split(r"[\[<>=!;\s]", ln.strip())[0].lower().replace("-", "_")
                        for ln in fh if ln.strip() and not ln.startswith("#")}
        # flask-cors and sentry-sdk[flask] are imported as flask_cors and
        # sentry_sdk; stripping the extras and folding dashes to underscores
        # above already reconciles those, so there is no allowlist here. An
        # allowlist is how this test would quietly stop checking the very
        # packages it names.
        ours = {"bet9ja", "server", "generate_code"}
        for mod in ("server.py", "bet9ja.py", "generate_code.py"):
            for name in self._imports(mod):
                if name in ours or name in sys.stdlib_module_names:
                    continue
                self.assertIn(
                    name.lower(), declared,
                    "%s imports %r and requirements.txt does not list it - "
                    "the container will fail to boot even though every test "
                    "here passes" % (mod, name))

    def test_bet9ja_does_not_use_plain_requests(self):
        with open("bet9ja.py", encoding="utf8") as fh:
            src = fh.read()
        self.assertIn("from curl_cffi import requests", src)
        self.assertNotRegex(
            src, r"(?m)^import requests\b",
            "plain requests gets an HTML block page from a datacentre; it "
            "works locally, which is what makes it dangerous")

    def test_every_outbound_call_impersonates(self):
        """A call added without impersonate= works on a laptop and returns an
        empty result in production, on a route that still reports success."""
        with open("bet9ja.py", encoding="utf8") as fh:
            src = fh.read()
        calls = len(re.findall(r"\brequests\.(?:get|post)\(", src))
        marked = len(re.findall(r"\bimpersonate=IMPERSONATE\b", src))
        self.assertEqual(calls, marked,
                         "%d outbound calls but %d carry impersonate=" %
                         (calls, marked))


if __name__ == "__main__":
    unittest.main()
