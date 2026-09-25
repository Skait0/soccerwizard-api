"""Sportradar's match id: what counts as one, and that every book's legs get it."""
import unittest
from unittest import mock

import server
from srid import sr_id


class WhatCountsAsAnId(unittest.TestCase):
    def test_the_three_shapes_the_books_use(self):
        self.assertEqual(sr_id("sr:match:72202660"), "72202660")   # SportyBet
        self.assertEqual(sr_id("72221244"), "72221244")            # Bet9ja EXTID
        self.assertEqual(sr_id(72202660), "72202660")              # BetKing, an int

    def test_another_providers_number_is_not_trusted(self):
        # BetKing's Copa del Rey tie on 25 Sep: ProviderEventId 545514.
        self.assertIsNone(sr_id(545514))
        self.assertIsNone(sr_id(""))
        self.assertIsNone(sr_id(None))
        self.assertIsNone(sr_id("sr:match:abc"))


class EveryLegGetsItsId(unittest.TestCase):
    def test_sporty_reads_it_off_the_event_id(self):
        legs = [{"eventId": "sr:match:72202666"}]
        server._stamp_sr("sporty", legs)
        self.assertEqual(legs[0]["srId"], "72202666")

    def test_bet9ja_reads_it_off_the_cached_feed(self):
        # Their coupon carries only their own event id; EXTID is on the feed.
        cache = {"at": 1, "data": {"838256035": {"srId": "72202662"}}}
        legs = [{"eventId": 838256035}, {"eventId": 1}]
        with mock.patch.object(server, "_cache_get", return_value=cache):
            server._stamp_sr("bet9ja", legs)
        self.assertEqual(legs[0]["srId"], "72202662")
        self.assertIsNone(legs[1]["srId"], "a leg the feed has not seen falls back to names")


if __name__ == "__main__":
    unittest.main()
