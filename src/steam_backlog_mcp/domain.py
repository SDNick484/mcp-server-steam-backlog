"""The backlog's rules, as pure functions: no I/O, no MCP, no HTTP.

Everything here mirrors steam-backlog-triage's ``backlog.html`` so the MCP and the
dashboard agree. The page is the reference; ``docs/backlog-api-contract.md`` §6
writes its rules down, and ``tests/fixtures/score_vectors.json`` pins the score.
When this file and the page disagree, the page wins and this file is the bug.

  normalize()       the page's normalize(): clean one export game
  tag_key()         the page's tagKey(): the loose key Steam tags are matched by
  play_state()      never / tried / played (TRIED_MIN = 120 minutes)
  length_hours()    the page's mainH(): HLTB main, else extras (JS truthiness)
  len_bucket()      the overview grid's buckets (lenBucket)
  score_breakdown() score(), term by term
  Filters, passes() the page's filter panel (passes())
  find(), picks()   the list and the couch picks, sorted as the page sorts them
  resolve()         app id or name -> one game, or candidates (never a guess)
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .lookup import norm, suggest

TRIED_MIN = 120  # minutes; under this a launched game counts as "tried"

Lane = Literal["next", "someday", "skip", "done"]
LaneOrNone = Literal["next", "someday", "skip", "done", "none"]
PlayState = Literal["never", "tried", "played"]
LenBucket = Literal["short", "mid", "long", "epic", "none"]
Measure = Literal["main", "extra", "complete"]
DeckName = Literal["verified", "playable", "unsupported", "unknown"]
Controller = Literal["full", "partial", "none"]
Tier = Literal["native", "platinum", "gold", "silver", "bronze", "borked", "pending"]
Sort = Literal["score", "short", "name", "recent", "most", "mc"]

LANES: tuple[Lane, ...] = ("next", "someday", "skip", "done")
DECK_BY_NAME: dict[str, int] = {"verified": 3, "playable": 2, "unsupported": 1, "unknown": 0}
DECK_NAME: dict[int, DeckName] = {3: "verified", 2: "playable", 1: "unsupported", 0: "unknown"}
DECK_LABEL: dict[int, str] = {3: "Verified", 2: "Playable", 1: "Unsupported", 0: "Unknown"}
DECK_ORDER = (3, 2, 0, 1)  # the overview grid's row order
LEN_BUCKETS: tuple[LenBucket, ...] = ("short", "mid", "long", "epic", "none")
TIERS: tuple[Tier, ...] = ("native", "platinum", "gold", "silver", "bronze", "borked", "pending")
# For protondb_min only (the page filters by a set of tiers). Pending has no rank.
TIER_RANK: dict[str, int] = {"native": 6, "platinum": 5, "gold": 4, "silver": 3, "bronze": 2, "borked": 1}
# The filter panel's length presets (values rounded to 0.1 h, so 4.9 is "under 5").
LEN_PRESET: dict[str, tuple[float | None, float | None]] = {
    "short": (None, 4.9),
    "mid": (5, 14.9),
    "long": (15, 39.9),
    "epic": (40, None),
}


# --------------------------------------------------------------------------- games
@dataclass(frozen=True)
class Hltb:
    main: float | None
    extra: float | None
    complete: float | None

    def get(self, measure: Measure) -> float | None:
        return {"main": self.main, "extra": self.extra, "complete": self.complete}[measure]


@dataclass(frozen=True)
class Proton:
    tier: str
    total: float


@dataclass(frozen=True)
class Game:
    """One library game after normalize(). Field names follow the export."""

    appid: int
    name: str
    playtime: float  # minutes
    deck_playtime: float  # minutes
    last_played: float  # unix seconds, 0 = never
    deck: int  # 0 unknown, 1 unsupported, 2 playable, 3 verified
    hltb: Hltb | None
    genres: tuple[str, ...]
    metacritic: float | None
    year: float | None
    controller: str | None  # "full" | "partial" | None
    proton: Proton | None
    has_proton: bool  # the export has a "proton" key at all (ProtonDB lookups ran)
    has_details: bool  # store details were fetched (genres or a year)
    export_index: int = 0  # position in the export: the page's stable-sort tie-break
    tags: tuple[str, ...] = ()  # Steam store tags as Steam spells them, in vote order; () = unknown
    tag_keys: tuple[str, ...] = ()  # tag_key() of each, blanks dropped, deduplicated


def _num(v: Any) -> float | None:
    """JS ``typeof v === "number" && isFinite(v) ? v : null``; bools aren't numbers there."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


