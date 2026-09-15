"use strict";
/* Generate BetKing's pass-through table from their own cards.
 *
 * Nothing here is typed by hand and nothing is continued from a sibling: every
 * candidate triple is PROPOSED from our code's meaning and then kept only if
 * it is actually present on live events. That is the rule that stopped 24
 * invented codes shipping last time.
 *
 * The handicap sign is CHECKED, not assumed. Their SpecialValue is a single
 * number and the obvious reading ("it is the home team's handicap") is exactly
 * the kind of guess that books the wrong side. So the generator verifies it
 * against the prices: a home handicap that gets more negative must get LONGER,
 * and the away side must move the other way. If that does not hold the family
 * is dropped rather than shipped.
 */
const fs = require("fs");
const FEED = "https://sportsapicdn-desktop.betking.com";
const H = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  Referer: "https://www.betking.com/", Accept: "application/json, text/plain, */*",
};
const get = async (u) => (await fetch(u, { headers: H })).json();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const num = (v) => (v === null || v === undefined || v === "" ? null : Number(v));

/* ---- our vocabulary, and what each code means ------------------------- */
/* [marketId, outcomeId] proposed from the code itself. `sv` is the line as
   BETKING expresses it, which for a handicap is always the HOME side's. */
const AH = { 1: 1714, 2: 1715 };
const EHS = { "1": 1714, X: 1712, "2": 1715 };

