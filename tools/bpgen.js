"use strict";
/* Generate Betpawa's pass-through table from their own cards.
 *
 *     node tools/bpgen.js <ourcodes.json> <cards> <out.json>
 *
 * Nothing here is typed by hand and nothing is continued from a sibling: every
 * candidate is PROPOSED from our code's meaning and kept only if Betpawa
 * actually prices it on a live card. That is the rule that stopped 24 invented
 * codes shipping on an earlier book.
 *
 * KEYED ON NAMES, NOT ON IDS, which is the one way this differs from
 * tools/gentail.js. Betpawa names every market and every outcome in full -
 * "Total Score Over/Under - FT - Home Team", "Home by 3+" - so a proposal can
 * say what it MEANS and the index answers with the ids. On BetKing the same
 * job had to guess numeric ids from siblings, which is where the invented
 * codes came from.
 *
 * THE LINE COMES FROM THE OUTCOME. Their row carries `handicap` as an integer
 * in quarter units (10 for a 2.5 goals line) while the price beside it carries
 * "2.5" as text, and for a handicap it carries "Home -1" / "Away +1" - a
 * different string per side of the same row. Keying on the row would fold
 * every line of a market onto one entry.
 *
 * THE HANDICAP SIGN IS CHECKED, NOT READ. Their Asian lines are signed per
 * outcome, so "which side is giving?" is a coin toss that books the wrong
 * team. The generator verifies it against the prices: the side receiving the
 * bigger head start must be SHORTER. A family that fails is dropped rather
 * than shipped.
 *
 * The rejects matter as much as the map. Grouped by family with a reason each,
 * they are the asymmetry note - and "verified absent" must never be written
 * down where "nobody has checked" is the truth.
 */
const fs = require("fs");

const API = "https://www.betpawa.ng/api/sportsbook";
const H = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  Referer: "https://www.betpawa.ng/",
  Origin: "https://www.betpawa.ng",
  "x-pawa-brand": "betpawa-nigeria",
  "x-pawa-language": "en",
  devicetype: "web",
  Accept: "application/json, text/plain, */*",
};
const get = async (u) => (await fetch(u, { headers: H })).json();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- what each of our codes means, in their words --------------------- */

const SIDE3 = { "1": "1", X: "X", "2": "2" };

/* A proposal is {market, outcome, line, family} - the three strings the index
   is keyed on - or {family, why} for a code we refuse to propose at all. A
   refusal here is a statement about MEANING (their rung is not our rung), not
   about coverage; coverage is decided by the cards below. */
