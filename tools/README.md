# tools

Read-only recon for adding or extending a bookmaker. Neither script writes to
the repo or books anything; both talk to a bookmaker's public feed.

    node tools/bkcat.js <YYYY-MM-DD> <cards> <out.json>

Inventory a book's catalogue across the deepest cards of one day. Deepest, not
first: the pass-through tail only exists on big competitions, so a market
missing from a thin fixture is not missing from the book.

    node tools/gentail.js <ourcodes.json> <YYYY-MM-DD> <cards> <out.json>

Generate the pass-through table. Each candidate triple is proposed from the
meaning of our own code and then kept ONLY if the book actually prices it on a
live card. It also checks the handicap sign against the prices rather than
reading it off a field, and reports which lines each market really offers - so
"absent from my sample" never gets written down as "absent from their
catalogue".

Produce `ourcodes.json` with:

    python -c "import json,server,bet9ja; json.dump(sorted(set(server.PASSTHROUGH_MAP)|set(bet9ja.PASSTHROUGH_MAP)), open('ourcodes.json','w'))"

    node tools/bpgen.js <ourcodes.json> <cards> <out.json>

The same job for Betpawa, and the one difference worth knowing: their feed
NAMES every market and every outcome in full, so a candidate is proposed in
words ("Total Score Over/Under - FT - Home Team", "Home by 3+") and the card
answers with the ids. Nothing is continued from a sibling family, which is
where the 24 invented codes came from on the book before it.

It also checks each handicap sign against the prices PER CARD. Pooling prices
across cards - the first version - reported three disagreements out of
sixteen, because one fixture's -1.5 is shorter than another's -0.5 for reasons
that have nothing to do with the sign.

`ourcodes.json` for that one wants BetKing's table in the union as well:

    python -c "import json,server,bet9ja,betking; json.dump(sorted(set(server.PASSTHROUGH_MAP)|set(bet9ja.PASSTHROUGH_MAP)|set(betking.PASSTHROUGH_MAP)), open('ourcodes.json','w'))"

The rejects matter as much as the map: grouped by family they are the
asymmetry note that says what a book does not sell, and why.
