"""MSport, the seventh book (22 Oct 2026). Offline: every answer of theirs is faked."""
import unittest
from unittest import mock

import server  # noqa: F401 - msport reads its market table from server at call time
import msport


def _event(code_odds, eid="sr:match:74165976"):
    row = msport._row({"eventId": eid, "homeTeam": "Shakhtar Donetsk", "awayTeam": "Sporting",
                       "startTime": 1793727900000, "tournament": "UEFA Champions League"})
    for code, odd in code_odds.items():
        m = msport.market_for(code)
        row["odds"][code] = odd
        row["sel"][code] = {"marketId": int(m["marketId"]), "specifier": m.get("specifier") or "",
                            "outcomeId": str(m["outcomeId"]), "odds": odd}
    return row


def _readback(legs):
    """Their read-back answer for [(eventId, marketId, specifier, outcomeId)]."""
    return {"bizCode": 10000, "data": {"originalSelectionCount": len(legs), "bettableBetSlip": [
        {"event": {"eventId": e, "homeTeam": "H", "awayTeam": "A", "startTime": 1793727900000},
         "market": {"id": m, "specifiers": s, "description": "x"},
         "outcome": {"id": o, "description": "y", "odds": "1.50"}} for e, m, s, o in legs]}}


class TheMarketsAreSportyBets(unittest.TestCase):
    def test_every_modelled_code_is_carried(self):
        # Their deepest cards priced every code the board models (22 Oct 2026).
        for code in server.MARKET_MAP:
            self.assertIsNotNone(msport.market_for(code), code)

    def test_what_they_do_not_sell_is_refused_with_a_reason(self):
        self.assertTrue(msport.NOT_CARRIED)
        code = next(iter(msport.NOT_CARRIED))
        self.assertIsNone(msport.market_for(code))
        self.assertTrue(msport.reason_uncarried(code))

    def test_a_selection_is_their_four_fields(self):
        sel = msport.build_selection(_event({"OVER_2.5": 1.6}), "OVER_2.5")
        self.assertEqual(sel, {"eventId": "sr:match:74165976", "marketId": 18,
                               "specifier": "total=2.5", "outcomeId": "12"})


class ReadingACode(unittest.TestCase):
    def test_legs_come_back_in_our_words_with_sportradar_ids(self):
        body = _readback([("sr:match:1", 1, "", "1"), ("sr:match:2", 18, "total=2.5", "12")])
        with mock.patch.object(msport, "_call", return_value=body):
            got = msport.read_coupon("B2YSC72")
        self.assertEqual([l["prediction"] for l in got["legs"]], ["1", "OVER_2.5"])
        self.assertEqual([l["srId"] for l in got["legs"]], ["1", "2"])

    def test_a_code_that_does_not_exist_is_not_found(self):
        with mock.patch.object(msport, "_call", return_value={"bizCode": 19000, "message": "The code is invalid."}):
            self.assertTrue(msport.read_coupon("ZZZZZZ9").get("notFound"))


class MintingReadsEveryLegBack(unittest.TestCase):
    def test_a_code_holding_every_leg_is_verified(self):
        ev = _event({"1X": 1.3, "OVER_1.5": 1.2})
        back = _readback([("sr:match:74165976", 10, "", "9"), ("sr:match:74165976", 18, "total=1.5", "12")])
        with mock.patch.object(msport, "_call", side_effect=[{"bizCode": 10000, "data": {"shareCode": "B2YTV99"}}, back]):
            out = msport.generate_code([{"event": ev, "code": "1X"}, {"event": ev, "code": "OVER_1.5"}])
        self.assertEqual(out.get("code"), "B2YTV99")
        self.assertTrue(out.get("verified"))

    def test_a_leg_missing_from_the_code_is_named(self):
        ev = _event({"1X": 1.3, "OVER_1.5": 1.2})
        back = _readback([("sr:match:74165976", 10, "", "9")])          # over 1.5 vanished
        with mock.patch.object(msport, "_call", side_effect=[{"bizCode": 10000, "data": {"shareCode": "B2YTV99"}}, back]):
            out = msport.generate_code([{"event": ev, "code": "1X"}, {"event": ev, "code": "OVER_1.5"}])
        self.assertIn("error", out)
        self.assertEqual(out["not_priced"], [{"eventId": "sr:match:74165976", "prediction": "OVER_1.5"}])


class SameGameIsRefusedBeforeMSport(unittest.TestCase):
    """MSport mints two legs of one game, then its betslip will not place them as one
    multiple ("contingent outcomes", doubles only - owner's phone, B2YSMQJ, 22 Oct 2026)."""
    def test_the_second_leg_on_a_game_is_named_and_nothing_is_booked(self):
        ev = _event({"OVER_2.5": 1.43, "GG": 1.41})
        client = server.app.test_client()
        with mock.patch.object(msport, "fetch_event", return_value=ev), \
             mock.patch.object(msport, "generate_code") as mint:
            r = client.post("/api/msport/booking-code", json={"selections": [
                {"eventId": "sr:match:74165976", "code": "OVER_2.5"},
                {"eventId": "sr:match:74165976", "code": "GG"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual([(u["prediction"], u["reason"]) for u in r.get_json()["unbookable"]], [("GG", "same_game")])
        mint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
