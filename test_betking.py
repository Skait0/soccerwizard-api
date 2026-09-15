"""Tests for betking.py - the market table, the ids their booking endpoint
actually reads, and the guard against the code they hand back for a slip they
did not understand. Zero external deps (stdlib unittest), no network.

Run:  python -m unittest test_betking -v
"""
import unittest
from unittest import mock

import betking


# A feed payload in their real shape, cut down to the markets under test. The
# numbers are from Leeds v Newcastle, 14 Sep 2026, read off the live card.
def _collection(market_id, sbv, outcomes, collection_id=732487597, name="1X2"):
    return {
        "OddCollectionID": collection_id,
        "OddsType": {"OddsTypeID": market_id, "OddsTypeName": name},
        "SpecialBetValue": sbv,
        "GroupNo": 0,
        "Combinability": 1,
        "CompatibleMarkets": [],
        "MatchOdds": [
            {
                "OddCollectionID": collection_id,
                # THE TWO IDS. MatchOddsID is the one their coupon wants;
                # OutcomeID sits beside it and is five digits longer.
                "MatchOddsID": mo_id,
                "OddAttribute": {"OddTypeID": otype, "OddName": oname,
                                 "SpecialValue": sbv},
                "Outcome": {"OutcomeID": out_id, "OddOutcome": price},
            }
            for mo_id, otype, oname, out_id, price in outcomes
        ],
    }


def _payload(collections, item_id=1005309147):
    return {"AreaMatches": [{"Items": [{
        "ItemID": item_id,
        "ItemName": "Leeds - Newcastle",
        "ItemDate": "2026-09-14T21:00:00+02:00",
        "CategoryId": 10000840, "CategoryName": "England",
        "TournamentId": 20000841, "TournamentName": "Premier League",
        "SmartBetCode": 22615, "ExtProvIDItem": "72221262",
        "EventCategory": "F", "IncompatibleEvents": [],
        "OddsCollection": collections,
    }]}], "TotalNoOfItems": 1}


ONE_X_TWO = _collection(110, 0, [
    (2339553331, 4, "1", 13222310353, 2.35),
    (2339553332, 2, "X", 13222347944, 3.45),
    (2339553333, 5, "2", 13222347945, 2.97),
])


