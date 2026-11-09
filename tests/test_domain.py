"""domain.py against the page's rules: score parity (the shared vectors), buckets, filters, names."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from steam_backlog_mcp import domain
from steam_backlog_mcp.sim.fake_backlog import FIXTURE

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "score_vectors.json").read_text(encoding="utf-8"))


def games() -> list[domain.Game]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [domain.normalize(g, i) for i, g in enumerate(data["games"])]


def by_name(gs: list[domain.Game], name: str) -> domain.Game:
    (g,) = [g for g in gs if g.name == name]
    return g


def raw(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"appid": 1, "name": "X", "playtime": 0, "deckPlaytime": 0, "lastPlayed": 0, "deck": 0,
                            "hltb": None, "genres": [], "metacritic": None, "year": None, "controller": None,
                            "proton": None}  # fmt: skip
    return {**base, **kw}


# --------------------------------------------------------------------------- parity
@pytest.mark.parametrize("v", VECTORS["vectors"], ids=lambda v: f"{v['id']:02d}")
def test_score_matches_the_page(v: dict[str, Any]) -> None:
    g = domain.normalize(v["game"])
    exp = v["expected"]
    assert domain.score(g, v["lane"]) == pytest.approx(exp["score"], abs=VECTORS["tolerance"])
    assert domain.play_state(g) == exp["playState"]
    assert domain.length_hours(g) == exp["lengthHours"]
    # The breakdown adds up to the score, and leaves out zero terms.
    terms = domain.score_breakdown(g, v["lane"])
    assert sum(t.points for t in terms) == pytest.approx(exp["score"], abs=VECTORS["tolerance"])
    assert all(t.points != 0 for t in terms if t.term != "play")


def test_vectors_cover_every_term() -> None:
    seen = {t.term for v in VECTORS["vectors"] for t in domain.score_breakdown(domain.normalize(v["game"]), v["lane"])}
    assert seen == {"play", "deck", "length", "metacritic", "deck_played", "controller", "next"}
    assert len(VECTORS["vectors"]) >= 30


def test_explain_reads_like_the_design_doc() -> None:
    g = domain.normalize(raw(deck=3, hltb={"main": 8, "extra": 13, "complete": 22}, metacritic=86))
    assert domain.explain(domain.score_breakdown(g, "")) == "never played +30, Verified +30, 8 h +18, MC 86 +5.5"


# --------------------------------------------------------------------------- normalize
def test_normalize_matches_the_page() -> None:
    g = domain.normalize(
        raw(deck=True, playtime=-5, hltb={"main": 0, "extra": 0}, controller="none", genres=[1, "RPG"])
    )
    assert (g.deck, g.playtime, g.hltb, g.controller, g.genres) == (0, 0, None, None, ("RPG",))
    assert g.has_details and g.has_proton
    g = domain.normalize({"appid": 7})
    assert (g.name, g.has_proton, g.has_details, g.proton) == ("App 7", False, False, None)
    g = domain.normalize(raw(proton={"tier": "gold"}, metacritic=float("nan"), year=True))
    assert (g.proton, g.metacritic, g.year) == (domain.Proton("gold", 0), None, None)


@pytest.mark.parametrize(
    ("hours", "bucket"), [(None, "none"), (4.9, "short"), (5, "mid"), (14.9, "mid"), (15, "long"), (40, "epic")]
)
def test_len_bucket(hours: float | None, bucket: str) -> None:
    h = {"main": hours, "extra": None, "complete": None} if hours else None
    assert domain.len_bucket(domain.normalize(raw(hltb=h))) == bucket


# --------------------------------------------------------------------------- filters
def find(f: domain.Filters, triage: dict[int, str] | None = None, notes: dict[int, str] | None = None,
         **kw: Any) -> domain.FindResult:  # fmt: skip
    return domain.find(games(), f, triage or {}, notes or {}, **kw)


def names(r: domain.FindResult) -> set[str]:
    return {g.name for g in r.games}


def test_query_matches_name_or_genre() -> None:
    assert names(find(domain.Filters(query="portal"))) == {"Portal", "Portal 2"}
    assert "Hades" in names(find(domain.Filters(query="roguelike")))


def test_genres_any_all_and_unknown() -> None:
    any_ = names(find(domain.Filters(genres=("Puzzle", "Cozy"))))
    all_ = names(find(domain.Filters(genres=("Puzzle", "Cozy"), genre_match="all")))
    assert all_ == {"Unpacking"} and all_ < any_
    assert "Mystery Prototype" not in any_
    assert "Mystery Prototype" in names(find(domain.Filters(genres=("Puzzle",), include_unknown=True)))


def test_length_presets_use_one_measure_without_fallback() -> None:
    short = names(find(domain.Filters(length="short")))
    assert {"A Short Hike", "Portal", "Unpacking"} <= short and "Hades" not in short
    # extra measure: Portal's extras is 4 h (short), Unpacking's 4.5 h
    assert "Portal" in names(find(domain.Filters(length="short", length_measure="extra")))
    assert names(find(domain.Filters(length="none"))) == {"Destiny 2", "Mystery Prototype"}
    ranged = find(domain.Filters(hours_min=50, hours_max=60))
    assert names(ranged) == {"Stardew Valley", "The Witcher 3: Wild Hunt", "ELDEN RING", "Red Dead Redemption 2"}


def test_protondb_set_and_minimum() -> None:
    gold_up = names(find(domain.Filters(protondb_min="gold")))
    assert "Hotline Miami" not in gold_up and "Cyberpunk 2077" not in gold_up and "Hades" in gold_up
    assert names(find(domain.Filters(protondb=("borked",)))) == {"Cyberpunk 2077"}
    assert len(names(find(domain.Filters(protondb_min="gold", include_unknown=True)))) > len(gold_up)


def test_metacritic_year_controller_deck() -> None:
    assert all(g.metacritic and g.metacritic >= 93 for g in find(domain.Filters(metacritic_min=93)).games)
    assert names(find(domain.Filters(year_from=2023))) == {"Sea of Stars", "Baldur's Gate 3", "COCOON", "Pizza Tower",
                                                             "DAVE THE DIVER"}  # fmt: skip
    full = names(find(domain.Filters(controller=("full",))))
    assert {"Portal 2", "Hades", "Celeste"} <= full
    none = names(find(domain.Filters(controller=("none",))))
    assert "Mystery Prototype" not in none and "Inscryption" in none  # no details = unknown, not "none"
    assert names(find(domain.Filters(deck=(1,)))) == {"Half-Life: Alyx", "Destiny 2"}


def test_play_state_lane_and_notes() -> None:
    tri = {620: "next", 1145360: "done"}
    assert names(find(domain.Filters(lane=("next",)), tri)) == {"Portal 2"}
    untriaged = names(find(domain.Filters(lane=("none",)), tri))
    assert "Portal 2" not in untriaged and "Hades" not in untriaged and "Portal" in untriaged
    assert names(find(domain.Filters(has_note=True), notes={400: "x"})) == {"Portal"}
    tried = names(find(domain.Filters(play_state=("tried",))))
    assert "Disco Elysium" in tried and "Hades" not in tried


def test_backlog_only_and_sorts() -> None:
    r = find(domain.Filters(), {620: "skip"}, backlog_only=True)
    assert "Hades" not in names(r) and "Portal 2" not in names(r)  # played, and skipped
    assert [g.name for g in find(domain.Filters(), sort="name").games][:2] == ["A Short Hike", "Baldur's Gate 3"]
    shortest = find(domain.Filters(), sort="short").games[0]
    assert domain.length_hours(shortest) == 1.5
    assert find(domain.Filters(), sort="most").games[0].name == "Stardew Valley"
    assert find(domain.Filters(), sort="recent").games[0].name == "Vampire Survivors"
    assert find(domain.Filters(), sort="mc").games[0].name == "Baldur's Gate 3"


def test_nothing_found_relaxes_weakest_first() -> None:
    r = find(domain.Filters(deck=(1,), metacritic_min=95))
    assert (r.exact, r.relaxed) == (False, ["metacritic_min"])
    assert names(r) == {"Half-Life: Alyx", "Destiny 2"}
    r = find(domain.Filters(query="hollw knight"))
    assert (r.exact, r.fuzzy_query, names(r)) == (False, True, {"Hollow Knight"})
    r = find(domain.Filters(query="zzzzzz"))
    assert (r.exact, r.games) == (False, [])
    assert find(domain.Filters(query="zzzzzz"), relax=False).exact


# --------------------------------------------------------------------------- picks and Up next
def test_picks_rank_like_the_page() -> None:
    gs = games()
    top = domain.picks(gs, domain.Filters(), {}, {})
    scores = [domain.score(g, "") for g in top]
    assert scores == sorted(scores, reverse=True)
    assert all(domain.in_backlog(g, {}) for g in top)
    # Ties keep export order (the page's stable sort).
    for a, b in zip(top, top[1:], strict=False):
        if domain.score(a, "") == domain.score(b, ""):
            assert a.export_index < b.export_index


def test_next_bonus_does_not_force_first() -> None:
    """D9: a weak Up next game can rank below a strong untriaged one, exactly as on the page."""
    gs = games()
    tri = {1085660: "next"}  # Destiny 2: tried, Unsupported on the Deck, no length: 22 - 45 + 4 + 100 = 81
    top = domain.picks(gs, domain.Filters(), tri, {})
    assert domain.score(by_name(gs, "Destiny 2"), "next") == 81
    assert top[0].name == "Portal" and domain.score(top[0], "") == 95.5
    assert [g.name for g in top].index("Destiny 2") > 0
    assert "Destiny 2" not in {g.name for g in domain.picks(gs, domain.Filters(), tri, {}, exclude_next=True)}


def test_next_queue_and_move() -> None:
    gs = games()
    tri = {620: "next", 400: "next", 753640: "next"}
    q = [g.appid for g in domain.next_queue(gs, tri, [753640])]
    assert q[0] == 753640 and set(q) == {620, 400, 753640}
    assert domain.move([1, 2, 3], 3, 1) == [3, 1, 2]
    assert domain.move([1, 2, 3], 1, 99) == [2, 3, 1]
    assert domain.move([1, 2, 3], 2, 0) == [2, 1, 3]


def test_overview() -> None:
    o = domain.overview(games(), {620: "next"})
    assert o.total == 49 and sum(o.play_states.values()) == 49
    assert sum(sum(row.values()) for row in o.grid.values()) == o.backlog
    assert o.lanes["next"] == 1 and o.lanes["none"] == 48
    assert 0 < o.coverage["hltb"] < 1 and o.coverage["details"] < 1
    assert domain.overview([], {}).coverage["hltb"] == 0.0


# --------------------------------------------------------------------------- names
def test_resolve() -> None:
    gs = games()
    assert domain.resolve(620, gs).game == by_name(gs, "Portal 2")
    assert domain.resolve("620", gs).game == by_name(gs, "Portal 2")
    assert domain.resolve("portal", gs).game == by_name(gs, "Portal")  # exact beats prefix
    assert domain.resolve("the witcher 3 wild hunt", gs).game == by_name(gs, "The Witcher 3: Wild Hunt")
    doom = domain.resolve("doom", gs)
    assert (doom.game, doom.reason, {g.appid for g in doom.candidates}) == (None, "ambiguous", {2280, 379720})
    near = domain.resolve("hollow", gs)
    assert near.game is None and near.reason == "no_match" and near.candidates[0].name == "Hollow Knight"
    assert domain.resolve(12345, gs).reason == "unknown_appid"
    assert domain.resolve("12345", gs).reason == "unknown_appid"
    assert domain.resolve("", gs).reason == "no_match"


def test_filters_without_every_name() -> None:
    f = domain.Filters(query="x", metacritic_min=1, year_from=2000, protondb=("gold",), controller=("full",),
                       tags=("soulslike",), genres=("RPG",), has_note=True, length="short", deck=(3,),
                       play_state=("never",), lane=("next",))  # fmt: skip
    for name in [*f.active(), "query"]:
        assert name not in f.without(name).active()
    assert f.without("query").query == ""


# --------------------------------------------------------------------------- store tags
# The same spellings as steam-backlog-triage's tests/test_stats.py, which checks them against the page's tagKey().
TAG_KEY_CASES = ["Soulslike", "Souls-like", "souls like", "SOULS-LIKE", "Soulsborne", "soulsbourne", "Metroidvania",
                 "Roguelike", "Rogue-like", "Roguelite", "2.5D", "Co-op", "co op", "  Pixel   Graphics ", "Sci-fi",
                 "\uff33\uff4f\uff55\uff4c\uff53\u2010\uff4c\uff49\uff4b\uff45", "Caf\u00e9", "", "+",
                 "constructor", "__proto__"]  # fmt: skip
TAG_KEY_WANT = ["soulslike", "soulslike", "soulslike", "soulslike", "soulslike", "soulslike", "metroidvania",
                "roguelike", "roguelike", "roguelite", "25d", "coop", "coop", "pixelgraphics", "scifi",
                "soulslike", "caf\u00e9", "", "", "constructor", "proto"]  # fmt: skip


def test_tag_key_spellings_share_a_key() -> None:
    assert [domain.tag_key(x) for x in TAG_KEY_CASES] == TAG_KEY_WANT
    assert [domain.tag_key(x) for x in (None, 5)] == ["", "5"]
    assert domain.tag_key("Roguelike") != domain.tag_key("Roguelite")
    assert domain.tag_key("Action RPG") != domain.tag_key("RPG")


@pytest.mark.real
def test_tag_key_matches_the_page(real_checkout: Path) -> None:
    """Run the checkout's own tagKey() in node on the same spellings (CI has both)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node isn't installed")
    page = (real_checkout / "backlog.html").read_text(encoding="utf-8")
    m = re.search(r"const TAG_ALIASES = .*?\nfunction tagKey\(raw\) \{.*?\n\}", page, re.S)
    assert m, "backlog.html has no tagKey(): is the checkout older than store tags?"
    script = m.group(0) + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(tagKey)));"
    out = subprocess.run([node, "-e", script, json.dumps(TAG_KEY_CASES)], capture_output=True, text=True,
                         check=True, timeout=30).stdout  # fmt: skip
    assert json.loads(out) == [domain.tag_key(x) for x in TAG_KEY_CASES]


