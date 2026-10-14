"""Tests for betway.py - the id arithmetic, the read-back, the session, and the
guards around booking. Stdlib unittest, no network.

Run:  python -m unittest test_betway -v
"""
import base64
import json
import time
import unittest
from unittest import mock

import betway

# Ids and shapes read off live cards, 14 Oct 2026.
EID = "73835276"


class TheIds(unittest.TestCase):
    """Their ids are Sportradar's glued together with no separator."""

    def test_an_outcome_id_is_built_the_way_their_card_writes_it(self):
        self.assertEqual(betway.outcome_id(EID, betway.MARKET_MAP["1"]), "7383527611")
        self.assertEqual(betway.outcome_id(EID, betway.MARKET_MAP["OVER_2.5"]),
                         "7383527618total=2.5~12")
        self.assertEqual(betway.outcome_id(EID, ("105", "from=1|to=10", "1")),
                         "73835276105from=1~to=10~1")

    def test_the_specifier_is_read_from_the_outcome_not_the_market(self):
        # [Total Goals]: every line's outcome hangs off the bare "{eid}18".
        # Reading the specifier off the market id dropped every match total.
        self.assertEqual(betway.decode(EID, EID + "18", EID + "18total=2.5~12"),
                         ("18", "total=2.5", "12"))
        self.assertEqual(betway.decode(EID, EID + "19total=0.5~", EID + "19total=0.5~13"),
                         ("19", "total=0.5", "13"))

    def test_the_market_id_says_where_the_market_number_ends(self):
        # "{eid}1" + "1" and "{eid}11" + "" are the same string; only the
        # market id tells them apart.
        self.assertEqual(betway.decode(EID, EID + "1", EID + "11"), ("1", None, "1"))
        self.assertEqual(betway.decode(EID, EID + "10", EID + "1011"), ("10", None, "11"))

    def test_every_carried_code_round_trips(self):
        # Both market-id forms they use: bare "{eid}18" and "{eid}19total=0.5~".
        for code, triple in list(betway.MARKET_MAP.items()) + list(betway.PASSTHROUGH_MAP.items()):
            oid = betway.outcome_id(EID, triple)
            for mid in (EID + triple[0], betway.market_id(EID, triple)):
                self.assertEqual(betway._BY_TRIPLE[betway.decode(EID, mid, oid)], code)

    def test_the_aliases_read_as_betway_writes_them(self):
        # 1UP is Sportradar's 1601 xup=1 on Betway (SportyBet's own id is
        # 60200); a variant outcome is the bare number, not sr:...:68.
        self.assertEqual(betway.outcome_id(EID, betway.PASSTHROUGH_MAP["UP1_1"]),
                         EID + "1601xup=1~1")
        self.assertEqual(betway.outcome_id(EID, betway.PASSTHROUGH_MAP["EXACT_0"]),
                         EID + "21variant=sr:exact_goals:6+~68")
        self.assertEqual(betway.PASSTHROUGH_MAP["CORNERS_H_OV_4.5"][0], "167")
        self.assertEqual(betway.PASSTHROUGH_MAP["CORNERS_A_OV_4.5"][0], "168")

    def test_a_foreign_id_decodes_to_nothing(self):
        self.assertIsNone(betway.decode(EID, "99999991", "999999911"))


class MarketTable(unittest.TestCase):

    def test_the_numbers_are_sportybets_numbers(self):
        # Same Sportradar ids: if the two ever disagree, one of them books
        # a different bet under the same name.
        import server
        for code, (m, spec, o) in betway.MARKET_MAP.items():
            sp = server.MARKET_MAP[code]
            self.assertEqual((m, spec, o),
                             (sp["marketId"], sp.get("specifier"), sp["outcomeId"]), code)

    def test_every_modelled_market_sportybet_carries_is_carried_here(self):
        import server
        self.assertEqual(sorted(server.MARKET_MAP), sorted(betway.MARKET_MAP))

    def test_an_unknown_market_refuses_rather_than_defaulting(self):
        self.assertIsNone(betway.market_for("NOPE"))
        self.assertIsNone(betway.market_for("AH_1_-0.5"))

    def test_nothing_is_silently_missing(self):
        import bet9ja
        import betking
        import server
        vocab = (set(server.PASSTHROUGH_MAP) | set(bet9ja.PASSTHROUGH_MAP) |
                 set(betking.PASSTHROUGH_MAP) | set(server.MARKET_MAP))
        orphans = [c for c in vocab
                   if not betway.market_for(c) and not betway.reason_uncarried(c)]
        self.assertEqual(orphans, [])

    def test_a_carried_code_never_also_claims_a_reason(self):
        for code in list(betway.MARKET_MAP) + list(betway.PASSTHROUGH_MAP):
            self.assertIsNone(betway.reason_uncarried(code), code)


