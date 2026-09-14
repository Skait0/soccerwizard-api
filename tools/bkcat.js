"use strict";
/* Inventory BetKing's catalogue across several deep fixtures and write it to
   disk. Several, not one: a market missing from one card is not evidence, and
   generating lines from a sibling rather than from the card is how 24 codes
   that matched nothing got written last time. */
const fs = require("fs");
const FEED = "https://sportsapicdn-desktop.betking.com";
const H = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  Referer: "https://www.betking.com/",
  Accept: "application/json, text/plain, */*",
};
const get = async (u) => (await fetch(u, { headers: H })).json();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const day = await get(FEED + "/api/feeds/prematch/GetEvents/en/" +
    process.argv[2] + "/0/0/1");
  const items = (day.AreaMatches || []).flatMap((a) => a.Items || []);
  /* Deepest cards first: a market only the big competitions carry is exactly
     what the tail is made of. */
  items.sort((a, b) => (b.TotalOdds || 0) - (a.TotalOdds || 0));
  const picked = items.slice(0, Number(process.argv[3] || 5));
  console.log("fixtures:", picked.map((i) =>
    i.ItemName + " (" + i.TournamentName + ", " + i.TotalOdds + " odds)").join(" | "));

  const cat = {};          // "mid|sbv" -> {mid, sbv, name, outcomes:{otid:name}, seen}
  for (const it of picked) {
    const payload = await get(FEED + "/api/feeds/prematch/event/en/1/" + it.ItemID + "/0");
    for (const area of payload.AreaMatches || []) {
      for (const item of area.Items || []) {
        if (String(item.ItemID) !== String(it.ItemID)) continue;
        for (const coll of item.OddsCollection || []) {
          const mid = coll.OddsType.OddsTypeID;
          const sbv = Number(coll.SpecialBetValue || 0);
          const key = mid + "|" + sbv;
          const row = cat[key] || (cat[key] = {
            mid, sbv, name: coll.OddsType.OddsTypeName, outcomes: {}, seen: 0,
          });
          row.seen++;
          for (const mo of coll.MatchOdds || []) {
            const a = mo.OddAttribute || {};
            if (Number((mo.Outcome || {}).OddOutcome) > 1.01) {
              row.outcomes[a.OddTypeID] = a.OddName;
            }
          }
        }
      }
    }
    await wait(450);
  }

  const rows = Object.values(cat).sort((a, b) =>
    b.seen - a.seen || a.mid - b.mid || a.sbv - b.sbv);
  fs.writeFileSync(process.argv[4] || "bkcat.json", JSON.stringify(rows, null, 1));
  console.log("distinct (market, line) pairs:", rows.length);
  console.log("distinct market ids:", new Set(rows.map((r) => r.mid)).size);
  console.log("\nnames, one line each (seen/N):");
  const byName = {};
  for (const r of rows) (byName[r.name.replace(/[\d.]+$/, "").trim()] ||= []).push(r);
  for (const n of Object.keys(byName).sort()) {
    const g = byName[n];
    console.log("  %s  id=%s lines=[%s] seen=%d",
      n.padEnd(42), [...new Set(g.map((x) => x.mid))].join(","),
      g.map((x) => x.sbv).join(" "), Math.max(...g.map((x) => x.seen)));
  }
})();
