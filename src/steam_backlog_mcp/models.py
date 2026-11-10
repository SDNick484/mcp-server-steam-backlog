"""Structured tool output (Pydantic). Each model becomes a tool's outputSchema.

Game names and notes appear only in their own fields, never inside ``detail`` text written
for the model, so a hostile store title or note can't pose as an instruction (they're data).
SteamID and persona never appear: the client drops them when it parses the export.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, Field

from . import domain

STORE_URL = "https://store.steampowered.com/app/{appid}/"


class ScoreTerm(BaseModel):
    term: str = Field(description="play, deck, length, metacritic, deck_played, controller or next")
    label: str
    points: float


class GameRow(BaseModel):
    """One game, compact."""

    appid: int
    name: str
    deck: domain.DeckName
    play_state: domain.PlayState
    playtime_hours: float
    length_hours: float | None = Field(description="HowLongToBeat main story (else main + extras); null = unknown")
    metacritic: int | None
    protondb: str | None = Field(description="ProtonDB tier; null = unknown")
    lane: domain.Lane | None = Field(description="Triage lane; null = untriaged")
    has_note: bool
    score: float


class ReviewSummary(BaseModel):
    desc: str | None
    pct: float | None
    total: int


class GameDetail(GameRow):
    deck_playtime_hours: float
    last_played: str | None = Field(description="UTC date, or null if never")
    hltb: dict[str, float | None] | None
    protondb_reports: int | None
    genres: list[str]
    tags: list[str] = Field(description="Steam user tags, most voted first; [] = unknown (not fetched)")
    publishers: list[str] = Field(description="[] = unknown")
    developers: list[str] = Field(description="[] = unknown")
    reviews: ReviewSummary | None = Field(description="null = unknown")
    year: int | None
    controller: Literal["full", "partial", "none"] | None = Field(description="null = unknown")
    note: str | None
    next_position: int | None = Field(description="1-based position in Up next, if in that lane")
    in_backlog: bool
    score_breakdown: list[ScoreTerm]
    score_explained: str
    store_url: str


class Pick(GameRow):
    rank: int
    score_breakdown: list[ScoreTerm]
    score_explained: str
    note: str | None


class Candidate(BaseModel):
    appid: int
    name: str
    year: int | None


class Unresolved(BaseModel):
    query: str
    reason: Literal["ambiguous", "unknown_appid", "no_match"]
    candidates: list[Candidate]


class GameLookup(BaseModel):
    found: bool
    game: GameDetail | None
    unresolved: Unresolved | None = Field(description="When found is false: why, and candidates to pick from")


class ExportInfo(BaseModel):
    exported_at: str | None
    age_days: float | None
    schema_version: int | None


class Overview(BaseModel):
    games: int
    play_states: dict[str, int]
    deck: dict[str, int]
    backlog: int
    grid: dict[str, dict[str, int]] = Field(description="Backlog games by Deck rating, then length bucket")
    lanes: dict[str, int]
    next_queue: int
    coverage: dict[str, float] = Field(description="Share of games with each data source (0..1)")
    export: ExportInfo
    api_version: int
    dry_run: bool
    notes: list[str]


class FindOut(BaseModel):
    matches: int
    offset: int
    games: list[GameRow]
    truncated: bool
    exact: bool = Field(description="false: nothing matched as asked; these are the closest matches")
    relaxed: list[str] = Field(description="Filters dropped to find these (weakest first)")
    fuzzy_query: bool
    detail: str


class PicksOut(BaseModel):
    picks: list[Pick]
    considered: int = Field(description="Backlog games that passed the filters")
    exact: bool
    relaxed: list[str]
    detail: str


class QueueEntry(GameRow):
    position: int
    note: str | None


class NextQueueOut(BaseModel):
    queue: list[QueueEntry]
    detail: str


Outcome = Literal["done", "unchanged", "dry_run", "needs_confirmation", "not_resolved", "conflict"]


class LaneChange(BaseModel):
    appid: int
    name: str
    previous: domain.Lane | None
    lane: domain.Lane | None
    changed: bool


class SetLaneOut(BaseModel):
    outcome: Outcome
    detail: str
    changes: list[LaneChange]
    unresolved: list[Unresolved]
    atomic: bool = Field(description="false: previous values were read separately (older backlog server)")
    warnings: list[str]


class SetNoteOut(BaseModel):
    outcome: Outcome
    detail: str
    appid: int | None
    name: str | None
    previous: str | None
    note: str | None
    unresolved: list[Unresolved]
    atomic: bool
    warnings: list[str]


class OrderEntry(BaseModel):
    appid: int
    name: str


class ReorderOut(BaseModel):
    outcome: Outcome
    detail: str
    previous: list[OrderEntry]
    order: list[OrderEntry]
    unresolved: list[Unresolved]
    protected: bool = Field(description="false: written without a concurrency check (older backlog server)")
    warnings: list[str]


class ExportStatus(BaseModel):
    available: bool = Field(description="false: this backlog server can't run exports from the API")
    state: Literal["idle", "running", "succeeded", "failed", "unavailable"]
    origin: Literal["server", "external"] | None
    phase: str | None
    progress: dict[str, int] | None
    started_at: str | None
    finished_at: str | None
    elapsed_seconds: int | None
    exit_code: int | None
    reason: str | None
    lines: list[str]
    library_changed: bool | None
    exported_at: str | None
    detail: str


class StartExportOut(BaseModel):
    outcome: Literal["started", "already_running", "dry_run", "unavailable"]
    detail: str
    status: ExportStatus


# --------------------------------------------------------------------------- builders
def _hours(minutes: float) -> float:
    return round(minutes / 60, 1)


def _int(v: float | None) -> int | None:
    return int(v) if v is not None else None


def _lane(raw: str) -> domain.Lane | None:
    for lane in domain.LANES:
        if raw == lane:
            return lane
    return None


def row(g: domain.Game, lane: str, has_note: bool) -> dict[str, object]:
    return {
        "appid": g.appid,
        "name": g.name,
        "deck": domain.DECK_NAME[g.deck],
        "play_state": domain.play_state(g),
        "playtime_hours": _hours(g.playtime),
        "length_hours": domain.length_hours(g),
        "metacritic": _int(g.metacritic) if g.metacritic else None,
        "protondb": g.proton.tier if g.proton else None,
        "lane": _lane(lane),
        "has_note": has_note,
        "score": domain.score(g, lane),
    }


def game_row(g: domain.Game, lane: str, has_note: bool) -> GameRow:
    return GameRow.model_validate(row(g, lane, has_note))


def terms(g: domain.Game, lane: str) -> list[ScoreTerm]:
    return [ScoreTerm(term=t.term, label=t.label, points=t.points) for t in domain.score_breakdown(g, lane)]


def game_detail(g: domain.Game, lane: str, note: str | None, next_position: int | None, in_backlog: bool) -> GameDetail:
    # The page's controller filter: no value but store details fetched = "none"; neither = unknown.
    controller = g.controller if g.controller in ("full", "partial") else "none" if g.has_details else None
    last = datetime.fromtimestamp(g.last_played, UTC).date().isoformat() if g.last_played else None
    return GameDetail.model_validate(
        {
            **row(g, lane, note is not None),
            "deck_playtime_hours": _hours(g.deck_playtime),
            "last_played": last,
            "hltb": {"main": g.hltb.main, "extra": g.hltb.extra, "complete": g.hltb.complete} if g.hltb else None,
            "protondb_reports": int(g.proton.total) if g.proton else None,
            "genres": list(g.genres),
            "tags": list(g.tags),
            "publishers": list(g.publishers),
            "developers": list(g.developers),
            "reviews": {"desc": g.reviews.desc, "pct": g.reviews.pct, "total": int(g.reviews.total)}
            if g.reviews
            else None,
            "year": _int(g.year),
            "controller": controller,
            "note": note,
            "next_position": next_position,
            "in_backlog": in_backlog,
            "score_breakdown": terms(g, lane),
            "score_explained": domain.explain(domain.score_breakdown(g, lane)),
            "store_url": STORE_URL.format(appid=g.appid),
        }
    )


def candidates(r: domain.Resolved) -> Unresolved:
    # backlog_query's reason is a plain str, one of ok | ambiguous | unknown_appid | no_match.
    reason = cast(Literal["ambiguous", "unknown_appid", "no_match"], "no_match" if r.reason == "ok" else r.reason)
    return Unresolved(
        query=r.query,
        reason=reason,
        candidates=[Candidate(appid=g.appid, name=g.name, year=_int(g.year)) for g in r.candidates],
    )
