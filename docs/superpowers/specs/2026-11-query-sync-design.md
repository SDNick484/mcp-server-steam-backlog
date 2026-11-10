# Query sync: the MCP takes backlog_query.py from steam-backlog-triage

Status: approved 2026-11-10 (review answers in §13). Branch `feature/query-sync`.
Based on this repo at `b8e29c7` and steam-backlog-triage at `4d0b53c` (`backlog_query.py` last changed in `00b0586`).

## 1. Goal

The dashboard's rules (normalize, score, filters, relaxation, overview, name resolution, stats) exist three times:
`backlog.html`, triage's `backlog_query.py` (pinned to the page by parity tests) and this repo's `domain.py`
(pinned only by the score vectors). This change makes `backlog_query.py` the one Python copy:

- the MCP vendors it verbatim at a pinned commit and never edits it;
- `domain.py` shrinks to the MCP-specific parts: its filter shape, its relaxation names and the typed aliases the
  tool signatures use;
- `backlog_find_games` and `backlog_suggest_picks` gain the page's newer filters;
- a new `backlog_get_stats` tool exposes the same statistics Ask has;
- CI is tightened to triage's standard, and the running build can say which commit it is.

Success: every existing tool answers exactly as before (the current suite is the proof), the vendored file cannot
drift silently, and the next filter or scoring change in triage reaches the MCP by a sync, not a port.

## 2. What the code says today

**The two implementations agree.** `backlog_query.py` was started from this repo's `domain.py` at `ee288ba` and
extended. Run side by side over this repo's sample library (49 games) and triage's `stats_library.json`
(40 games) with arbitrary triage and notes, across 872 filter states (each filter alone, every pair, each with and
without `include_unknown`): score breakdowns, play state, length bucket, `in_backlog`, `passes`, unrelaxed `find`,
`picks`, all six sorts, `next_queue`, `resolve` and the overview's counts are identical. `lookup.norm` and
`lookup.suggest` match triage's inlined copies line for line apart from docstrings.

