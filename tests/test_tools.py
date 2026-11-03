"""Every tool, resource and prompt through the real MCP layer (an in-process Client), against
the wire-level fake. These are the rules from the design doc's sections 4 to 7."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from mcp import Client

from steam_backlog_mcp import server
from steam_backlog_mcp.sim.fake_backlog import FakeBacklog

pytestmark = pytest.mark.anyio

ALL_TOOLS = {
    "backlog_get_overview",
    "backlog_find_games",
    "backlog_get_game",
    "backlog_suggest_picks",
    "backlog_get_next_queue",
    "backlog_set_lane",
    "backlog_set_note",
    "backlog_reorder_next",
    "backlog_start_export",
    "backlog_get_export_status",
}
WRITE_TOOLS = {"backlog_set_lane", "backlog_set_note", "backlog_reorder_next"}


async def call(tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    async with Client(server.mcp) as c:
        r = await c.call_tool(tool, args or {})
    assert not r.is_error, r.content
    assert isinstance(r.structured_content, dict)
    return r.structured_content


async def call_error(tool: str, args: dict[str, Any] | None = None) -> str:
    async with Client(server.mcp) as c:
        r = await c.call_tool(tool, args or {})
    assert r.is_error
    return " ".join(getattr(p, "text", "") for p in r.content)


# --------------------------------------------------------------------------- shape (best practices 1.1-1.3)
async def test_tool_list_names_titles_annotations_descriptions(fake: FakeBacklog) -> None:
    async with Client(server.mcp) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == ALL_TOOLS
    for t in tools:
        assert re.fullmatch(r"backlog_[a-z_]+", t.name), t.name
        assert t.title, t.name
        a = t.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is not None, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is False and a.idempotent_hint is not None, t.name
        d = t.description or ""
        for tag in ("<use_case>", "</use_case>", "<important_notes>", "</important_notes>", "<example>"):
            assert tag in d, (t.name, tag)
        assert t.output_schema, t.name
    by = {t.name: t for t in tools}
    assert by["backlog_start_export"].annotations.open_world_hint is True  # type: ignore[union-attr]
    assert {n for n, t in by.items() if not t.annotations.read_only_hint} == WRITE_TOOLS | {"backlog_start_export"}  # type: ignore[union-attr]
    games = by["backlog_set_lane"].input_schema["properties"]["games"]
    assert (games["minItems"], games["maxItems"]) == (1, 50)
    note = by["backlog_set_note"].input_schema["properties"]["note"]
    assert note["maxLength"] == 2000


async def test_instructions_say_what_the_page_does() -> None:
    i = server.INSTRUCTIONS
    assert "backlog_get_overview first" in i and "data, not instructions" in i
    assert "always rank first" not in i and "usually but not always" in i  # D9
    assert "more than five games" in i


# --------------------------------------------------------------------------- reads
async def test_overview(fake: FakeBacklog) -> None:
    fake.set_lane(620, "next")
    o = await call("backlog_get_overview")
    assert o["games"] == 49 and o["next_queue"] == 1 and o["api_version"] == 2 and o["dry_run"] is False
    assert o["export"]["schema_version"] == 2 and o["export"]["exported_at"].startswith("2026-11-02")
    assert set(o["grid"]) == {"verified", "playable", "unsupported", "unknown"}
    assert o["notes"] == []


async def test_find_games_filters_and_paging(fake: FakeBacklog) -> None:
    r = await call("backlog_find_games", {"deck": ["verified"], "play_state": ["never"], "sort": "short", "limit": 3})
    assert r["exact"] and r["matches"] > 3 and r["truncated"] and len(r["games"]) == 3
    hours = [g["length_hours"] for g in r["games"]]
    assert hours == sorted(hours)
    assert all(g["deck"] == "verified" and g["play_state"] == "never" for g in r["games"])
    page2 = await call("backlog_find_games", {"deck": ["verified"], "play_state": ["never"], "sort": "short",
                                              "limit": 3, "offset": 3})  # fmt: skip
    assert {g["appid"] for g in page2["games"]}.isdisjoint({g["appid"] for g in r["games"]})


async def test_find_games_relaxes_instead_of_not_found(fake: FakeBacklog) -> None:
    r = await call("backlog_find_games", {"deck": ["unsupported"], "metacritic_min": 99})
    assert (r["exact"], r["relaxed"]) == (False, ["metacritic_min"]) and r["matches"] == 2
    assert "dropping: metacritic_min" in r["detail"]
    r = await call("backlog_find_games", {"query": "celest"})
    assert r["exact"] and r["games"][0]["name"] == "Celeste"
    r = await call("backlog_find_games", {"query": "selestee"})
    assert r["fuzzy_query"] and r["games"][0]["name"] == "Celeste"


async def test_pending_is_not_a_minimum(fake: FakeBacklog) -> None:
    assert "protondb_min can't be 'pending'" in await call_error("backlog_find_games", {"protondb_min": "pending"})


async def test_get_game_detail_and_candidates(fake: FakeBacklog) -> None:
    fake.set_lane(620, "next")
    fake.notes["620"] = "co-op"
    lookup = await call("backlog_get_game", {"game": "portal 2"})
    assert lookup["found"] and lookup["unresolved"] is None
    g = lookup["game"]
    assert (g["appid"], g["lane"], g["note"], g["next_position"], g["controller"]) == (620, "next", "co-op", 1, "full")
    assert g["store_url"] == "https://store.steampowered.com/app/620/"
    assert sum(t["points"] for t in g["score_breakdown"]) == g["score"]
    assert g["last_played"] and g["hltb"] == {"main": 8.5, "extra": 13.5, "complete": 22}
    miss = await call("backlog_get_game", {"game": "DOOM"})
    assert miss["found"] is False and miss["game"] is None
    doom = miss["unresolved"]
    assert doom["reason"] == "ambiguous" and {c["year"] for c in doom["candidates"]} == {1993, 2016}
    unknown = (await call("backlog_get_game", {"game": "Mystery Prototype"}))["game"]
    assert (unknown["controller"], unknown["length_hours"], unknown["protondb"]) == (None, None, None)


async def test_suggest_picks_explains_from_the_breakdown(fake: FakeBacklog) -> None:
    r = await call("backlog_suggest_picks", {"count": 3, "deck": ["verified"]})
    assert r["exact"] and [p["rank"] for p in r["picks"]] == [1, 2, 3]
    top = r["picks"][0]
    assert top["name"] == "Portal" and top["score"] == 95.5
    assert top["score_explained"] == "never played +30, Verified +30, 3 h +24, MC 90 +7.5, full controller support +4"
    assert all(p["play_state"] != "played" for p in r["picks"])


async def test_suggest_picks_with_next_and_relaxation(fake: FakeBacklog) -> None:
    fake.set_lane(1085660, "next")  # Destiny 2: 81 with the bonus
    r = await call("backlog_suggest_picks", {"count": 20})
    names = [p["name"] for p in r["picks"]]
    assert names[0] == "Portal" and "Destiny 2" in names  # score alone (D9)
    r = await call("backlog_suggest_picks", {"exclude_next": True, "count": 20})
    assert "Destiny 2" not in [p["name"] for p in r["picks"]]
    r = await call("backlog_suggest_picks", {"deck": ["unsupported"], "metacritic_min": 99})
    assert r["exact"] is False and r["relaxed"] == ["metacritic_min"]
    for g in list(fake.library["games"]):  # type: ignore[index]
        fake.set_lane(g["appid"], "skip")
    r = await call("backlog_suggest_picks")
    assert r["picks"] == [] and "empty" in r["detail"]


async def test_next_queue_order(fake: FakeBacklog) -> None:
    for a in (620, 400, 753640):
        fake.set_lane(a, "next")
    fake.edit_order([753640])
    fake.notes["400"] = "short one"
    q = (await call("backlog_get_next_queue"))["queue"]
    assert [e["appid"] for e in q][0] == 753640 and [e["position"] for e in q] == [1, 2, 3]
    assert next(e for e in q if e["appid"] == 400)["note"] == "short one"
    fake.triage.clear()
    assert (await call("backlog_get_next_queue"))["detail"] == "Up next is empty."


# --------------------------------------------------------------------------- set_lane
async def test_set_lane_single_named_game_needs_no_confirmation(fake: FakeBacklog) -> None:
    r = await call("backlog_set_lane", {"games": ["Hades"], "lane": "done"})
    assert r["outcome"] == "done" and r["atomic"] is True
    assert r["changes"] == [{"appid": 1145360, "name": "Hades", "previous": None, "lane": "done", "changed": True}]
    assert fake.triage == {"1145360": "done"}
    again = await call("backlog_set_lane", {"games": ["1145360"], "lane": "done"})
    assert again["outcome"] == "unchanged"
    back = await call("backlog_set_lane", {"games": ["Hades"], "lane": "none"})
    assert back["changes"][0]["previous"] == "done" and fake.triage == {}


async def test_set_lane_more_than_five_needs_confirmation(fake: FakeBacklog) -> None:
    six = ["Portal", "Celeste", "Inscryption", "Outer Wilds", "Pentiment", "TUNIC"]
    r = await call("backlog_set_lane", {"games": six, "lane": "someday"})
    assert r["outcome"] == "needs_confirmation" and len(r["changes"]) == 6
    assert fake.writes() == [] and fake.triage == {}
    r = await call("backlog_set_lane", {"games": six, "lane": "someday", "confirmed": True})
    assert r["outcome"] == "done" and len(fake.triage) == 6
    # Five changes with one already there: only actual changes count.
    r = await call("backlog_set_lane", {"games": [*six[:5], "Hades"], "lane": "next"})
    assert r["outcome"] == "needs_confirmation"


async def test_set_lane_never_guesses(fake: FakeBacklog) -> None:
    r = await call("backlog_set_lane", {"games": ["Portal", "DOOM", "not a game"], "lane": "skip"})
    assert r["outcome"] == "not_resolved" and fake.writes() == []
    assert [u["reason"] for u in r["unresolved"]] == ["ambiguous", "no_match"]
    r = await call("backlog_set_lane", {"games": ["379720"], "lane": "skip"})
    assert r["outcome"] == "done" and fake.triage == {"379720": "skip"}
    r = await call("backlog_set_lane", {"games": ["99999999"], "lane": "skip"})
    assert r["unresolved"][0]["reason"] == "unknown_appid"


async def test_set_lane_bulk_cap_is_in_the_schema(fake: FakeBacklog) -> None:
    async with Client(server.mcp) as c:
        r = await c.call_tool("backlog_set_lane", {"games": ["620"] * 51, "lane": "skip"})
    assert r.is_error and fake.writes() == []


async def test_set_lane_dry_run(fake: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKLOG_DRY_RUN", "1")
    r = await call("backlog_set_lane", {"games": ["Hades"], "lane": "done"})
    assert r["outcome"] == "dry_run" and "DRY RUN" in r["detail"] and fake.writes() == []


async def test_writes_are_rate_limited(fake: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKLOG_WRITE_RATE", "3")
    async with Client(server.mcp) as c:
        r = await c.call_tool("backlog_set_lane", {"games": ["620", "400", "504230"], "lane": "next"})
        assert not r.is_error
        r = await c.call_tool("backlog_set_lane", {"games": ["753640"], "lane": "next"})
    assert r.is_error and "Rate limit on the backlog" in r.content[0].text  # type: ignore[union-attr]
    assert len(fake.triage) == 3


async def test_set_lane_on_api_1_reads_previous_first(fake_v1: FakeBacklog) -> None:
    fake_v1.set_lane(620, "someday")
    r = await call("backlog_set_lane", {"games": ["620"], "lane": "next"})
    assert r["outcome"] == "done" and r["atomic"] is False and r["warnings"]
    assert r["changes"][0]["previous"] == "someday"


# --------------------------------------------------------------------------- set_note
async def test_set_note_flow(fake: FakeBacklog) -> None:
    r = await call("backlog_set_note", {"game": "Outer Wilds", "note": "  headphones  "})
    assert (r["outcome"], r["previous"], r["note"]) == ("done", None, "headphones")
    assert fake.notes == {"753640": "headphones"}
    same = await call("backlog_set_note", {"game": "753640", "note": "headphones"})
    assert same["outcome"] == "unchanged"
    ask = await call("backlog_set_note", {"game": "Outer Wilds", "note": "no spoilers"})
    assert (ask["outcome"], ask["previous"]) == ("needs_confirmation", "headphones")
    assert fake.notes == {"753640": "headphones"}
    ok = await call("backlog_set_note", {"game": "Outer Wilds", "note": "no spoilers", "confirmed": True})
    assert (ok["outcome"], ok["previous"], ok["note"]) == ("done", "headphones", "no spoilers")
    clear = await call("backlog_set_note", {"game": "Outer Wilds", "note": "", "confirmed": True})
    assert (clear["outcome"], clear["note"], clear["detail"]) == ("done", None, "Cleared the note.")
    assert fake.notes == {}
    bad = await call("backlog_set_note", {"game": "DOOM", "note": "x"})
    assert bad["outcome"] == "not_resolved"


async def test_set_note_dry_run_and_api_1(fake_v1: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> None:
    r = await call("backlog_set_note", {"game": "620", "note": "x"})
    assert r["outcome"] == "done" and r["atomic"] is False
    monkeypatch.setenv("BACKLOG_DRY_RUN", "1")
    r = await call("backlog_set_note", {"game": "400", "note": "y"})
    assert r["outcome"] == "dry_run" and "400" not in fake_v1.notes


# --------------------------------------------------------------------------- reorder
async def test_reorder_move_and_order(fake: FakeBacklog) -> None:
    for a in (620, 400, 753640):
        fake.set_lane(a, "next")
    fake.edit_order([620, 400, 753640])
    r = await call("backlog_reorder_next", {"game": "Outer Wilds", "position": 1})
    assert r["outcome"] == "done" and r["protected"] is True
    assert [e["appid"] for e in r["previous"]] == [620, 400, 753640]
    assert fake.order == [753640, 620, 400]
    put = [h for (meth, path, h) in fake.requests if meth == "PUT" and path == "/api/order"][-1]
    assert put.get("if-match")
    r = await call("backlog_reorder_next", {"order": ["400"]})
    assert fake.order == [400, 753640, 620]
    r = await call("backlog_reorder_next", {"order": ["400"]})
    assert r["outcome"] == "unchanged"


async def test_reorder_refusals(fake: FakeBacklog) -> None:
    fake.set_lane(620, "next")
    assert "either game and position" in await call_error("backlog_reorder_next", {})
    assert "either game and position" in await call_error("backlog_reorder_next", {"game": "620", "order": ["620"]})
    assert "position is required" in await call_error("backlog_reorder_next", {"game": "620"})
    assert "aren't in Up next" in await call_error("backlog_reorder_next", {"game": "400", "position": 1})
    r = await call("backlog_reorder_next", {"order": ["DOOM"]})
    assert r["outcome"] == "not_resolved"


async def test_reorder_retries_once_on_conflict(fake: FakeBacklog) -> None:
    for a in (620, 400):
        fake.set_lane(a, "next")
    fake.edit_order([620, 400])
    fake.conflicts_left = 1
    r = await call("backlog_reorder_next", {"game": "400", "position": 1})
    assert r["outcome"] == "done" and fake.order == [400, 620]
    fake.conflicts_left = 2
    r = await call("backlog_reorder_next", {"game": "620", "position": 1})
    assert r["outcome"] == "conflict" and fake.order == [400, 620]


async def test_reorder_on_api_1_warns(fake_v1: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> None:
    for a in (620, 400):
        fake_v1.set_lane(a, "next")
    # Unordered, Up next sorts by score: Portal (400) before Portal 2 (620).
    r = await call("backlog_reorder_next", {"game": "620", "position": 1})
    assert r["outcome"] == "done" and r["protected"] is False and r["warnings"]
    assert fake_v1.order == [620, 400]
    monkeypatch.setenv("BACKLOG_DRY_RUN", "1")
    r = await call("backlog_reorder_next", {"game": "400", "position": 1})
    assert r["outcome"] == "dry_run" and fake_v1.order == [620, 400]


# --------------------------------------------------------------------------- export
async def wait_export(fake: FakeBacklog) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        s = await call("backlog_get_export_status")
        if s["state"] != "running":
            return s
        assert time.monotonic() < deadline
        time.sleep(0.02)


async def test_export_start_and_status(fake: FakeBacklog) -> None:
    s = await call("backlog_get_export_status")
    assert (s["available"], s["state"]) == (True, "idle")
    fake.export_step_s = 0.05
    r = await call("backlog_start_export")
    assert r["outcome"] == "started" and r["status"]["state"] == "running"
    again = await call("backlog_start_export")
    assert again["outcome"] == "already_running"
    done = await wait_export(fake)
    assert (done["state"], done["library_changed"], done["exit_code"]) == ("succeeded", True, 0)
    assert done["lines"] and "library changed" in done["detail"].lower()
    o = await call("backlog_get_overview")
    assert not o["export"]["exported_at"].startswith("2026-11-02")  # the new library was picked up


async def test_export_external_failed_and_rate_limited(fake: FakeBacklog) -> None:
    fake.external_export = True
    r = await call("backlog_start_export")
    assert r["outcome"] == "already_running" and r["status"]["origin"] == "external"
    assert "outside the dashboard" in r["status"]["detail"]
    fake.external_export = False
    fake.export_exit = 78
    async with Client(server.mcp) as c:
        for _ in range(2):
            assert not (await c.call_tool("backlog_start_export", {})).is_error
            deadline = time.monotonic() + 5
            while (await c.call_tool("backlog_get_export_status", {})).structured_content["state"] == "running":  # type: ignore[index]
                assert time.monotonic() < deadline
                time.sleep(0.02)
        s = (await c.call_tool("backlog_get_export_status", {})).structured_content
        assert s is not None and s["reason"] == "not_configured" and "key or profile" in s["detail"]
        third = await c.call_tool("backlog_start_export", {})
    assert third.is_error and "Rate limit" in third.content[0].text  # type: ignore[union-attr]


async def test_export_unavailable_on_api_1(fake_v1: FakeBacklog) -> None:
    r = await call("backlog_start_export")
    assert r["outcome"] == "unavailable" and r["status"]["available"] is False
    s = await call("backlog_get_export_status")
    assert (s["available"], s["state"]) == (False, "unavailable")
    o = await call("backlog_get_overview")
    assert o["api_version"] == 1 and "older version" in o["notes"][0]


async def test_export_dry_run(fake: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKLOG_DRY_RUN", "1")
    r = await call("backlog_start_export")
    assert r["outcome"] == "dry_run" and fake.writes() == []


async def test_export_not_configured_on_the_server(fake: FakeBacklog) -> None:
    fake.export_configured = False
    r = await call("backlog_start_export")
    assert r["outcome"] == "unavailable"


# --------------------------------------------------------------------------- errors
async def test_no_export_yet(fake: FakeBacklog) -> None:
    fake.library = None
    assert "no library export" in await call_error("backlog_get_overview")


async def test_unreachable_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKLOG_URL", "http://127.0.0.1:9")
    msg = await call_error("backlog_get_overview")
    assert "isn't responding" in msg and "127.0.0.1" not in msg and "Traceback" not in msg


async def test_upstream_errors_are_generic(fake: FakeBacklog) -> None:
    fake.library = {"not": "an export"}
    assert "isn't an export file" in await call_error("backlog_get_overview")


# --------------------------------------------------------------------------- resources and prompts
async def test_resources(fake: FakeBacklog) -> None:
    async with Client(server.mcp) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        scoring = await c.read_resource("backlog://scoring")
        game = await c.read_resource("backlog://game/620")
        assert "backlog://scoring" in uris and "backlog://game/{appid}" in templates
        assert "+100" in scoring.contents[0].text  # type: ignore[union-attr]
        assert '"name":"Portal 2"' in game.contents[0].text  # type: ignore[union-attr]
        for bad in ("backlog://game/123456789", "backlog://game/abc"):
            with pytest.raises(Exception, match="(?i)app id"):
                await c.read_resource(bad)


async def test_prompts(fake: FakeBacklog) -> None:
    async with Client(server.mcp) as c:
        names = {p.name for p in (await c.list_prompts()).prompts}
        assert names == {"triage_session", "pick_for_tonight"}
        t = await c.get_prompt("triage_session", {"count": "5", "filter": "only RPGs"})
        text = t.messages[0].content.text  # type: ignore[union-attr]
        assert "5 games" in text and "only RPGs" in text and "backlog_set_lane" in text
        p = await c.get_prompt("pick_for_tonight", {"minutes": "45", "device": "deck"})
        text = p.messages[0].content.text  # type: ignore[union-attr]
        assert "length='short'" in text and "verified" in text
        p = await c.get_prompt("pick_for_tonight", {"minutes": "180", "device": "pc"})
        text = p.messages[0].content.text  # type: ignore[union-attr]
        assert "don't filter by length" in text and "PC" in text
