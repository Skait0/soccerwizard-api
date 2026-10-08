"""Under 4.5 is bookable on every book (owner, 8 Oct 2026).

Readers asked to pick it, so the site offers it in the builders and in a
match's options. Each mapping below was resolved against a live event first
(PSV v Heerenveen, 9 Oct): SportyBet market 18 total=4.5 outcome 13, Bet9ja
S_OU@4.5_U, BetKing "Total Goals 4.5" Under, betPawa 5000/4.5/5002, 1xBet
type 10 Param 4.5. These pin each to the 4.5 line, the same shape as Under 3.5
one line down.
"""

import server
import bet9ja
import betking
import betpawa
import onexbet


def test_sportybet_reads_market_18_at_four_and_a_half():
    assert server.market_for("UNDER_4.5") == {"marketId": "18", "outcomeId": "13", "specifier": "total=4.5"}


def test_bet9ja_reads_the_four_and_a_half_under():
    assert bet9ja.market_for("UNDER_4.5")[0] == "S_OU@4.5_U"


def test_betking_reads_total_goals_at_four_and_a_half():
    assert betking.market_for("UNDER_4.5") == (160, 4.5, 13)


def test_betpawa_reads_the_four_and_a_half_line():
    assert betpawa.market_for("UNDER_4.5") == ("5000", "4.5", "5002")


def test_onexbet_reads_the_under_at_four_and_a_half():
    assert onexbet.market_for("UNDER_4.5") == ("", "10", "4.5")