**Where they differ** (read from the code; none of these showed up in the run above because they need relaxation
or inputs the fixtures don't have):

| # | Difference | MCP `domain.py` | `backlog_query.py` | Which wins, here |
|---|---|---|---|---|
| D1 | Filter shape | `length` preset field; one `include_unknown` bool | presets folded into `hours_min/max` + `length_none`; one `*_unknown` flag per filter; `view` | MCP shape stays in `domain.Filters` (the tests build it), converted to `backlog_query.Filters` |
| D2 | Relaxation step names | `metacritic_min` | `metacritic` (min and max together) | MCP: `relaxed` is in tool output and a test asserts `["metacritic_min"]` |
| D3 | Relaxation order | 11 names | adds `reviews`, `publishers`, `developers`, `unknown_only` | MCP gains the new filters at `backlog_query`'s positions (§5) |
| D4 | Fuzzy-name tie order | duplicate names keep the requested sort | duplicate names keep library order | MCP (its `find` loop is kept) |
| D5 | Picks relaxation | server drops `query` first, then filters | `picks_out` uses `find` (fuzzy name first) | MCP (it lives in `server.py`, unchanged) |
| D6 | Overview coverage | 5 keys | adds `publishers`, `developers`, `reviews` | `backlog_query` (additive output; see §9) |
| D7 | Unknown lane value from the server | `KeyError` in `overview` | counted as `none` | `backlog_query` (more robust, otherwise identical) |
| D8 | `lane_of` | `triage.get(id, "")` | `triage.get(id) or ""` | `backlog_query` (same for every value the client produces) |
| D9 | Types | `Literal` aliases | plain `str` | MCP keeps its aliases; `domain` casts where mypy needs a `Literal` |

Picks stay ranked by score alone (contract decision D9): `backlog_query.picks` sorts by `(-score, export_index)`,
the same key as today.

**G1 check.** Unchanged, `backlog_query.py` fails this repo's lint: `ruff format --check` (whole file), five E501
lines, and four strict-mypy errors (`type-arg`, `arg-type`, two `no-any-return`). So it is excluded from ruff,
mypy and coverage rather than edited (§4). It needs Python 3.11 (`datetime.UTC`), which matches `requires-python`.

## 3. Decisions, confirmed or adjusted

| | Decision | Status |
|---|---|---|
| G1 | Vendor `backlog_query.py` verbatim through a sync script, lock and drift check | Confirmed. New `scripts/sync_query.py` + `query.lock`; `sync_shared.py` is itself a shared file, so it can't be extended |
| G2 | `domain.py` becomes a thin typed layer | Confirmed: a layer, not deleted (D1, D2, D9 need it). Existing tests pass with import-only edits, except the two additions in §8 |
| G3 | New filters on find and picks | Confirmed; relaxation positions in §5 |
| G4 | `backlog_get_stats` | Confirmed; it calls `backlog_query.stats_out`, so grouping, top 25 and Other are Ask's by construction |
| G5 | CI: SHA pins, privacy scan, `BACKLOG_REF` bump | Confirmed, plus a fix for two existing scan findings (§8.3) |
| G6 | Pin bump PR in triage after merge | Confirmed |
| G7 | Build commit via `export-subst` | Confirmed. A throwaway check showed a wheel built from `git archive` carries the substituted short commit |

## 4. Vendoring (G1)

**Files.**
- `src/steam_backlog_mcp/backlog_query.py`: byte for byte triage's file at the locked commit. Its own docstring
  already records where it started from. Nothing in this repo edits it.
- `query.lock` (TOML, repo root):
  ```toml
  # backlog_query.py comes from steam-backlog-triage at this commit, byte for byte.
  # Written by `python scripts/sync_query.py --update`; never edit the vendored file.
  repo = "SDNick484/steam-backlog-triage"
  commit = "<full SHA>"
  path = "backlog_query.py"
  dest = "src/steam_backlog_mcp/backlog_query.py"
  sha256 = "<hex of the file>"
  ```
- `scripts/sync_query.py`: standard library only, about 150 lines, the same habits as `sync_shared.py`: fetch by
  commit SHA from raw.githubusercontent.com (never a branch URL), resolve `main` with one API call (token sent to
  api.github.com only), write atomically, write the lock last.

  ```
  python scripts/sync_query.py                    write the file as of the locked commit
  python scripts/sync_query.py --update [--ref R] move the lock to triage main (or ref R), then write
  python scripts/sync_query.py --check            exit 1 if the file's hash isn't the lock's (offline), or the
                                                  locked commit's file isn't the lock's hash (network)
  python scripts/sync_query.py --check --latest   exit 1 if triage main's file differs from the vendored one
  ... --source DIR                                read a local steam-backlog-triage checkout, not GitHub
  ```
  Exit status as `sync_shared.py`: 0 in sync, 1 differs, 2 anything else. `--check` without network still does
  the offline half and says the online half was skipped.

**Lint, types, coverage.** `pyproject.toml` gains (tool sections only; dependencies, scripts and build system are
untouched):
- `[tool.ruff] extend-exclude` for the vendored file and the two privacy-scan files (§8.3), which are also copied
  unchanged;
- `[[tool.mypy.overrides]] module = "steam_backlog_mcp.backlog_query"` with `ignore_errors = true` (mypy still reads
  its signatures, so the callers are checked);
- `[tool.coverage.run] omit` gains the vendored file: a large part of it (`clean()`, the JS-coercion helpers, Ask's
  `*_out` builders other than `stats_out`) is Ask-only, is tested in triage at the pinned commit, and would otherwise
  pull total coverage under 90 %.

**Parity.** The vendored copy is pinned to the page in triage's CI at the locked commit, so the MCP inherits that
parity rather than re-running triage's node tests. The existing score-vector job stays.

## 5. domain.py after the change (G2, G3)

`domain.py` keeps:
- the `Literal` aliases (`Lane`, `LaneOrNone`, `PlayState`, `LenBucket`, `Measure`, `DeckName`, `Controller`,
  `Tier`, `Sort`) plus new `ReviewLevel`, `StatDimension`, `StatMetric`;
- `Filters`, in today's shape, gaining `metacritic_max`, `publishers`, `developers`, `reviews`; its `active()` and
  `without()`; and `to_query(*, backlog_only=False)`, which builds a `backlog_query.Filters` (preset to hours, `length="none"` to
  `length_none`, `include_unknown` to every `*_unknown` flag, publisher and developer names to `name_key` keys);
- `tag_filter()`, new `name_filter()`, `move()`;
- `passes`, `find` (today's relaxation loop and fuzzy step, over `backlog_query.selected`), `picks` (a call to
  `backlog_query.picks`); typed copies of `LANES`, `DECK_NAME` and `TIERS` where a `Literal` is needed, each
  pinned equal to `backlog_query`'s by a test; and an explicit `__all__` (strict mypy has no implicit re-export).
  `models.candidates` narrows `Resolved.reason` to its `Literal` with a cast.

Everything else is re-exported from `backlog_query`: `Game`, `Hltb`, `Proton`, `Term`, `normalize`, `tag_key`,
`TAG_ALIASES`, `play_state`, `length_hours`, `len_bucket`, `lane_of`, `is_closed`, `in_backlog`, `score_breakdown`,
`score`, `explain`, `sort_games`, `next_queue`, `overview`, `Overview`, `Resolved`, `FindResult` and the constants.
`domain.matches` goes (nothing outside `domain.py` calls it). Expected size: about 200 lines, from 606.

**New filters, matched as the page matches them.**
- `publishers`, `developers`: up to 10 names of up to 80 characters each, any spelling; matched any-of by
  `name_key` ("Devolver Digital Inc." finds "DEVOLVER DIGITAL"). A game with none listed is unknown.
- `reviews`: Steam's review levels, any of `overwhelmingly-positive`, `very-positive`, `positive`,
  `mostly-positive`, `mixed`, `mostly-negative`, `negative`, `very-negative`, `overwhelmingly-negative`, `few`
  (too few reviews for a label). No review data is unknown.
- `metacritic_max`: integer 0 to 100, like the existing `metacritic_min` (the page's dropdown values are not
  enforced here, as they aren't for the minimum).
- `include_unknown` keeps meaning every per-filter unknown toggle, now including these three. The page's
  `unknown_only` ("only games with no data for X") is not exposed; not asked for.

**Relaxation order** (weakest first), `backlog_query`'s order with metacritic split in two so today's
`metacritic_min` name survives:

`metacritic_min`, **`metacritic_max`**, **`reviews`**, `year`, `protondb`, `controller`, **`publishers`**,
**`developers`**, `tags`, `genres`, `has_note`, `length`, `deck`, `play_state`, `lane`

## 6. backlog_get_stats (G4)

READ_ONLY, title "Library statistics". Registered in `register_read_tools`.

**Arguments.** `dimension` (required): `genre`, `publisher`, `developer`, `tags`, `deck`, `protondb`, `controller`,
`length`, `lane`, `metacritic`, `reviews`, `year`, `decade` (Ask's enum, unchanged, so one vocabulary).
`metric`: `games` (default), `hours` (hours played) or `never` (share never launched); it ranks the name
dimensions (genre, publisher, developer, tags); the others keep their natural order. Then every filter
`backlog_find_games` takes, with the same types and bounds, and `backlog_only` (default false).

**Output** (`StatsOut`): `dimension`, `metric`, `total` (games in the filtered set), `known` (games with data for
this dimension), `rows` (`label`, `games`, `hours_played`, `never`, `tried`, `played`), `other` (the folded rest:
the same fields plus `values`, the number of rows folded; null when nothing folded), `unknown` (the games with no
data for this dimension, as a row; null when none), `detail`.

**Behavior.** One call to `backlog_query.stats_out` over the filtered games: top 25 rows plus Other for the name
dimensions and decades, the most recent 25 years for `year`. A game with several genres, publishers, developers or
tags counts once under each, so rows can sum past `total`; the description says so. No relaxation: an empty
filtered set returns `total: 0` and a detail line saying the filters matched nothing.

**Description** with `<use_case>` (counts and breakdowns: "how many Devolver games do I own", "which genres have I
played most", "how much of my backlog is Verified"), `<important_notes>` (multi-valued dimensions; unknown is its
own row and never zero; a publisher row's label can be passed straight to `backlog_find_games(publishers=[...])`)
and `<example>backlog_get_stats(dimension="publisher", backlog_only=true)</example>`.

**Server instructions** gain one sentence: for counts and breakdowns of the library, use `backlog_get_stats`
rather than paging through `backlog_find_games`.

## 7. The build commit (G7)

- `src/steam_backlog_mcp/_build.py` holds `COMMIT = "$Format:%h$"` and `commit()`, which returns `"unknown"` when
  the value still starts with `$`. The check must not contain the placeholder text itself: `export-subst`
  replaces every occurrence in the file.
- `.gitattributes`: `src/steam_backlog_mcp/_build.py export-subst`.
- `mcp-server-steam-backlog --version` prints `mcp-server-steam-backlog 0.1.0 (abc1234)`; `doctor`'s versions line
  shows the same, and `doctor --json` adds `"build"`.
- A wheel built from `git archive` reports the commit. A `pip install` from a checkout or a `git+https` URL (what
  `deploy/alpine/install.sh` does by hand, and the Docker image) reports `unknown`. Stamping those too would mean
  changing the installer; not in this PR.

## 8. CI and tests (G5, tests)

### 8.1 Workflow

- Every `uses:` pinned to a full SHA with a version comment, the same pins as triage's `ci.yml`: checkout v7.0.1,
  setup-python v7.1.0, setup-node v7.1.0. Dependabot's existing github-actions entry keeps them current.
- `BACKLOG_REF` moves to triage `main` (`4d0b53c3c11d88e6afb5b513572d32d78ca9f24c`).
- New `query-drift` job: `sync_query.py --check` on every run; `--check --latest` on the weekly schedule and
  manual runs, like `shared-drift`.
- New `privacy-scan` job, copied from triage's with its `PRIVACY_DENYLIST` handling (runs the pattern checks and
  warns when the secret isn't available).

### 8.2 Tests

- **Unchanged and green:** the whole current suite. `test_domain.py` changes imports at most.
- **Two additions to existing files** (the only edits beyond imports, and both follow from G4): `ALL_TOOLS` in
  `test_tools.py` gains `backlog_get_stats`, since that test asserts the exact tool set; `CALLS` in
  `test_privacy.py` gains `backlog_get_stats` calls over the publisher and tags dimensions, so the privacy grep
  covers the new tool's output.
- **New:** `test_domain.py`-style tests for the three new filters and their unknowns, `name_filter` spellings, the
  new relaxation positions, and `to_query()` round trips; tool tests through the in-memory client for the new
  arguments and `backlog_get_stats` (every dimension, both multi-valued counting and unknown rows, folding past 25,
  bad arguments rejected by the schema); `test_query_sync.py` (the vendored file's SHA-256 equals `query.lock`, and
  the sync script's `--check` against a local `--source`); `test_build.py` (a wheel built from `git archive HEAD`
  carries `git rev-parse --short HEAD`; skipped outside a git checkout); contract tests against the real
  `backlog_server.py` at the new `BACKLOG_REF`.

### 8.3 Privacy scan

`.github/scripts/privacy_scan.py` and `tests/test_privacy_scan.py` are copied from triage at `0003503`, unchanged,
with that commit noted in this spec and in `CLAUDE.md` (a header line would be an edit). Run on this repo today
the scanner reports two findings, both rule `steamid`, both in `tests/test_privacy.py` (lines 7 and 28): the test's
deliberately unassignable SteamID-shaped constant and the docstring that explains it. Fix: build the same value
from two string pieces at run time and reword the docstring, which is what triage's tests do. The value, and so
what the privacy test proves, doesn't change. (The alternative is a `privacy-scan: allow` marker on both lines.)

## 9. What users of the tools will notice

- `backlog_find_games` and `backlog_suggest_picks` accept `publishers`, `developers`, `reviews`, `metacritic_max`.
- `backlog_get_stats` exists.
- `backlog_get_game`'s `game` gains `publishers`, `developers` (lists; `[]` = unknown) and `reviews`
  (`desc`, `pct`, `total`; null = unknown).
- `backlog_get_overview`'s `coverage` gains `publishers`, `developers` and `reviews` (additive; the model is an open
  `dict[str, float]`).
- Nothing else in any existing tool's output changes.

## 10. Sample library and docs

- `scripts/gen_sample_library.mjs` also reads the page's `SAMPLE_META`: games it lists get `publishers`,
  `developers` and `reviews` as the page's `sampleGames()` builds them; of the extra name-resolution games, one gets
  empty lists and `"reviews": null` (unknown), the rest none of the keys (an older export). Every existing field
  keeps its value, so existing tests see the same 49 games.
- `docs/backlog-api-contract.md` §6.8, "Publishers, developers and reviews (not part of the score)": the export
  fields, `name_key`, the review levels. API stays 2; no route changes.
- `README.md`: the tool table, the new filters. `CLAUDE.md`: the vendored file and its sync, the lint and
  coverage exclusions and why, the privacy scan's source commit, and that `BACKLOG_REF` and `query.lock` usually
  move together.

## 11. Order of work (detail goes in the plan)

1. Sample library regenerated with the new fields.
2. `sync_query.py`, `query.lock`, the vendored file, lint/type/coverage exclusions, `test_query_sync.py`.
3. `domain.py` slimmed to the layer over `backlog_query`; the existing suite green with import-only edits.
4. The new filters (domain and both tools) and their tests.
5. `backlog_get_stats`, its tests, the instructions sentence, the `ALL_TOOLS` and `CALLS` additions.
6. `_build.py`, `.gitattributes`, `--version` and `doctor`, `test_build.py`.
7. CI: pins, `BACKLOG_REF`, `query-drift`, privacy scan and the `test_privacy.py` fix.
8. Contract §6.8, README, CLAUDE.md.

Then G6, after merge: the one-line `MCP_PINNED_REF` PR in steam-backlog-triage.

## 12. Not in this PR

- `unknown_only`, the page's views beyond `backlog_only`, and the Ask-only builders.
- Stamping the commit on installs that don't go through `git archive`.
- Running triage's node parity tests here.

## 13. Review decisions (2026-11-10)

1. **Test edits: yes.** Adding `backlog_get_stats` to `ALL_TOOLS` and new entries to the privacy test's `CALLS`
   are the only edits to existing tests beyond imports; no existing assertion changes.
2. **`backlog_get_game` output: add `publishers`, `developers` and `reviews`** to `GameDetail` (§9), so a game's
   publisher is visible to a user who filters by it. Additive; no existing field changes.
3. **Privacy-scan findings: build the constant at run time** (§8.3).
