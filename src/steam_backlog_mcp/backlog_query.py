"""The backlog's read layer: the page's rules as pure functions over plain data. No I/O, no HTTP.

Everything here mirrors ``backlog.html`` (normalize, score, the filter panel, the statistics block), so
the Ask feature and the dashboard agree. The page is the reference; when this module and the page
disagree, the page wins and this module is the bug. tests/test_backlog_query.py runs the page's own
JavaScript under node and compares.

Started from mcp-server-steam-backlog ee288ba (src/steam_backlog_mcp/domain.py, lookup.py).

  normalize()           the page's normalizeGame() + annotateNames()
  play_state(), score() the couch score, term by term (score_breakdown)
  Filters, clean()      the filter panel (DEFAULT_F) and its validator
  passes(), selected()  the page's passes() and selected()
  find(), picks()       the list (with relaxation) and the couch picks
  stats_for()           the statistics block (camelCase keys, as the page's statsFor)
  *_out()               JSON-ready results for the model's tools

Standard library only.
"""
from __future__ import annotations

import difflib
import math
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

TRIED_MIN = 120  # minutes; under this a launched game counts as "tried"

LANES = ("next", "someday", "skip", "done")
DECK_BY_NAME = {"verified": 3, "playable": 2, "unsupported": 1, "unknown": 0}
DECK_NAME = {3: "verified", 2: "playable", 1: "unsupported", 0: "unknown"}
DECK_LABEL = {3: "Verified", 2: "Playable", 1: "Unsupported", 0: "Unknown"}
DECK_ORDER = (3, 2, 0, 1)  # the overview grid's row order
LEN_BUCKETS = ("short", "mid", "long", "epic", "none")
TIERS = ("native", "platinum", "gold", "silver", "bronze", "borked", "pending")
# For protondb_min only (the page filters by a set of tiers). Pending has no rank.
TIER_RANK = {"native": 6, "platinum": 5, "gold": 4, "silver": 3, "bronze": 2, "borked": 1}
# The filter panel's length presets (values rounded to 0.1 h, so 4.9 is "under 5").
LEN_PRESET: dict[str, tuple[float | None, float | None]] = {
    "short": (None, 4.9), "mid": (5, 14.9), "long": (15, 39.9), "epic": (40, None),
}
VIEWS = ("backlog", "next", "someday", "played", "closed", "all")
SORTS = ("score", "short", "name", "recent", "most", "mc")
SRCS = ("main", "extra", "complete")
CTRLS = ("full", "partial", "none")
PLAYS = ("never", "tried", "played")
LANE_F = ("none", "next", "someday", "skip", "done")
MC_OPTS = (60, 70, 75, 80, 85, 90)
MC_MAX_OPTS = (59, 69, 79, 89)
UNK_KEYS = ("gen", "pub", "dev", "tag", "pt", "mc", "yr", "ctrl", "rv")
NAME_SUFFIXES = ("inc", "llc", "ltd", "co", "corp", "corporation", "gmbh", "sa", "ab", "limited")
REVIEW_LEVELS = (
    ("overwhelmingly-positive", "Overwhelmingly Positive"), ("very-positive", "Very Positive"),
    ("positive", "Positive"), ("mostly-positive", "Mostly Positive"), ("mixed", "Mixed"),
    ("mostly-negative", "Mostly Negative"), ("negative", "Negative"), ("very-negative", "Very Negative"),
    ("overwhelmingly-negative", "Overwhelmingly Negative"), ("few", "Too few reviews"),
)
REVIEW_KEYS = tuple(k for k, _ in REVIEW_LEVELS)
MC_BUCKETS = (
    ("lt60", "Under 60", None, 59), ("60s", "60s", 60, 69), ("70s", "70s", 70, 79),
    ("80s", "80s", 80, 89), ("90p", "90+", 90, None),
)
STORE_URL = "https://store.steampowered.com/app/{appid}/"


class QueryError(ValueError):
    """A bad argument. The message goes to the model, so it names the argument and the allowed values."""


# --------------------------------------------------------------------------- lookup helpers
# Inlined from mcp-server-steam-backlog src/steam_backlog_mcp/lookup.py (ee288ba).
def norm(name: str) -> str:
    """Matching key: case-, space-, dash- and underscore-insensitive ('Volume Up' -> 'volumeup')."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def suggest(wanted: str, candidates: Iterable[str], *, limit: int = 5) -> list[str]:
    """Up to ``limit`` close matches: substring matches on the normalized key first, then difflib."""
    if limit <= 0:
        return []
    key = norm(wanted)
    if not key:
        return []
    by_norm: dict[str, str] = {}
    for c in candidates:
        n = norm(c)
        if n and n not in by_norm:
            by_norm[n] = c
    substr = [c for n, c in by_norm.items() if key in n or n in key]
    fuzzy = [by_norm[k] for k in difflib.get_close_matches(key, list(by_norm.keys()), n=limit, cutoff=0.5)]
    return list(dict.fromkeys([*substr, *fuzzy]))[:limit]


# --------------------------------------------------------------------------- JS helpers
def _u16(s: str) -> bytes:
    """Sort key that orders strings like JavaScript's ``<`` (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _round_half_up(x: float) -> float:
    """JS Math.round."""
    return math.floor(x + 0.5)


def _num(v: Any) -> float | None:
    """JS ``typeof v === "number" && isFinite(v) ? v : null``; bools aren't numbers there."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _js_str(v: Any) -> str:
    """JS String(v) for the JSON-ish values clean() sees."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return str(int(v))
    return str(v)


def _js_number(v: Any) -> float:
    """JS Number(v) for scalars; nan when it isn't one."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, int | float):
        return float(v)
    if v is None:
        return 0.0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        if "_" in s:
            return math.nan
        try:
            return float(s)
        except ValueError:
            return math.nan
    return math.nan


def _int_if_whole(x: float) -> float | int:
    return int(x) if isinstance(x, float) and x == int(x) else x


# --------------------------------------------------------------------------- names
def _strip_edges(x: str) -> str:
    i, j = 0, len(x)
    while i < j and not x[i].isalnum():
        i += 1
    while j > i and not x[j - 1].isalnum():
        j -= 1
    return x[i:j]


def name_key(raw: Any) -> str:
    """The page's nameKey(): NFKC, lower case, edges stripped, trailing corporate suffixes removed."""
    text = "" if raw is None else str(raw)
    for ch in "™®©":
        text = text.replace(ch, "")
    t = _strip_edges(unicodedata.normalize("NFKC", text).lower()).split()
    while len(t) > 1 and "".join(c for c in t[-1] if c.isalnum()) in NAME_SUFFIXES:
        t.pop()
        t[-1] = _strip_edges(t[-1])
        while len(t) > 1 and t[-1] == "":
            t.pop()
    return " ".join(t)


# A few names people use for a Steam tag, mapped onto that tag's key (the page's TAG_ALIASES).
TAG_ALIASES = {"soulsborne": "soulslike", "soulsbourne": "soulslike"}


