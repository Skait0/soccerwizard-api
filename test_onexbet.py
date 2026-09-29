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


if __name__ == "__main__":
    unittest.main()
