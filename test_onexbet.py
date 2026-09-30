"""1xBet: the fifth book. Unit tests against recorded responses - no network."""
import json
import os
import unittest
from unittest import mock

import onexbet

HERE = os.path.dirname(os.path.abspath(__file__))
REC = json.load(open(os.path.join(HERE, "fixtures", "onexbet_card.json"), encoding="utf8"))


class ParamsAreStrings(unittest.TestCase):
    def test_half_line(self):
        self.assertEqual(onexbet._p(2.5), "2.5")

    def test_whole_line_loses_its_point(self):
        # 1xBet sends 1 and 1.0 for the same handicap; one key, not two.
        self.assertEqual(onexbet._p(1.0), "1")
        self.assertEqual(onexbet._p(-1), "-1")

    def test_absent(self):
        self.assertIsNone(onexbet._p(None))


class ParsingTheCard(unittest.TestCase):
    def setUp(self):
        self.row = onexbet._row(REC["card"])
        onexbet._absorb(self.row, REC["card"])

    def test_shape_matches_the_other_books(self):
        for k in ("eventId", "slotId", "teams", "kickoff", "league", "odds", "raw", "sel", "sub"):
            self.assertIn(k, self.row)
        self.assertIsNone(self.row["srId"])
        self.assertRegex(self.row["kickoff"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.row["teams"], REC["card"]["O1"] + " - " + REC["card"]["O2"])

    def test_home_win_is_priced_with_its_ids(self):
        self.assertIn("1", self.row["odds"])
        sel = self.row["sel"]["1"]
        self.assertEqual(sel["gameId"], REC["card"]["I"])
        self.assertEqual(sel["T"], 1)

    def test_subgames_are_kept_by_period(self):
        # Halves and corners book against their OWN game id (spec).
        self.assertIn("1st half", self.row["sub"])
        self.assertIn("Corners", self.row["sub"])
        self.assertNotEqual(self.row["sub"]["1st half"], REC["card"]["I"])

    def test_suspended_price_is_not_a_price(self):
        card = json.loads(json.dumps(REC["card"]))
        for g in card["GE"]:
            if g["G"] == 1:
                for r in g["E"]:
                    for e in r:
                        e["C"] = 1.0
        row = onexbet._row(card)
        onexbet._absorb(row, card)
        self.assertNotIn("1", row["odds"])


class VirtualCompetitions(unittest.TestCase):
    def test_refused(self):
        for name in ("England. Premier League. Alternative Matches", "FIFA. eSports Battle",
                     "SRL Premier League", "Virtual Football. League", "Cyber Football"):
            self.assertTrue(onexbet._is_virtual(name), name)

    def test_real_ones_kept(self):
        for name in ("England. Premier League", "UEFA Champions League", "Nigeria. NPFL"):
            self.assertFalse(onexbet._is_virtual(name), name)


class TheSweep(unittest.TestCase):
    def _fake(self, champs, games_by_champ, card):
        def get_json(path, attempts=3):
            if "GetChampsZip" in path:
                return {"Value": champs}
            if "GetChampZip" in path:
                li = int(path.split("champ=")[1].split("&")[0])
                return {"Value": {"G": games_by_champ[li]}}
            if "GetGameZip" in path:
                gid = int(path.split("id=")[1].split("&")[0])
                c = json.loads(json.dumps(card))
                c["I"] = gid
                return {"Value": c}
            raise AssertionError(path)
        return get_json

    def test_virtuals_never_fetched_and_nearest_first(self):
        champs = [{"LI": 1, "L": "England. Premier League"},
                  {"LI": 2, "L": "England. Premier League. Alternative Matches"}]
        base = REC["listing"]["S"]
        games = {1: [dict(REC["listing"], I=11, S=base + 86400), dict(REC["listing"], I=10, S=base)],
                 2: [dict(REC["listing"], I=99, S=base)]}
        seen = []
        fake = self._fake(champs, games, REC["card"])

        def spy(path, attempts=3):
            if "GetGameZip" in path:
                seen.append(int(path.split("id=")[1].split("&")[0]))
            return fake(path, attempts)
        with mock.patch.object(onexbet, "_get_json", spy), mock.patch("time.sleep"):
            out, stats = onexbet.all_fixtures(deadline_s=999)
        self.assertEqual(seen, [10, 11])          # nearest first, virtual never asked
        self.assertEqual(set(out), {"10", "11"})
        self.assertEqual(stats["skipped"], 0)

    def test_deadline_says_what_it_skipped(self):
        champs = [{"LI": 1, "L": "England. Premier League"}]
        base = REC["listing"]["S"]
        games = {1: [dict(REC["listing"], I=i, S=base + i) for i in range(5)]}
        clock = iter([0, 0, 1, 2, 10_000, 10_000, 10_000, 10_000, 10_000])
        with mock.patch.object(onexbet, "_get_json", self._fake(champs, games, REC["card"])), \
             mock.patch("time.sleep"), mock.patch("time.monotonic", lambda: next(clock)):
            out, stats = onexbet.all_fixtures(deadline_s=5)
        self.assertGreater(stats["skipped"], 0)
        self.assertEqual(stats["listed"], 5)
        self.assertEqual(len(out) + stats["skipped"], 5)


class MarketTable(unittest.TestCase):
    def test_modelled_codes_are_all_here(self):
        # The dozen the model prices plus their unders, same list as betpawa.MARKET_MAP.
        import betpawa
        self.assertEqual(set(onexbet.MARKET_MAP), set(betpawa.MARKET_MAP))

    def test_never_a_default(self):
        self.assertIsNone(onexbet.market_for("NOT_A_MARKET"))
        self.assertIsNone(onexbet.market_for(None))

    def test_their_ids_for_the_ones_that_bite(self):
        # 5 is 12 and 6 is 2X on their card - NOT the 1X/X2/12 order.
        self.assertEqual(onexbet.market_for("12"), ("", "5", None))
        self.assertEqual(onexbet.market_for("X2"), ("", "6", None))
        # First half is its own game, so its period is part of the key.
        self.assertEqual(onexbet.market_for("FH_OVER_0.5"), ("1st half", "9", "0.5"))
        self.assertEqual(onexbet.market_for("AWAY_OVER_1.5"), ("", "13", "1.5"))

    def test_reverse_is_derived(self):
        for code, key in onexbet.MARKET_MAP.items():
            self.assertEqual(onexbet.code_for(*key), code)

    def test_one_key_one_code(self):
        keys = [onexbet._key(*v) for v in list(onexbet.MARKET_MAP.values()) +
                list(onexbet.PASSTHROUGH_MAP.values())]
        self.assertEqual(len(keys), len(set(keys)))

    def test_card_prices_the_modelled_set(self):
        row = onexbet._row(REC["card"])
        onexbet._absorb(row, REC["card"])
        for code in ("1", "X", "2", "1X", "12", "X2", "OVER_2.5", "UNDER_2.5", "GG", "NG",
                     "HOME_OVER_0.5", "AWAY_OVER_0.5"):
            self.assertIn(code, row["odds"], code)
        half = onexbet._row(REC["card"])
        onexbet._absorb(half, REC["half"], period="1st half")
        self.assertIn("FH_OVER_0.5", half["odds"])
        self.assertEqual(half["sel"]["FH_OVER_0.5"]["gameId"], REC["half"]["I"])


COUPON = json.load(open(os.path.join(HERE, "fixtures", "onexbet_coupon.json"), encoding="utf8"))


def _event():
    row = onexbet._row(REC["card"])
    onexbet._absorb(row, REC["card"])
    onexbet._absorb(row, REC["half"], period="1st half")
    return row


class BuildingTheSlip(unittest.TestCase):
    def test_full_time_leg_uses_the_main_game(self):
        sel = onexbet.build_selection(_event(), "1")
        self.assertEqual(sel["GameId"], REC["card"]["I"])
        self.assertEqual((sel["Type"], sel["Kind"]), (1, 3))

    def test_first_half_leg_uses_the_half_game(self):
        sel = onexbet.build_selection(_event(), "FH_OVER_0.5")
        self.assertEqual(sel["GameId"], REC["half"]["I"])
        self.assertEqual((sel["Type"], sel["Param"]), (9, 0.5))

    def test_unpriced_leg_is_a_key_error(self):
        with self.assertRaises(KeyError):
            onexbet.build_selection(_event(), "OVER_9.5")


class TheReadBack(unittest.TestCase):
    def test_decodes_both_legs_to_our_codes(self):
        with mock.patch.object(onexbet, "_post", return_value=COUPON["read"]):
            got = onexbet.read_coupon(COUPON["code"])
        self.assertEqual([l["prediction"] for l in got["legs"]], ["1", "FH_OVER_0.5"])
        # A half leg reports the MAIN fixture as its event, so it pairs with the board.
        self.assertEqual({l["eventId"] for l in got["legs"]}, {str(REC["card"]["I"])})

    def test_bad_code_is_not_found(self):
        bad = {"Success": False, "Error": "Incorrect code", "ErrorCode": 100849}
        with mock.patch.object(onexbet, "_post", return_value=bad):
            self.assertTrue(onexbet.read_coupon("ZZZZZ").get("notFound"))


class GeneratingACode(unittest.TestCase):
    def _gen(self, sels, saved, read):
        calls = iter([saved, read])
        with mock.patch.object(onexbet, "_post", side_effect=lambda *a, **k: next(calls)):
            return onexbet.generate_code(sels)

    def _read(self, n):
        read = json.loads(json.dumps(COUPON["read"]))
        read["Value"]["Events"] = read["Value"]["Events"][:n]
        return read

    def test_cap_is_theirs(self):
        ev = _event()
        out = onexbet.generate_code([{"event": ev, "code": "1"}] * 51)
        self.assertIn("50", out["error"])

    def test_half_and_full_time_on_one_fixture_is_one_game(self):
        ev = _event()
        out = onexbet.generate_code([{"event": ev, "code": "1"}, {"event": ev, "code": "FH_OVER_0.5"}])
        self.assertIn("one selection per game", out["error"])
        self.assertIn("FH_OVER_0.5", out["error"])

    def test_silently_dropped_leg_is_an_error(self):
        # 30 sent, 29 read back, no error from them (29 Sep 2026).
        ev = _event()
        other = dict(ev, eventId="1", sel={"X": dict(ev["sel"]["X"], gameId=1)})
        out = self._gen([{"event": ev, "code": "1"}, {"event": other, "code": "X"}],
                        {"Value": "ABCDE", "Success": True}, self._read(1))
        self.assertEqual(out["code"], "ABCDE")
        self.assertIn("1 of 2", out["error"])

    def test_clean_code_is_verified(self):
        out = self._gen([{"event": _event(), "code": "1"}],
                        {"Value": "ABCDE", "Success": True}, self._read(1))
        self.assertTrue(out["verified"])
        self.assertEqual((out["code"], out["legs"]), ("ABCDE", 1))

    def test_a_throttled_save_is_retried(self):
        # 161627 "call failed, try later": 12 of 155 back-to-back mints on
        # 29 Sep, and 11 of the 12 booked fine four seconds later.
        throttled = {"Success": False, "Error": "Сбой вызова, попробуйте позже.", "ErrorCode": 161627}
        calls = iter([throttled, {"Value": "ABCDE", "Success": True}, self._read(1)])
        with mock.patch.object(onexbet, "_post", side_effect=lambda *a, **k: next(calls)), \
             mock.patch("time.sleep") as slept:
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertEqual(out.get("code"), "ABCDE")
        self.assertTrue(out.get("verified"))
        self.assertTrue(slept.called)

    def test_a_throttle_that_persists_is_reported(self):
        throttled = {"Success": False, "Error": "try later", "ErrorCode": 161627}
        with mock.patch.object(onexbet, "_post", return_value=throttled), mock.patch("time.sleep"):
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertEqual(out["errorCode"], 161627)

    def test_their_refusal_is_passed_on(self):
        refused = {"Success": False, "Error": "limit", "ErrorCode": 157972}
        with mock.patch.object(onexbet, "_post", return_value=refused):
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertEqual(out["errorCode"], 157972)
        self.assertNotIn("code", out)


def _vocabulary():
    import bet9ja
    import betking
    import betpawa
    import server
    v = set()
    for m in (server, bet9ja, betking, betpawa):
        v |= set(m.MARKET_MAP) | set(m.PASSTHROUGH_MAP)
    return sorted(v)


class TheAsymmetryIsAccountedFor(unittest.TestCase):
    """Every code any other book carries, 1xBet either carries or has a reason
    not to - so a code quietly added elsewhere forces a decision here."""

    def test_every_code_is_mapped_or_excused(self):
        orphans = [c for c in _vocabulary()
                   if not onexbet.market_for(c) and not onexbet.reason_uncarried(c)]
        self.assertEqual(orphans, [])

    def test_absent_and_unread_are_not_the_same_word(self):
        for prefix, why in onexbet.NOT_CARRIED.items():
            self.assertRegex(why, r"^(verified absent|not read yet|carried)", prefix)

    def test_a_carried_code_never_also_claims_a_reason(self):
        for code in list(onexbet.MARKET_MAP) + list(onexbet.PASSTHROUGH_MAP):
            self.assertIsNone(onexbet.reason_uncarried(code), code)

    def test_generated_keys_are_priced_in_the_evidence(self):
        cat = json.load(open(os.path.join(HERE, "tools", "xbcat.json"), encoding="utf8"))
        priced = {onexbet._key(per, T, P if P not in (None, 0) else None)
                  for _c, _g, per, _G, T, P, _C in cat["rows"]}
        for code, key in onexbet.PASSTHROUGH_MAP.items():
            self.assertIn(onexbet._key(*key), priced, code)

    def test_the_tail_was_generated(self):
        with open(os.path.join(HERE, "onexbet.py"), encoding="utf-8") as fh:
            self.assertIn("tools/xbgen.py", fh.read())
        self.assertGreater(len(onexbet.PASSTHROUGH_MAP), 150)

    def test_or_markets_are_not_mapped_onto_and_markets(self):
        # THE SEMANTIC TRAP. Our full-time MIX_x_OV_n, MIXGG_x and MIXNG_x are
        # "x OR ..." bets (the site labels them "Home or over 1.5", "Draw or
        # both score"); 1xBet sells only "W1 And Total >" / "W1 And Both Teams
        # To Score" - a narrower bet. Caught reading index.html's labels, 29 Sep.
        for code in ("MIX_1_OV_1.5", "MIX_2_UN_2.5", "MIXGG_1", "MIXGG_X", "MIXNG_2"):
            self.assertIsNone(onexbet.market_for(code), code)
            self.assertIn(" OR ", onexbet.reason_uncarried(code), code)
        # The first-half family IS an "and" bet on our side, so it stays.
        self.assertIsNotNone(onexbet.market_for("FH_MIX_1_OV_1.5"))

    def test_decoding_a_passthrough_leg_gives_the_code_back(self):
        for code, key in onexbet.PASSTHROUGH_MAP.items():
            self.assertEqual(onexbet.code_for(*key), code)


class BookingIsFast(unittest.TestCase):
    """THE BOOKING PATH MUST FIT INSIDE THE PROXIES' TIMEOUTS (review C1).

    The site's proxy gives up at 15s and the bot's converter at 8s. Reading
    three subgame cards with a pause before each, for every game, cost ~2.2s a
    leg - past six legs the reader got a timeout while a code was minted
    behind it. So an event is read only for the periods its legs need."""

    def test_full_time_legs_read_the_main_card_only(self):
        seen = []

        def fake(gid):
            seen.append(int(gid))
            return REC["card"] if int(gid) == REC["card"]["I"] else REC["half"]
        with mock.patch.object(onexbet, "fetch_card", side_effect=fake), mock.patch("time.sleep"):
            row = onexbet.fetch_event(str(REC["card"]["I"]), periods=[""])
        self.assertEqual(seen, [REC["card"]["I"]])
        self.assertIn("1", row["odds"])

    def test_a_first_half_leg_reads_the_half_card_too(self):
        seen = []

        def fake(gid):
            seen.append(int(gid))
            return REC["card"] if int(gid) == REC["card"]["I"] else REC["half"]
        with mock.patch.object(onexbet, "fetch_card", side_effect=fake), mock.patch("time.sleep"):
            row = onexbet.fetch_event(str(REC["card"]["I"]), periods=["", "1st half"])
        self.assertEqual(len(seen), 2)
        self.assertIn("FH_OVER_0.5", row["odds"])

    def test_periods_for_codes(self):
        self.assertEqual(onexbet.periods_for(["1", "OVER_2.5"]), [""])
        self.assertEqual(onexbet.periods_for(["1", "FH_OVER_0.5"]), ["", "1st half"])


class TheReadBackChecksEveryLeg(unittest.TestCase):
    """A matching COUNT is not a matching slip (review I1): a leg booked
    against the wrong game or re-lined by them keeps the count. Each leg read
    back must decode to the code that was sent, on the event it was sent for,
    and the ones that do not are named so the client can drop exactly them."""

    def test_a_leg_that_reads_back_as_a_different_bet_is_named(self):
        read = json.loads(json.dumps(COUPON["read"]))
        read["Value"]["Events"] = read["Value"]["Events"][:1]
        read["Value"]["Events"][0]["Type"] = 3          # sent home win, reads back away win
        calls = iter([{"Value": "ABCDE", "Success": True}, read])
        with mock.patch.object(onexbet, "_post", side_effect=lambda *a, **k: next(calls)):
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertIn("error", out)
        self.assertEqual(out["missing"], [{"eventId": str(REC["card"]["I"]), "prediction": "1"}])

    def test_a_dropped_leg_is_named(self):
        ev = _event()
        other = dict(ev, eventId="1", sel={"X": dict(ev["sel"]["X"], gameId=1)})
        read = json.loads(json.dumps(COUPON["read"]))
        read["Value"]["Events"] = read["Value"]["Events"][:1]
        calls = iter([{"Value": "ABCDE", "Success": True}, read])
        with mock.patch.object(onexbet, "_post", side_effect=lambda *a, **k: next(calls)):
            out = onexbet.generate_code([{"event": ev, "code": "1"}, {"event": other, "code": "X"}])
        self.assertEqual(out["missing"], [{"eventId": "1", "prediction": "X"}])


class ReviewMinors(unittest.TestCase):
    """The six minor findings of the 29 Sep review, fixed together."""

    def test_m1_a_read_back_that_could_not_be_read_is_not_a_dropped_leg(self):
        # The save worked; OUR read failed. Say so, keep the code, unverified.
        calls = iter([{"Value": "ABCDE", "Success": True}, OSError("reset by peer")])

        def post(*a, **k):
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v
        with mock.patch.object(onexbet, "_post", side_effect=post):
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertEqual(out.get("code"), "ABCDE")
        self.assertIs(out.get("verified"), False)
        self.assertNotIn("error", out)

    def test_m2_a_leg_with_no_price_gives_no_total(self):
        read = json.loads(json.dumps(COUPON["read"]))
        read["Value"]["Events"] = read["Value"]["Events"][:1]
        read["Value"]["Events"][0]["Coef"] = None
        calls = iter([{"Value": "ABCDE", "Success": True}, read])
        with mock.patch.object(onexbet, "_post", side_effect=lambda *a, **k: next(calls)):
            out = onexbet.generate_code([{"event": _event(), "code": "1"}])
        self.assertTrue(out["verified"])
        self.assertIsNone(out["odds"])

    def test_m4_half_handicap_reasons_say_what_is_absent(self):
        for code in ("FH_AH_1_0.5", "FH_AH_2_-0.5", "SH_AH_1_0", "SH_AH_2_-2"):
            why = onexbet.reason_uncarried(code)
            self.assertIn("verified absent", why, code)
            self.assertNotIn("0.25", why, code)   # no claim about quarters we never map


class AwayHandicapSign(unittest.TestCase):
    """OUR AH_2_L CARRIES THE HOME TEAM'S LINE; the away side's own is -L
    (index.html's label, betpawa.py's bpgen negation). 1xBet's "Handicap 2 (P)"
    is the away side's OWN line, so AH_2_L must map to P = -L. The first table
    mapped P = L - the opposite bet - and neither the price-ladder sign check
    nor the round-trip verifier could see it (found 30 Sep 2026)."""

    def test_away_codes_take_the_negated_line(self):
        self.assertEqual(onexbet.market_for("AH_2_1.5"), ("", "8", "-1.5"))    # away -1.5
        self.assertEqual(onexbet.market_for("AH_2_-1.5"), ("", "8", "1.5"))    # away +1.5
        self.assertEqual(onexbet.market_for("AH_2_0.75")[2], "-0.75")
        self.assertEqual(onexbet.market_for("FH_AH_2_-1"), ("1st half", "8", "1"))

    def test_home_codes_are_untouched(self):
        self.assertEqual(onexbet.market_for("AH_1_-1.5"), ("", "7", "-1.5"))


if __name__ == "__main__":
    unittest.main()