def tag_key(raw: Any) -> str:
    """The page's tagKey(): NFKC, lower case, letters and digits only, then the alias table. "Souls-like",
    "souls like" and "Soulslike" share a key, and so does "Soulsborne"."""
    text = unicodedata.normalize("NFKC", "" if raw is None else str(raw)).lower()
    k = "".join(c for c in text if unicodedata.category(c)[0] in "LN")
    return TAG_ALIASES.get(k, k)


# --------------------------------------------------------------------------- games
@dataclass(frozen=True)
class Hltb:
    main: float | None
    extra: float | None
    complete: float | None

    def get(self, measure: str) -> float | None:
        return {"main": self.main, "extra": self.extra, "complete": self.complete}[measure]


@dataclass(frozen=True)
class Proton:
    tier: str
    total: float


@dataclass(frozen=True)
class Reviews:
    total: float
    pct: float | None
    desc: str | None


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
    has_proton: bool  # the export has a "proton" key at all
    has_details: bool  # store details were fetched (genres or a year)
    export_index: int = 0  # position in the export: the page's stable-sort tie-break
    publishers: tuple[str, ...] = ()
    developers: tuple[str, ...] = ()
    pub_keys: tuple[str, ...] = ()
    dev_keys: tuple[str, ...] = ()
    reviews: Reviews | None = None
    tags: tuple[str, ...] = ()  # Steam store tags as Steam spells them, in vote order
    tag_keys: tuple[str, ...] = ()  # tag_key() of each, deduplicated


def _names(v: Any) -> tuple[str, ...]:
    """normalizeExtras names(): strings, trimmed, blanks dropped, at most 10."""
    if not isinstance(v, list):
        return ()
    return tuple(x.strip() for x in v if isinstance(x, str) and x.strip())[:10]


def _keys_by(key_fn: Callable[[Any], str], names: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(k for k in map(key_fn, names) if k))


def _keys(names: Sequence[str]) -> tuple[str, ...]:
    return _keys_by(name_key, names)


def normalize(raw: Mapping[str, Any], index: int = 0) -> Game:
    """The page's normalizeGame() (normalize + normalizeExtras) and annotateNames()."""
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
    r = raw.get("reviews")
    reviews: Reviews | None = None
    if isinstance(r, Mapping):
        reviews = Reviews(
            total=max(0.0, _num(r.get("total")) or 0), pct=_num(r.get("pct")),
            desc=r["desc"] if isinstance(r.get("desc"), str) else None)
    elif isinstance(r, list):  # a JS array is an object too: no usable fields
        reviews = Reviews(0.0, None, None)
    publishers, developers = _names(raw.get("publishers")), _names(raw.get("developers"))
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
        publishers=publishers, developers=developers,
        pub_keys=_keys(publishers), dev_keys=_keys(developers),
        reviews=reviews,
        tags=tags, tag_keys=_keys_by(tag_key, tags),
    )


def play_state(g: Game) -> str:
    if g.playtime == 0:
        return "never"
    return "tried" if g.playtime < TRIED_MIN else "played"


def length_hours(g: Game) -> float | None:
    """mainH(): ``hltb.main || hltb.extra``. A main time of 0 falls through to extras."""
    if g.hltb is None:
        return None
    return g.hltb.main or g.hltb.extra or None


def _main_len(g: Game, src: str) -> float | None:
    """The page's mainLen(): one measure, no fallback."""
    return g.hltb.get(src) if g.hltb else None


def len_bucket(g: Game) -> str:
    h = length_hours(g)
    if not h:
        return "none"
    return "short" if h < 5 else "mid" if h < 15 else "long" if h < 40 else "epic"


def lane_of(g: Game, triage: Mapping[int, str]) -> str:
    return triage.get(g.appid) or ""


def is_closed(g: Game, triage: Mapping[int, str]) -> bool:
    return lane_of(g, triage) in ("skip", "done")


def in_backlog(g: Game, triage: Mapping[int, str]) -> bool:
    return play_state(g) != "played" and not is_closed(g, triage)


def review_bucket(g: Game) -> str | None:
    """None when the export has no review data; "few" when Steam gave no label."""
    if not g.reviews:
        return None
    for key, label in REVIEW_LEVELS:
        if key != "few" and label == g.reviews.desc:
            return key
    return "few"


def mc_bucket(g: Game) -> str | None:
    if not g.metacritic:
        return None
    for key, _label, lo, hi in MC_BUCKETS:
        if (lo is None or g.metacritic >= lo) and (hi is None or g.metacritic <= hi):
            return key
    return None


def is_unknown(g: Game, k: str) -> bool:
    if k == "gen":
        return not g.genres
    if k == "pub":
        return not g.pub_keys
    if k == "dev":
        return not g.dev_keys
    if k == "tag":
        return not g.tag_keys
    if k == "pt":
        return not (g.proton and g.proton.tier)
    if k == "mc":
        return not g.metacritic
    if k == "yr":
        return not g.year
    if k == "ctrl":
        return not g.controller and not g.has_details
    if k == "rv":
        return review_bucket(g) is None
    return False


# --------------------------------------------------------------------------- score
@dataclass(frozen=True)
class Term:
    term: str  # play | deck | length | metacritic | deck_played | controller | next
    label: str  # "never played", "Verified", "8 h", "MC 86", ...
    points: float


