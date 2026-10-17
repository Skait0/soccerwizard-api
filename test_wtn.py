"""Win to Nil NO, home and away, is bookable on five books (owner, 17 Oct 2026).

Every mapping was read off a live card first (Roma v Genoa, 17 Oct): SportyBet
markets 33/34 outcome 76 (1.53/1.06), Betway the same UOF numbers (1.51/1.05),
BetKing 9643/9644 selection 76 (1.46/1.03), betPawa 5051/5053 and 5042/5044
(1.53/1.06), Bet9ja S_HOMEWINNIL_N / S_AWAYWINNIL_N (1.42/1.01). 1xBet does
not sell it, and says so in NOT_CARRIED.
"""

import server
import bet9ja
import betking
import betpawa
import betway
import onexbet


def test_sportybet_reads_markets_33_and_34_outcome_no():
    assert server.market_for("WTN_H_N") == {"marketId": "33", "outcomeId": "76"}
    assert server.market_for("WTN_A_N") == {"marketId": "34", "outcomeId": "76"}


def test_betway_uses_the_same_uof_numbers():
    assert betway.market_for("WTN_H_N") == ("33", None, "76")
    assert betway.market_for("WTN_A_N") == ("34", None, "76")


def test_betking_reads_home_and_away_win_to_nil_no():
    assert betking.market_for("WTN_H_N") == (9643, 0, 76)
    assert betking.market_for("WTN_A_N") == (9644, 0, 76)


def test_betpawa_reads_the_no_outcome_of_each_side():
    assert betpawa.market_for("WTN_H_N") == ("5051", None, "5053")
    assert betpawa.market_for("WTN_A_N") == ("5042", None, "5044")


def test_bet9ja_reads_the_win_nil_no_keys():
    assert bet9ja.market_for("WTN_H_N")[0] == "S_HOMEWINNIL_N"
    assert bet9ja.market_for("WTN_A_N")[0] == "S_AWAYWINNIL_N"


def test_onexbet_refuses_and_says_why():
    assert onexbet.market_for("WTN_H_N") is None
    assert onexbet.market_for("WTN_A_N") is None
    assert onexbet.NOT_CARRIED["WTN_"].startswith("verified absent")
