"""Tests for betpawa.py - the market table, the place the line is read from,
the payload shape their booking endpoint actually accepts, and the guards
around a slip they will refuse. Zero external deps (stdlib unittest), no
network.

Run:  python -m unittest test_betpawa -v
"""
import json
import unittest
from unittest import mock

import betpawa


# An event in their real shape, cut to the markets under test. Ids are from
# Manta FC - Orense SC, 21 Sep 2026, read off the live card.
def _market(type_id, name, rows, handicap_type=None):
    mt = {"id": type_id, "name": name, "displayName": name, "tabs": ["all"]}
    if handicap_type:
        mt["handicapType"] = handicap_type
    return {"marketType": mt, "row": rows}


def _row(prices, specifier=None, handicap=None):
    row = {"id": "404289409", "prices": prices}
    if specifier is not None:
        row["specifier"] = specifier
    if handicap is not None:
        # THEIR ROW-LEVEL NUMBER, IN QUARTER UNITS. 10 is a 2.5 goals line.
        # Never the thing to key on - see TheLineLivesOnTheOutcome.
        row["handicap"] = handicap
    return row


def _price(pid, name, type_id, odds, handicap=None, display=None):
    p = {"id": pid, "name": name, "typeId": type_id, "odds": odds,
         "displayName": display or name}
    if handicap is not None:
        p["handicap"] = handicap
    return p


ONE_X_TWO = _market("3743", "1X2 - FT", [_row([
    _price("1547926177", "1", "3744", 2.53),
    _price("1547926178", "X", "3745", 3.18),
    _price("1547926179", "2", "3746", 2.93),
])])

TOTALS = _market("5000", "Total Score Over/Under - FT", [
    _row([_price("1547925884", "Over", "5001", 1.30, "1.5",
                 "Over {formattedHandicap}"),
          _price("1547925887", "Under", "5002", 3.30, "1.5",
                 "Under {formattedHandicap}")],
         specifier={"total": "1.5"}, handicap=6),
    _row([_price("1547925885", "Over", "5001", 2.15, "2.5",
                 "Over {formattedHandicap}"),
          _price("1547925886", "Under", "5002", 1.58, "2.5",
                 "Under {formattedHandicap}")],
         specifier={"total": "2.5"}, handicap=10),
], handicap_type="NORMAL")


def _event(markets, event_id="38090806"):
    return {
        "id": event_id,
        "name": "Manta FC - Orense SC",
        "startTime": "2026-09-21T19:00:00Z",
        "participants": [{"name": "Manta FC"}, {"name": "Orense SC"}],
        "competition": {"id": "12751", "name": "LigaPro Primera A"},
        "region": {"id": "131", "name": "Ecuador"},
        "markets": markets,
    }


def _parse(event):
    row = betpawa._row(event)
    betpawa._absorb(row, event)
    return row


class MarketTable(unittest.TestCase):
    """Every triple was read off a live card. These guard the reading."""

    def test_no_two_codes_share_a_triple(self):
        # Two codes on one triple means the reverse table silently loses one,
        # and a pasted leg decodes as the other bet.
        triples = list(betpawa.MARKET_MAP.values())
        self.assertEqual(len(triples), len(set(triples)))

    def test_the_reverse_table_is_derived_not_typed(self):
        for code, triple in betpawa.MARKET_MAP.items():
            self.assertEqual(betpawa._BY_TRIPLE[triple], code)

    def test_an_unknown_market_refuses_rather_than_defaulting(self):
        # `market_for(pred) or HOME_WIN` shipped on another book and booked a
        # bet nobody asked for, because the bookmaker accepted it.
        self.assertIsNone(betpawa.market_for("CORNERS_OV_9.5"))
        self.assertIsNone(betpawa.market_for(""))
        self.assertIsNone(betpawa.market_for(None))

    def test_the_sweep_asks_for_every_market_the_table_names(self):
        # Two lists that can drift is how a book ends up unable to book a
        # market it can price. SWEEP_MARKETS is derived; this pins that.
        self.assertEqual(set(betpawa.SWEEP_MARKETS),
                         {m for m, _l, _o in betpawa.MARKET_MAP.values()})

    def test_every_modelled_market_the_other_books_carry_is_carried_here(self):
        # A fourth book that cannot price what the board publishes is a book
        # the builder has to special-case. All four carry the same 24.
        import server, bet9ja, betking
        modelled = set(server.MARKET_MAP) | set(bet9ja.MARKET_MAP) | \
            set(betking.MARKET_MAP)
        missing = sorted(modelled - set(betpawa.MARKET_MAP))
        self.assertEqual(missing, [])

    def test_the_ids_are_strings_on_both_sides(self):
        # Their JSON carries ids as strings. A str/int mix in the table is how
        # a lookup stops matching without anything failing.
        for code, (mid, line, out) in betpawa.MARKET_MAP.items():
            self.assertIsInstance(mid, str, code)
            self.assertIsInstance(out, str, code)
            self.assertTrue(line is None or isinstance(line, str), code)