def score_breakdown(g: Game, lane: str) -> list[Term]:
    """score() term by term; the points add up to score() exactly. Zero terms are left out (play stays)."""
    out: list[Term] = []
    st = play_state(g)
    labels = {"never": "never played", "tried": "tried (under 2 h)", "played": "played 2 h+"}
    out.append(Term("play", labels[st], {"never": 30, "tried": 22, "played": -20}[st]))
    deck_pts = {3: 30, 2: 14, 0: 0, 1: -45}[g.deck]
    if deck_pts:
        out.append(Term("deck", DECK_LABEL[g.deck], deck_pts))
    h = length_hours(g)
    if h:
        pts = 24 if h <= 5 else 18 if h <= 12 else 9 if h <= 25 else 0 if h <= 50 else -10
        if pts:
            out.append(Term("length", f"{h:g} h", pts))
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
    """The page's DEFAULT_F, field for field, plus protondb_min. Empty / None means "any"."""

    view: str = "backlog"
    query: str = ""
    deck: tuple[int, ...] = ()
    genres: tuple[str, ...] = ()
    genre_match: str = "any"
    genres_unknown: bool = False
    length_measure: str = "main"
    hours_min: float | None = None
    hours_max: float | None = None
    length_none: bool = False
    length_unknown: bool = False
    protondb: tuple[str, ...] = ()
    protondb_unknown: bool = False
    protondb_min: str | None = None  # MCP-only; never set from a page state
    metacritic_min: float | None = None
    metacritic_max: float | None = None
    metacritic_unknown: bool = False
    year_from: float | None = None
    year_to: float | None = None
    year_unknown: bool = False
    controller: tuple[str, ...] = ()
    controller_unknown: bool = False
    publishers: tuple[str, ...] = ()  # normalized keys (name_key)
    publishers_unknown: bool = False
    developers: tuple[str, ...] = ()
    developers_unknown: bool = False
    tags: tuple[str, ...] = ()  # normalized keys (tag_key)
    tags_unknown: bool = False
    reviews: tuple[str, ...] = ()
    reviews_unknown: bool = False
    unknown_only: tuple[str, ...] = ()  # UNK_KEYS
    play_state: tuple[str, ...] = ()
    lane: tuple[str, ...] = ()  # "none" = untriaged
    has_note: bool | None = None

    def active(self) -> list[str]:
        """Names of the filters that are set, weakest first (the relaxation order). View is never relaxed."""
        names: list[str] = []
        if self.metacritic_min is not None or self.metacritic_max is not None:
            names.append("metacritic")
        if self.reviews:
            names.append("reviews")
        if self.year_from is not None or self.year_to is not None:
            names.append("year")
        if self.protondb or self.protondb_min:
            names.append("protondb")
        if self.controller:
            names.append("controller")
        if self.publishers:
            names.append("publishers")
        if self.developers:
            names.append("developers")
        if self.tags:
            names.append("tags")
        if self.genres:
            names.append("genres")
        if self.has_note is not None:
            names.append("has_note")
        if self.unknown_only:
            names.append("unknown_only")
        if self.length_none or self.hours_min is not None or self.hours_max is not None:
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
            "query": {"query": ""},
            "metacritic": {"metacritic_min": None, "metacritic_max": None, "metacritic_unknown": False},
            "reviews": {"reviews": (), "reviews_unknown": False},
            "year": {"year_from": None, "year_to": None, "year_unknown": False},
            "protondb": {"protondb": (), "protondb_min": None, "protondb_unknown": False},
            "controller": {"controller": (), "controller_unknown": False},
            "publishers": {"publishers": (), "publishers_unknown": False},
            "developers": {"developers": (), "developers_unknown": False},
            "tags": {"tags": (), "tags_unknown": False},
            "genres": {"genres": (), "genres_unknown": False},
            "has_note": {"has_note": None},
            "unknown_only": {"unknown_only": ()},
            "length": {"hours_min": None, "hours_max": None, "length_none": False, "length_unknown": False},
            "deck": {"deck": ()},
            "play_state": {"play_state": ()},
            "lane": {"lane": ()},
        }
        return replace(self, **cleared[name])


def _pick_in(v: Any, allowed: Sequence[str]) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x for x in dict.fromkeys(_js_str(i) for i in v) if x in allowed]


def _num_in(v: Any, lo: float, hi: float) -> float | None:
    if v is None or v == "":
        return None
    x = _js_number(v)
    return x if math.isfinite(x) and lo <= x <= hi else None


def _names_in(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list):
        return ()
    return tuple(x for x in dict.fromkeys(_js_str(i) for i in v) if x and len(x) <= 80)[:50]


def clean(raw: Mapping[str, Any]) -> Filters:
    """The page's clean(): a page-shaped state (DEFAULT_F keys) -> Filters; invalid values are dropped."""
    o = raw or {}
    mc = _js_number(o.get("mc"))
    mcmax = _js_number(o.get("mcmax"))
    gen = o.get("gen")
    q = o.get("q")
    return Filters(
        view=o["view"] if o.get("view") in VIEWS else "backlog",
        query=q[:200] if isinstance(q, str) else "",
        deck=tuple(int(x) for x in _pick_in(o.get("deck"), ["0", "1", "2", "3"])),
        genres=tuple(x for x in dict.fromkeys(_js_str(i) for i in gen) if x and len(x) <= 60)[:20]
        if isinstance(gen, list) else (),
        genres_unknown=o.get("genUnk") is True,
        genre_match="all" if o.get("gm") == "all" else "any",
        length_measure=o["dsrc"] if o.get("dsrc") in SRCS else "main",
        hours_min=_num_in(o.get("dmin"), 0, 1000),
        hours_max=_num_in(o.get("dmax"), 0, 1000),
        length_none=o.get("dnone") is True,
        length_unknown=o.get("durUnk") is True,
        protondb=tuple(_pick_in(o.get("pt"), TIERS)),
        protondb_unknown=o.get("ptUnk") is True,
        metacritic_min=mc if mc in MC_OPTS else None,
        metacritic_max=mcmax if o.get("mcmax") is not None and mcmax in MC_MAX_OPTS else None,
        metacritic_unknown=o.get("mcUnk") is True,
        year_from=_num_in(o.get("ymin"), 1950, 2100),
        year_to=_num_in(o.get("ymax"), 1950, 2100),
        year_unknown=o.get("yrUnk") is True,
        controller=tuple(_pick_in(o.get("ctrl"), CTRLS)),
        controller_unknown=o.get("ctrlUnk") is True,
        publishers=_names_in(o.get("pub")),
        publishers_unknown=o.get("pubUnk") is True,
        developers=_names_in(o.get("dev")),
        developers_unknown=o.get("devUnk") is True,
        tags=_keys_by(tag_key, _names_in(o.get("tag"))),
        tags_unknown=o.get("tagUnk") is True,
        reviews=tuple(_pick_in(o.get("rv"), REVIEW_KEYS)),
        reviews_unknown=o.get("rvUnk") is True,
        unknown_only=tuple(_pick_in(o.get("unk"), UNK_KEYS)),
        play_state=tuple(_pick_in(o.get("play"), PLAYS)),
        lane=tuple(_pick_in(o.get("lane"), LANE_F)),
        has_note={"yes": True, "no": False}.get(o.get("notes")) if isinstance(o.get("notes"), str) else None,
    )