# A few names people use for a Steam tag, mapped onto that tag's key (the page's TAG_ALIASES).
TAG_ALIASES = {"soulsborne": "soulslike", "soulsbourne": "soulslike"}


def tag_key(raw: Any) -> str:
    """The page's tagKey(): NFKC, lower case, letters and digits only, then the alias table.

    "Souls-like", "souls like" and "Soulslike" share the key "soulslike", and so does "Soulsborne".
    NFKC first, so fullwidth letters and compatibility forms fold onto their plain spelling.
    """
    text = unicodedata.normalize("NFKC", "" if raw is None else str(raw)).lower()
    k = "".join(c for c in text if unicodedata.category(c)[0] in "LN")
    return TAG_ALIASES.get(k, k)


def _names(v: Any) -> tuple[str, ...]:
    """The page's normalizeExtras() names(): strings only, trimmed, blanks dropped, at most 10."""
    if not isinstance(v, list):
        return ()
    return tuple(x.strip() for x in v if isinstance(x, str) and x.strip())[:10]


def _tag_keys(names: Iterable[str]) -> tuple[str, ...]:
    """tag_key() of each name, blanks dropped ("+" has no key), first occurrence kept."""
    return tuple(dict.fromkeys(k for k in map(tag_key, names) if k))


def normalize(raw: Mapping[str, Any], index: int = 0) -> Game:
    """The page's normalize(), field for field."""
    h_raw = raw.get("hltb")
    hltb: Hltb | None = None
    if isinstance(h_raw, Mapping):
        h = Hltb(_num(h_raw.get("main")), _num(h_raw.get("extra")), _num(h_raw.get("complete")))
        if h.main or h.extra:  # JS truthiness: 0 and null both fail
            hltb = h
    genres_raw = raw.get("genres")
    genres = tuple(x for x in genres_raw if isinstance(x, str))[:6] if isinstance(genres_raw, list) else ()
    p_raw = raw.get("proton")
    proton: Proton | None = None
    if isinstance(p_raw, Mapping) and isinstance(p_raw.get("tier"), str):
        proton = Proton(tier=p_raw["tier"], total=_num(p_raw.get("total")) or 0)
    deck = raw.get("deck")
    controller = raw.get("controller")
    year = _num(raw.get("year"))
    appid = raw.get("appid")
    name = raw.get("name")
    tags = _names(raw.get("tags"))
    return Game(
        appid=int(appid) if isinstance(appid, int | float) and not isinstance(appid, bool) else 0,
        name=str(name) if name else f"App {appid}",
        playtime=max(0.0, _num(raw.get("playtime")) or 0),
        deck_playtime=max(0.0, _num(raw.get("deckPlaytime")) or 0),
        last_played=_num(raw.get("lastPlayed")) or 0,
        deck=int(deck) if deck in (0, 1, 2, 3) and not isinstance(deck, bool) else 0,
        hltb=hltb,
        genres=genres,
        metacritic=_num(raw.get("metacritic")),
        year=year,
        controller=controller if controller in ("full", "partial") else None,
        proton=proton,
        has_proton="proton" in raw,
        has_details=(isinstance(genres_raw, list) and len(genres_raw) > 0) or year is not None,
        export_index=index,
        tags=tags,
        tag_keys=_tag_keys(tags),
    )


def play_state(g: Game) -> PlayState:
    if g.playtime == 0:
        return "never"
    return "tried" if g.playtime < TRIED_MIN else "played"