class MarketTable(unittest.TestCase):
    """Every triple was read off a live card. These guard the reading."""

    def test_no_two_codes_share_a_triple(self):
        # The reverse lookup is derived from BOTH forward tables, so a duplicate
        # triple would silently drop a code rather than fail. Counted against
        # the sum rather than the modelled table alone - the invariant is that
        # nothing collides, not that the map is small.
        # Smaller than the sum only by the codes that are DELIBERATELY two
        # names for one bet - EXGOALS_0 and OVER_0.5 say the same thing about
        # a non-negative integer. Anything else colliding is a code silently
        # dropped from the reverse index.
        # Only the first-half pair collides: our vocabulary has no OVER_0.5,
        # so EXGOALS_0 is the only name we hold for (160, 0.5, 12) and owns it
        # outright.
        alias = {"EXGOALS_FH_0"}
        self.assertEqual(
            len(betking._BY_TRIPLE),
            len(betking.MARKET_MAP) + len(betking.PASSTHROUGH_MAP) - len(alias))

    def test_a_code_is_in_one_table_or_the_other_never_both(self):
        # A code in both is two answers to one question, and which one wins
        # depends on statement order in market_for.
        self.assertEqual(set(betking.MARKET_MAP) & set(betking.PASSTHROUGH_MAP),
                         set())

    def test_a_carried_market_resolves_through_market_for(self):
        # Keyed on MARKET_MAP alone, every handicap and corner leg came back
        # "not_mapped" however well BetKing prices it, so no converted slip
        # carrying one could be booked. bet9ja.py made exactly this mistake.
        for code in ("AH_1_-0.5", "CORNERS_OV_9.5", "EH_0_1_X", "DNB_1"):
            self.assertIsNotNone(betking.market_for(code), code)

    def test_the_away_handicap_carries_the_negated_line(self):
        # Their SpecialValue is the HOME side's handicap, one number for a
        # two-sided bet. AH_2_-0.5 is the AWAY team giving half a goal, which
        # is their "0.5 : 0" - so the sign flips. Reading it the obvious way
        # books the other team.
        self.assertEqual(betking.market_for("AH_1_-0.5"), (305, -0.5, 1714))
        self.assertEqual(betking.market_for("AH_2_-0.5"), (305, 0.5, 1715))

    def test_the_whole_ball_asian_lines_stay_unmapped(self):
        # 305 sells half-ball lines only. The whole-ball lines exist at 342 and
        # that is a DIFFERENT BET - three-way, draw its own outcome, no push -
        # so mapping AH_1_-1 onto it hands somebody a narrower bet than they
        # placed. The same shape as an exact-goals top rung meaning "N or more"
        # on one book and "exactly N" on the other.
        for code in ("AH_1_-1", "AH_1_-2", "AH_1_-0.25", "AH_1_0.75"):
            self.assertIsNone(betking.market_for(code), code)

    def test_double_chance_ids_are_the_ones_read_not_the_ones_assumed(self):
        # 9/10/11 happen to run in name order here. SportyBet's double chance
        # uses the same three ids with 10 meaning home-or-away, so agreeing is
        # a coincidence and this pins which one we verified.
        self.assertEqual(betking.MARKET_MAP["1X"], (146, 0.0, 9))
        self.assertEqual(betking.MARKET_MAP["12"], (146, 0.0, 10))
        self.assertEqual(betking.MARKET_MAP["X2"], (146, 0.0, 11))

    def test_team_totals_are_separate_markets_per_side(self):
        self.assertEqual(betking.MARKET_MAP["HOME_OVER_0.5"][0], 10283)
        self.assertEqual(betking.MARKET_MAP["AWAY_OVER_0.5"][0], 10284)

    def test_every_over_has_its_under(self):
        # Without both sides there is nothing to de-vig with, which is the
        # whole reason the unders are in the table at all.
        for code in betking.MARKET_MAP:
            if code.startswith(("OVER_", "HOME_OVER", "AWAY_OVER", "FH_OVER")):
                self.assertIn(code.replace("OVER", "UNDER"),
                              betking.MARKET_MAP, code)
        self.assertIn("NG", betking.MARKET_MAP)

    def test_an_unknown_code_is_refused_not_defaulted(self):
        # There is no safe default market. Booking an unmapped code as a home
        # win returns a valid code for a bet nobody asked for.
        for code in ("CORNERS_OVER_9.5", "", None, "1X2"):
            self.assertIsNone(betking.market_for(code), code)


class ParsingTheFeed(unittest.TestCase):

    def test_a_market_is_read_by_type_line_and_outcome(self):
        rows, listed = self._day(_payload([ONE_X_TWO]))
        row = rows["1005309147"]
        self.assertEqual(row["odds"]["1"], 2.35)
        self.assertEqual(row["odds"]["X"], 3.45)
        self.assertEqual(row["odds"]["2"], 2.97)
        self.assertEqual(listed, 1)

    def test_the_line_is_part_of_the_market_not_decoration(self):
        # 160 alone is "Total Goals" and says nothing about which rung. Two
        # collections differing only in SpecialBetValue must not collide.
        rows, _ = self._day(_payload([
            _collection(160, 1.5, [(1, 12, "Over", 9, 1.21),
                                   (2, 13, "Under", 10, 4.30)], name="TG 1.5"),
            _collection(160, 2.5, [(3, 12, "Over", 11, 1.71),
                                   (4, 13, "Under", 12, 2.15)], name="TG 2.5"),
        ]))
        odds = rows["1005309147"]["odds"]
        self.assertEqual(odds["OVER_1.5"], 1.21)
        self.assertEqual(odds["OVER_2.5"], 1.71)
        self.assertEqual(odds["UNDER_1.5"], 4.30)

    def test_a_suspended_price_is_not_a_price(self):
        # They leave a closed market on the board with the price collapsed to
        # 1 rather than removing it, so "the code is present" is not the same
        # as "the market is on sale". It would pay nothing either way.
        rows, _ = self._day(_payload([_collection(110, 0, [
            (1, 4, "1", 9, 1.0), (2, 2, "X", 10, 3.45), (3, 5, "2", 11, 2.97),
        ])]))
        odds = rows["1005309147"]["odds"]
        self.assertNotIn("1", odds)
        self.assertIn("X", odds)

    def test_a_fixture_we_can_price_nothing_on_is_dropped(self):
        rows, _ = self._day(_payload([_collection(
            99999, 0, [(1, 1, "?", 9, 2.0)], name="Something else")]))
        self.assertEqual(rows, {})

    def test_the_same_match_arriving_many_times_is_merged(self):
        # Their per-event endpoint repeats the match once per market group -
        # 183 copies for a deep fixture - so taking the first copy loses
        # almost everything.
        payload = _payload([ONE_X_TWO])
        payload["AreaMatches"].append({"Items": [dict(
            payload["AreaMatches"][0]["Items"][0],
            OddsCollection=[_collection(302, 0, [
                (5, 74, "GG", 20, 1.56), (6, 76, "NG", 21, 2.40)], name="GG/NG")],
        )]})
        rows, _ = self._day(payload)
        odds = rows["1005309147"]["odds"]
        self.assertEqual(sorted(odds), ["1", "2", "GG", "NG", "X"])

    @staticmethod
    def _day(payload):
        with mock.patch.object(betking, "_get_json", return_value=payload):
            return betking.fetch_day("2026-09-14")