class TheLineLivesOnTheOutcome(unittest.TestCase):
    """The trap that makes a generated table silently wrong."""

    def test_the_outcome_beats_the_row(self):
        row = _row([_price("1", "Over", "5001", 2.15, "2.5")],
                   specifier={"total": "9.5"}, handicap=10)
        self.assertEqual(betpawa._line(row["prices"][0], row), "2.5")

    def test_the_specifier_is_the_fallback(self):
        # Which is what the read-back leans on: their reprint carries the line
        # on the market for some families and on the selection for others.
        row = _row([_price("1", "Over", "5001", 2.15)],
                   specifier={"total": "2.5"})
        self.assertEqual(betpawa._line(row["prices"][0], row), "2.5")

    def test_the_rows_own_number_is_never_the_line(self):
        # 10 is their quarter-unit encoding of 2.5, and keying on it would
        # fold every line of a market onto one entry.
        row = _row([_price("1", "Over", "5001", 2.15)], handicap=10)
        self.assertIsNone(betpawa._line(row["prices"][0], row))

    def test_a_market_with_one_row_has_no_line(self):
        self.assertIsNone(betpawa._line(ONE_X_TWO["row"][0]["prices"][0],
                                        ONE_X_TWO["row"][0]))

    def test_each_line_of_a_market_keeps_its_own_price(self):
        row = _parse(_event([TOTALS]))
        self.assertEqual(row["odds"]["OVER_1.5"], 1.30)
        self.assertEqual(row["odds"]["OVER_2.5"], 2.15)
        self.assertEqual(row["odds"]["UNDER_2.5"], 1.58)
        self.assertEqual(row["sel"]["OVER_2.5"]["priceId"], "1547925885")


class ParsingTheCard(unittest.TestCase):

    def test_the_fixture_shell_matches_the_other_books(self):
        row = _parse(_event([ONE_X_TWO]))
        self.assertEqual(row["eventId"], "38090806")
        self.assertEqual(row["teams"], "Manta FC - Orense SC")
        self.assertEqual(row["league"], "LigaPro Primera A")
        self.assertEqual(row["country"], "Ecuador")
        self.assertEqual(row["startdate"], "2026-09-21")
        # Passed through untouched: the site pairs on this stamp.
        self.assertEqual(row["kickoff"], "2026-09-21T19:00:00Z")

    def test_a_collapsed_price_is_not_a_price(self):
        # A suspended outcome stays on the card at 1.0. It pays nothing and
        # cannot be booked, so carrying it would advertise an unbookable leg.
        row = _parse(_event([_market("3743", "1X2 - FT", [_row([
            _price("1", "1", "3744", 1.0),
            _price("2", "X", "3745", 3.18),
        ])])]))
        self.assertNotIn("1", row["odds"])
        self.assertIn("X", row["odds"])

    def test_a_market_we_do_not_carry_is_skipped_not_guessed(self):
        row = _parse(_event([_market("28000869", "Correct Score", [_row([
            _price("9", "0-0", "28000870", 9.5)])])]))
        self.assertEqual(row["odds"], {})

    def test_a_price_that_is_not_a_number_is_dropped(self):
        row = _parse(_event([_market("3743", "1X2 - FT", [_row([
            _price("1", "1", "3744", None),
            _price("2", "X", "3745", "3.18"),
        ])])]))
        self.assertNotIn("1", row["odds"])
        self.assertEqual(row["odds"]["X"], 3.18)


