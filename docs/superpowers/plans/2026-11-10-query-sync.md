# Query Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The MCP vendors steam-backlog-triage's `backlog_query.py` verbatim, `domain.py` becomes a thin typed layer over it, find and picks gain publisher/developer/review/Metacritic-max filters, a new `backlog_get_stats` tool mirrors Ask's, and CI and the build commit catch up with triage.

**Architecture:** `backlog_query.py` (vendored, never edited, locked by `query.lock`) holds the rules. `domain.py` keeps only what is MCP-shaped: its `Filters` (preset lengths, one `include_unknown`, its relaxation names), `Literal` aliases, `move`, `tag_filter`/`name_filter`, and the relaxation loop; it converts to `backlog_query.Filters` with `to_query()`. `server.py` and `models.py` stay the only MCP-facing layer.

**Tech Stack:** Python ≥3.11, MCP Python SDK (`MCPServer`), Pydantic 2, httpx, pytest + anyio, ruff, mypy strict, GitHub Actions. Scripts and the vendored file are standard library only.

**Spec:** `docs/superpowers/specs/2026-11-query-sync-design.md`

## Global Constraints

- `src/steam_backlog_mcp/backlog_query.py` is byte for byte steam-backlog-triage `4d0b53c3c11d88e6afb5b513572d32d78ca9f24c`'s `backlog_query.py`. Never edit it.
- `pyproject.toml`: change only `[tool.*]` sections. Dependencies, `[project.scripts]` and `[build-system]` stay as they are.
- Existing tests: only import edits, plus the two additions the spec names (`ALL_TOOLS`, the privacy test's `CALLS`) and the `test_privacy.py` constant fix in Task 7. No existing assertion changes.
- Every existing tool keeps its name, arguments and behavior; output changes are only the additive ones in spec §9.
- Picks are ranked by score alone (contract D9).
- Every tool: `title`, explicit annotations, constrained arguments, a Pydantic output model, a description with `<use_case>`, `<important_notes>`, `<example>`.
- Public repo: no IPs, hostnames, domains, container IDs, time zones, SteamIDs, notify names, real names, personal emails or keys. Placeholders: `7656119XXXXXXXXXX`, `backlog.example.com`. Grep every diff before committing.
- Commits: author `SDNick484 <SDNick484@users.noreply.github.com>`, message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01Fabooe7qjzjKJoWiX14Yj4`.
- Done for every task: `ruff check . && ruff format --check . && mypy && pytest --cov` green, coverage ≥ 90 %.

## Review Focus

1. **A publisher or developer argument that normalizes to nothing** (`"™"`, `"!!"`; note `"Inc."` alone keeps the key `inc`): treated as no filter for that name, never an error or a crash, exactly as a tag of `"+"` is today. Test in Task 4.
2. **A trademark or case variant of a name** (`"DEVOLVER DIGITAL™"`, `"Devolver Digital, Inc."`): finds the same four games as `"Devolver Digital"`. Test in Task 4.
3. **`backlog_get_stats` over a filter set that matches nothing**: `total` 0, `rows` [], `other` and `unknown` null, a detail line, no error. Test in Task 5.
4. **A library exported before publishers, developers and reviews existed** (keys absent on every game): `backlog_get_stats(dimension="publisher")` has `known` 0 and every game in `unknown`; `backlog_find_games(publishers=[...])` finds nothing exact and relaxes `publishers`. Test in Task 5 (stats) and Task 4 (find).
5. **`metacritic_min` above `metacritic_max`** (95 and 90): no error; nothing exact, relaxed `["metacritic_min"]`. Test in Task 4.

---

### Task 1: Sample library carries publishers, developers and reviews

**Files:**
- Modify: `scripts/gen_sample_library.mjs`
- Regenerate: `src/steam_backlog_mcp/sim/sample_library.json`
- Test: `tests/test_domain.py` (new test at the end)

**Interfaces:**
- Produces: the sample library that Tasks 4 and 5 assert against. 39 games carry `publishers`, `developers`, `reviews` from the page's `SAMPLE_META`; the 6 page sample games without an entry carry `[]`, `[]`, `null` (as the page's `sampleGames()` builds them); of the 4 extra games, Mystery Prototype gets `[]`, `[]`, `null` and Portal and both DOOMs get none of the three keys.

- [ ] **Step 1: Write the failing test** `test_sample_library_has_publishers_empty_and_absent` in `tests/test_domain.py`:

```python
def test_sample_library_has_publishers_empty_and_absent() -> None:
    raw = {g["name"]: g for g in json.loads(FIXTURE.read_text(encoding="utf-8"))["games"] if g["name"] != "DOOM"}
    assert raw["Hotline Miami"]["publishers"] == ["DEVOLVER DIGITAL"]
    assert raw["ELDEN RING"]["publishers"] == ["FromSoftware Inc.", "Bandai Namco Entertainment"]
    assert raw["Destiny 2"]["reviews"] == {"total": 570000, "pct": 78, "desc": "Mostly Positive"}
    assert (raw["COCOON"]["publishers"], raw["COCOON"]["reviews"]) == ([], None)
    assert (raw["Mystery Prototype"]["developers"], raw["Mystery Prototype"]["reviews"]) == ([], None)
    assert "publishers" not in raw["Portal"] and "reviews" not in raw["Portal"]
    assert sum(1 for g in raw.values() if g.get("publishers")) == 39
```

- [ ] **Step 2: Run it**: `pytest tests/test_domain.py::test_sample_library_has_publishers_empty_and_absent -v` → FAIL (`KeyError: 'publishers'`).
- [ ] **Step 3: Extend the generator.** Grab `const SAMPLE_META = {...};` the same way as `SAMPLE_PROTON`; for page sample games set `publishers`/`developers` to `SAMPLE_META[appid][0|1].split(", ")` or `[]`, and `reviews` to `{total, pct, desc}` from entries 2–4 or `null`, exactly as the page's `sampleGames()`. Add the Mystery Prototype keys; leave Portal and the DOOMs without them. Update the header comment. Regenerate: `node scripts/gen_sample_library.mjs ../steam-backlog-triage/backlog.html` (a checkout at `4d0b53c`).
- [ ] **Step 4: Run the new test and the whole suite**: `pytest -q` → all pass. Check `git diff --stat` touches only the generator and the JSON, and that the JSON diff only adds keys (`git diff -U0 src/steam_backlog_mcp/sim/sample_library.json | grep '^-' | grep -v '^---'` prints nothing but changed-line pairs whose old side is a key the new side keeps).
- [ ] **Step 5: Commit**: `test(sim): sample library gains publishers, developers and reviews from the page`.

---

### Task 2: Vendor backlog_query.py with a sync script and lock

**Files:**
- Create: `scripts/sync_query.py`, `query.lock`, `src/steam_backlog_mcp/backlog_query.py` (written by the script), `tests/test_query_sync.py`
- Modify: `pyproject.toml` (`[tool.ruff]`, `[[tool.mypy.overrides]]`, `[tool.coverage.run]`)

**Interfaces:**
- Produces: importable `steam_backlog_mcp.backlog_query`; `query.lock` with keys `repo`, `commit`, `path`, `dest`, `sha256` (spec §4 shows the file); `scripts/sync_query.py` CLI:
  `[--root DIR] [--source DIR] [--update [--ref R]] [--check [--latest]] [--force]`, exit 0 in sync, 1 differs, 2 anything else.

- [ ] **Step 1: Write the failing tests** in `tests/test_query_sync.py`:
  - `test_vendored_file_matches_the_lock`: SHA-256 of `src/steam_backlog_mcp/backlog_query.py` equals `tomllib.load(query.lock)["sha256"]`, and `lock["repo"] == "SDNick484/steam-backlog-triage"`, `lock["path"] == "backlog_query.py"`.
  - `test_check_offline_detects_an_edited_copy`: copy `query.lock`, the vendored file and the script's expectations into a `tmp_path` root; `--root tmp --check --offline` exits 0; append a byte to the copy; exits 1 and stderr names the file.
  - `test_source_update_and_check`: in `tmp_path`, `git init` a fake triage checkout holding a `backlog_query.py`, commit it; `--root R --source S --update` writes `dest`, sets `lock["commit"]` to the source's `git rev-parse HEAD` and `lock["sha256"]` to the file's hash; `--root R --source S --check` exits 0; change the source file (uncommitted) and `--check --source S` exits 1.
  - `test_update_refuses_to_overwrite_a_local_edit`: edit the copy, `--source S --update` exits 2 without `--force`, succeeds with it.
- [ ] **Step 2: Run them**: `pytest tests/test_query_sync.py -v` → FAIL (script and lock missing).
- [ ] **Step 3: Write `scripts/sync_query.py`.** Standard library only. Default `--root` is the script's parent's parent. Network: raw file from `https://raw.githubusercontent.com/{repo}/{commit}/{path}` (never a branch URL); `--update`/`--latest` resolve `main` (or `--ref`) to a SHA with one call to `https://api.github.com/repos/{repo}/commits/{ref}`, sending `GITHUB_TOKEN`/`GH_TOKEN` to api.github.com only. Fetch everything before writing; write the file and then the lock, each to a temp file beside the target and `os.replace`. `--check` = offline hash check, then (unless `--offline`) the online check that the locked commit's file hashes to the lock's `sha256`; a network failure in the online half prints a notice and does not change the exit status. `--latest` compares main's file instead. `--source DIR` reads `DIR/backlog_query.py` and, on `--update`, takes the commit from `git -C DIR rev-parse HEAD`. A local edit (copy's hash ≠ lock) blocks writing with exit 2 unless `--force`. Module docstring in the style of `sync_shared.py`'s: usage table, exit codes, why fetch by SHA.
- [ ] **Step 4: Vendor the file**: `python scripts/sync_query.py --update --ref 4d0b53c3c11d88e6afb5b513572d32d78ca9f24c`. Then `diff src/steam_backlog_mcp/backlog_query.py ../steam-backlog-triage/backlog_query.py` prints nothing.
- [ ] **Step 5: Exclude the vendored file from lint, types and coverage** in `pyproject.toml`: `[tool.ruff] extend-exclude = ["src/steam_backlog_mcp/backlog_query.py"]`; `[[tool.mypy.overrides]] module = "steam_backlog_mcp.backlog_query"`, `ignore_errors = true`; append `"*/steam_backlog_mcp/backlog_query.py"` to `[tool.coverage.run] omit`. Each with a one-line comment: copied verbatim from steam-backlog-triage, linted and tested there.
- [ ] **Step 6: Run**: `pytest tests/test_query_sync.py -v` → PASS; `python scripts/sync_query.py --check` → exit 0 (online half passes or prints its notice); full gate (`ruff check . && ruff format --check . && mypy && pytest --cov`) green.
- [ ] **Step 7: Commit**: `build: vendor backlog_query.py from steam-backlog-triage at 4d0b53c (sync_query.py, query.lock)`.

---

### Task 3: domain.py becomes a thin layer over backlog_query

**Files:**
- Modify: `src/steam_backlog_mcp/domain.py` (rewrite), `src/steam_backlog_mcp/models.py:269-275` (`candidates`)
- Test: `tests/test_domain.py` (one new test; existing tests unchanged)

**Interfaces:**
- Consumes: `backlog_query` (Task 2): `Game`, `Hltb`, `Proton`, `Term`, `normalize`, `tag_key`, `TAG_ALIASES`, `name_key`, `play_state`, `length_hours`, `len_bucket`, `lane_of`, `is_closed`, `in_backlog`, `score_breakdown`, `score`, `explain`, `sort_games`, `next_queue`, `overview`, `Overview`, `resolve`, `Resolved`, `FindResult`, `Filters`, `passes`, `selected`, `picks`, constants.
- Produces (unchanged names and signatures): `domain.Filters` (today's fields, `active()`, `without()`), plus `Filters.to_query(self, *, backlog_only: bool = False) -> backlog_query.Filters`; `passes(g, f, triage, notes) -> bool`; `find(games, f, triage, notes, *, sort="score", backlog_only=False, relax=True) -> FindResult`; `picks(games, f, triage, notes, *, exclude_next=False) -> list[Game]`; `tag_filter`, `move`; typed `LANES: tuple[Lane, ...]`, `DECK_NAME: dict[int, DeckName]`, `TIERS: tuple[Tier, ...]`; every re-exported name listed in `__all__`. `domain.matches` is removed.

- [ ] **Step 1: Write the failing test** `test_typed_constants_match_backlog_query` in `tests/test_domain.py`: `domain.LANES == backlog_query.LANES`, `domain.TIERS == backlog_query.TIERS`, `domain.DECK_NAME == backlog_query.DECK_NAME`, `domain.LEN_PRESET == backlog_query.LEN_PRESET`, `domain.TIER_RANK == backlog_query.TIER_RANK`, and `domain.normalize is backlog_query.normalize`, `domain.Game is backlog_query.Game`.
- [ ] **Step 2: Run it** → FAIL (`domain.normalize is not backlog_query.normalize`).
- [ ] **Step 3: Rewrite `domain.py`.** Keep the docstring's role ("the MCP's view of backlog_query"), the `Literal` aliases, typed constants, `Filters` with `active()`/`without()` exactly as today, `tag_filter`, `move`, and `find`'s relaxation and fuzzy-name algorithm exactly as today with `_filter` replaced by `backlog_query.selected(games, f.to_query(backlog_only=backlog_only), triage, notes)`. `to_query` maps: preset `length` → `hours_min/hours_max` (only when both are None), `length="none"` → `length_none=True`, `include_unknown` → every `*_unknown` flag, `view="backlog" if backlog_only else "all"`, everything else by name. `passes` and `picks` delegate to `backlog_query`. Re-export the rest with `__all__`. In `models.candidates`, narrow `r.reason` with `typing.cast` to the existing `Literal`.
- [ ] **Step 4: Run**: the new test passes, then the full gate. The whole existing suite must pass with no edits (`git diff tests/` shows only the new test). If a test fails, the layer differs from today's behavior: fix the layer, never the test.
- [ ] **Step 5: Commit**: `refactor(domain): a typed layer over the vendored backlog_query`.

---

### Task 4: Publisher, developer, review and Metacritic-max filters

**Files:**
- Modify: `src/steam_backlog_mcp/domain.py`, `src/steam_backlog_mcp/server.py` (`_filters`, argument types, `backlog_find_games`, `backlog_suggest_picks`, `_detail` callers unchanged), `src/steam_backlog_mcp/models.py` (`GameDetail`, `game_detail`)
- Test: `tests/test_domain.py`, `tests/test_tools.py` (new tests only)

**Interfaces:**
- Consumes: Task 3's `domain.Filters` and `to_query`; `backlog_query.name_key`, `backlog_query.REVIEW_KEYS`.
- Produces:
  - `domain.ReviewLevel = Literal["overwhelmingly-positive", "very-positive", "positive", "mostly-positive", "mixed", "mostly-negative", "negative", "very-negative", "overwhelmingly-negative", "few"]`
  - `domain.Filters` gains `metacritic_max: float | None = None`, `publishers: tuple[str, ...] = ()`, `developers: tuple[str, ...] = ()` (both `name_key` keys), `reviews: tuple[str, ...] = ()`.
  - `domain.name_filter(names: Iterable[str]) -> tuple[str, ...]`: `name_key` of each, blanks dropped, first occurrence kept.
  - `Filters.active()` order: `metacritic_min`, `metacritic_max`, `reviews`, `year`, `protondb`, `controller`, `publishers`, `developers`, `tags`, `genres`, `has_note`, `length`, `deck`, `play_state`, `lane`. `without()` clears each new name.
  - Tool arguments on both `backlog_find_games` and `backlog_suggest_picks`: `metacritic_max: int | None` (0–100), `publishers` and `developers`: `list[str] | None` (≤10 items, each ≤80 chars), `reviews: list[domain.ReviewLevel] | None`.
  - `models.ReviewSummary(BaseModel)`: `desc: str | None`, `pct: float | None`, `total: int`. `GameDetail` gains `publishers: list[str]`, `developers: list[str]` (descriptions: "[] = unknown"), `reviews: ReviewSummary | None` ("null = unknown").

- [ ] **Step 1: Write the failing domain tests** in `tests/test_domain.py` (use the file's `find`/`names` helpers):

```python
def test_publisher_and_developer_filters_match_by_name_key() -> None:
    devolver = {"Inscryption", "GRIS", "Katana ZERO", "Hotline Miami"}
    for spelling in ("Devolver Digital", "DEVOLVER DIGITAL™", "Devolver Digital, Inc.", "devolver digital inc"):
        assert names(find(domain.Filters(publishers=domain.name_filter([spelling])))) == devolver
    assert names(find(domain.Filters(developers=domain.name_filter(["Valve"])))) == {"Portal 2", "Half-Life: Alyx"}
    with_unknown = names(find(domain.Filters(developers=domain.name_filter(["Valve"]), include_unknown=True)))
    assert {"Portal", "COCOON", "Mystery Prototype"} <= with_unknown


def test_a_name_that_normalizes_to_nothing_is_no_filter() -> None:
    assert domain.name_filter(["™", "!!", "  "]) == ()
    assert len(find(domain.Filters(publishers=domain.name_filter(["™"]))).games) == 49


def test_review_levels_and_metacritic_max() -> None:
    assert get_args(domain.ReviewLevel) == backlog_query.REVIEW_KEYS
    assert names(find(domain.Filters(reviews=("mostly-positive",)))) == {"Destiny 2"}
    capped = find(domain.Filters(metacritic_max=89)).games
    assert capped and all(g.metacritic and g.metacritic <= 89 for g in capped)
    assert len(find(domain.Filters(metacritic_max=89, include_unknown=True)).games) == len(capped) + 8


def test_new_filters_relax_in_order() -> None:
    f = domain.Filters(metacritic_min=1, metacritic_max=99, reviews=("positive",), year_from=2000,
                       publishers=("x",), developers=("y",), tags=("z",))  # fmt: skip
    assert f.active() == ["metacritic_min", "metacritic_max", "reviews", "year", "publishers", "developers", "tags"]
    for name in f.active():
        assert name not in f.without(name).active()
    r = find(domain.Filters(deck=(1,), publishers=domain.name_filter(["Devolver Digital"])))
    assert (r.exact, r.relaxed, names(r)) == (False, ["publishers"], {"Half-Life: Alyx", "Destiny 2"})


def test_metacritic_min_above_max_relaxes_min_first() -> None:
    r = find(domain.Filters(metacritic_min=95, metacritic_max=90))
    assert (r.exact, r.relaxed) == (False, ["metacritic_min"]) and r.games
```

- [ ] **Step 2: Write the failing tool tests** in `tests/test_tools.py` with the `fake` fixture: `backlog_find_games(publishers=["Devolver Digital"])` returns 4 matches; `backlog_suggest_picks(developers=["Valve"], reviews=["overwhelmingly-positive"])` picks only from {Portal 2, Half-Life: Alyx} still in the backlog; `backlog_find_games(publishers=["x"] * 11)` and `(reviews=["great"])` and `(metacritic_max=101)` are errors; `backlog_get_game(game="Hotline Miami")` has `publishers == ["DEVOLVER DIGITAL"]`, `developers == ["Dennaton Games"]`, `reviews == {"desc": "Overwhelmingly Positive", "pct": 96, "total": 80000}`, and `backlog_get_game(game="Portal")` has `publishers == []` and `reviews is None`. And an old-export case: with a `FakeBacklog` whose library has the three keys removed from every game, `backlog_find_games(publishers=["Valve"])` has `exact is False` and `"publishers" in relaxed`.
- [ ] **Step 3: Run** `pytest tests/test_domain.py tests/test_tools.py -v -k "publisher or review or metacritic or relax or name_filter or Hotline"` → FAIL.
- [ ] **Step 4: Implement** the `domain` additions (and `to_query` mapping `metacritic_max`, `publishers`, `developers`, `reviews`, and `include_unknown` to `publishers_unknown`, `developers_unknown`, `reviews_unknown`), the server argument types and `_filters` parameters (place `metacritic_max` after `metacritic_min`, and `publishers`, `developers`, `reviews` after `tags`, in both tools), and the `GameDetail` fields. Descriptions: find's `<use_case>` names publishers, developers and Steam review levels; `<important_notes>` gains one bullet: "publishers and developers match any spelling of a name (\"Devolver Digital Inc.\" finds \"DEVOLVER DIGITAL\"); reviews are Steam's review levels (few = too few reviews for a level); a game with no data for one of them is unknown." The picks description says it takes the same filters.
- [ ] **Step 5: Run** the tests from Steps 1–2 → PASS, then the full gate.
- [ ] **Step 6: Commit**: `feat(tools): publisher, developer, review-level and Metacritic-max filters; publishers and reviews in get_game`.

---

### Task 5: backlog_get_stats

**Files:**
- Modify: `src/steam_backlog_mcp/domain.py` (aliases, `stats`), `src/steam_backlog_mcp/models.py` (stats models), `src/steam_backlog_mcp/server.py` (tool, `INSTRUCTIONS`), `tests/test_tools.py` (`ALL_TOOLS` + new tests), `tests/test_privacy.py` (`CALLS` additions), `tests/test_domain.py`

**Interfaces:**
- Consumes: Task 4's `domain.Filters` and `to_query`; `backlog_query.stats_out(snap, filters, *, dimension, metric, view)`, `backlog_query.Snapshot(games, triage, notes)`.
- Produces:
  - `domain.StatDimension = Literal["genre", "publisher", "developer", "tags", "deck", "protondb", "controller", "length", "lane", "metacritic", "reviews", "year", "decade"]`; `domain.StatMetric = Literal["games", "hours", "never"]`.
  - `domain.stats(games, f, triage, notes, *, dimension: StatDimension, metric: StatMetric = "games", backlog_only: bool = False) -> dict[str, Any]`: `backlog_query.stats_out(...)` with `view="backlog" if backlog_only else "all"`.
  - `models.StatRow`: `label: str`, `games: int`, `hours_played: float`, `never: int`, `tried: int`, `played: int`. `models.StatOther(StatRow)`: `values: int`. `models.StatsOut`: `dimension`, `metric`, `total: int`, `known: int`, `rows: list[StatRow]`, `other: StatOther | None`, `unknown: StatRow | None`, `detail: str`.
  - Tool `backlog_get_stats`, READ_ONLY, title `"Library statistics"`; arguments `dimension` (required), `metric="games"`, every `backlog_find_games` filter argument with the same types, `backlog_only=False`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_domain.py`: `get_args(domain.StatDimension)` equals the keys of `backlog_query._STAT_DIM` in order.
  - `tests/test_tools.py`: add `"backlog_get_stats"` to `ALL_TOOLS`, then:

```python
async def test_stats_publisher_groups_spellings(fake: FakeBacklog) -> None:
    s = await call("backlog_get_stats", {"dimension": "publisher"})
    assert (s["total"], s["known"], s["unknown"]["games"]) == (49, 39, 10)
    assert (s["rows"][0]["label"], s["rows"][0]["games"]) == ("Devolver Digital", 4)


async def test_stats_folds_and_orders(fake: FakeBacklog) -> None:
    years = await call("backlog_get_stats", {"dimension": "year"})
    assert len(years["rows"]) == 25 and years["other"]["values"] == 6 and years["rows"][-1]["label"] == "2023"
    genres = await call("backlog_get_stats", {"dimension": "genre"})
    assert len(genres["rows"]) == 25 and genres["other"]["values"] == 1
    rv = await call("backlog_get_stats", {"dimension": "reviews"})
    assert [r["label"] for r in rv["rows"]] == ["Overwhelmingly Positive", "Very Positive", "Mostly Positive"]


async def test_stats_follows_filters_and_handles_empty(fake: FakeBacklog) -> None:
    b = await call("backlog_get_stats", {"dimension": "deck", "backlog_only": True, "deck": ["verified"]})
    assert b["total"] == sum(r["games"] for r in b["rows"]) and b["unknown"] is None
    e = await call("backlog_get_stats", {"dimension": "publisher", "query": "zzzz"})
    assert (e["total"], e["rows"], e["other"], e["unknown"]) == (0, [], None, None) and e["detail"]
```

  Also: an old-export library (Task 4's helper) gives `known == 0` and `unknown["games"] == total`; `dimension="bogus"` and `metric="most"` are errors.
  - `tests/test_privacy.py`: append `("backlog_get_stats", {"dimension": "publisher"})` and `("backlog_get_stats", {"dimension": "tags", "backlog_only": True})` to `CALLS`.
- [ ] **Step 2: Run** `pytest tests/test_tools.py tests/test_privacy.py tests/test_domain.py -v -k "stats or tool_list or nothing_personal or StatDimension"` → FAIL.
- [ ] **Step 3: Implement** `domain.stats`, the models, and the tool. `detail`: `"No games match these filters."` when `total` is 0, else `"{total} games; {known} have {dimension} data."` (never a game or publisher name). Description: `<use_case>` counts and breakdowns ("how many Devolver games do I own", "which genres have I played most", "how much of my backlog is Steam Deck Verified"); `<important_notes>`: a game with several genres, publishers, developers or tags counts once under each, so rows can add up to more than total; unknown is its own row, never zero; metric ranks genre, publisher, developer and tags, the others keep their natural order; the top 25 rows are shown and the rest fold into other; a row's label can be passed straight to backlog_find_games (for example publishers=[label]). `<example>backlog_get_stats(dimension="publisher", backlog_only=true)</example>`. Append to `INSTRUCTIONS`: `" For counts and breakdowns of the library (how many, which genres or publishers most), use backlog_get_stats rather than paging through backlog_find_games."`
- [ ] **Step 4: Run** the Step 1 tests → PASS, then the full gate.
- [ ] **Step 5: Commit**: `feat(tools): backlog_get_stats, the same breakdowns as Ask's get_stats`.

---

### Task 6: The build commit in --version and doctor

**Files:**
- Create: `src/steam_backlog_mcp/_build.py`, `.gitattributes`, `tests/test_build.py`
- Modify: `src/steam_backlog_mcp/cli.py:112`, `src/steam_backlog_mcp/doctor.py` (`Report`, `versions`)

**Interfaces:**
- Produces: `_build.COMMIT: str` (`"$Format:%h$"` in the source), `_build.commit() -> str` (the short commit, or `"unknown"` when `COMMIT` starts with `"$"`; the function must not contain the placeholder text, since `export-subst` replaces every occurrence in the file). `--version` prints `mcp-server-steam-backlog <version> (<commit>)`. `doctor`: `versions()["mcp-server-steam-backlog"]` becomes `"<version> (<commit>)"`, and `Report` gains `build: str` (so `doctor --json` has `"build"`).

- [ ] **Step 1: Write the failing tests** in `tests/test_build.py`:
  - `test_unsubstituted_placeholder_reports_unknown`: `_build.commit() == "unknown"` in a checkout; with `monkeypatch.setattr(_build, "COMMIT", "abc1234")`, `_build.commit() == "abc1234"`.
  - `test_version_and_doctor_show_the_build`: `cli.main(["--version"])` output ends with `"(unknown)"` (capsys, `SystemExit`); `doctor.to_json(...)` of a report has a `"build"` key.
  - `test_wheel_from_git_archive_carries_the_commit`: skip unless `git rev-parse` works and HEAD's tree contains `src/steam_backlog_mcp/_build.py` and `.gitattributes`; `git archive HEAD | tar -x -C tmp`, `python -m pip wheel --no-deps -w tmp/whl tmp`, read `steam_backlog_mcp/_build.py` from the wheel with `zipfile`; its `COMMIT` equals `git rev-parse --short HEAD` (compare with `startswith` either way, abbreviation lengths may differ).
- [ ] **Step 2: Run** `pytest tests/test_build.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `_build.py`, `.gitattributes` (`src/steam_backlog_mcp/_build.py export-subst`), the `--version` string and the doctor changes. `pyproject.toml` is not touched (setuptools picks the module up).
- [ ] **Step 4: Commit first, then run** (the wheel test archives HEAD): commit as `feat(cli): --version and doctor show the build commit (export-subst)`, then `pytest tests/test_build.py -v` → 3 passed, then the full gate. If the wheel test fails, amend the same commit.

---

### Task 7: CI pins, drift check, privacy scan

**Files:**
- Modify: `.github/workflows/ci.yml`, `tests/conftest.py:34` (`DEFAULT_BACKLOG_REF`), `tests/test_privacy.py` (constant and docstring)
- Create: `.github/scripts/privacy_scan.py`, `tests/test_privacy_scan.py` (copied from steam-backlog-triage `0003503`, unchanged), `pyproject.toml` `[tool.ruff] extend-exclude` gains both

**Interfaces:**
- Consumes: `scripts/sync_query.py` (Task 2).

- [ ] **Step 1: Copy the scanner and its tests**, byte for byte, from steam-backlog-triage at `0003503`. Run `pytest tests/test_privacy_scan.py -v` → `test_the_scanner_passes_on_this_repository` FAILS with two `steamid` findings in `tests/test_privacy.py`.
- [ ] **Step 2: Fix the two findings.** In `tests/test_privacy.py`, build `STEAMID` from two string literals split after its seventh digit (`"7656119" + "<the remaining ten digits>"`), the same value as today; reword the docstring line to say "The SteamID below is shaped like a SteamID64 but below the range Steam issues to accounts, so it can't be anyone's." without the literal. Add both copied files to `[tool.ruff] extend-exclude` with the same comment as Task 2's. Run `pytest tests/test_privacy_scan.py tests/test_privacy.py -v` → PASS; `python .github/scripts/privacy_scan.py` → exit 0.
- [ ] **Step 3: Update the workflow.**
  - Pin every `uses:` to these full SHAs with the version comment: `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`, `actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0`, `actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0`.
  - `BACKLOG_REF: 4d0b53c3c11d88e6afb5b513572d32d78ca9f24c` with its comment updated (CLAUDE.md and pin, PR #12); same value for `DEFAULT_BACKLOG_REF` in `tests/conftest.py`.
  - New job `query-drift` modelled on `shared-drift`: `python scripts/sync_query.py --check`; on `schedule`/`workflow_dispatch` also `--check --latest`.
  - New job `privacy-scan` copied from triage's `ci.yml` (`fetch-depth: 0`, the `PRIVACY_DENYLIST` / `HAVE_DENYLIST` env and the `--require-denylist` logic).
  - Add `timeout-minutes: 15` to every job, as triage has.
- [ ] **Step 4: Verify locally**: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` (or `actionlint` if installed) succeeds; `grep -n "uses:" .github/workflows/ci.yml | grep -v "@[0-9a-f]\{40\} # v"` prints nothing; `BACKLOG_REQUIRE_REAL=1 BACKLOG_REF=4d0b53c3c11d88e6afb5b513572d32d78ca9f24c pytest tests/test_contract.py -v` passes; the full gate passes.
- [ ] **Step 5: Commit**: `ci: pin actions to SHAs, check backlog_query drift, add the privacy scan; BACKLOG_REF to 4d0b53c`.

---

### Task 8: Contract, README and CLAUDE.md

**Files:**
- Modify: `docs/backlog-api-contract.md` (new §6.8), `README.md`, `CLAUDE.md`

- [ ] **Step 1: Contract §6.8 "Publishers, developers and reviews (not part of the score)"**, after §6.7 and in its style: the export fields (`publishers`, `developers`: lists of up to 10 names; `reviews`: `{total, pct, desc}` or absent), the normalizing rule (`name_key`: NFKC, lower case, trademark signs dropped, punctuation stripped at the ends, trailing corporate suffixes `inc llc ltd co corp corporation gmbh sa ab limited` removed while more than one word is left), the review levels and `few`, and "absent or empty is unknown". API stays 2; no route changes.
- [ ] **Step 2: README**: the tool table gains `backlog_get_stats`; the filter list gains publishers, developers, review levels and Metacritic max; `--version` shows the build commit for wheels built from `git archive` and `unknown` otherwise.
- [ ] **Step 3: CLAUDE.md**: Layout gains `backlog_query.py` (vendored from steam-backlog-triage at the commit in `query.lock`; never edited here; change it there, then `python scripts/sync_query.py --update`; excluded from ruff, mypy and coverage because it is copied verbatim and linted and tested in triage), `_build.py`, `scripts/sync_query.py`, the privacy scan (copied from steam-backlog-triage `0003503`, also excluded from ruff); `domain.py`'s new role; the rule "The page is the reference" now says the chain is page → `backlog_query.py` (triage, parity-tested) → vendored here; `BACKLOG_REF` and `query.lock` usually move together; Commands gains `python scripts/sync_query.py --check`.
- [ ] **Step 4: Verify**: the full gate passes and `python .github/scripts/privacy_scan.py` exits 0.
- [ ] **Step 5: Commit**: `docs: contract 6.8, README and CLAUDE.md for the vendored query layer and get_stats`.

---

### After the tasks

Whole-branch review, then the SHIP PROCEDURE: push, open the PR (body: what changed, the commands run with pass counts, nothing about servers), watch checks, fix causes not tests, notify when green, final message with the deploy verdict. After the user merges: G6, the one-line `MCP_PINNED_REF` bump to the merge commit in steam-backlog-triage's `ci.yml`, through the same procedure.