def length_hours(g: Game) -> float | None:
    """mainH(): ``hltb.main || hltb.extra``. A main time of 0 falls through to extras."""
    if g.hltb is None:
        return None
    return g.hltb.main or g.hltb.extra or None


def len_bucket(g: Game) -> LenBucket:
    h = length_hours(g)
    if not h:
        return "none"
    return "short" if h < 5 else "mid" if h < 15 else "long" if h < 40 else "epic"


def lane_of(g: Game, triage: Mapping[int, str]) -> str:
    return triage.get(g.appid, "")


def is_closed(g: Game, triage: Mapping[int, str]) -> bool:
    return lane_of(g, triage) in ("skip", "done")


def in_backlog(g: Game, triage: Mapping[int, str]) -> bool:
    return play_state(g) != "played" and not is_closed(g, triage)


# --------------------------------------------------------------------------- score
@dataclass(frozen=True)
class Term:
    term: str  # play | deck | length | metacritic | deck_played | controller | next
    label: str  # "never played", "Verified", "8 h", "MC 86", ...
    points: float


def _fmt_h(h: float) -> str:
    return f"{h:g} h"


def score_breakdown(g: Game, lane: str) -> list[Term]:
    """score() term by term; the points add up to score() exactly. Zero terms are left out."""
    out: list[Term] = []
    st = play_state(g)
    play_pts = {"never": 30, "tried": 22, "played": -20}[st]
    out.append(
        Term("play", {"never": "never played", "tried": "tried (under 2 h)", "played": "played 2 h+"}[st], play_pts)
    )
    deck_pts = {3: 30, 2: 14, 0: 0, 1: -45}[g.deck]
    if deck_pts:
        out.append(Term("deck", DECK_LABEL[g.deck], deck_pts))
    h = length_hours(g)
    if h:
        pts = 24 if h <= 5 else 18 if h <= 12 else 9 if h <= 25 else 0 if h <= 50 else -10
        if pts:
            out.append(Term("length", _fmt_h(h), pts))
    if g.metacritic:
        mc_pts = (g.metacritic - 75) / 2
        if mc_pts:
            out.append(Term("metacritic", f"MC {g.metacritic:g}", mc_pts))
    if g.deck_playtime > 0:
        out.append(Term("deck_played", "played on the Deck", 5))
    if g.controller == "full":
        out.append(Term("controller", "full controller support", 4))
    if lane == "next":
        out.append(Term("next", "in Up next", 100))
    return out


def score(g: Game, lane: str) -> float:
    total = 0.0
    for t in score_breakdown(g, lane):
        total += t.points
    return total


def explain(terms: Sequence[Term]) -> str:
    """'never played +30, Verified +30, 8 h +18, MC 86 +5.5'"""
    return ", ".join(f"{t.label} {t.points:+g}" for t in terms)