def passes(g: Game, f: Filters, triage: Mapping[int, str], notes: Mapping[int, str]) -> bool:
    """The page's passes(), in its order. The view and the search box are selected()'s."""
    if f.deck and g.deck not in f.deck:
        return False
    if f.genres:
        if not g.genres:
            if not f.genres_unknown:
                return False
        elif not (all(x in g.genres for x in f.genres) if f.genre_match == "all"
                  else any(x in g.genres for x in f.genres)):
            return False
    if f.length_none or f.hours_min is not None or f.hours_max is not None:
        v = _main_len(g, f.length_measure)
        if f.length_none:
            if v is not None:
                return False
        elif v is None:
            if not f.length_unknown:
                return False
        elif (f.hours_min is not None and v < f.hours_min) or (f.hours_max is not None and v > f.hours_max):
            return False
    if f.protondb or f.protondb_min:
        tier = g.proton.tier if g.proton else None
        if not tier:
            if not f.protondb_unknown:
                return False
        else:
            if f.protondb and tier not in f.protondb:
                return False
            if f.protondb_min and TIER_RANK.get(tier, 0) < TIER_RANK[f.protondb_min]:
                return False
    if f.metacritic_min is not None or f.metacritic_max is not None:
        if not g.metacritic:
            if not f.metacritic_unknown:
                return False
        elif ((f.metacritic_min is not None and g.metacritic < f.metacritic_min)
              or (f.metacritic_max is not None and g.metacritic > f.metacritic_max)):
            return False
    if f.publishers:
        if not g.pub_keys:
            if not f.publishers_unknown:
                return False
        elif not any(k in f.publishers for k in g.pub_keys):
            return False
    if f.developers:
        if not g.dev_keys:
            if not f.developers_unknown:
                return False
        elif not any(k in f.developers for k in g.dev_keys):
            return False
    if f.tags:
        if not g.tag_keys:
            if not f.tags_unknown:
                return False
        elif not any(k in f.tags for k in g.tag_keys):
            return False
    if f.reviews:
        b = review_bucket(g)
        if b is None:
            if not f.reviews_unknown:
                return False
        elif b not in f.reviews:
            return False
    for k in f.unknown_only:
        if not is_unknown(g, k):
            return False
    if f.year_from is not None or f.year_to is not None:
        if not g.year:
            if not f.year_unknown:
                return False
        elif (f.year_from is not None and g.year < f.year_from) or (f.year_to is not None and g.year > f.year_to):
            return False
    if f.controller:
        if not g.controller and not g.has_details:
            if not f.controller_unknown:
                return False
        elif (g.controller or "none") not in f.controller:
            return False
    if f.play_state and play_state(g) not in f.play_state:
        return False
    if f.lane and (lane_of(g, triage) or "none") not in f.lane:
        return False
    return not (f.has_note is not None and f.has_note != (g.appid in notes))


def _in_view(g: Game, view: str, triage: Mapping[int, str]) -> bool:
    ln = lane_of(g, triage)
    if view == "backlog":
        return in_backlog(g, triage)
    if view == "next":
        return ln == "next"
    if view == "someday":
        return ln == "someday"
    if view == "played":
        return play_state(g) == "played" and not is_closed(g, triage)
    if view == "closed":
        return is_closed(g, triage)
    return True


def _matches(g: Game, query: str) -> bool:
    q = query.strip().lower()
    return not q or q in g.name.lower() or any(q in x.lower() for x in g.genres)


def selected(games: Iterable[Game], f: Filters, triage: Mapping[int, str], notes: Mapping[int, str]) -> list[Game]:
    """The page's selected(): the view, then passes(), then the search box; library order."""
    return [g for g in games if _in_view(g, f.view, triage) and passes(g, f, triage, notes) and _matches(g, f.query)]


def _name_sort(g: Game) -> tuple[str, str]:
    # JS localeCompare is locale-aware; casefold first, then the raw name, is close and deterministic.
    return (g.name.casefold(), g.name)


def sort_games(games: list[Game], sort: str, triage: Mapping[int, str]) -> list[Game]:
    """The page's list sort; every comparison falls back to the name."""
    key: Callable[[Game], Any]
    if sort == "short":
        key = lambda g: (length_hours(g) or 1e9, _name_sort(g))  # noqa: E731
    elif sort == "name":
        key = _name_sort
    elif sort == "recent":
        key = lambda g: (-g.last_played, _name_sort(g))  # noqa: E731
    elif sort == "most":
        key = lambda g: (-g.playtime, _name_sort(g))  # noqa: E731
    elif sort == "mc":
        key = lambda g: (-(g.metacritic or 0), _name_sort(g))  # noqa: E731
    else:
        key = lambda g: (-score(g, lane_of(g, triage)), _name_sort(g))  # noqa: E731
    return sorted(games, key=key)


@dataclass
class FindResult:
    games: list[Game]
    exact: bool = True
    relaxed: list[str] = field(default_factory=list)
    fuzzy_query: bool = False


def find(games: Sequence[Game], f: Filters, triage: Mapping[int, str], notes: Mapping[int, str],
         *, sort: str = "score", relax: bool = True) -> FindResult:
    """Filter and sort. With nothing found and ``relax``: a fuzzy name first, then drop filters weakest
    first until something matches, and say what was dropped. ``f.view`` is applied and never relaxed."""
    hits = selected(games, f, triage, notes)
    if hits or not relax:
        return FindResult(sort_games(hits, sort, triage))
    if f.query.strip():
        pool = selected(games, f.without("query"), triage, notes)
        close = suggest(f.query, [g.name for g in pool], limit=10)
        rank = {name: i for i, name in enumerate(close)}
        fuzzy = [g for g in pool if g.name in rank]
        if fuzzy:
            fuzzy.sort(key=lambda g: rank[g.name])  # "did you mean" beats the requested sort here
            return FindResult(fuzzy, exact=False, fuzzy_query=True)
    relaxed: list[str] = []
    current = f
    for name in f.active():
        current = current.without(name)
        relaxed.append(name)
        hits = selected(games, current, triage, notes)
        if hits:
            return FindResult(sort_games(hits, sort, triage), exact=False, relaxed=relaxed)
    return FindResult([], exact=False, relaxed=relaxed)


def picks(games: Sequence[Game], f: Filters, triage: Mapping[int, str], notes: Mapping[int, str],
          *, exclude_next: bool = False) -> list[Game]:
    """renderPicks(): backlog games passing the filters, by score, ties in export order."""
    pool = [g for g in selected(games, replace(f, view="backlog"), triage, notes)
            if not (exclude_next and lane_of(g, triage) == "next")]
    return sorted(pool, key=lambda g: (-score(g, lane_of(g, triage)), g.export_index))


def next_queue(games: Sequence[Game], triage: Mapping[int, str], order: Sequence[int]) -> list[Game]:
    """nextIds(): the Up next lane in saved order, then unordered ones by score, then name."""
    index = {a: i for i, a in enumerate(order)}
    lane = [g for g in games if lane_of(g, triage) == "next"]
    return sorted(lane, key=lambda g: (index.get(g.appid, 10**9), -score(g, "next"), _name_sort(g)))


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
    deck = {DECK_NAME[d]: 0 for d in DECK_ORDER}
    grid = {DECK_NAME[d]: {b: 0 for b in LEN_BUCKETS} for d in DECK_ORDER}
    lanes = {k: 0 for k in (*LANES, "none")}
    backlog = 0
    cov = {"hltb": 0, "protondb": 0, "details": 0, "metacritic": 0, "deck_rating": 0,
           "publishers": 0, "developers": 0, "reviews": 0}
    for g in games:
        states[play_state(g)] += 1
        deck[DECK_NAME[g.deck]] += 1
        lanes[lane_of(g, triage) if lane_of(g, triage) in LANES else "none"] += 1
        if in_backlog(g, triage):
            backlog += 1
            grid[DECK_NAME[g.deck]][len_bucket(g)] += 1
        cov["hltb"] += g.hltb is not None
        cov["protondb"] += g.proton is not None
        cov["details"] += g.has_details
        cov["metacritic"] += bool(g.metacritic)
        cov["deck_rating"] += g.deck != 0
        cov["publishers"] += bool(g.pub_keys)
        cov["developers"] += bool(g.dev_keys)
        cov["reviews"] += g.reviews is not None
    coverage = {k: round(v / n, 3) if n else 0.0 for k, v in cov.items()}
    return Overview(n, states, deck, backlog, grid, lanes, coverage)