class TheSelectionId(unittest.TestCase):
    """The mistake that costs an evening, pinned.

    Booking with Outcome.OutcomeID is ACCEPTED and returns an ordinary code.
    Reading that code back gives an empty coupon. Their own bundle settles it:
    `oddId: b.MatchOddsID`.
    """

    def test_a_leg_carries_MatchOddsID_and_not_the_OutcomeID(self):
        event = self._event()
        leg = betking.build_selection(event, "1")
        self.assertEqual(leg["SelectionId"], 2339553331)
        self.assertNotEqual(leg["SelectionId"], 13222310353)

    def test_a_leg_carries_the_collection_id_as_its_market(self):
        leg = betking.build_selection(self._event(), "X")
        self.assertEqual(leg["MarketId"], 732487597)
        self.assertEqual(leg["MarketTypeId"], 110)

    def test_a_leg_carries_the_fixture_it_belongs_to(self):
        leg = betking.build_selection(self._event(), "1")
        self.assertEqual(leg["MatchId"], 1005309147)
        self.assertEqual(leg["ProviderEventId"], "72221262")
        self.assertEqual(leg["MatchName"], "Leeds - Newcastle")

    def test_a_code_the_fixture_does_not_price_raises(self):
        with self.assertRaises(KeyError):
            betking.build_selection(self._event(), "GG")

    def test_a_shallow_event_cannot_build_a_leg(self):
        # The day feed has no event-level fields, so a leg built from it would
        # be missing half the coupon. Refuse rather than send a hollow one.
        event = self._event()
        del event["event"]
        with self.assertRaises(KeyError):
            betking.build_selection(event, "1")

    @staticmethod
    def _event():
        with mock.patch.object(betking, "_get_json",
                               return_value=_payload([ONE_X_TWO])):
            return betking.fetch_event(1005309147)