def _resp(status, body):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    return r


def _coupon(eid, oid, mid, price=1.5):
    return {"outcomeId": oid, "marketId": mid, "marketName": "1X2",
            "price": {"priceDecimal": price},
            "outcome": {"outcomeId": oid, "eventId": int(eid), "displayName": "Home"},
            "sportEvent": {"eventId": int(eid), "homeTeam": "A", "awayTeam": "B",
                           "league": "L", "expectedStartEpoch": 1791972000}}


class TheReadBack(unittest.TestCase):

    def test_a_leg_decodes_into_our_vocabulary_with_its_sportradar_id(self):
        body = {"selections": [_coupon(EID, EID + "18total=2.5~12", EID + "18", 1.9)]}
        with mock.patch.object(betway.requests, "post", return_value=_resp(200, body)):
            got = betway.read_coupon("BWXXXX")
        leg = got["legs"][0]
        self.assertEqual(leg["prediction"], "OVER_2.5")
        self.assertEqual(leg["srId"], EID)
        self.assertEqual((leg["home"], leg["away"], leg["odds"]), ("A", "B", 1.9))
        self.assertEqual(leg["kickoff"], "2026-10-14T10:00:00Z")

    def test_their_misnamed_not_found_is_not_found(self):
        # 400 6000359 "BookABetLimitExceeded" is what a code that never
        # existed answers; the real code read straight after answers 200.
        body = {"errorCode": 6000359, "errorMessage": "BookABetLimitExceeded"}
        with mock.patch.object(betway.requests, "post", return_value=_resp(400, body)):
            self.assertTrue(betway.read_coupon("BW00000000").get("notFound"))

    def test_any_other_refusal_is_an_error_not_a_missing_code(self):
        with mock.patch.object(betway.requests, "post",
                               return_value=_resp(500, {"errorCode": 1})):
            got = betway.read_coupon("BWXXXX")
        self.assertFalse(got.get("notFound"))
        self.assertIn("error", got)


def _event(eid, *codes):
    row = betway._row({"eventId": eid, "homeTeam": "A", "awayTeam": "B",
                       "expectedStartEpoch": int(time.time()) + 9000})
    for c in codes:
        oid = betway.outcome_id(eid, betway.MARKET_MAP[c])
        betway._put(row, c, oid, eid + betway.MARKET_MAP[c][0], 1.5)
    return row


class Booking(unittest.TestCase):

    def setUp(self):
        self.ev1 = _event("11111111", "1")
        self.ev2 = _event("22222222", "OVER_2.5")
        self.sels = [{"event": self.ev1, "code": "1"},
                     {"event": self.ev2, "code": "OVER_2.5"}]
        self.live = {self.ev1["sel"]["1"]["outcomeId"]: (1.6, "111111111"),
                     self.ev2["sel"]["OVER_2.5"]["outcomeId"]: (1.8, "2222222218")}

    def _run(self, book_status=200, readback=None, live=None):
        posts = []

        def post(url, data=None, headers=None, **kw):
            posts.append((url, json.loads(data), headers))
            if url == betway.BOOK_URL:
                return _resp(book_status, {"bookingCode": "BWTEST"} if book_status == 200 else {})
            back = readback if readback is not None else [
                _coupon(s["event"]["eventId"], s["event"]["sel"][s["code"]]["outcomeId"], "x")
                for s in self.sels]
            return _resp(200, {"selections": back})

        with mock.patch.object(betway, "tokens", return_value=("jwt", "ref")), \
             mock.patch.object(betway, "fetch_prices",
                               return_value=self.live if live is None else live), \
             mock.patch.object(betway.requests, "post", side_effect=post):
            return betway.generate_code(self.sels), posts

    def test_the_payload_carries_the_live_price_and_their_market_id(self):
        out, posts = self._run()
        self.assertTrue(out["verified"])
        url, body, headers = posts[0]
        self.assertEqual(url, betway.BOOK_URL)
        self.assertEqual(headers["Authorization"], "Bearer jwt")
        self.assertEqual(headers["X-Brand-Id"], betway.BRAND_ID)
        legs = body["outcomes"]
        self.assertEqual([l["value"] for l in legs], [1.6, 1.8])
        self.assertEqual(legs[1]["marketId"], "2222222218")
        self.assertEqual(legs[0]["eventId"], 11111111)
        self.assertEqual(out["odds"], round(1.6 * 1.8, 2))

    def test_a_leg_missing_from_the_read_back_is_named(self):
        one = [_coupon("11111111", self.ev1["sel"]["1"]["outcomeId"], "x")]
        out, _ = self._run(readback=one)
        self.assertIn("1 of 2", out["error"])
        self.assertEqual(out["unbookable"][0]["prediction"], "OVER_2.5")

    def test_a_leg_they_no_longer_price_is_named_before_booking(self):
        live = {self.ev1["sel"]["1"]["outcomeId"]: (1.6, "111111111")}
        out, posts = self._run(live=live)
        self.assertEqual(posts, [])
        self.assertEqual(out["unbookable"][0]["prediction"], "OVER_2.5")

    def test_a_401_renews_once_and_retries(self):
        calls = []

        def post(url, data=None, headers=None, **kw):
            calls.append(url)
            if url == betway.BOOK_URL:
                return _resp(401 if calls.count(url) == 1 else 200,
                             {"bookingCode": "BWTEST"})
            return _resp(200, {"selections": [
                _coupon(s["event"]["eventId"], s["event"]["sel"][s["code"]]["outcomeId"], "x")
                for s in self.sels]})

        with mock.patch.object(betway, "tokens", return_value=("jwt", "ref")), \
             mock.patch.object(betway, "fetch_prices", return_value=self.live), \
             mock.patch.object(betway, "renew", return_value=True) as renew, \
             mock.patch.object(betway.requests, "post", side_effect=post):
            out = betway.generate_code(self.sels)
        renew.assert_called_once()
        self.assertEqual(out["code"], "BWTEST")

    def test_an_empty_slip_and_an_oversized_one_both_refuse(self):
        self.assertIn("error", betway.generate_code([]))
        big = [{"event": self.ev1, "code": "1"}] * (betway.BETSLIP_MAX + 1)
        self.assertIn("capped", betway.generate_code(big)["error"])


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "h." + payload + ".s"


