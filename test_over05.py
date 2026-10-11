"""Over 0.5 match goals on every book (owner, 11 Oct 2026: the Bankers leg).

SportyBet checked live before adding (11 Oct: market 18 total=0.5, Over at
x1.02-1.06 on Liverpool v Man City, Hull v Everton, Crystal Palace v Forest,
Coventry v Newcastle). The other four sit on the same total-goals market as
their 1.5 line, one line down; their live feeds confirm after deploy.
"""

import server
import bet9ja
import betking
import betpawa
import onexbet


def test_sportybet_reads_market_18_at_a_half():
    assert server.market_for("OVER_0.5") == {"marketId": "18", "outcomeId": "12", "specifier": "total=0.5"}
    assert server.market_for("UNDER_0.5") == {"marketId": "18", "outcomeId": "13", "specifier": "total=0.5"}


def test_bet9ja_reads_the_half_line():
    assert bet9ja.market_for("OVER_0.5")[0] == "S_OU@0.5_O"
    assert bet9ja.market_for("UNDER_0.5")[0] == "S_OU@0.5_U"


def test_betking_reads_total_goals_at_a_half():
    assert betking.market_for("OVER_0.5") == (160, 0.5, 12)
    assert betking.market_for("UNDER_0.5") == (160, 0.5, 13)


def test_betpawa_reads_the_half_line():
    assert betpawa.market_for("OVER_0.5") == ("5000", "0.5", "5001")
    assert betpawa.market_for("UNDER_0.5") == ("5000", "0.5", "5002")


def test_onexbet_reads_the_half_line():
    assert onexbet.market_for("OVER_0.5") == ("", "9", "0.5")
    assert onexbet.market_for("UNDER_0.5") == ("", "10", "0.5")