class BookingGuards(unittest.TestCase):

    def setUp(self):
        with mock.patch.object(betking, "_get_json",
                               return_value=_payload([ONE_X_TWO])):
            self.event = betking.fetch_event(1005309147)
        self.picks = [{"event": self.event, "code": "1"}]

    def _book(self, booked, read_legs):
        """Stub the POST and the read-back, leave everything else real."""
        post = mock.Mock(return_value=mock.Mock(json=lambda: booked))
        with mock.patch.object(betking.requests, "post", post), \
             mock.patch.object(betking, "global_variables", return_value={}), \
             mock.patch.object(betking, "read_code",
                               return_value=(len(read_legs), read_legs)):
            return betking.generate_code(self.picks), post

    def test_a_booked_code_that_reads_back_whole_is_returned(self):
        out, _ = self._book({"ResponseStatus": 1, "BookedCouponCode": "PM18D2"},
                            [{"MatchName": "Leeds - Newcastle"}])
        self.assertEqual(out["code"], "PM18D2")
        self.assertEqual(out["legs"], 1)
        self.assertTrue(out["verified"])

    def test_an_empty_code_is_a_failure_even_though_they_said_success(self):
        # THE ONE THEY WILL NOT TELL US ABOUT. A selection id they do not
        # recognise is accepted, the response is an ordinary success, and the
        # punter holds a code containing nothing. Without the read-back this
        # ships as a working feature.
        out, _ = self._book({"ResponseStatus": 1, "BookedCouponCode": "QP1GZZ"},
                            [])
        self.assertIn("error", out)
        self.assertIn("empty", out["error"])
        self.assertNotEqual(out.get("verified"), True)

    def test_their_own_refusal_is_reported_as_one(self):
        out, _ = self._book({"ResponseStatus": 21, "BookedCouponCode": None},
                            [])
        self.assertIn("error", out)
        self.assertNotIn("code", out)

    def test_a_read_back_that_cannot_run_returns_the_code_unverified(self):
        # Not being able to check is not the same as having checked and found
        # it empty, and calling it a failure would refuse good codes whenever
        # their read endpoint hiccups.
        post = mock.Mock(return_value=mock.Mock(
            json=lambda: {"ResponseStatus": 1, "BookedCouponCode": "PM18D2"}))
        with mock.patch.object(betking.requests, "post", post), \
             mock.patch.object(betking, "global_variables", return_value={}), \
             mock.patch.object(betking, "read_code",
                               side_effect=RuntimeError("timeout")):
            out = betking.generate_code(self.picks)
        self.assertEqual(out["code"], "PM18D2")
        self.assertFalse(out["verified"])

    def test_the_slip_cap_is_forty_not_fifty(self):
        # Their own global variables say MaxNoOfSelections 40. Both other books
        # take 50, so a slip that books elsewhere is refused here - and it is
        # refused BEFORE the request, since minting a code that will not open
        # is the failure worth avoiding.
        picks = self.picks * (betking.BETSLIP_MAX + 1)
        with mock.patch.object(betking.requests, "post") as post:
            out = betking.generate_code(picks)
        self.assertIn("error", out)
        self.assertIn("40", out["error"])
        post.assert_not_called()

    def test_two_legs_on_one_game_are_refused_before_the_request(self):
        """BetKing takes ONE selection per match on a multiple.

        Their client only combines two selections from one event when the
        first's CompatibleMarkets names the second's market, and that list is
        empty on every collection of every event checked. Send the pair anyway
        and the coupon is accepted, a code comes back, and it holds nothing -
        the same silent shape as an unknown selection id. Measured 14 Sep on
        one fixture: four legs on four games booked (DT166R), the team-goals
        leg alone booked (N827WM), the two together came back empty (8J2DDW).
        """
        picks = [{"event": self.event, "code": "1"},
                 {"event": self.event, "code": "X"}]
        with mock.patch.object(betking.requests, "post") as post:
            out = betking.generate_code(picks)
        self.assertIn("error", out)
        self.assertIn("one game", out["error"])
        post.assert_not_called()

    def test_two_legs_on_DIFFERENT_games_are_fine(self):
        other = dict(self.event, event=dict(self.event["event"], MatchId=999))
        out, _ = self._book({"ResponseStatus": 1, "BookedCouponCode": "DT166R"},
                            [{}, {}])
        self.assertEqual(out["code"], "DT166R")
        # And the same through the real guard rather than the stubbed book:
        # two different MatchIds must reach the POST, where the same-game pair
        # above never does.
        with mock.patch.object(betking.requests, "post") as post,              mock.patch.object(betking, "global_variables", return_value={}),              mock.patch.object(betking, "read_code", return_value=(2, [{}, {}])):
            post.return_value.json.return_value = {
                "ResponseStatus": 1, "BookedCouponCode": "DT166R"}
            betking.generate_code([{"event": self.event, "code": "1"},
                                   {"event": other, "code": "1"}])
        post.assert_called_once()

    def test_nothing_is_sent_for_an_empty_slip(self):
        with mock.patch.object(betking.requests, "post") as post:
            self.assertIn("error", betking.generate_code([]))
        post.assert_not_called()