# --------------------------------------------------------------------------- resolving a game
@dataclass(frozen=True)
class Resolved:
    """A game argument resolved to one game, or not: then ``candidates`` (best first)."""

    query: str
    game: Game | None
    candidates: tuple[Game, ...] = ()
    reason: str = "ok"  # ok | ambiguous | unknown_appid | no_match


def resolve(ref: str | int, games: Sequence[Game]) -> Resolved:
    """An app id or a name -> exactly one game, or candidates. Never a guess."""
    text = str(ref).strip()
    by_id = {g.appid: g for g in games}
    if isinstance(ref, int) or text.isdigit():
        g = by_id.get(int(text))
        if g is not None:
            return Resolved(text, g)
        if isinstance(ref, int):
            return Resolved(text, None, reason="unknown_appid")
        # A title made of digits ("2048") can still be a name: fall through.
    key = norm(text)
    exact = [g for g in games if norm(g.name) == key] if key else []
    if len(exact) == 1:
        return Resolved(text, exact[0])
    if len(exact) > 1:
        return Resolved(text, None, tuple(exact), reason="ambiguous")
    cands: list[Game] = []
    for name in suggest(text, [g.name for g in games], limit=5):
        cands.extend(g for g in games if g.name == name)
    return Resolved(text, None, tuple(cands[:8]), reason="unknown_appid" if text.isdigit() else "no_match")


# --------------------------------------------------------------------------- statistics
STAT_LABELS: dict[str, dict[Any, str]] = {
    "deck": {3: "Verified", 2: "Playable", 1: "Unsupported", 0: "Unknown"},
    "pt": {"native": "Native", "platinum": "Platinum", "gold": "Gold", "silver": "Silver", "bronze": "Bronze",
           "borked": "Borked", "pending": "Pending"},
    "ctrl": {"full": "Full", "partial": "Partial", "none": "None listed"},
    "len": {"short": "Under 5 h", "mid": "5–15 h", "long": "15–40 h", "epic": "40 h+"},
    "lane": {"none": "No lane", "next": "Up next", "someday": "Someday", "skip": "Skipped", "done": "Finished"},
}
LEN_ORDER = ("short", "mid", "long", "epic")
LANE_ORDER = ("next", "someday", "skip", "done")
STAT_DIMS = ("gen", "pub", "dev", "tag", "deck", "pt", "ctrl", "len", "lane", "mc", "rv", "year")


def _stat_row(key: Any, label: str) -> dict[str, Any]:
    return {"key": key, "label": label, "n": 0, "minutes": 0, "never": 0, "tried": 0, "played": 0}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    a = sorted(xs)
    m = len(a) >> 1
    return a[m] if len(a) % 2 else (a[m - 1] + a[m]) / 2


def name_groups(games: Iterable[Game], field_: str) -> dict[str, dict[str, Any]]:
    """key -> {key, label, n}: n counts games once each; label is the most common spelling (ties: A to Z)."""
    groups: dict[str, dict[str, Any]] = {}
    for g in games:
        raws, keys, key_fn = {"pub": (g.publishers, g.pub_keys, name_key), "dev": (g.developers, g.dev_keys, name_key),
                              "tag": (g.tags, g.tag_keys, tag_key)}[field_]
        for raw in raws:
            key = key_fn(raw)
            if not key:
                continue
            e = groups.setdefault(key, {"key": key, "label": "", "n": 0, "spell": {}})
            e["spell"][raw] = e["spell"].get(raw, 0) + 1
        for key in keys:
            if key in groups:
                groups[key]["n"] += 1
    for e in groups.values():
        e["label"] = sorted(e["spell"].items(), key=lambda kv: (-kv[1], _u16(kv[0])))[0][0]
        del e["spell"]
    return groups