class TheSession(unittest.TestCase):

    def test_expiry_is_read_from_the_jwt(self):
        self.assertEqual(betway.token_expiry(_jwt(1792000000)), 1792000000)
        self.assertIsNone(betway.token_expiry("not-a-jwt"))

    def test_keep_alive_renews_only_inside_the_last_day(self):
        with mock.patch.object(betway, "tokens",
                               return_value=(_jwt(int(time.time()) + 3 * 86400), "r")), \
             mock.patch.object(betway, "renew") as renew:
            self.assertTrue(betway.keep_alive()["ok"])
        renew.assert_not_called()
        with mock.patch.object(betway, "tokens",
                               return_value=(_jwt(int(time.time()) + 3600), "r")), \
             mock.patch.object(betway, "renew") as renew:
            betway.keep_alive()
        renew.assert_called_once()

    def test_the_state_never_carries_a_token(self):
        secret = _jwt(int(time.time()) + 86400 * 3)
        with mock.patch.object(betway, "tokens", return_value=(secret, "refresh-secret")):
            st = json.dumps(betway.session_state())
        self.assertNotIn(secret, st)
        self.assertNotIn("refresh-secret", st)

    def test_a_renewal_stores_what_they_return(self):
        stored = {}
        with mock.patch.object(betway, "tokens", return_value=("old", "ref")), \
             mock.patch.object(betway, "_set", side_effect=stored.__setitem__), \
             mock.patch.object(betway.requests, "post",
                               return_value=_resp(200, {"access_token": "new",
                                                        "refresh_token": "ref2"})):
            self.assertTrue(betway.renew())
        self.assertEqual(stored, {betway._AUTH_KEY: "new", betway._REFRESH_KEY: "ref2"})

    def test_a_refused_renewal_stores_nothing(self):
        with mock.patch.object(betway, "tokens", return_value=("old", "ref")), \
             mock.patch.object(betway, "_set") as put, \
             mock.patch.object(betway.requests, "post", return_value=_resp(401, {})):
            self.assertFalse(betway.renew())
        put.assert_not_called()


class TheSweep(unittest.TestCase):

    def test_simulated_games_are_dropped(self):
        self.assertTrue(betway._is_fake({"regionId": "esoccer"}))
        self.assertTrue(betway._is_fake({"name": "Poland SRL vs. Wales SRL"}))
        self.assertFalse(betway._is_fake({"name": "Vissel Kobe vs. Jeonbuk FC",
                                          "regionId": "international-clubs"}))

    def test_every_outbound_call_impersonates(self):
        import re
        with open("betway.py", encoding="utf8") as fh:
            src = fh.read()
        calls = len(re.findall(r"\brequests\.(?:get|post)\(", src))
        self.assertEqual(calls, len(re.findall(r"\bimpersonate=IMPERSONATE\b", src)))


if __name__ == "__main__":
    unittest.main()