class ReadingACodeBack(unittest.TestCase):
    """The converter's half. Their coupon rows carry the market as the SAME
    triple the feed does, so the decode is the derived reverse table rather
    than a second map that could drift from the forward one."""

    ROWS = [{
        "MatchId": 1005309147, "MatchName": "Leeds - Newcastle",
        "MarketTypeId": 160, "Spread": 2.5, "IDSelectionType": 12,
        "MarketName": "Total Goals 2.5", "SelectionName": "Over (2.50)",
        "OddValue": 1.71, "TournamentName": "Premier League",
        "EventDate": "2026-09-14T21:00:00+02:00",
    }]

    def _read(self, rows, available=0, body=None):
        """Stub the RESPONSE, not read_code.

        These stubbed read_code until read_coupon stopped going through it, at
        which point every one of them quietly started hitting the live API and
        asserting against whatever real code FR2D84 happened to be that
        afternoon. They failed loudly, which is the only reason it was noticed;
        a seam moved under a mock is otherwise invisible.
        """
        payload = dict(body or {})
        payload.setdefault("AvailableEventCount", available)
        payload.setdefault("BookedCoupon", {"Odds": rows})
        with mock.patch.object(betking, "_read_body", return_value=payload):
            return betking.read_coupon("FR2D84")

    def test_a_leg_comes_back_in_our_own_market_code(self):
        out = self._read(self.ROWS)
        leg = out["legs"][0]
        self.assertEqual(leg["prediction"], "OVER_2.5")
        self.assertEqual(leg["home"], "Leeds")
        self.assertEqual(leg["away"], "Newcastle")
        self.assertEqual(leg["odds"], 1.71)
        self.assertEqual(leg["eventId"], 1005309147)

    def test_the_line_is_read_from_Spread_not_assumed(self):
        # 160 alone is "Total Goals" and says nothing about which rung. Their
        # coupon carries it in Spread; dropping it decodes every total as the
        # same bet.
        rows = [dict(self.ROWS[0], Spread=3.5)]
        self.assertEqual(self._read(rows)["legs"][0]["prediction"], "OVER_3.5")

    def test_a_market_we_do_not_map_is_named_not_guessed(self):
        # None is the contract bet9ja.read_coupon answers with, and the
        # converter shows it as "a market we don't carry". Their own words are
        # kept so the leg is still nameable on screen.
        rows = [dict(self.ROWS[0], MarketTypeId=99999,
                     MarketName="Corner Handicap", SelectionName="Home -2")]
        leg = self._read(rows)["legs"][0]
        self.assertIsNone(leg["prediction"])
        self.assertIn("Corner Handicap", leg["raw"])

    def test_a_code_with_nothing_in_it_is_not_found(self):
        self.assertEqual(self._read([]).get("notFound"), True)

    def test_a_thinned_coupon_says_what_it_lost(self):
        """A leg leaves the coupon the moment its fixture starts, so a code
        pasted in the afternoon is shorter than the one the punter was handed.
        Measured on DT166R: booked with four legs, read back with one four
        hours later, and their answer named the two that had gone.

        Showing the remainder silently reads as "your code only had one game
        in it", which is a different and worse claim."""
        out = self._read(self.ROWS, body={
            "OriginalEventCount": 4,
            "RemovedEvents": ["Instituto AC Cordoba - Estudiantes Rio Cuarto",
                              None, "CD Universidad Catolica - Orense SC"],
        })
        self.assertEqual(out["booked"], 4)
        # The null is theirs: a dropped leg they will not even name. Carrying
        # it through would print an empty line at the reader.
        self.assertEqual(len(out["removed"]), 2)
        self.assertIn("Instituto AC Cordoba - Estudiantes Rio Cuarto", out["removed"])

    def test_a_whole_coupon_reports_nothing_removed(self):
        out = self._read(self.ROWS)
        self.assertEqual(out["removed"], [])

    def test_available_is_not_the_leg_count(self):
        # A four-leg coupon reads back with available=0 while its Odds array
        # holds all four. Using it as a count would report every code empty.
        out = self._read(self.ROWS, available=0)
        self.assertEqual(len(out["legs"]), 1)
        self.assertEqual(out["available"], 0)

    def test_the_decode_is_the_derived_table(self):
        # Not a second literal. A hand-typed reverse map is a thing to keep in
        # step, and the pair drifts silently.
        for code, triple in betking.MARKET_MAP.items():
            self.assertEqual(betking._BY_TRIPLE[triple], code)

    def test_an_alias_decodes_to_the_modelled_name(self):
        """Two names for one bet, and the reader gets the clearer one.

        EXGOALS_FH_0 is "not exactly 0 goals in the first half"; FH_OVER_0.5 is
        "goal in 1st half". Same winners, same losers. Both map to (161,0.5,12)
        and the reverse index has to pick, so it picks the modelled one."""
        self.assertEqual(betking.market_for("EXGOALS_FH_0"),
                         betking.market_for("FH_OVER_0.5"))
        self.assertEqual(betking._BY_TRIPLE[(161, 0.5, 12)], "FH_OVER_0.5")