def stats_for(games: Sequence[Game], *, dsrc: str = "main", lane_of: Callable[[Game], str] | None = None,
              pub_labels: Mapping[str, str] | None = None, dev_labels: Mapping[str, str] | None = None,
              tag_labels: Mapping[str, str] | None = None) -> dict:
    """The page's statsFor(): ``{"tiles": {...}, "dims": {gen, pub, dev, tag, deck, pt, ctrl, len, lane, mc, rv, year}}``
    with the same camelCase keys; json.dumps of it equals JSON.stringify of the page's result."""
    dsrc = dsrc if dsrc in SRCS else "main"
    lane_fn = lane_of if callable(lane_of) else (lambda _g: "")
    if pub_labels is None:
        pub_labels = {k: e["label"] for k, e in name_groups(games, "pub").items()}
    if dev_labels is None:
        dev_labels = {k: e["label"] for k, e in name_groups(games, "dev").items()}
    if tag_labels is None:
        tag_labels = {k: e["label"] for k, e in name_groups(games, "tag").items()}
    rows: dict[str, dict[Any, dict[str, Any]]] = {d: {} for d in STAT_DIMS}
    unknown: dict[str, dict[str, Any] | None] = {d: None for d in STAT_DIMS}
    known = {d: 0 for d in STAT_DIMS}

    def bump(r: dict[str, Any], g: Game, st: str) -> None:
        r["n"] += 1
        r["minutes"] += g.playtime
        r[st] += 1

    def put(d: str, key: Any, label: str, g: Game, st: str) -> None:
        r = rows[d].get(key)
        if r is None:
            r = rows[d][key] = _stat_row(key, label)
        bump(r, g, st)

    def unk(d: str, key: Any, label: str, g: Game, st: str) -> None:
        if unknown[d] is None:
            unknown[d] = _stat_row(key, label)
        bump(unknown[d], g, st)  # type: ignore[arg-type]

    tiles: dict[str, Any] = {"games": len(games), "minutes": 0, "deckMinutes": 0, "never": 0, "tried": 0,
                             "played": 0, "backlogGames": 0, "backlogWithTime": 0, "backlogHours": 0,
                             "mcMedian": None, "mcCount": 0, "rvMedian": None, "rvCount": 0}
    mcs: list[float] = []
    rvs: list[float] = []
    for g in games:
        st, ln = play_state(g), lane_fn(g) or ""
        tiles["minutes"] += g.playtime
        tiles["deckMinutes"] += g.deck_playtime
        tiles[st] += 1
        if st != "played" and ln not in ("skip", "done"):
            tiles["backlogGames"] += 1
            h = _main_len(g, dsrc)
            if h is not None:
                tiles["backlogWithTime"] += 1
                tiles["backlogHours"] += h
        if g.metacritic:
            mcs.append(g.metacritic)
        if g.reviews and g.reviews.total > 0 and g.reviews.pct is not None:
            rvs.append(g.reviews.pct)

        genres = list(dict.fromkeys(g.genres))
        if genres:
            known["gen"] += 1
            for x in genres:
                put("gen", x, x, g, st)
        else:
            unk("gen", "", "Unknown", g, st)
        if g.pub_keys:
            known["pub"] += 1
            for k in g.pub_keys:
                put("pub", k, pub_labels.get(k) or k, g, st)
        else:
            unk("pub", "", "Unknown", g, st)
        if g.dev_keys:
            known["dev"] += 1
            for k in g.dev_keys:
                put("dev", k, dev_labels.get(k) or k, g, st)
        else:
            unk("dev", "", "Unknown", g, st)
        if g.tag_keys:
            known["tag"] += 1
            for k in g.tag_keys:
                put("tag", k, tag_labels.get(k) or k, g, st)
        else:
            unk("tag", "", "Unknown", g, st)
        if g.deck:
            known["deck"] += 1
            put("deck", g.deck, STAT_LABELS["deck"][g.deck], g, st)
        else:
            unk("deck", 0, STAT_LABELS["deck"][0], g, st)
        tier = g.proton.tier if g.proton and g.proton.tier in TIERS else None
        if tier:
            known["pt"] += 1
            put("pt", tier, STAT_LABELS["pt"][tier], g, st)
        else:
            unk("pt", "", "Unknown", g, st)
        if is_unknown(g, "ctrl"):
            unk("ctrl", "", "Unknown", g, st)
        else:
            c = g.controller or "none"
            known["ctrl"] += 1
            put("ctrl", c, STAT_LABELS["ctrl"][c], g, st)
        h = _main_len(g, dsrc)
        if h is not None:
            lb = "short" if h < 5 else "mid" if h < 15 else "long" if h < 40 else "epic"
            known["len"] += 1
            put("len", lb, STAT_LABELS["len"][lb], g, st)
        else:
            unk("len", "none", "No HLTB time", g, st)
        if ln:
            known["lane"] += 1
            put("lane", ln, STAT_LABELS["lane"].get(ln, ln), g, st)
        else:
            unk("lane", "none", STAT_LABELS["lane"]["none"], g, st)
        mb = mc_bucket(g)
        if mb:
            known["mc"] += 1
            put("mc", mb, next(b[1] for b in MC_BUCKETS if b[0] == mb), g, st)
        else:
            unk("mc", "", "Unknown", g, st)
        rb = review_bucket(g)
        if rb:
            known["rv"] += 1
            put("rv", rb, next(lv[1] for lv in REVIEW_LEVELS if lv[0] == rb), g, st)
        else:
            unk("rv", "", "Unknown", g, st)
        if g.year:
            known["year"] += 1
            y = _int_if_whole(g.year)
            put("year", y, str(y), g, st)
        else:
            unk("year", "", "Unknown", g, st)

    tiles["backlogHours"] = _round_half_up(tiles["backlogHours"] * 10) / 10
    tiles["mcMedian"], tiles["mcCount"] = _median(mcs), len(mcs)
    tiles["rvMedian"], tiles["rvCount"] = _median(rvs), len(rvs)

    def ordered(d: str, order: Iterable[Any]) -> list[dict[str, Any]]:
        return [rows[d][k] for k in order if k in rows[d]]

    out: dict[str, dict[str, Any]] = {}
    for d in ("gen", "pub", "dev", "tag"):
        out[d] = {"rows": sorted(rows[d].values(), key=lambda r: (-r["n"], _u16(r["label"])))}
    out["deck"] = {"rows": ordered("deck", (3, 2, 1))}
    out["pt"] = {"rows": ordered("pt", TIERS)}
    out["ctrl"] = {"rows": ordered("ctrl", CTRLS)}
    out["len"] = {"rows": ordered("len", LEN_ORDER)}
    out["lane"] = {"rows": ordered("lane", LANE_ORDER)}
    out["mc"] = {"rows": ordered("mc", [b[0] for b in MC_BUCKETS])}
    out["rv"] = {"rows": ordered("rv", REVIEW_KEYS)}
    yrows: list[dict[str, Any]] = []
    if rows["year"]:
        y = min(rows["year"])
        hi = max(rows["year"])
        while y <= hi:
            yrows.append(rows["year"].get(_int_if_whole(y)) or _stat_row(_int_if_whole(y), str(_int_if_whole(y))))
            y += 1
    out["year"] = {"rows": yrows}
    for d in out:
        out[d].update(unknown=unknown[d], known=known[d], total=len(games))
    return {"tiles": tiles, "dims": out}


