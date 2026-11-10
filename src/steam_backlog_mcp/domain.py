"""The MCP's view of backlog_query: its filter shape, its relaxation names and typed aliases.

The backlog's rules live in ``backlog_query``, vendored verbatim from steam-backlog-triage (never edit it;
``scripts/sync_query.py`` keeps it in step). It mirrors the page's ``backlog.html``, and triage's CI pins it
to the page; ``tests/fixtures/score_vectors.json`` still pins the score here. This module adds only what
the MCP needs on top:

  Literal aliases          the tools' argument and output types (backlog_query returns plain str)
  LANES, DECK_NAME, TIERS  typed copies of backlog_query's constants (a test pins them equal)
  Filters                  the tools' filter shape: a ``length`` preset and one ``include_unknown``;
                           to_query() turns it into a backlog_query.Filters
  passes(), picks()        backlog_query's, over the converted filters
  find()                   the list, with the MCP's relaxation names ("metacritic_min") and fuzzy-name
                           step (duplicate names keep the requested sort)
  tag_filter(), move()

Everything else (normalize, score, sort_games, next_queue, overview, resolve, ...) is backlog_query's own,
re-exported here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from . import backlog_query
from .backlog_query import (
    DECK_BY_NAME,
    DECK_LABEL,
    DECK_ORDER,
    LEN_BUCKETS,
    LEN_PRESET,
    TAG_ALIASES,
    TIER_RANK,
    TRIED_MIN,
    FindResult,
    Game,
    Hltb,
    Overview,
    Proton,
    Resolved,
    Term,
    explain,
    in_backlog,
    is_closed,
    lane_of,
    len_bucket,
    length_hours,
    name_key,
    next_queue,
    normalize,
    overview,
    play_state,
    resolve,
    score,
    score_breakdown,
    sort_games,
    tag_key,
)

__all__ = [
    "DECK_BY_NAME",
    "DECK_LABEL",
    "DECK_NAME",
    "DECK_ORDER",
    "LANES",
    "LEN_BUCKETS",
    "LEN_PRESET",
    "TAG_ALIASES",
    "TIERS",
    "TIER_RANK",
    "TRIED_MIN",
    "Controller",
    "DeckName",
    "Filters",
    "FindResult",
    "Game",
    "Hltb",
    "Lane",
    "LaneOrNone",
    "LenBucket",
    "Measure",
    "Overview",
    "PlayState",
    "Proton",
    "Resolved",
    "Sort",
    "Term",
    "Tier",
    "explain",
    "find",
    "in_backlog",
    "is_closed",
    "lane_of",
    "len_bucket",
    "length_hours",
    "move",
    "name_key",
    "next_queue",
    "normalize",
    "overview",
    "passes",
    "picks",
    "play_state",
    "resolve",
    "score",
    "score_breakdown",
    "sort_games",
    "tag_filter",
    "tag_key",
]

Lane = Literal["next", "someday", "skip", "done"]
LaneOrNone = Literal["next", "someday", "skip", "done", "none"]
PlayState = Literal["never", "tried", "played"]
LenBucket = Literal["short", "mid", "long", "epic", "none"]
Measure = Literal["main", "extra", "complete"]
DeckName = Literal["verified", "playable", "unsupported", "unknown"]
Controller = Literal["full", "partial", "none"]
Tier = Literal["native", "platinum", "gold", "silver", "bronze", "borked", "pending"]
Sort = Literal["score", "short", "name", "recent", "most", "mc"]

# Typed copies of backlog_query's constants, for the places that need the Literal.
LANES: tuple[Lane, ...] = ("next", "someday", "skip", "done")
DECK_NAME: dict[int, DeckName] = {3: "verified", 2: "playable", 1: "unsupported", 0: "unknown"}
TIERS: tuple[Tier, ...] = ("native", "platinum", "gold", "silver", "bronze", "borked", "pending")


# --------------------------------------------------------------------------- filters
@dataclass(frozen=True)
class Filters:
    """The tools' filter panel, plus protondb_min. Empty means "any".

    ``include_unknown`` sets every per-filter "include unknown" box at once.
    """

    query: str = ""
    genres: tuple[str, ...] = ()
    genre_match: Literal["any", "all"] = "any"
    length: LenBucket | None = None  # a preset; "none" = no data for the measure
    length_measure: Measure = "main"
    hours_min: float | None = None  # explicit range; overrides the preset's bounds
    hours_max: float | None = None
    protondb: tuple[str, ...] = ()
    protondb_min: str | None = None
    metacritic_min: float | None = None
    year_from: float | None = None
    year_to: float | None = None
    deck: tuple[int, ...] = ()
    controller: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()  # tag_key() keys, not names: build with tag_filter()
    play_state: tuple[str, ...] = ()
    lane: tuple[str, ...] = ()  # "none" = untriaged
    has_note: bool | None = None
    include_unknown: bool = False

    def active(self) -> list[str]:
        """Names of the filters that are set, weakest first (the relaxation order)."""
        names: list[str] = []
        if self.metacritic_min is not None:
            names.append("metacritic_min")
        if self.year_from is not None or self.year_to is not None:
            names.append("year")
        if self.protondb or self.protondb_min:
            names.append("protondb")
        if self.controller:
            names.append("controller")
        if self.tags:
            names.append("tags")
        if self.genres:
            names.append("genres")
        if self.has_note is not None:
            names.append("has_note")
        if self.length is not None or self.hours_min is not None or self.hours_max is not None:
            names.append("length")
        if self.deck:
            names.append("deck")
        if self.play_state:
            names.append("play_state")
        if self.lane:
            names.append("lane")
        return names

    def without(self, name: str) -> Filters:
        cleared: dict[str, dict[str, Any]] = {
            "metacritic_min": {"metacritic_min": None},
            "year": {"year_from": None, "year_to": None},
            "protondb": {"protondb": (), "protondb_min": None},
            "controller": {"controller": ()},
            "tags": {"tags": ()},
            "genres": {"genres": ()},
            "has_note": {"has_note": None},
            "length": {"length": None, "hours_min": None, "hours_max": None},
            "deck": {"deck": ()},
            "play_state": {"play_state": ()},
            "lane": {"lane": ()},
            "query": {"query": ""},
        }
        return replace(self, **cleared[name])

    def to_query(self, *, backlog_only: bool = False) -> backlog_query.Filters:
        """The same filters in backlog_query's shape (the page's DEFAULT_F)."""
        lo, hi = self.hours_min, self.hours_max
        if self.length in LEN_PRESET and lo is None and hi is None:
            lo, hi = LEN_PRESET[self.length]
        unk = self.include_unknown
        return backlog_query.Filters(
            view="backlog" if backlog_only else "all",
            query=self.query,
            deck=self.deck,
            genres=self.genres,
            genre_match=self.genre_match,
            genres_unknown=unk,
            length_measure=self.length_measure,
            hours_min=lo,
            hours_max=hi,
            length_none=self.length == "none",
            length_unknown=unk,
            protondb=self.protondb,
            protondb_unknown=unk,
            protondb_min=self.protondb_min,
            metacritic_min=self.metacritic_min,
            metacritic_unknown=unk,
            year_from=self.year_from,
            year_to=self.year_to,
            year_unknown=unk,
            controller=self.controller,
            controller_unknown=unk,
            publishers_unknown=unk,
            developers_unknown=unk,
            tags=self.tags,
            tags_unknown=unk,
            reviews_unknown=unk,
            play_state=self.play_state,
            lane=self.lane,
            has_note=self.has_note,
        )


def tag_filter(names: Iterable[str]) -> tuple[str, ...]:
    """Filter input (any spelling) -> the keys Filters.tags holds: "souls like" and "Souls-like" are one."""
    return tuple(dict.fromkeys(k for k in map(tag_key, names) if k))


def passes(g: Game, f: Filters, triage: Mapping[int, str], notes: Mapping[int, str]) -> bool:
    """The page's passes(). The query is the search box's, applied by find()."""
    return backlog_query.passes(g, f.to_query(), triage, notes)


def _filter(
    games: Iterable[Game], f: Filters, triage: Mapping[int, str], notes: Mapping[int, str], *, backlog_only: bool
) -> list[Game]:
    return backlog_query.selected(games, f.to_query(backlog_only=backlog_only), triage, notes)


def find(
    games: Sequence[Game],
    f: Filters,
    triage: Mapping[int, str],
    notes: Mapping[int, str],
    *,
    sort: Sort = "score",
    backlog_only: bool = False,
    relax: bool = True,
) -> FindResult:
    """Filter and sort. With nothing found and ``relax``, try a fuzzy name first, then drop
    filters weakest-first until something matches, and say what was relaxed (best practice 1.4)."""
    hits = _filter(games, f, triage, notes, backlog_only=backlog_only)
    if hits or not relax:
        return FindResult(sort_games(hits, sort, triage))
    if f.query.strip():
        pool = _filter(games, f.without("query"), triage, notes, backlog_only=backlog_only)
        close = backlog_query.suggest(f.query, [g.name for g in pool], limit=10)
        rank = {name: i for i, name in enumerate(close)}
        fuzzy = [g for g in sort_games(pool, sort, triage) if g.name in rank]
        if fuzzy:
            # Closest name first: "did you mean" beats the requested sort here.
            fuzzy.sort(key=lambda g: rank[g.name])
            return FindResult(fuzzy, exact=False, fuzzy_query=True)
    relaxed: list[str] = []
    current = f
    for name in f.active():
        current = current.without(name)
        relaxed.append(name)
        hits = _filter(games, current, triage, notes, backlog_only=backlog_only)
        if hits:
            return FindResult(sort_games(hits, sort, triage), exact=False, relaxed=relaxed)
    return FindResult([], exact=False, relaxed=relaxed)


def picks(
    games: Sequence[Game],
    f: Filters,
    triage: Mapping[int, str],
    notes: Mapping[int, str],
    *,
    exclude_next: bool = False,
) -> list[Game]:
    """renderPicks(): backlog games passing the filters, by score, ties in export order.

    Sorted by score alone, like the page: the Up next bonus usually, not always, puts those first.
    """
    return backlog_query.picks(games, f.to_query(), triage, notes, exclude_next=exclude_next)


def move(ids: list[int], appid: int, position: int) -> list[int]:
    """Move ``appid`` to 1-based ``position`` (clamped), as the page's drag and Move buttons do."""
    out = [a for a in ids if a != appid]
    pos = min(max(position, 1), len(out) + 1) - 1
    out.insert(pos, appid)
    return out
