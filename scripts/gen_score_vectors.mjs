#!/usr/bin/env node
// gen_score_vectors.mjs - generate tests/fixtures/score_vectors.json from backlog.html.
//
// The couch score is defined by score() in steam-backlog-triage's backlog.html. This script
// pulls that function (and the helpers it calls) out of the page's source text, runs it under
// node on a fixed set of export-shaped games, and writes the expected scores. Both repos test
// against the resulting file, so the dashboard and the MCP server can't disagree about ranking.
//
// Usage:
//   node scripts/gen_score_vectors.mjs path/to/backlog.html [out.json] [--source-ref REF]
//
// Nothing here is copied by hand from the page: if score() changes, re-run this script and
// commit the new vectors in both repos.

import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import vm from "node:vm";

const args = process.argv.slice(2);
const refIdx = args.indexOf("--source-ref");
let sourceRef = null;
if (refIdx >= 0) {
  sourceRef = args[refIdx + 1];
  args.splice(refIdx, 2);
}
const [pagePath, outPath = "tests/fixtures/score_vectors.json"] = args;
if (!pagePath) {
  console.error("usage: gen_score_vectors.mjs path/to/backlog.html [out.json] [--source-ref REF]");
  process.exit(2);
}
const page = readFileSync(pagePath, "utf8");

// ------------------------------------------------------------ extract from the page
function extractStatement(src, startRe) {
  const m = src.match(startRe);
  if (!m) throw new Error(`not found in backlog.html: ${startRe}`);
  const end = src.indexOf(";\n", m.index);
  return src.slice(m.index, end + 1);
}
function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in backlog.html`);
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const pieces = [
  extractStatement(page, /const TRIED_MIN = /),
  extractStatement(page, /const playState = /),
  extractStatement(page, /const mainH = /),
  extractFunction(page, "normalize"),
  extractFunction(page, "score"),
];
const extracted = pieces.join("\n");
// score() reads the game's lane through lane(g); the vectors carry it on the game itself.
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  `${extracted}\nconst lane = (g) => g.__lane || "";\nthis.normalize = normalize; this.score = score; this.playState = playState; this.mainH = mainH;`,
  sandbox,
);

// ------------------------------------------------------------ the cases
// Raw export-shaped games (steam_backlog_export.py schema 2). Each case targets a boundary
// or a combination; "lane" is the triage lane ("" = untriaged).
const base = {
  appid: 0, name: "", playtime: 0, deckPlaytime: 0, lastPlayed: 0, deck: 0,
  hltb: null, genres: [], metacritic: null, year: null, controller: null, proton: null,
};
const H = (main, extra = null, complete = null) => ({ main, extra, complete });
const cases = [
  // play state: 0 / 1 / 119 / 120 minutes (TRIED_MIN = 120)
  ["never launched, nothing else known", {}, ""],
  ["tried 1 min", { playtime: 1 }, ""],
  ["tried 119 min (just under TRIED_MIN)", { playtime: 119 }, ""],
  ["played 120 min (exactly TRIED_MIN)", { playtime: 120 }, ""],
  // deck rating
  ["Verified, never played", { deck: 3 }, ""],
  ["Playable, never played", { deck: 2 }, ""],
  ["Unsupported, never played", { deck: 1 }, ""],
  ["invalid deck value normalizes to Unknown", { deck: 7 }, ""],
  // HLTB length boundaries (<=5, <=12, <=25, <=50, >50)
  ["main 5 h (boundary)", { deck: 3, hltb: H(5, 8, 12) }, ""],
  ["main 5.1 h", { deck: 3, hltb: H(5.1, 8, 12) }, ""],
  ["main 12 h (boundary)", { deck: 3, hltb: H(12, 18, 30) }, ""],
  ["main 12.5 h", { deck: 3, hltb: H(12.5, 18, 30) }, ""],
  ["main 25 h (boundary)", { deck: 3, hltb: H(25, 40, 60) }, ""],
  ["main 50 h (boundary)", { deck: 3, hltb: H(50, 80, 120) }, ""],
  ["main 50.5 h", { deck: 3, hltb: H(50.5, 80, 120) }, ""],
  ["main missing, extras 9 h (falls back to extras)", { deck: 3, hltb: H(null, 9, 20) }, ""],
  ["main 0, extras 30 h (0 is falsy, falls back)", { deck: 3, hltb: H(0, 30, 50) }, ""],
  ["only completionist time (treated as no length)", { deck: 3, hltb: H(null, null, 40) }, ""],
  // Metacritic: (mc - 75) / 2, skipped when missing or 0
  ["Metacritic 86", { deck: 3, metacritic: 86 }, ""],
  ["Metacritic 75 (zero term)", { deck: 3, metacritic: 75 }, ""],
  ["Metacritic 41 (negative term)", { deck: 2, metacritic: 41 }, ""],
  ["Metacritic 0 (treated as missing)", { deck: 3, metacritic: 0 }, ""],
  // Deck playtime, controller
  ["tried on the Deck", { deck: 3, playtime: 45, deckPlaytime: 45 }, ""],
  ["full controller support", { deck: 2, controller: "full" }, ""],
  ["partial controller support (no bonus)", { deck: 2, controller: "partial" }, ""],
  // lanes: only next changes the score
  ["lane next", { deck: 3, hltb: H(8, 12, 20), metacritic: 90 }, "next"],
  ["lane someday (no effect)", { deck: 3, hltb: H(8, 12, 20), metacritic: 90 }, "someday"],
  ["lane skip (no effect on score)", { deck: 3, hltb: H(8, 12, 20), metacritic: 90 }, "skip"],
  ["played 2 h+ but in next", { deck: 1, playtime: 3000, hltb: H(80) }, "next"],
  // realistic combinations
  ["design-doc example: never, Verified, 8 h, MC 86", { deck: 3, hltb: H(8, 13, 22), metacritic: 86 }, ""],
  ["everything good", { deck: 3, playtime: 30, deckPlaytime: 30, hltb: H(3.5, 4.5, 5), metacritic: 92, controller: "full" }, ""],
  ["everything bad", { deck: 1, playtime: 9000, hltb: H(120, 200, 400), metacritic: 52 }, ""],
  ["sparse: Unknown deck, no data, tried", { deck: 0, playtime: 60 }, ""],
];

const vectors = cases.map(([why, fields, lane], i) => {
  const appid = 900001 + i;
  const game = { ...base, appid, name: `Vector ${String(i + 1).padStart(2, "0")}`, ...fields };
  const g = sandbox.normalize(game);
  g.__lane = lane;
  return {
    id: i + 1,
    why,
    game,
    lane,
    expected: {
      score: sandbox.score(g),
      playState: sandbox.playState(g),
      lengthHours: sandbox.mainH(g),
    },
  };
});

const out = {
  description:
    "Couch-score parity vectors. Generated by scripts/gen_score_vectors.mjs from score() in " +
    "steam-backlog-triage's backlog.html. Each game is in export schema 2 form; implementations " +
    "normalize it as the page does, then score it with the given lane. Do not edit by hand.",
  source: {
    file: "backlog.html",
    ref: sourceRef,
    functionsSha256: createHash("sha256").update(extracted).digest("hex"),
  },
  tolerance: 1e-9,
  vectors,
};
writeFileSync(outPath, JSON.stringify(out, null, 2) + "\n");
console.log(`wrote ${vectors.length} vectors to ${outPath}`);
