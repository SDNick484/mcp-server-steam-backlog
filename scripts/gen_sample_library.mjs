#!/usr/bin/env node
// gen_sample_library.mjs - write src/steam_backlog_mcp/sim/sample_library.json from backlog.html's sample library.
//
// The page ships a 45-game sample (SAMPLE and SAMPLE_PROTON). This turns it into an export file
// (steam_backlog_export.py schema 2) with fixed timestamps, placeholder identity fields, a few
// extra games for name-resolution tests, and some controller and ProtonDB variety.
//   node scripts/gen_sample_library.mjs path/to/backlog.html [out.json]
import { readFileSync, writeFileSync } from "node:fs";
import vm from "node:vm";

const [pagePath, outPath = "src/steam_backlog_mcp/sim/sample_library.json"] = process.argv.slice(2);
const page = readFileSync(pagePath, "utf8");
const grab = (re) => {
  const m = page.match(re);
  if (!m) throw new Error(`not found: ${re}`);
  return m[0];
};
const ctx = {};
vm.createContext(ctx);
vm.runInContext(
  grab(/const SAMPLE = \[[\s\S]*?\n\];/) + "\n" + grab(/const SAMPLE_PROTON = \{[\s\S]*?\};/) +
    "\nthis.SAMPLE = SAMPLE; this.SAMPLE_PROTON = SAMPLE_PROTON;",
  ctx,
);

const NOW = 1793750400; // 2026-11-04T00:00:00Z, fixed so the file never changes
const CONTROLLER = { 620: "full", 1145360: "full", 504230: "full", 553420: "full", 1086940: "partial", 1091500: "partial" };
const games = ctx.SAMPLE.map(([appid, name, pt, dpt, days, deck, h, genres, mc, year]) => ({
  appid, name, playtime: pt, deckPlaytime: dpt,
  lastPlayed: days ? NOW - days * 86400 : 0,
  deck, hltb: h ? { main: h[0], extra: h[1], complete: h[2] } : null,
  genres, metacritic: mc, year, controller: CONTROLLER[appid] ?? null,
  proton: ctx.SAMPLE_PROTON[appid] ? { tier: ctx.SAMPLE_PROTON[appid][0], total: ctx.SAMPLE_PROTON[appid][1] } : null,
}));
// Name-resolution cases: a prefix of another title, and two games with the same name.
games.push(
  { appid: 400, name: "Portal", playtime: 0, deckPlaytime: 0, lastPlayed: 0, deck: 3, hltb: { main: 3, extra: 4, complete: 5.5 },
    genres: ["Puzzle"], metacritic: 90, year: 2007, controller: "full", proton: { tier: "platinum", total: 900 } },
  { appid: 2280, name: "DOOM", playtime: 0, deckPlaytime: 0, lastPlayed: 0, deck: 2, hltb: { main: 7, extra: 9, complete: 12 },
    genres: ["Shooter"], metacritic: null, year: 1993, controller: "partial", proton: { tier: "gold", total: 120 } },
  { appid: 379720, name: "DOOM", playtime: 0, deckPlaytime: 0, lastPlayed: 0, deck: 3, hltb: { main: 12, extra: 17, complete: 30 },
    genres: ["Shooter", "Action"], metacritic: 85, year: 2016, controller: "full", proton: { tier: "platinum", total: 2400 } },
  // No store details, no ProtonDB entry, no HLTB: everything unknown.
  { appid: 999990, name: "Mystery Prototype", playtime: 0, deckPlaytime: 0, lastPlayed: 0, deck: 0, hltb: null,
    genres: [], metacritic: null, year: null, controller: null, proton: null },
);
// The exporter writes games sorted by lower-cased name.
games.sort((a, b) => (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0));

const out = {
  schema: 2,
  source: "steam_backlog_export.py",
  exportedAt: "2026-11-02T12:00:00+00:00",
  steamid: "7656119XXXXXXXXXX",
  persona: "Sample Persona",
  games,
};
writeFileSync(outPath, JSON.stringify(out, null, 1) + "\n");
console.log(`wrote ${games.length} games to ${outPath}`);