class TheSweep(unittest.TestCase):

    def test_the_query_goes_in_q_as_json(self):
        # `events=` answers BAD_REQUEST / "Request is empty", which reads like
        # a refusal and is only a wrong parameter name.
        seen = {}

        def fake(url, params=None, timeout=30):
            seen["url"], seen["params"] = url, params
            return {"responses": [{"responses": []}]}

        with mock.patch.object(betpawa, "_get_json", side_effect=fake):
            betpawa.fetch_page(200)
        self.assertIn("q", seen["params"])
        query = json.loads(seen["params"]["q"])["queries"][0]
        self.assertEqual(query["skip"], 200)
        self.assertEqual(query["take"], betpawa.PAGE)
        self.assertEqual(query["query"]["categories"], ["2"])
        # Ids, not the slugs their front end shows: `_1X2` is accepted and
        # answers events with no markets at all.
        self.assertEqual(query["view"]["marketTypes"], betpawa.SWEEP_MARKETS)

    def test_a_page_past_the_end_is_the_end_and_not_a_failure(self):
        # Their answer is {"responses": [{}]} - a 200 with the inner key
        # ABSENT. Read as an error it retries the terminal page every cycle.
        with mock.patch.object(betpawa, "_get_json",
                               return_value={"responses": [{}]}):
            self.assertEqual(betpawa.fetch_page(0), [])

    def test_the_sweep_stops_when_the_board_does(self):
        pages = [[_event([ONE_X_TWO], "1"), _event([ONE_X_TWO], "2")], []]
        with mock.patch.object(betpawa, "fetch_page",
                               side_effect=pages + [[]] * 10), \
             mock.patch.object(betpawa.time, "sleep"):
            fixtures, stats = betpawa.all_fixtures(pages=10)
        self.assertEqual(sorted(fixtures), ["1", "2"])
        self.assertEqual(stats["pages"], 1)
        self.assertEqual(stats["failed"], [])

    def test_a_refused_page_is_named_rather_than_counted_as_the_end(self):
        # The difference between "the board ends here" and "they stopped
        # answering" is the whole point of the stats, so it cannot be silent.
        with mock.patch.object(betpawa, "fetch_page",
                               side_effect=RuntimeError("blocked")), \
             mock.patch.object(betpawa.time, "sleep"):
            fixtures, stats = betpawa.all_fixtures(pages=3)
        self.assertEqual(fixtures, {})
        self.assertEqual(stats["failed"], [0])

    def test_a_fixture_priced_on_nothing_is_not_carried(self):
        # Carrying it makes the count lie about coverage.
        empty = _event([_market("28000869", "Correct Score", [_row([
            _price("9", "0-0", "28000870", 9.5)])])], "3")
        with mock.patch.object(betpawa, "fetch_page",
                               side_effect=[[empty], []]), \
             mock.patch.object(betpawa.time, "sleep"):
            fixtures, _ = betpawa.all_fixtures(pages=2)
        self.assertEqual(fixtures, {})

    def test_one_transient_blip_does_not_lose_a_page(self):
        # Measured: one bodyless 200 in ten pages of a real sweep.
        calls = {"n": 0}

        class Resp:
            @staticmethod
            def json():
                return {"responses": [{"responses": []}]}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("no json")
            return Resp()

        with mock.patch.object(betpawa.requests, "get", side_effect=flaky), \
             mock.patch.object(betpawa.time, "sleep"):
            self.assertEqual(betpawa.fetch_page(0), [])
        self.assertEqual(calls["n"], 2)


