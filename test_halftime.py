"""The four half-time legs are bookable (owner, 17 Oct 2026: "add the top four").

1st half under 2.5, 2nd half under 2.5, 2nd half over 0.5 and half-time 1X.
The 2nd-half lines were already carried; the 1st half under 2.5 and half-time
1X were read off Tottenham v Coventry's live cards on 19 Oct 2026: SportyBet
68/13 total=2.5 (1.17) and 63/9 (1.17), Bet9ja S_OU1T@2.5_U (1.12) and
S_DC1T_1X (1.11), BetKing (161, 2.5, 13) 1.14 and (9588, 0, 9) 1.16, betPawa
4958/4960 at 2.5 and 4673/4674, 1xBet's 1st-half sub-game T10 at 2.5 (1.114)
and T4 (1.103). Betway uses Sportradar's numbers, as SportyBet does; it sells
no 2nd-half match total and says so in NOT_CARRIED.
"""

import server
import bet9ja
import betking
import betpawa
import betway
import onexbet

NEW = ("FH_UNDER_2.5", "FH_DC_1X")
SECOND = ("SH_OVER_0.5", "SH_UNDER_2.5")


def test_sportybet():
    assert server.market_for("FH_UNDER_2.5") == {"marketId": "68", "outcomeId": "13", "specifier": "total=2.5"}
    assert server.market_for("FH_DC_1X") == {"marketId": "63", "outcomeId": "9"}
    for c in SECOND:
        assert server.market_for(c)


def test_sportybet_sweep_fetches_the_half_markets():
    assert "63,90" in server.FIXTURE_MARKET_IDS


def test_other_books():
    assert bet9ja.market_for("FH_UNDER_2.5")[0] == "S_OU1T@2.5_U"
    assert bet9ja.market_for("FH_DC_1X")[0] == "S_DC1T_1X"
    assert betking.market_for("FH_UNDER_2.5") == (161, 2.5, 13)
    assert betking.market_for("FH_DC_1X") == (9588, 0.0, 9)
    assert betpawa.market_for("FH_UNDER_2.5") == ("4958", "2.5", "4960")
    assert betpawa.market_for("FH_DC_1X") == ("4673", None, "4674")
    assert onexbet.market_for("FH_UNDER_2.5") == ("1st half", "10", "2.5")
    assert onexbet.market_for("FH_DC_1X") == ("1st half", "4", None)
    assert betway.market_for("FH_UNDER_2.5") == ("68", "total=2.5", "13")
    assert betway.market_for("FH_DC_1X") == ("63", None, "9")
    for mod in (bet9ja, betking, betpawa, onexbet):
        for c in SECOND:
            assert mod.market_for(c), (mod.__name__, c)
    for c in SECOND:
        assert betway.market_for(c) is None
        assert betway.reason_uncarried(c).startswith("verified absent")