# --------------------------------------------------------------------------- filters
@dataclass(frozen=True)
class Filters:
    """The page's filter panel (DEFAULT_F), plus protondb_min. Empty means "any".

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


def tag_filter(names: Iterable[str]) -> tuple[str, ...]:
    """Filter input (any spelling) -> the keys Filters.tags holds: "souls like" and "Souls-like" are one."""
    return _tag_keys(names)


def passes(g: Game, f: Filters, triage: Mapping[int, str], notes: Mapping[int, str]) -> bool:
    """The page's passes(), one filter at a time, in its order. Query is separate (matches())."""
    unk = f.include_unknown
    if f.deck and g.deck not in f.deck:
        return False
    if f.genres:
        if not g.genres:
            if not unk:
                return False
        else:
            ok = (
                all(x in g.genres for x in f.genres) if f.genre_match == "all" else any(x in g.genres for x in f.genres)
            )
            if not ok:
                return False
    lo, hi = f.hours_min, f.hours_max
    none_wanted = f.length == "none"
    if f.length in LEN_PRESET and lo is None and hi is None:
        lo, hi = LEN_PRESET[f.length]
    if none_wanted or lo is not None or hi is not None:
        v = g.hltb.get(f.length_measure) if g.hltb else None
        if none_wanted:
            if v is not None:
                return False
        elif v is None:
            if not unk:
                return False
        elif (lo is not None and v < lo) or (hi is not None and v > hi):
            return False
    if f.protondb or f.protondb_min:
        tier = g.proton.tier if g.proton else None
        if not tier:
            if not unk:
                return False
        else:
            if f.protondb and tier not in f.protondb:
                return False
            if f.protondb_min and TIER_RANK.get(tier, 0) < TIER_RANK[f.protondb_min]:
                return False
    if f.metacritic_min is not None:
        if not g.metacritic:
            if not unk:
                return False
        elif g.metacritic < f.metacritic_min:
            return False
    if f.year_from is not None or f.year_to is not None:
        if not g.year:
            if not unk:
                return False
        elif (f.year_from is not None and g.year < f.year_from) or (f.year_to is not None and g.year > f.year_to):
            return False
    if f.controller:
        if not g.controller and not g.has_details:
            if not unk:
                return False
        elif (g.controller or "none") not in f.controller:
            return False
    if f.tags:
        # Any-of: one shared key is enough. No tags at all means the tags were never fetched (unknown).
        if not g.tag_keys:
            if not unk:
                return False
        elif not any(k in f.tags for k in g.tag_keys):
            return False
    if f.play_state and play_state(g) not in f.play_state:
        return False
    if f.lane and (lane_of(g, triage) or "none") not in f.lane:
        return False
    return not (f.has_note is not None and f.has_note != (g.appid in notes))


def matches(g: Game, query: str) -> bool:
    """The page's search box: a substring of the name or of any genre, case-insensitive."""
    q = query.strip().lower()
    if not q:
        return True
    return q in g.name.lower() or any(q in x.lower() for x in g.genres)


def _name_key(g: Game) -> tuple[str, str]:
    # JS localeCompare is locale-aware; casefold first, then the raw name, is close and deterministic.
    return (g.name.casefold(), g.name)


def sort_games(games: list[Game], sort: Sort, triage: Mapping[int, str]) -> list[Game]:
    """The page's list sort; every comparison falls back to the name."""
    if sort == "short":
        key: Any = lambda g: (length_hours(g) or 1e9, _name_key(g))  # noqa: E731
    elif sort == "name":
        key = _name_key
    elif sort == "recent":
        key = lambda g: (-g.last_played, _name_key(g))  # noqa: E731
    elif sort == "most":
        key = lambda g: (-g.playtime, _name_key(g))  # noqa: E731
    elif sort == "mc":
        key = lambda g: (-(g.metacritic or 0), _name_key(g))  # noqa: E731
    else:
        key = lambda g: (-score(g, lane_of(g, triage)), _name_key(g))  # noqa: E731
    return sorted(games, key=key)


@dataclass
class FindResult:
    games: list[Game]
    exact: bool = True
    relaxed: list[str] = field(default_factory=list)
    fuzzy_query: bool = False


def _filter(
    games: Iterable[Game],
    f: Filters,
    triage: Mapping[int, str],
    notes: Mapping[int, str],
    *,
    backlog_only: bool,
) -> list[Game]:
    return [
        g
        for g in games
        if (not backlog_only or in_backlog(g, triage)) and passes(g, f, triage, notes) and matches(g, f.query)
    ]


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
        close = suggest(f.query, [g.name for g in pool], limit=10)
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
    pool = [
        g
        for g in _filter(games, f, triage, notes, backlog_only=True)
        if not (exclude_next and lane_of(g, triage) == "next")
    ]
    return sorted(pool, key=lambda g: (-score(g, lane_of(g, triage)), g.export_index))


def next_queue(games: Sequence[Game], triage: Mapping[int, str], order: Sequence[int]) -> list[Game]:
    """nextIds(): the Up next lane in saved order, then unordered ones by score, then name."""
    index = {a: i for i, a in enumerate(order)}
    lane = [g for g in games if lane_of(g, triage) == "next"]
    return sorted(lane, key=lambda g: (index.get(g.appid, 10**9), -score(g, "next"), _name_key(g)))