class BuildingTheSlip(unittest.TestCase):

    def test_a_leg_is_a_price_id_and_nothing_else(self):
        row = _parse(_event([TOTALS]))
        self.assertEqual(betpawa.build_selection(row, "OVER_2.5"), 1547925885)

    def test_a_market_not_on_the_event_refuses(self):
        row = _parse(_event([TOTALS]))
        with self.assertRaises(KeyError):
            betpawa.build_selection(row, "GG")

    def test_the_payload_is_the_shape_their_endpoint_accepts(self):
        """`type` as a NUMBER answers SPORTSBOOK_WRONG_SELECTION.

        The doubled `selections` key is not a typo either - it is what their
        own bundle sends, and this is the only place in four books where the
        whole leg is one integer.
        """
        row = _parse(_event([TOTALS]))
        sent = {}

        class Resp:
            @staticmethod
            def json():
                return {"code": "NQD5C01"}

        def fake_post(url, data=None, headers=None, **kw):
            sent["url"], sent["body"] = url, json.loads(data)
            return Resp()

        with mock.patch.object(betpawa.requests, "post", side_effect=fake_post):
            out = betpawa.generate_code(
                [{"event": row, "code": "OVER_2.5"}], verify=False)
        self.assertEqual(out["code"], "NQD5C01")
        self.assertEqual(sent["url"], betpawa.BOOKING_URL)
        self.assertEqual(sent["body"], {"selections": {"selections": [
            {"type": "SINGLE", "selections": [1547925885]}]}})

    def test_two_legs_on_one_game_are_named_before_they_are_sent(self):
        # Their multiple refuses the pair with 400 SPORTSBOOK_WRONG_SELECTION,
        # naming nothing - so the whole slip would die for one leg.
        row = _parse(_event([TOTALS]))
        out = betpawa.generate_code([{"event": row, "code": "OVER_2.5"},
                                     {"event": row, "code": "UNDER_1.5"}])
        self.assertIn("one game", out["error"])
        self.assertIn("UNDER_1.5", out["error"])

    def test_an_empty_slip_and_an_oversized_one_both_refuse(self):
        self.assertIn("error", betpawa.generate_code([]))
        row = _parse(_event([TOTALS]))
        big = [{"event": row, "code": "OVER_2.5"}] * (betpawa.BETSLIP_MAX + 1)
        self.assertIn("capped", betpawa.generate_code(big)["error"])