def year_buckets(rows: Sequence[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    """Release years grouped into decades (1990s, 2000s...). Any step but 10 returns the rows as they are."""
    if step != 10:
        return list(rows)
    m: dict[int, dict[str, Any]] = {}
    for r in rows:
        k = math.floor(r["key"] / 10) * 10
        e = m.get(k)
        if e is None:
            e = m[k] = _stat_row(k, f"{k}s")
        for f_ in ("n", "minutes", "never", "tried", "played"):
            e[f_] += r[f_]
    return sorted(m.values(), key=lambda r: r["key"])


def _metric_value(r: Mapping[str, Any], metric: str) -> float:
    if metric == "hours":
        return r["minutes"] / 60
    if metric == "never":
        return 100 * r["never"] / r["n"] if r["n"] else 0
    return r["n"]


def rank_rows(rows: Sequence[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Highest first; ties go to the bigger group, then A to Z."""
    return sorted(rows, key=lambda r: (-_metric_value(r, metric), -r["n"], _u16(r["label"])))


def fold_rows(rows: Sequence[dict[str, Any]], top: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The first ``top`` rows plus one folded "Other" row for the rest (or None)."""
    if len(rows) <= top:
        return list(rows), None
    other: dict[str, Any] = {"key": "", "label": "Other", "values": len(rows) - top,
                             "n": 0, "minutes": 0, "never": 0, "tried": 0, "played": 0}
    for r in rows[top:]:
        for f_ in ("n", "minutes", "never", "tried", "played"):
            other[f_] += r[f_]
    return list(rows[:top]), other


# --------------------------------------------------------------------------- snapshot and tool inputs
@dataclass
class Snapshot:
    games: list[Game]
    triage: dict[int, str] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    exported_at: str | None = None


def _bad(name: str, allowed: Any, got: Any = None) -> QueryError:
    shown = allowed if isinstance(allowed, str) else ", ".join(str(a) for a in allowed)
    tail = "" if got is None else f" (got {str(got)[:40]!r})"
    return QueryError(f"{name} must be {shown}{tail}")


def _enum(name: str, v: Any, allowed: Sequence[Any]) -> Any:
    if isinstance(v, bool) or v not in allowed:
        raise _bad(name, f"one of: {', '.join(str(a) for a in allowed)}", v)
    return v


def _enum_list(name: str, v: Any, allowed: Sequence[Any]) -> tuple[Any, ...]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list | tuple):
        raise _bad(name, f"a list of: {', '.join(str(a) for a in allowed)}", v)
    out: list[Any] = []
    for x in items:
        if isinstance(x, bool) or x not in allowed:
            raise _bad(name, f"a list of: {', '.join(str(a) for a in allowed)}", x)
        if x not in out:
            out.append(x)
    return tuple(out)


def _number(name: str, v: Any, lo: float, hi: float, *, integer: bool = False) -> float | int:
    ok = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
    if ok and integer and v != int(v):
        ok = False
    if not ok:
        raise _bad(name, f"{'an integer' if integer else 'a number'} from {lo:g} to {hi:g}", v)
    return int(v) if integer else v


def _str_list(name: str, v: Any, max_items: int, max_len: int) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list | tuple) or len(items) > max_items or not all(
            isinstance(x, str) and len(x) <= max_len for x in items):
        raise _bad(name, f"a list of at most {max_items} strings of up to {max_len} characters")
    return list(items)


_UNK_TOOL = {"genres": "gen", "publishers": "pub", "developers": "dev", "tags": "tag", "protondb": "pt", "metacritic": "mc",
             "year": "yr", "controller": "ctrl", "reviews": "rv"}
_INCLUDE_FLAG = {"genres": "genres_unknown", "length": "length_unknown", "protondb": "protondb_unknown",
                 "metacritic": "metacritic_unknown", "year": "year_unknown", "controller": "controller_unknown",
                 "publishers": "publishers_unknown", "developers": "developers_unknown", "tags": "tags_unknown",
                 "reviews": "reviews_unknown"}


def tool_filters(*, query: str | None = None, genres: Any = None, genre_match: str = "any",
                 length: str | None = None, length_measure: str = "main", hours_min: float | None = None,
                 hours_max: float | None = None, deck: Any = None, protondb: Any = None,
                 protondb_min: str | None = None, metacritic_min: Any = None, metacritic_max: Any = None,
                 year_from: Any = None, year_to: Any = None, controller: Any = None, publishers: Any = None,
                 developers: Any = None, tags: Any = None, reviews: Any = None, play_state: Any = None, lane: Any = None,
                 has_note: bool | None = None, include_unknown: Any = None, unknown_only: Any = None) -> Filters:
    """Tool-shaped arguments (spec 7.5 names and enums) -> Filters. A bad value raises QueryError."""
    kw: dict[str, Any] = {}
    if query is not None:
        if not isinstance(query, str) or len(query) > 200:
            raise _bad("query", "a string of up to 200 characters")
        kw["query"] = query
    if genres is not None:
        kw["genres"] = tuple(dict.fromkeys(_str_list("genres", genres, 20, 60)))
    kw["genre_match"] = _enum("genre_match", genre_match, ("any", "all"))
    kw["length_measure"] = _enum("length_measure", length_measure, SRCS)
    lo = _number("hours_min", hours_min, 0, 1000) if hours_min is not None else None
    hi = _number("hours_max", hours_max, 0, 1000) if hours_max is not None else None
    if length is not None:
        _enum("length", length, ("short", "mid", "long", "epic", "none"))
        if length == "none":
            kw["length_none"] = True
        elif lo is None and hi is None:  # an explicit range overrides the preset
            lo, hi = LEN_PRESET[length]
    kw["hours_min"], kw["hours_max"] = lo, hi
    if deck is not None:
        kw["deck"] = tuple(DECK_BY_NAME[x] for x in _enum_list("deck", deck, tuple(DECK_BY_NAME)))
    if protondb is not None:
        kw["protondb"] = _enum_list("protondb", protondb, TIERS)
    if protondb_min is not None:
        kw["protondb_min"] = _enum("protondb_min", protondb_min, tuple(TIER_RANK))
    if metacritic_min is not None:
        kw["metacritic_min"] = _enum("metacritic_min", metacritic_min, MC_OPTS)
    if metacritic_max is not None:
        kw["metacritic_max"] = _enum("metacritic_max", metacritic_max, MC_MAX_OPTS)
    if year_from is not None:
        kw["year_from"] = _number("year_from", year_from, 1950, 2100, integer=True)
    if year_to is not None:
        kw["year_to"] = _number("year_to", year_to, 1950, 2100, integer=True)
    if controller is not None:
        kw["controller"] = _enum_list("controller", controller, CTRLS)
    if publishers is not None:
        kw["publishers"] = tuple(dict.fromkeys(k for k in map(name_key, _str_list("publishers", publishers, 10, 80)) if k))
    if developers is not None:
        kw["developers"] = tuple(dict.fromkeys(k for k in map(name_key, _str_list("developers", developers, 10, 80)) if k))
    if tags is not None:  # any spelling: matched by tag_key ("soulslike", "Souls-like", "souls like", "soulsborne")
        kw["tags"] = _keys_by(tag_key, _str_list("tags", tags, 10, 80))
    if reviews is not None:
        kw["reviews"] = _enum_list("reviews", reviews, REVIEW_KEYS)
    if play_state is not None:
        kw["play_state"] = _enum_list("play_state", play_state, PLAYS)
    if lane is not None:
        kw["lane"] = _enum_list("lane", lane, LANE_F)
    if has_note is not None:
        if not isinstance(has_note, bool):
            raise _bad("has_note", "true or false", has_note)
        kw["has_note"] = has_note
    if include_unknown is not None:
        for x in _enum_list("include_unknown", include_unknown, tuple(_INCLUDE_FLAG)):
            kw[_INCLUDE_FLAG[x]] = True
    if unknown_only is not None:
        kw["unknown_only"] = tuple(_UNK_TOOL[x] for x in _enum_list("unknown_only", unknown_only, tuple(_UNK_TOOL)))
    return Filters(**kw)


# --------------------------------------------------------------------------- tool outputs
def _hours(minutes: float) -> float:
    return round(minutes / 60, 1)


def _int(v: float | None) -> int | None:
    return int(v) if v is not None else None


def _terms(g: Game, lane: str) -> list[dict[str, Any]]:
    return [{"term": t.term, "label": t.label, "points": t.points} for t in score_breakdown(g, lane)]


def _row(g: Game, snap: Snapshot) -> dict[str, Any]:
    lane = lane_of(g, snap.triage)
    return {
        "appid": g.appid, "name": g.name, "year": _int(g.year), "deck": DECK_NAME[g.deck],
        "play_state": play_state(g), "playtime_hours": _hours(g.playtime), "length_hours": length_hours(g),
        "metacritic": _int(g.metacritic) if g.metacritic else None,
        "protondb": g.proton.tier if g.proton else None,
        "lane": lane if lane in LANES else None, "has_note": g.appid in snap.notes, "score": score(g, lane),
    }


def _detail(g: Game, snap: Snapshot) -> dict[str, Any]:
    lane = lane_of(g, snap.triage)
    controller = g.controller if g.controller in ("full", "partial") else "none" if g.has_details else None
    last = datetime.fromtimestamp(g.last_played, UTC).date().isoformat() if g.last_played else None
    queue = [x.appid for x in next_queue(snap.games, snap.triage, snap.order)]
    return {
        **_row(g, snap),
        "deck_playtime_hours": _hours(g.deck_playtime),
        "last_played": last,
        "hltb": {"main": g.hltb.main, "extra": g.hltb.extra, "complete": g.hltb.complete} if g.hltb else None,
        "protondb_reports": int(g.proton.total) if g.proton else None,
        "genres": list(g.genres), "publishers": list(g.publishers), "developers": list(g.developers),
        "tags": list(g.tags),
        "reviews": {"desc": g.reviews.desc, "pct": g.reviews.pct, "total": g.reviews.total} if g.reviews else None,
        "controller": controller,
        "note": snap.notes.get(g.appid),
        "next_position": queue.index(g.appid) + 1 if g.appid in queue else None,
        "in_backlog": in_backlog(g, snap.triage),
        "score_breakdown": _terms(g, lane), "score_explained": explain(score_breakdown(g, lane)),
        "store_url": STORE_URL.format(appid=g.appid),
    }


def _age_days(exported_at: str | None, now: datetime | None) -> float | None:
    if not exported_at:
        return None
    try:
        then = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round(((now or datetime.now(UTC)) - then).total_seconds() / 86400, 1)


def overview_out(snap: Snapshot, *, now: datetime | None = None) -> dict[str, Any]:
    o = overview(snap.games, snap.triage)
    return {
        "games": o.total, "play_states": o.play_states, "deck": o.deck, "backlog": o.backlog, "grid": o.grid,
        "lanes": o.lanes, "next_queue": o.lanes["next"], "coverage": o.coverage,
        "export": {"exported_at": snap.exported_at, "age_days": _age_days(snap.exported_at, now)},
    }


def _relax_note(exact: bool, relaxed: list[str], fuzzy: bool) -> str:
    if exact:
        return ""
    if fuzzy:
        return " Nothing matched the name exactly; these are the closest names."
    if relaxed:
        return f" Nothing matched as asked; dropped, weakest first: {', '.join(relaxed)}."
    return " Nothing matched, even with filters dropped."


def find_out(snap: Snapshot, filters: Filters, *, view: str = "all", sort: str = "score",
             limit: int = 10, offset: int = 0) -> dict[str, Any]:
    _enum("view", view, VIEWS)
    _enum("sort", sort, SORTS)
    limit = max(1, min(25, int(limit)))
    offset = max(0, int(offset))
    res = find(snap.games, replace(filters, view=view), snap.triage, snap.notes, sort=sort)
    page = res.games[offset:offset + limit]
    return {
        "matches": len(res.games), "offset": offset, "games": [_row(g, snap) for g in page],
        "truncated": offset + len(page) < len(res.games), "exact": res.exact, "relaxed": res.relaxed,
        "fuzzy_query": res.fuzzy_query,
        "detail": f"{len(res.games)} matches; showing {len(page)} from {offset + 1}."
                  + _relax_note(res.exact, res.relaxed, res.fuzzy_query),
    }


def game_out(snap: Snapshot, *, game: str) -> dict[str, Any]:
    r = resolve(game, snap.games)
    if r.game is not None:
        return {"found": True, "game": _detail(r.game, snap)}
    return {"found": False, "unresolved": {
        "query": r.query, "reason": r.reason,
        "candidates": [{"appid": g.appid, "name": g.name, "year": _int(g.year)} for g in r.candidates]}}


def picks_out(snap: Snapshot, filters: Filters, *, count: int = 6, exclude_next: bool = False) -> dict[str, Any]:
    count = max(1, min(12, int(count)))
    res = find(snap.games, replace(filters, view="backlog"), snap.triage, snap.notes)
    pool = [g for g in res.games if not (exclude_next and lane_of(g, snap.triage) == "next")]
    pool.sort(key=lambda g: (-score(g, lane_of(g, snap.triage)), g.export_index))
    out = []
    for i, g in enumerate(pool[:count], 1):
        lane = lane_of(g, snap.triage)
        out.append({**_row(g, snap), "rank": i, "score_breakdown": _terms(g, lane),
                    "score_explained": explain(score_breakdown(g, lane)), "note": snap.notes.get(g.appid)})
    return {
        "picks": out, "considered": len(pool), "exact": res.exact, "relaxed": res.relaxed,
        "detail": f"{len(pool)} backlog games passed; top {len(out)} by couch score."
                  + _relax_note(res.exact, res.relaxed, res.fuzzy_query),
    }


def next_queue_out(snap: Snapshot) -> dict[str, Any]:
    q = next_queue(snap.games, snap.triage, snap.order)
    return {"queue": [{**_row(g, snap), "position": i, "note": snap.notes.get(g.appid)} for i, g in enumerate(q, 1)],
            "detail": f"{len(q)} games in Up next, in queue order."}


_STAT_DIM = {"genre": "gen", "publisher": "pub", "developer": "dev", "tags": "tag", "deck": "deck", "protondb": "pt",
             "controller": "ctrl", "length": "len", "lane": "lane", "metacritic": "mc", "reviews": "rv",
             "year": "year", "decade": "year"}


def _stat_out_row(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"label": r["label"], "games": r["n"], "hours_played": round(r["minutes"] / 60, 1),
            "never": r["never"], "tried": r["tried"], "played": r["played"]}


def stats_out(snap: Snapshot, filters: Filters, *, dimension: str, metric: str = "games",
              view: str = "all") -> dict[str, Any]:
    _enum("dimension", dimension, tuple(_STAT_DIM))
    _enum("metric", metric, ("games", "hours", "never"))
    _enum("view", view, VIEWS)
    pool = selected(snap.games, replace(filters, view=view), snap.triage, snap.notes)
    st = stats_for(pool, dsrc=filters.length_measure, lane_of=lambda g: lane_of(g, snap.triage))
    d = _STAT_DIM[dimension]
    dim = st["dims"][d]
    rows: list[dict[str, Any]] = dim["rows"]
    other: dict[str, Any] | None
    if d in ("gen", "pub", "dev", "tag"):
        rows, other = fold_rows(rank_rows(rows, metric), 25)
    elif dimension == "decade":
        rows, other = fold_rows(year_buckets(rows, 10), 25)
    elif d == "year" and len(rows) > 25:  # oldest years fold into Other, the recent 25 stay
        tail, other = fold_rows(rows[::-1], 25)
        rows = tail[::-1]
    else:
        other = None
    other_out = None
    if other is not None:
        other_out = {**_stat_out_row(other), "values": other["values"]}
    return {
        "dimension": dimension, "total": dim["total"], "known": dim["known"],
        "rows": [_stat_out_row(r) for r in rows], "other": other_out,
        "unknown": _stat_out_row(dim["unknown"]) if dim["unknown"] else None,
    }