function propose(code) {
  let m;
  if ((m = /^AH_([12])_(-?[\d.]+)$/.exec(code))) {
    const line = Number(m[2]);
    return [305, m[1] === "1" ? line : -line, AH[m[1]], "asian handicap"];
  }
  if ((m = /^FH_AH_([12])_(-?[\d.]+)$/.exec(code))) {
    const line = Number(m[2]);
    return [344, m[1] === "1" ? line : -line, AH[m[1]], "1st half handicap"];
  }
  if ((m = /^SH_AH_([12])_(-?[\d.]+)$/.exec(code))) {
    const line = Number(m[2]);
    return [9335, m[1] === "1" ? line : -line, AH[m[1]], "2nd half handicap"];
  }
  /* EH_0_n_S is "0 : n" - the away side starts n up, so home is -n. */
  if ((m = /^EH_0_(\d+)_([12X])$/.exec(code))) {
    return [342, -Number(m[1]), EHS[m[2]], "european handicap"];
  }
  if ((m = /^EH_(\d+)_0_([12X])$/.exec(code))) {
    return [342, Number(m[1]), EHS[m[2]], "european handicap"];
  }
  if ((m = /^CORNERS_(OV|UN)_([\d.]+)$/.exec(code))) {
    return [190, Number(m[2]), m[1] === "OV" ? 12 : 13, "total corners"];
  }
  if ((m = /^CORNERS_([HA])_(OV|UN)_([\d.]+)$/.exec(code))) {
    return [m[1] === "H" ? 10332 : 10333, Number(m[3]),
            m[2] === "OV" ? 12 : 13, "team corners"];
  }
  if ((m = /^FH_(HOME|AWAY)_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    return [m[1] === "HOME" ? 10290 : 10291, Number(m[3]),
            m[2] === "OVER" ? 12 : 13, "1st half team goals"];
  }
  if ((m = /^SH_(HOME|AWAY)_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    return [m[1] === "HOME" ? 10302 : 10303, Number(m[3]),
            m[2] === "OVER" ? 12 : 13, "2nd half team goals"];
  }
  if ((m = /^SH_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    return [9280, Number(m[2]), m[1] === "OVER" ? 12 : 13, "2nd half total"];
  }
  if ((m = /^FH_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    return [161, Number(m[2]), m[1] === "OVER" ? 12 : 13, "1st half total"];
  }
  /* Second-half double chance. Their outcome ids are the SAME 9/10/11 the
     match market uses, which is a coincidence worth not relying on elsewhere. */
  if ((m = /^DC2_(1X|12|X2)$/.exec(code))) {
    return [10299, 0, { "1X": 9, "12": 10, X2: 11 }[m[1]], "2nd half double chance"];
  }
  /* THE OR FAMILY, and it is a different bet from the AND family that sits
     next to it in every catalogue. 9334 is "1x2 or GG/NG"; 9277 is "1X2 &
     Total Goals". Mapping our MIXGG_1 ("home or both score") onto an AND
     market would hand somebody a far narrower bet at a far longer price. */
  /* 1x2-or-total. 9648, and I declared it absent because I searched MARKET
     names for "or" - "Chance Mix Total Goals 1.5" has none, the OR lives in
     the outcome labels. A real punter's code (3T2NBQ, Brighton v Arsenal,
     "2 or Over (1.50)") is what found it. Search the outcomes. */
  /* EXACTLY N GOALS - and our EXACT_ is that, not correct score, whatever the
     name suggests. Their 9641 carries the number as the LINE and a single
     outcome 74 "Goals", so the line is the bet. Do not confuse with EXGOALS_,
     which is the complement ("anything but N") and has no home here. */
  /* The two excluded-goals codes that are EQUIVALENCES rather than absences.
     Total goals is a non-negative integer, so "anything but nought" and "over
     0.5" have the same winners on every scoreline. Kept as a generator rule
     rather than hand-added to the table: a hand-added entry is destroyed the
     next time the table is regenerated, which is exactly what happened. */
  if ((m = /^EXGOALS_(FH_)?0$/.exec(code))) {
    return [m[1] ? 161 : 160, 0.5, 12, "excluded goals, the nought cases"];
  }
  if ((m = /^EXACT_(\d)$/.exec(code))) {
    return [9641, Number(m[1]), 74, "exactly N goals"];
  }
  if ((m = /^MIX_([12X])_(OV|UN)_([\d.]+)$/.exec(code))) {
    const side = { "1": 0, X: 1, "2": 2 }[m[1]];
    const ids = [[2354, 2355], [2352, 2353], [2350, 2351]][side];
    return [9648, Number(m[3]), m[2] === "OV" ? ids[0] : ids[1],
            "chance mix 1x2 or total"];
  }
  if ((m = /^MIXGG_([12X])$/.exec(code))) {
    return [9334, 0, { "1": 2348, X: 2346, "2": 2344 }[m[1]], "chance mix 1x2 or GG"];
  }
  if ((m = /^MIXNG_([12X])$/.exec(code))) {
    return [9334, 0, { "1": 2349, X: 2347, "2": 2345 }[m[1]], "chance mix 1x2 or NG"];
  }
  if ((m = /^UP1_([12X])$/.exec(code))) {
    return [10974, 0, { "1": 4, X: 2, "2": 5 }[m[1]], "1UP"];
  }
  if ((m = /^UP2_([12X])$/.exec(code))) {
    return [10975, 0, { "1": 4, X: 2, "2": 5 }[m[1]], "2UP"];
  }
  if ((m = /^DC1UP_(1X|12|X2)$/.exec(code))) {
    return [10987, 0, { "1X": 9, "12": 10, X2: 11 }[m[1]], "double chance 1UP"];
  }
  if ((m = /^HALFCORNER_([12E])$/.exec(code))) {
    return [9793, 0, { "1": 436, "2": 438, E: 924 }[m[1]], "half most corners"];
  }
  if ((m = /^PEN_([YN])$/.exec(code))) {
    return [699, 0, m[1] === "Y" ? 74 : 76, "penalty awarded"];
  }
  if ((m = /^WINHALF_([HA])_([YN])$/.exec(code))) {
    return [m[1] === "H" ? 628 : 627, 0, m[2] === "Y" ? 74 : 76, "win either half"];
  }
  if ((m = /^DNB_([12])$/.exec(code))) {
    /* 4 and 5, read off their card - NOT the 1714/1715 the handicaps use.
       Guessed wrong first time and the triple matched nothing, which is the
       whole reason nothing here ships unverified. */
    return [147, 0, m[1] === "1" ? 4 : 5, "draw no bet"];
  }
  return null;
}

/* ---- read their cards -------------------------------------------------- */
async function cards(date, n) {
  const day = await get(`${FEED}/api/feeds/prematch/GetEvents/en/${date}/0/0/1`);
  const items = (day.AreaMatches || []).flatMap((a) => a.Items || [])
    .sort((a, b) => (b.TotalOdds || 0) - (a.TotalOdds || 0)).slice(0, n);
  const out = [];
  for (const it of items) {
    const p = await get(`${FEED}/api/feeds/prematch/event/en/1/${it.ItemID}/0`);
    const seen = new Map();          // "mid|sv|otid" -> price
    for (const area of p.AreaMatches || []) {
      for (const item of area.Items || []) {
        if (String(item.ItemID) !== String(it.ItemID)) continue;
        for (const c of item.OddsCollection || []) {
          const mid = c.OddsType.OddsTypeID;
          const cs = num(c.SpecialBetValue) || 0;
          for (const mo of c.MatchOdds || []) {
            const a = mo.OddAttribute || {};
            const price = num((mo.Outcome || {}).OddOutcome);
            if (!(price > 1.01)) continue;
            const sv = a.SpecialValue === null || a.SpecialValue === undefined
              ? cs : num(a.SpecialValue);
            seen.set(`${mid}|${sv}|${a.OddTypeID}`, price);
          }
        }
      }
    }
    out.push({ name: it.ItemName, odds: seen });
    await wait(450);
  }
  return out;
}

(async () => {
  const codes = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const deck = await cards(process.argv[3], Number(process.argv[4] || 6));
  console.log("cards:", deck.map((d) => d.name).join(" | "));

  /* --- the sign check, before anything is written ---------------------- */
  const mono = (mid, otid, want) => {
    let ok = 0, bad = 0;
    for (const c of deck) {
      const pts = [];
      for (const [k, v] of c.odds) {
        const [m, sv, o] = k.split("|");
        if (Number(m) === mid && Number(o) === otid) pts.push([Number(sv), v]);
      }
      pts.sort((a, b) => a[0] - b[0]);
      for (let i = 1; i < pts.length; i++) {
        /* More generous to the side = shorter price. For the HOME outcome a
           bigger SpecialValue is more generous, so prices must FALL. */
        const rising = pts[i][1] > pts[i - 1][1];
        if (rising === (want === "up")) ok++; else bad++;
      }
    }
    return { ok, bad, verdict: ok > bad * 4 ? "holds" : "FAILS" };
  };
  const homeSign = mono(305, 1714, "down");
  const awaySign = mono(305, 1715, "up");
  console.log("\nhandicap sign check (SpecialValue is the HOME handicap):");
  console.log("  home 1714, price falls as the line rises:", JSON.stringify(homeSign));
  console.log("  away 1715, price rises as the line rises:", JSON.stringify(awaySign));
  if (homeSign.verdict === "FAILS" || awaySign.verdict === "FAILS") {
    console.log("\nSIGN CHECK FAILED - handicap families NOT written.");
  }
  const signOK = homeSign.verdict === "holds" && awaySign.verdict === "holds";

  /* --- propose, then keep only what is really on a card ---------------- */
  const map = {}, missing = {}, skipped = [];
  for (const code of codes) {
    const p = propose(code);
    if (!p) { skipped.push(code); continue; }
    const [mid, sv, otid, fam] = p;
    if (!signOK && /handicap/.test(fam)) { (missing[fam] ||= []).push(code); continue; }
    const key = `${mid}|${sv}|${otid}`;
    const on = deck.filter((c) => c.odds.has(key)).length;
    if (on) map[code] = [mid, sv, otid, fam, on];
    else (missing[fam] ||= []).push(code);
  }

  console.log("\nmapped %d, unverified %d, no rule %d",
    Object.keys(map).length,
    Object.values(missing).reduce((t, a) => t + a.length, 0), skipped.length);
  const byFam = {};
  for (const [c, v] of Object.entries(map)) (byFam[v[3]] ||= []).push(c);
  console.log("\nby family (mapped / on how many of %d cards):", deck.length);
  for (const f of Object.keys(byFam).sort()) {
    const n = byFam[f].length;
    const min = Math.min(...byFam[f].map((c) => map[c][4]));
    console.log("  " + f.padEnd(24) + String(n).padStart(3) +
      "   thinnest on " + min + "/" + deck.length + " cards");
  }
  console.log("\nproposed but NOT on any card:");
  for (const f of Object.keys(missing).sort()) {
    console.log("  " + f.padEnd(24) + String(missing[f].length).padStart(3) + "   " +
      missing[f].slice(0, 5).join(" ") + (missing[f].length > 5 ? " ..." : ""));
  }
  /* Which lines each market ACTUALLY offers, so an absence can be stated as a
     fact about their catalogue rather than about our sample. */
  const lines = {};
  for (const c of deck) for (const k of c.odds.keys()) {
    const [mid, sv] = k.split("|");
    (lines[mid] ||= new Set()).add(Number(sv));
  }
  console.log("\nlines offered, by market:");
  for (const mid of [305, 342, 344, 9335, 190, 10332, 10333, 147]) {
    const v = lines[mid];
    console.log("  " + String(mid).padEnd(7) +
      (v ? [...v].sort((a, b) => a - b).join(" ") : "absent from every card"));
  }
  fs.writeFileSync(process.argv[5] || "bktail.json",
    JSON.stringify({ map, missing, skipped }, null, 1));
  console.log("\nwritten:", process.argv[5]);
})();