class TheReadBack(unittest.TestCase):
    """They refuse a bad selection outright, unlike BetKing. The check stays."""

    @staticmethod
    def _reprint(selections, original=1):
        return {"originalCount": original, "items": [{
            "eventInfo": {
                "id": "38090806",
                "participants": [{"name": "Manta FC"}, {"name": "Orense SC"}],
                "startTime": "2026-09-21T19:00:00Z",
                "competition": {"name": "LigaPro Primera A"},
            },
            "selections": selections,
            "odds": {"price": 2.15},
        }]}

    TOTAL_LEG = [{
        "market": {"typeId": "5000", "name": "Total Score Over/Under - FT",
                   "specifier": {"total": "2.5"}},
        "selectionInfo": {"id": "1547925885", "name": "Over", "typeId": "5001",
                          "handicap": "2.5",
                          "displayName": "Over {formattedHandicap}"},
    }]

    def test_a_leg_decodes_through_the_same_table_it_was_written_with(self):
        with mock.patch.object(betpawa, "_get_json",
                               return_value=self._reprint(self.TOTAL_LEG)):
            got = betpawa.read_coupon("NQD5C01")
        leg = got["legs"][0]
        self.assertEqual(leg["prediction"], "OVER_2.5")
        self.assertEqual(leg["home"], "Manta FC")
        self.assertEqual(leg["away"], "Orense SC")
        self.assertEqual(leg["odds"], 2.15)
        self.assertEqual(got["booked"], 1)

    def test_the_line_is_read_from_the_market_when_the_outcome_omits_it(self):
        legs = [{"market": dict(self.TOTAL_LEG[0]["market"]),
                 "selectionInfo": {k: v for k, v
                                   in self.TOTAL_LEG[0]["selectionInfo"].items()
                                   if k != "handicap"}}]
        with mock.patch.object(betpawa, "_get_json",
                               return_value=self._reprint(legs)):
            got = betpawa.read_coupon("NQD5C01")
        self.assertEqual(got["legs"][0]["prediction"], "OVER_2.5")

    def test_their_template_never_reaches_the_reader(self):
        # displayName is "Over {formattedHandicap}" and their front end fills
        # it. Shipped raw it reads as braces on our panel.
        with mock.patch.object(betpawa, "_get_json",
                               return_value=self._reprint(self.TOTAL_LEG)):
            raw = betpawa.read_coupon("NQD5C01")["legs"][0]["raw"]
        self.assertNotIn("{", raw)
        self.assertIn("Over 2.5", raw)

    def test_an_unmapped_market_still_has_a_name(self):
        legs = [{"market": {"typeId": "28000869", "name": "Correct Score"},
                 "selectionInfo": {"id": "9", "name": "2-1",
                                   "typeId": "28000871"}}]
        with mock.patch.object(betpawa, "_get_json",
                               return_value=self._reprint(legs)):
            leg = betpawa.read_coupon("XXXXXXX")["legs"][0]
        self.assertIsNone(leg["prediction"])
        self.assertIn("2-1", leg["raw"])

    def test_a_code_they_do_not_know_is_not_found_rather_than_an_error(self):
        with mock.patch.object(betpawa, "_get_json", return_value={
                "error": "BOOKING_CODE_NOT_FOUND", "payload": None}):
            self.assertTrue(betpawa.read_coupon("ZZZZZZ")["notFound"])

    def test_a_short_code_is_reported_as_our_bug_not_their_refusal(self):
        # The BetKing lesson: a booking endpoint can answer success for a slip
        # it did not understand, and nothing in the response says so.
        row = _parse(_event([TOTALS]))

        class Resp:
            @staticmethod
            def json():
                return {"code": "SHORT01"}

        with mock.patch.object(betpawa.requests, "post", return_value=Resp()), \
             mock.patch.object(betpawa, "read_code", return_value=(2, [{}])):
            out = betpawa.generate_code([{"event": row, "code": "OVER_2.5"},
                                         {"event": dict(row, eventId="9"),
                                          "code": "UNDER_2.5"}])
        self.assertIn("1 of 2 legs", out["error"])
        self.assertEqual(out["code"], "SHORT01")

    def test_a_verified_code_says_so(self):
        row = _parse(_event([TOTALS]))

        class Resp:
            @staticmethod
            def json():
                return {"code": "NQD5C01"}

        with mock.patch.object(betpawa.requests, "post", return_value=Resp()), \
             mock.patch.object(betpawa, "read_code", return_value=(1, [{}])):
            out = betpawa.generate_code([{"event": row, "code": "OVER_2.5"}])
        self.assertTrue(out["verified"])
        self.assertEqual(out["legs"], 1)


class TheAsymmetryIsWrittenDown(unittest.TestCase):
    """What this book does NOT carry yet, said out loud rather than implied.

    Betpawa has the modelled 24 and no pass-through tail, so a code from
    another book carrying corners or a handicap reads and splits here and
    cannot convert. That is a decision about work not yet done - the tail is
    step 5 of the integration order - and not a statement about their
    catalogue, which demonstrably sells all of it (63 markets on a mid-table
    fixture). The two must never look the same in a year.
    """

    def test_the_module_says_the_tail_is_unmapped_rather_than_absent(self):
        with open("betpawa.py", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("PASSTHROUGH_MAP", src,
                         "if the tail exists now, this test has to say so")


class TheContainerCanActuallyRunThis(unittest.TestCase):
    """An import that only resolved as somebody else's dependency took the
    service down once. Same check as the other books."""

    def test_every_import_is_declared(self):
        with open("betpawa.py", encoding="utf-8") as fh:
            src = fh.read()
        with open("requirements.txt", encoding="utf-8") as fh:
            declared = fh.read().lower()
        for name in ("curl_cffi",):
            if "import %s" % name in src or "from %s" % name in src:
                self.assertIn(name, declared, name)


if __name__ == "__main__":
    unittest.main()
