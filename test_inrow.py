"""3 or more goals in a row - No, on SportyBet (owner, 11 Oct 2026).

Markets 60020 (any team), 60021 (home), 60022 (away); outcome 74 Yes, 76 No,
no specifier. Read off a live card first (Crystal Palace v Forest, Hull v
Everton, Liverpool v Man City). The sweep asks for all three in one request.
"""

import server


def test_the_no_sides_map_to_outcome_76():
    assert server.market_for("NR3_ANY") == {"marketId": "60020", "outcomeId": "76"}
    assert server.market_for("NR3_H") == {"marketId": "60021", "outcomeId": "76"}
    assert server.market_for("NR3_A") == {"marketId": "60022", "outcomeId": "76"}


def test_the_yes_sides_are_swept_for_the_de_vig():
    assert server.market_for("R3_ANY")["outcomeId"] == "74"
    assert server._ODDS_LOOKUP[("60021", "74", "")] == "R3_H"
    assert server._ODDS_LOOKUP[("60022", "76", "")] == "NR3_A"


def test_one_request_carries_all_three():
    assert "60020,60021,60022" in server.FIXTURE_MARKET_IDS
    assert {"60020", "60021", "60022"} <= server.SWEPT_MARKET_IDS
    # corners and shots stay last: a sweep short on time drops them first
    assert server.FIXTURE_MARKET_IDS[-2:] == ("900552", "900553")