function propose(code) {
  let m;

  if ((m = /^MIX_([12X])_(OV|UN)_([\d.]+)$/.exec(code))) {
    return { market: "1X2 and Totals - FT", line: m[3], family: "1X2 + totals",
             outcome: SIDE3[m[1]] + (m[2] === "OV" ? " - Over" : " - Under") };
  }
  if ((m = /^FH_MIX_([12X])_(OV|UN)_([\d.]+)$/.exec(code))) {
    return { market: "1X2 and Totals - 1H", line: m[3],
             family: "1X2 + totals, first half",
             outcome: SIDE3[m[1]] + (m[2] === "OV" ? " - Over" : " - Under") };
  }
  if ((m = /^MIX(GG|NG)_([12X])$/.exec(code))) {
    return { market: "1X2 and Both Teams To Score - FT",
             outcome: SIDE3[m[2]] + (m[1] === "GG" ? " - Yes" : " - No"),
             line: null, family: "1X2 + both to score" };
  }
  if ((m = /^UP([12])_([12X])$/.exec(code))) {
    return { market: "1X2 " + m[1] + "UP - FT", outcome: SIDE3[m[2]],
             line: null, family: "1UP / 2UP" };
  }
  if ((m = /^DC1UP_(1X|X2|12)$/.exec(code))) {
    return { market: "Double Chance 1UP - FT", outcome: m[1], line: null,
             family: "double chance 1UP" };
  }
  if ((m = /^DC2_(1X|X2|12)$/.exec(code))) {
    return { family: "double chance 2UP",
             why: "they run 1UP on double chance and not 2UP - the market is " +
                  "absent from every deep card, not merely from a thin one" };
  }
  if ((m = /^DNB_([12])$/.exec(code))) {
    return { market: "Draw No Bet - FT", outcome: m[1], line: null,
             family: "draw no bet" };
  }

  /* Asian handicaps. The line is signed FROM THE SIDE THAT CARRIES IT on this
     book - the home outcome reads "-0.5" where ours reads AH_1_-0.5, and the
     away outcome of the same row reads "+0.5" where ours reads AH_2_-0.5. So
     the away code's line is negated, and the sign is checked against the
     prices before any of it is kept. */
  if ((m = /^(FH_|SH_)?AH_([12])_(-?[\d.]+)$/.exec(code))) {
    const half = m[1] === "FH_" ? "1H" : m[1] === "SH_" ? "2H" : "FT";
    const n = Number(m[3]);
    if (Math.abs(n * 2 - Math.round(n * 2)) > 1e-9) {
      return { family: "asian handicap, quarter balls",
               why: "quarter balls are not sold: every Asian line on every " +
                    "deep card is a half ball" };
    }
    if (Number.isInteger(n)) {
      return { family: "asian handicap, whole balls",
               why: "whole balls are not sold on the Asian market. Their " +
                    "three-way Handicap 1X2 carries whole numbers and is a " +
                    "DIFFERENT bet - the draw is its own outcome and nothing " +
                    "pushes - so mapping onto it narrows the punter's bet" };
    }
    const shown = m[2] === "1" ? n : -n;
    return { market: "Asian Handicap - " + half, outcome: m[2],
             line: (shown > 0 ? "+" : "") + shown,
             family: "asian handicap" + (half === "FT" ? "" : ", " + half) };
  }

  /* European (three-way) handicap. Ours is a scoreline head start - EH_0_1 is
     the AWAY side starting a goal up - and theirs is the home team's
     adjustment said in words, "Home -1" on the 1 and X outcomes and "Away +1"
     on the 2. Both halves of that were read off a card, not inferred. */
  if ((m = /^(FH_|SH_)?EH_(\d+)_(\d+)_([12X])$/.exec(code))) {
    const half = m[1] === "FH_" ? "1H" : m[1] === "SH_" ? "2H" : "FT";
    const home = Number(m[2]), away = Number(m[3]);
    if (home && away) return { family: "european handicap",
                               why: "a two-sided head start is not a line " +
                                    "they quote" };
    const n = home ? home : -away;                 /* the home side's number */
    const sign = (v) => (v > 0 ? "+" : "") + v;
    /* THE DRAW OUTCOME NAMES WHICHEVER SIDE IS GIVING, not the home one. On a
       row where the home team gives a goal the X price reads "Home -1"; on
       the row where the away team gives one it reads "Away -1", never
       "Home +1". Read off three cards rather than continued from the pattern
       the other two outcomes follow - assuming it cost a whole family. */
    const line = m[4] === "2" ? "Away " + sign(-n)
      : m[4] === "X" ? (n > 0 ? "Away -" + n : "Home " + sign(n))
      : "Home " + sign(n);
    return {
      market: "Handicap 1X2 - " + half, outcome: m[4], line,
      family: "european handicap" + (half === "FT" ? "" : ", " + half),
    };
  }

  if ((m = /^(FH_|SH_)?(HOME|AWAY)_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    const half = m[1] === "FH_" ? "1H" : m[1] === "SH_" ? "2H" : "FT";
    return { market: "Total Score Over/Under - " + half + " - " +
                     (m[2] === "HOME" ? "Home" : "Away") + " Team",
             outcome: m[3] === "OVER" ? "Over" : "Under", line: m[4],
             family: "team goals" + (half === "FT" ? "" : ", " + half) };
  }
  if ((m = /^SH_(OVER|UNDER)_([\d.]+)$/.exec(code))) {
    return { market: "Total Score Over/Under - 2H",
             outcome: m[1] === "OVER" ? "Over" : "Under", line: m[2],
             family: "second-half goals" };
  }
  if ((m = /^(OVER|UNDER)_(\d+)$/.exec(code))) {
    return { family: "whole goal lines",
             why: "every total they sell is a half line, so there is nothing " +
                  "that pushes on the number - a whole line mapped onto the " +
                  "half beside it is a different bet" };
  }

  if ((m = /^CORNERS_(OV|UN)_([\d.]+)$/.exec(code))) {
    return { market: "Total Corners Over/Under - FT",
             outcome: m[1] === "OV" ? "Over" : "Under", line: m[2],
             family: "total corners" };
  }
  if ((m = /^CORNERS_([HA])_(OV|UN)_([\d.]+)$/.exec(code))) {
    return { family: "team corners",
             why: "they sell total corners and corner 1X2, and no per-team " +
                  "corner line appears on any deep card" };
  }
  if (/^CORNRANGE_[HA]/.test(code)) {
    return { family: "team corner ranges", why: "see team corners" };
  }
  if ((m = /^CORNRANGE_(\d+)(?:_(\d+))?$/.exec(code))) {
    return { market: "Total Corners - FT", line: null,
             outcome: m[2] ? m[1] + "-" + m[2] : m[1] + "+",
             family: "corner ranges" };
  }

  if ((m = /^EXACT_(\d)$/.exec(code))) {
    return { market: "Total Goals Exact - FT", line: null,
             outcome: m[1] === "6" ? "6+" : m[1], family: "exact goals" };
  }
  /* THE SEMANTIC TRAP, and the reason this file refuses rather than maps.
     Our half-time exact goals top out at "3+" and theirs at "2+". So their
     "2" is not our EXACT_FH_2 - it is every score of two or more - and
     pairing them by position hands somebody a wider bet than they placed. */
  if ((m = /^EXACT_FH_(\d)$/.exec(code))) {
    if (m[1] === "0" || m[1] === "1") {
      return { market: "Total Goals Exact - 1H", outcome: m[1], line: null,
               family: "exact goals, first half" };
    }
    return { family: "exact goals, first half",
             why: "their first-half card stops at 2+ where ours stops at 3+, " +
                  "so their top rung means MORE than our 2 and there is no " +
                  "rung at all for our 3" };
  }
  if ((m = /^EXACT_SH_(\d)$/.exec(code))) {
    return { market: "Total Goals Exact - 2H", line: null,
             outcome: m[1] === "2" ? "2+" : m[1],
             family: "exact goals, second half" };
  }
  if ((m = /^TEAMGOALS_([HA])_(\d)$/.exec(code))) {
    return { market: "Total Goals Exact - FT - " +
                     (m[1] === "H" ? "Home" : "Away") + " Team", line: null,
             outcome: m[2] === "3" ? "3+" : m[2], family: "team goals exact" };
  }
  if ((m = /^BOUNDS_([HA])_(\d)(\d)?$/.exec(code))) {
    /* Their per-team Multigoals reads 1-2, 1-3, 2-3, 4+, No goal. Ours are a
       different set and two of them mean something wider than they look
       ("1-3+" rather than 1-3), so only the one that matches exactly crosses -
       and "exactly none" is already TEAMGOALS_x_0, which would give one triple
       two codes and make a read-back ambiguous. */
    return { family: "team goal bounds",
             why: "their per-team Multigoals rungs are 1-2, 1-3, 2-3, 4+ and " +
                  "no goal; ours are a different set, and the ones that look " +
                  "alike are not (our 1-3 is 1-3-or-more). Where they do " +
                  "agree the bet is already TEAMGOALS_x_0, and one triple " +
                  "cannot decode back to two codes" };
  }
  if (/^GOALRANGE_/.test(code)) {
    return { family: "goal ranges",
             why: "their grouped totals exist on the halves only (0-1, 2-3, " +
                  "4+); the full-time equivalent is Multigoals, whose rungs " +
                  "start at 1-2 and never carry our 0-1" };
  }

  if ((m = /^MARGIN_(?:([HA])(\d)|DRAW)$/.exec(code))) {
    if (!m[1]) return { market: "Winning Margin - FT", outcome: "Draw",
                        line: null, family: "winning margin" };
    return { market: "Winning Margin - FT", line: null, family: "winning margin",
             outcome: (m[1] === "H" ? "Home" : "Away") + " by " +
                      (m[2] === "3" ? "3+" : m[2]) };
  }

  if ((m = /^FIRSTGOAL_([12N])$/.exec(code))) {
    /* Their "Goal" market with the specifier 1 - the first goal - and the
       outcomes named 1 / None / 2. */
    return { market: "Goal", outcome: m[1] === "N" ? "None" : m[1], line: "1",
             family: "first goal" };
  }
  if (/^FIRSTGOAL_(FH|SH)_/.test(code)) {
    return { family: "first goal by half",
             why: "their Goal market is numbered (1st, 2nd, ...) rather than " +
                  "split by half, so there is no first-goal-in-the-half bet " +
                  "to map onto" };
  }

  if ((m = /^WINHALF_([HA])_([YN])$/.exec(code))) {
    return { market: "Either half win " + (m[1] === "H" ? "Home" : "Away") +
                     " - FT", outcome: m[2] === "Y" ? "Yes" : "No", line: null,
             family: "win either half" };
  }
  if ((m = /^HIGHHALF_(?:([HA])_)?([12E])$/.exec(code))) {
    const who = m[1] === "H" ? "Home Team" : m[1] === "A" ? "Away Team" : "Total";
    return { market: "Half More Goals - " + who, line: null,
             outcome: m[2] === "1" ? "First Half"
                    : m[2] === "2" ? "Second Half" : "Equal",
             family: "higher-scoring half" };
  }

  if (/^BOTHHALVES_/.test(code)) {
    return { family: "both halves over/under",
             why: "their both-halves markets are about a TEAM scoring in each " +
                  "half, not about each half clearing a goals line" };
  }
  if (/^(CARD_|FH_CARD)/.test(code)) {
    return { family: "team cards",
             why: "they sell total bookings and team-with-most-bookings; no " +
                  "per-team booking count appears on any deep card" };
  }
  if (/^HMC_/.test(code)) {
    return { family: "half with most cards",
             why: "their booking markets carry no half-versus-half bet" };
  }
  if (/^HALFCORNER_/.test(code)) {
    return { family: "half with most corners",
             why: "their corner markets carry no half-versus-half bet" };
  }
  if (/^PEN_/.test(code)) {
    return { family: "penalty awarded",
             why: "absent from every deep card" };
  }
  if (/^EARLY_/.test(code)) {
    return { family: "goals in the opening minutes",
             why: "their early market is a 1X2 over the first ten minutes, " +
                  "not a goals line" };
  }
  if (/^EXGOALS_/.test(code)) {
    return { family: "excluded goal count",
             why: "a bet that the total is anything BUT n, which they do not " +
                  "sell in any form" };
  }
  return null;                                     /* modelled, or unknown */
}

/* ---- the cards -------------------------------------------------------- */

async function deepest(n) {
  const board = [];
  for (let page = 0; page < 6; page++) {
    const q = JSON.stringify({ queries: [{
      query: { categories: ["2"], zones: {}, hasOdds: true },
      view: { marketTypes: ["3743"] }, skip: page * 100, take: 100 }] });
    const body = await get(API + "/v4/events/lists/by-queries?q=" +
                           encodeURIComponent(q));
    const rows = ((body.responses || [{}])[0] || {}).responses || [];
    if (!rows.length) break;
    for (const e of rows) {
      board.push({ id: e.id, name: e.name,
                   n: Number(e.totalMarketCount || 0) });
    }
    await wait(450);
  }
  board.sort((a, b) => b.n - a.n);
  return board.slice(0, n);
}

/* market name | outcome name | line -> the ids, plus every price seen, which
   is what the sign check reads. */
function index(cards) {
  const out = new Map();
  const lines = new Map();
  for (const card of cards) {
    for (const market of card.markets || []) {
      const mt = market.marketType || {};
      for (const row of market.row || []) {
        for (const price of row.prices || []) {
          const line = price.handicap != null && price.handicap !== ""
            ? String(price.handicap)
            : ((row.specifier || {}).total || (row.specifier || {}).hcp || null);
          const key = [mt.name, price.name, line].join("|");
          if (!out.has(key)) {
            out.set(key, { market: String(mt.id), outcome: String(price.typeId),
                           line, odds: [] });
          }
          out.get(key).odds.push(Number(price.odds));
          if (line != null) {
            if (!lines.has(mt.name)) lines.set(mt.name, new Set());
            lines.get(mt.name).add(String(line));
          }
        }
      }
    }
  }
  return { out, lines };
}

/* The side receiving the bigger head start must be SHORTER. Read off the
   prices rather than believed from a field.
 *
 * WITHIN ONE CARD, which is the whole of the method. The first version of
 * this compared prices pooled across eight fixtures and reported three
 * disagreements out of sixteen - Barcelona's -1.5 is shorter than a Norwegian
 * fixture's -0.5 for reasons that have nothing to do with the sign. A ladder
 * only means anything against the same two teams.
 *
 * The number is each outcome's OWN adjustment: "+0.5" on the home outcome and
 * "Away +1" on the away one both say "this side starts ahead by that much",
 * so both must shorten as it rises. */
function signIsRight(cards, market) {
  let agree = 0, against = 0;
  const num = (s) => {
    const m = /(-?\+?[\d.]+)\s*$/.exec(String(s));
    return m ? Number(m[1].replace("+", "")) : NaN;
  };
  for (const card of cards) {
    const bySide = {};
    for (const mk of card.markets || []) {
      if ((mk.marketType || {}).name !== market) continue;
      for (const row of mk.row || []) {
        for (const price of row.prices || []) {
          const n = num(price.handicap);
          if (!Number.isFinite(n) || price.name === "X") continue;
          (bySide[price.name] = bySide[price.name] || []).push(
            [n, Number(price.odds)]);
        }
      }
    }
    for (const side of Object.keys(bySide)) {
      const seen = bySide[side].sort((a, b) => a[0] - b[0]);
      for (let i = 1; i < seen.length; i++) {
        if (seen[i][1] < seen[i - 1][1]) agree++;
        else against++;
      }
    }
  }
  return { agree, against, ok: agree > 0 && against === 0 };
}

async function main() {
  const [codesPath, nCards, outPath] = process.argv.slice(2);
  if (!codesPath || !outPath) {
    console.error("usage: node tools/bpgen.js <ourcodes.json> <cards> <out.json>");
    process.exit(2);
  }
  const codes = JSON.parse(fs.readFileSync(codesPath, "utf8"));
  const picked = await deepest(Number(nCards || 8));
  console.error("deepest cards: " +
    picked.map((p) => p.n + " " + p.name).join(" | "));
  const cards = [];
  for (const p of picked) {
    cards.push(await get(API + "/v4/events/" + p.id));
    await wait(450);
  }
  const idx = index(cards);

  const sign = {};
  for (const market of ["Asian Handicap - FT", "Asian Handicap - 1H",
                        "Asian Handicap - 2H", "Handicap 1X2 - FT"]) {
    sign[market] = signIsRight(cards, market);
  }

  const map = {}, rejects = {}, skipped = {};
  for (const code of codes) {
    const p = propose(code);
    if (!p) continue;
    if (p.why) {
      (skipped[p.family] = skipped[p.family] || { why: p.why, codes: [] })
        .codes.push(code);
      continue;
    }
    const hit = idx.out.get([p.market, p.outcome, p.line].join("|"));
    if (!hit) {
      const marketSeen = [...idx.out.keys()].some(
        (k) => k.split("|")[0] === p.market);
      const why = marketSeen
        ? "market is on their card; this outcome or line is not (" +
          p.outcome + " @ " + p.line + "). Lines they do sell: " +
          [...(idx.lines.get(p.market) || [])].join(" ")
        : "no market named " + JSON.stringify(p.market) + " on any card read";
      (rejects[p.family] = rejects[p.family] || []).push({ code, why });
      continue;
    }
    if (/Handicap/.test(p.market) && sign[p.market] && !sign[p.market].ok) {
      (rejects[p.family] = rejects[p.family] || []).push(
        { code, why: "the handicap sign did not verify against the prices" });
      continue;
    }
    map[code] = [hit.market, hit.line, hit.outcome];
  }

  fs.writeFileSync(outPath, JSON.stringify(
    { map, rejects, skipped, sign,
      cards: picked.map((p) => ({ id: p.id, name: p.name, markets: p.n })),
      lines: Object.fromEntries([...idx.lines].map(
        ([k, v]) => [k, [...v].sort()])) }, null, 1));
  console.error("mapped " + Object.keys(map).length + " of " + codes.length +
    "; rejected " + Object.values(rejects).reduce((a, b) => a + b.length, 0) +
    " on the cards, refused " +
    Object.values(skipped).reduce((a, b) => a + b.codes.length, 0) +
    " on meaning");
}

main().catch((ex) => { console.error(ex); process.exit(1); });
