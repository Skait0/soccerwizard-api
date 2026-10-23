"""/api/scores: final scores by Sportradar id, from SportyBet's event endpoint (23 Oct 2026).

The site's ledger grades slips with these when its own results table has no row
(K4MXX4, 12/12 x99: Vitoria SC Guimaraes 5-0 Maritimo was missing). Only an
ended game answers; a game in play or a failed read says nothing.
"""

import server


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return {"bizCode": 10000, "data": self._d}


EVENTS = {
    "72530980": {"homeTeamName": "Vitoria SC Guimaraes", "awayTeamName": "Maritimo Madeira",
                 "matchStatus": "Ended", "setScore": "5:0", "gameScore": ["1:0", "4:0"]},
    "111": {"homeTeamName": "A", "awayTeamName": "B", "matchStatus": "H2", "setScore": "1:0"},
}


def test_ended_games_only(monkeypatch):
    server._SCORE_CACHE.clear()
    monkeypatch.setattr(server.requests, "get",
                        lambda url, **k: _Resp(EVENTS.get(url.split("sr:match:")[1].split("&")[0], {})))
    j = server.app.test_client().get("/api/scores?ids=sr:match:72530980,111,999,abc").get_json()
    assert j["success"] is True
    assert j["scores"] == {"72530980": {"home": "Vitoria SC Guimaraes", "away": "Maritimo Madeira",
                                        "hg": 5, "ag": 0, "hth": 1, "hta": 0, "start": None}}