class TheAsymmetryIsAccountedFor(unittest.TestCase):
    """Every code we can say, this book either carries or has a reason not to.

    "A code in one table and not the other is a leg that reads and splits and
    can never convert." Keeping that difference as prose meant a code quietly
    added to SportyBet's table looked exactly like one deliberately left off
    BetKing's. As a test it cannot: adding a market anywhere in our vocabulary
    now forces a decision here.

    It earned itself immediately - it found 36 codes across five half-scoped
    families (FH_EH_, SH_EH_, FH_MIX_, FH_CARD_, FH_CARDUN_) that no generator
    rule proposed and no comment covered.
    """

    @staticmethod
    def _vocabulary():
        import server, bet9ja
        return sorted(set(server.PASSTHROUGH_MAP) | set(bet9ja.PASSTHROUGH_MAP) |
                      set(server.MARKET_MAP) | set(bet9ja.MARKET_MAP))

    def test_nothing_is_silently_missing(self):
        orphans = [c for c in self._vocabulary()
                   if not betking.market_for(c) and not betking.reason_uncarried(c)]
        self.assertEqual(orphans, [],
                         "carried by another book, not carried here, and no "
                         "reason given - say which it is in NOT_CARRIED")

    def test_a_carried_code_never_also_claims_a_reason(self):
        # A code cannot both convert and have an excuse for not converting.
        for code in list(betking.MARKET_MAP) + list(betking.PASSTHROUGH_MAP):
            self.assertIsNone(betking.reason_uncarried(code), code)

    def test_the_longest_prefix_wins(self):
        # FH_AH_ must beat AH_ and FH_, or a half-handicap inherits the match
        # handicap's reason and says something untrue about their catalogue.
        self.assertIn("344", betking.reason_uncarried("FH_AH_1_-1"))
        self.assertIn("305", betking.reason_uncarried("AH_1_-1"))

    def test_absent_and_unread_are_not_the_same_word(self):
        # The distinction this file exists to protect: "verified absent" is a
        # fact about their catalogue, "not read yet" is work we have not done.
        for prefix, why in betking.NOT_CARRIED.items():
            self.assertTrue(
                why.startswith(("verified absent", "not read yet", "carried")),
                "%s: a reason has to say which of the two it is" % prefix)

    def test_the_whole_ball_asian_reason_names_the_trap(self):
        # The one entry where the tempting fix ships a different bet. If this
        # wording ever goes, so has the argument against mapping it.
        why = betking.reason_uncarried("AH_1_-1")
        self.assertIn("342", why)
        self.assertIn("DIFFERENT bet", why)


class TheSweep(unittest.TestCase):

    def test_a_day_reports_what_they_say_they_hold(self):
        # The outside opinion. Without it a throttled sweep and a thin Tuesday
        # look identical, and storing the first quietly shrinks the board.
        rows, listed = ParsingTheFeed._day(_payload([ONE_X_TWO]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(listed, 1)

    def test_the_sweep_reaches_the_weekend(self):
        """Three days would hold none of the weekend until the Thursday.

        The board reaches about nine days out and its two biggest days by far
        are Saturday and Sunday - 146 and 102 fixtures on one measured board
        against 8 to 37 on a weekday - so a short sweep makes BetKing look
        like a book that carries nothing, exactly when slips get built.
        """
        import inspect
        self.assertGreaterEqual(
            inspect.signature(betking.all_fixtures).parameters["days"].default, 8)

    def test_a_failed_day_is_named_rather_than_counted_as_empty(self):
        with mock.patch.object(betking, "_get_json",
                               side_effect=RuntimeError("blocked")), \
             mock.patch.object(betking.time, "sleep"):
            fixtures, stats = betking.all_fixtures(days=2)
        self.assertEqual(fixtures, {})
        self.assertEqual(len(stats["failed"]), 2)
        self.assertEqual(stats["listed"], 0)


class TheContainerCanActuallyRunThis(unittest.TestCase):
    """An import that only resolves as somebody else's dependency took the
    service down once. Same check as bet9ja's."""

    def test_every_import_is_declared(self):
        with open("betking.py", encoding="utf-8") as fh:
            src = fh.read()
        with open("requirements.txt", encoding="utf-8") as fh:
            declared = fh.read().lower()
        for name in ("curl_cffi",):
            if "import %s" % name in src or "from %s" % name in src:
                self.assertIn(name, declared, name)


if __name__ == "__main__":
    unittest.main()
