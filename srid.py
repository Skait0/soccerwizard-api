"""Sportradar's match id, the one number three of the four books share.

SportyBet's event id is `sr:match:NNN`; Bet9ja's feed carries the same NNN as
EXTID and BetKing's as ExtProvIDItem / ProviderEventId. Checked 24-25 Sep 2026:
503 of 506 Bet9ja events and 1,248 of 1,256 BetKing events matched a SportyBet
id, names agreeing. Betpawa has its own ids and nothing else.

Not every provider id is Sportradar's. BetKing handed back 545514 for a Copa
del Rey tie on 25 Sep - some other feed's number. Sportradar's current match
ids run eight digits (60-80 million), so anything shorter is not one and is
dropped rather than trusted: a wrong id would pair two different games, and a
missing one only falls back to matching on names.
"""
import re

_SR = re.compile(r"(?:sr:match:)?(\d{8,9})$")


def sr_id(value):
    """The Sportradar match number as a string, or None when it is not one."""
    m = _SR.match(str(value or "").strip())
    return m.group(1) if m else None