def test_normalize_tags_like_the_page() -> None:
    g = domain.normalize(raw(tags=["Souls-like", " Soulslike ", "", "  ", 5, None, "+", "Metroidvania"]))
    assert g.tags == ("Souls-like", "Soulslike", "+", "Metroidvania")  # strings, trimmed, blanks dropped
    assert g.tag_keys == ("soulslike", "metroidvania")  # "+" has no key; duplicates kept once, in order
    assert domain.normalize(raw(tags=[f"T{i}" for i in range(15)])).tags == tuple(f"T{i}" for i in range(10))
    for bad in ("Souls-like", {"a": 1}, None):
        assert domain.normalize(raw(tags=bad)).tags == ()
    old = domain.normalize(raw())  # an export made before tags: no "tags" key at all
    assert "tags" not in raw() and (old.tags, old.tag_keys) == ((), ())


def test_sample_library_has_tags_empty_and_absent() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))["games"]
    assert any(g.get("tags") for g in data)
    assert any(g.get("tags") == [] for g in data)
    assert any("tags" not in g for g in data)


def test_tag_filter_is_any_of_by_key() -> None:
    assert domain.tag_filter(["souls like", "Soulsborne", "SOULS-LIKE", "+", ""]) == ("soulslike",)
    souls = names(find(domain.Filters(tags=domain.tag_filter(["soulslike"]))))
    assert souls == {"ELDEN RING", "Hollow Knight", "TUNIC"}  # "Souls-like" and "Soulslike" are one tag
    either = names(find(domain.Filters(tags=domain.tag_filter(["Soulsborne", "cozy"]))))
    assert either == souls | {"A Short Hike", "Unpacking"}
    assert names(find(domain.Filters(tags=("soulslike",), genres=("Metroidvania",)))) == {"Hollow Knight"}


def test_tag_filter_unknown() -> None:
    gs = games()
    f = domain.Filters(tags=("cozy",))
    unknown = {g.name for g in gs if not g.tag_keys}
    assert {"Mystery Prototype", "Kathy Rain", "Celeste"} <= unknown  # [] and no "tags" key are both unknown
    assert not unknown & names(find(f))
    with_unknown = names(find(replace(f, include_unknown=True)))
    assert unknown <= with_unknown and "ELDEN RING" not in with_unknown  # known tags still have to match


def test_tags_relax_before_genres() -> None:
    f = domain.Filters(tags=("soulslike",), genres=("Puzzle",), controller=("full",))
    assert f.active() == ["controller", "tags", "genres"]
    r = find(replace(f, controller=()))
    assert (r.exact, r.relaxed) == (False, ["tags"])
    assert "Portal 2" in names(r)