def move(ids: list[int], appid: int, position: int) -> list[int]:
    """Move ``appid`` to 1-based ``position`` (clamped), as the page's drag and Move buttons do."""
    out = [a for a in ids if a != appid]
    pos = min(max(position, 1), len(out) + 1) - 1
    out.insert(pos, appid)
    return out


# --------------------------------------------------------------------------- overview
@dataclass(frozen=True)
class Overview:
    total: int
    play_states: dict[str, int]
    deck: dict[str, int]
    backlog: int
    grid: dict[str, dict[str, int]]  # deck name -> length bucket -> backlog games
    lanes: dict[str, int]  # next/someday/skip/done/none
    coverage: dict[str, float]  # share of games with each data source, 0..1


def overview(games: Sequence[Game], triage: Mapping[int, str]) -> Overview:
    n = len(games)
    states = {"never": 0, "tried": 0, "played": 0}
    deck: dict[str, int] = {DECK_NAME[d]: 0 for d in DECK_ORDER}
    grid: dict[str, dict[str, int]] = {DECK_NAME[d]: {b: 0 for b in LEN_BUCKETS} for d in DECK_ORDER}
    lanes = {k: 0 for k in (*LANES, "none")}
    backlog = 0
    cov = {"hltb": 0, "protondb": 0, "details": 0, "metacritic": 0, "deck_rating": 0}
    for g in games:
        states[play_state(g)] += 1
        deck[DECK_NAME[g.deck]] += 1
        lanes[lane_of(g, triage) or "none"] += 1
        if in_backlog(g, triage):
            backlog += 1
            grid[DECK_NAME[g.deck]][len_bucket(g)] += 1
        cov["hltb"] += g.hltb is not None
        cov["protondb"] += g.proton is not None
        cov["details"] += g.has_details
        cov["metacritic"] += bool(g.metacritic)
        cov["deck_rating"] += g.deck != 0
    coverage = {k: round(v / n, 3) if n else 0.0 for k, v in cov.items()}
    return Overview(n, states, deck, backlog, grid, lanes, coverage)


# --------------------------------------------------------------------------- names
@dataclass(frozen=True)
class Resolved:
    """A game argument resolved to one game, or not: then ``candidates`` (best first)."""

    query: str
    game: Game | None
    candidates: tuple[Game, ...] = ()
    reason: Literal["ok", "ambiguous", "unknown_appid", "no_match"] = "ok"


def resolve(ref: str | int, games: Sequence[Game]) -> Resolved:
    """An app id or a name -> exactly one game, or candidates. Never a guess.

    - an int, or a string of digits: that app id, if it's in the library;
    - otherwise a name, matched exactly after lookup.norm() (case, spaces and punctuation ignored).
      One match resolves; several (two games called "DOOM") or none return candidates.
    """
    text = str(ref).strip()
    by_id = {g.appid: g for g in games}
    if isinstance(ref, int) or text.isdigit():
        appid = int(text)
        g = by_id.get(appid)
        if g is not None:
            return Resolved(text, g)
        if isinstance(ref, int):
            return Resolved(text, None, reason="unknown_appid")
        # A title made of digits ("1979 Revolution" wouldn't be, but "2048" could be): try it as a name too.
    key = norm(text)
    exact = [g for g in games if norm(g.name) == key] if key else []
    if len(exact) == 1:
        return Resolved(text, exact[0])
    if len(exact) > 1:
        return Resolved(text, None, tuple(exact), reason="ambiguous")
    names = suggest(text, [g.name for g in games], limit=5)
    cands: list[Game] = []
    for name in names:
        cands.extend(g for g in games if g.name == name)
    reason: Literal["unknown_appid", "no_match"] = "unknown_appid" if text.isdigit() else "no_match"
    return Resolved(text, None, tuple(cands[:8]), reason=reason)
