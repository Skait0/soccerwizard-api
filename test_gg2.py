"""Both teams score 2+ (owner, 19 Oct 2026: "its a crazy bet"; the site offers the Yes).

Read off Tottenham v Coventry's live cards, 19 Oct 2026: SportyBet's own market
60000 'GG/NG 2+' 74/76 (4.75/1.15), Bet9ja S_GGNG2PLUS_GG/NG (4.6/1.16),
BetKing 'Both Teams Score 2+' 9905 74/76 (5.2/1.11), 1xBet 'Each Team To Score
(2) Or More' T11273/T11274 at 2 (4.815/1.138). betPawa does not sell it; Betway
is not read yet.
"""

import server
import bet9ja
import betking
import betpawa
import betway
import onexbet


def test_mapped_where_it_is_sold():
    assert server.market_for("GG2_Y") == {"marketId": "60000", "outcomeId": "74"}
    assert server.market_for("GG2_N") == {"marketId": "60000", "outcomeId": "76"}
    assert bet9ja.market_for("GG2_Y")[0] == "S_GGNG2PLUS_GG"
    assert betking.market_for("GG2_Y") == (9905, 0.0, 74)
    assert onexbet.market_for("GG2_Y") == ("", "11273", "2")
    assert "60000" in server.FIXTURE_MARKET_IDS


def test_excused_where_it_is_not():
    assert betpawa.market_for("GG2_Y") is None
    assert betpawa.reason_uncarried("GG2_Y").startswith("verified absent")
    assert betway.market_for("GG2_Y") is None
    assert betway.reason_uncarried("GG2_Y").startswith("not read yet")
