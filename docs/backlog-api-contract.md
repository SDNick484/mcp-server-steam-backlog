# Backlog API contract (API version 2)

This is the interface between **steam-backlog-triage** (`backlog_server.py`, `backlog.html`,
`run-export.sh`, `steam_backlog_export.py`) and **mcp-server-steam-backlog**. The backlog repo
implements it in "PR A"; this repo's client is built and tested against it.

- Baseline: steam-backlog-triage `729fa93` ("API version 1", everything that exists today).
- Everything in this document is **additive**. A client that sends none of the new headers and
  ignores the new response fields (that is, today's `backlog.html`) sees exactly today's behavior.
  Sections marked **Page: unaffected** spell out what the page must keep working without.
- The words MUST, SHOULD and MAY are used in the RFC 2119 sense.
- Where this contract and the code disagree after PR A merges, the contract tests in this repo
  decide (`tests/contract/`, run against a real checkout; see `docs/handoff-pr-a.md`).

Contents:
1. [Versioning and feature detection](#1-versioning-and-feature-detection)
2. [ETags on GET](#2-etags-on-get)
3. [Conditional PUTs and previous values](#3-conditional-puts-and-previous-values)
4. [Starting an export and reading its status](#4-starting-an-export-and-reading-its-status)
5. [Exporter and wrapper changes](#5-exporter-and-wrapper-changes)
6. [Scoring spec (SCORING.md)](#6-scoring-spec-scoringmd)
7. [Error format and status codes](#7-error-format-and-status-codes)
8. [What the MCP does on an API-1 server](#8-what-the-mcp-does-on-an-api-1-server)
9. [Decisions](#9-decisions)

---

## 1. Versioning and feature detection

Every response from `backlog_server.py` (all paths, all status codes, including 304 and errors)
MUST carry:

```
X-Backlog-Server: 1          (unchanged; the page checks for it)
X-Backlog-Api: 2             (new)
```

A response without `X-Backlog-Api` is API version 1. The MCP decides what it can use from this
header alone; it does not probe individual routes. `doctor` reports the value.

The integer goes up only for changes a client must know about. Fields added to a JSON body do
not bump it; clients MUST ignore unknown fields.

**Page: unaffected.**

---

## 2. ETags on GET

### 2.1 Which resources

| Resource | ETag source |
|---|---|
| `GET /api/library` | the bytes of `steam_backlog.json` as served |
| `GET /api/triage` | the bytes of the JSON body as served |
| `GET /api/notes` | the bytes of the JSON body as served |
| `GET /api/order` | the bytes of the JSON body as served |

`HEAD` on each of these MUST return the same headers as `GET`, ETag included, with no body.

### 2.2 Format

```
ETag: "<32 lowercase hex chars>"
```

The value is the first 32 hex characters of SHA-256 over the exact response body bytes. It is a
**strong** validator (no `W/` prefix). Two bodies with the same bytes MUST get the same ETag,
and a body that changes MUST get a different one, including across server restarts. That rules
out mtime- or counter-based tags.

The server MAY cache the library hash keyed on the file's `(st_ino, st_mtime_ns, st_size)` so a
304 doesn't rehash a multi-megabyte file. The exporter's atomic write (§5.1) changes the inode on
every export, so that key is safe.

The triage, notes and order bodies are small. The server computes their ETag from the body it is
about to send, under the same lock it reads them with.

`Cache-Control: no-store` stays on every response. ETags here are for explicit conditional
requests by API clients, not for browser caching.

### 2.3 `If-None-Match` → 304

On a GET or HEAD to the four resources above, if the request carries `If-None-Match`:

- The header is a comma-separated list of entity tags, or `*`.
- Comparison is **weak** (RFC 9110 §13.1.2): `W/"abc"` matches `"abc"`.
- If any listed tag matches the current ETag, or the value is `*` and the resource exists, the
  server MUST answer `304 Not Modified` with `ETag`, `Cache-Control`, `X-Backlog-Server` and
  `X-Backlog-Api` headers and **no body**.
- Otherwise it answers as a normal GET (200 with the new ETag).
- An unparseable value is treated as no match (normal 200), never as an error.

`If-Modified-Since` is ignored.

### 2.4 Library before the first export

`GET /api/library` with no export file still answers `404 {"error": "no export yet"}`, with no
`ETag`. `If-None-Match: *` on a missing library also gets that 404.

### 2.5 Examples

First fetch:

```http
GET /api/library HTTP/1.1

HTTP/1.0 200 OK
Content-Type: application/json
Content-Length: 1843310
Cache-Control: no-store
X-Backlog-Server: 1
X-Backlog-Api: 2
ETag: "3f1c0e9a7d2b4c58a9e6f0d1b2c3a4e5"

{"schema":2,"source":"steam_backlog_export.py","exportedAt":"2026-11-02T12:15:09+00:00", ...}
```

Revalidation, unchanged:

```http
GET /api/library HTTP/1.1
If-None-Match: "3f1c0e9a7d2b4c58a9e6f0d1b2c3a4e5"

HTTP/1.0 304 Not Modified
Cache-Control: no-store
X-Backlog-Server: 1
X-Backlog-Api: 2
ETag: "3f1c0e9a7d2b4c58a9e6f0d1b2c3a4e5"
```

Revalidation after an export: `200` with the new body and new `ETag`.

Triage:

```http
GET /api/triage HTTP/1.1

HTTP/1.0 200 OK
ETag: "0b9d2e81c4f7a6305e1d9c8b7a6f5e4d"
...
{"620": "next", "1145360": "done"}
```

**Page: unaffected.** It fetches with `cache: "no-store"` and sends no conditional headers.

---

## 3. Conditional PUTs and previous values

### 3.1 `If-Match` on PUT

`PUT /api/triage`, `PUT /api/notes` and `PUT /api/order` accept an optional `If-Match` header.

- **No `If-Match`:** exactly today's behavior (plus the new response fields in §3.2).
- **`If-Match` present:** inside the server lock, before writing, the server computes the current
  ETag of that resource (as §2 defines it) and compares with **strong** comparison. `*`
  matches always (the three resources always exist; an absent file reads as `{}` or `[]`).
  - Match: perform the write.
  - No match: write nothing and answer `412 Precondition Failed` with the current ETag in the
    header and the body:

    ```json
    {"error": "precondition failed", "etag": "\"<current etag>\""}
    ```

    (The `etag` field carries the full quoted tag, ready to send back as `If-Match`.)
- Validation (400) is checked **before** the precondition, so a malformed request is a 400 even
  with a stale tag.

The check and the write MUST happen under one hold of the lock; otherwise the precondition
proves nothing.

Note on granularity: triage and notes PUTs change one game, but the ETag covers the whole map.
An `If-Match` there fails if *any* game changed. That's correct but coarse, so the MCP sends
`If-Match` only on `PUT /api/order` (a whole-list replace, where lost updates are real) and
relies on §3.2 for triage and notes. See [§9 D1](#9-decisions).

### 3.2 Success responses

Every successful PUT to these three resources MUST return the new ETag in the `ETag` header and
the **previous value read under the lock** in the body:

| Route | Today's body | API 2 body |
|---|---|---|
| `PUT /api/triage` | `{"ok": true}` | `{"ok": true, "appid": 620, "lane": "done", "previous": "next"}` |
| `PUT /api/notes` | `{"ok": true}` | `{"ok": true, "appid": 620, "note": "<stored text>", "previous": "<old text>"}` |
| `PUT /api/order` | `{"ok": true, "count": 3}` | `{"ok": true, "count": 3, "order": [..], "previous": [..]}` |

- `lane` and `previous` (triage) are `"next" | "someday" | "skip" | "done" | ""`; `""` means no lane.
- `note` is the text actually stored: today's server strips it, so it can differ from what was sent.
  `""` means the note was cleared (or none existed, for `previous`).
- `order` is the de-duplicated list actually stored; `previous` is the list before the write.

### 3.3 Validation tightening (bug fixes riding along)

These are 400s in API 2. Today some of them crash the handler (the client sees a reset
connection or a 500) or write garbage:

| Request | Today | API 2 |
|---|---|---|
| `PUT /api/triage` with a non-object body (`[1]`) | `AttributeError`, no response | `400 {"error": "expected a JSON object"}` |
| `PUT /api/triage` with `"appid": true` | writes key `"True"` | `400 {"error": "expected {appid: int, lane: next\|someday\|skip\|done\|''}"}` |
| `PUT /api/library` with a non-object body | 400 already | unchanged |

### 3.4 Examples

Triage, no precondition (the page and the MCP both do this):

```http
PUT /api/triage HTTP/1.1
Content-Type: application/json

{"appid": 620, "lane": "done"}

HTTP/1.0 200 OK
ETag: "9a0e3c1d5b7f2e4a6c8d0b1f3e5a7c9d"

{"ok": true, "appid": 620, "lane": "done", "previous": "next"}
```

Order, read-modify-write with a precondition (the MCP's `backlog_reorder_next`):

```http
GET /api/order HTTP/1.1

HTTP/1.0 200 OK
ETag: "5d4c3b2a19f8e7d6c5b4a39281706f5e"

[620, 1145360, 753640]
```

```http
PUT /api/order HTTP/1.1
Content-Type: application/json
If-Match: "5d4c3b2a19f8e7d6c5b4a39281706f5e"

{"order": [753640, 620, 1145360]}

HTTP/1.0 200 OK
ETag: "e1f2a3b4c5d6e7f8091a2b3c4d5e6f70"

{"ok": true, "count": 3, "order": [753640, 620, 1145360], "previous": [620, 1145360, 753640]}
```

Same PUT after the page reordered in between:

```http
HTTP/1.0 412 Precondition Failed
ETag: "77aa0c1e2d3f4a5b6c7d8e9f0a1b2c3d"

{"error": "precondition failed", "etag": "\"77aa0c1e2d3f4a5b6c7d8e9f0a1b2c3d\""}
```

The MCP then re-reads the order once, reapplies the move to the fresh list and retries; on a
second 412 it reports a conflict to the user without writing.

Note, with a precondition on the wrong tag but a bad body:

```http
PUT /api/notes HTTP/1.1
If-Match: "deadbeefdeadbeefdeadbeefdeadbeef"

{"appid": 620, "note": "<2,001 characters>"}

HTTP/1.0 400 Bad Request

{"error": "notes are limited to 2000 characters"}
```

**Page: unaffected.** It sends no `If-Match` and only checks `r.ok` on PUT responses.

---

## 4. Starting an export and reading its status

### 4.1 Server options

`backlog_server.py` gains two options:

| Option | Default | Meaning |
|---|---|---|
| `--export-cmd PATH` | `run-export.sh` next to `backlog_server.py`, if it exists and is executable | the command to run; no arguments are passed |
| `--export-timeout SECONDS` | `10800` (3 h) | wall-clock limit for one run started by the server |

If no export command is configured (no option and no `run-export.sh` beside the server), both
export routes answer `501 {"error": "export not configured on this server"}`. The installer
always has one, so on a real install this doesn't happen.

### 4.2 How a run is started

- The server spawns the command as a child process **with no shell**, as the server's own user,
  with stdin `/dev/null` and stdout+stderr merged into one pipe.
- Its environment is the server's environment plus `EXPORT_DIR=<--data dir>`, so the export
  writes where the server reads. Nothing else is added: no keys pass through the server.
  `run-export.sh` reads `~/.steam-env` itself, as it does under cron.
- The server reads the pipe on a background thread, keeps the last 200 lines in memory, and
  also writes every line (unredacted) to `<data>/export-last.log`, truncated at the start of
  each server-started run. That file is for the admin on the box; it never crosses the API.
- On timeout the server sends `SIGINT` (the exporter catches it, saves its cache and writes
  what it has), then `SIGTERM` after 30 s, then `SIGKILL` after another 30 s.

### 4.3 One run at a time, including cron

"Running" means **either** the server's own child is alive **or** another process holds the
export lock (`<data>/.export.lock`, an exclusive `flock(2)` lock; see §5.2). The weekly cron
run takes the same lock, so it shows up here as `origin: "external"`.

The server checks the lock with a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on that file,
releasing it immediately. It never holds the lock itself; the child does (through
`run-export.sh`). Spawning happens under a server mutex so two POSTs can't both start a child.

There is a small window between the server's lock check and the child taking the lock. If the
child loses that race, it exits with code 75 (§5.2) and the run ends as
`failed` / `reason: "busy"`.

### 4.4 `POST /api/export`

Request: `Content-Type: application/json` is **required**, with an empty body or `{}`. Any
other content type gets `415 {"error": "expected Content-Type: application/json"}`. That keeps
a plain cross-site HTML form from starting an export through a browser's existing login. Any
JSON object body is accepted and ignored (reserved for future options).

| Case | Status | Body |
|---|---|---|
| started | `202 Accepted` | the status object (§4.6), `state: "running"`, `origin: "server"`; plus header `Location: /api/export/status` |
| a run is already going (either origin) | `409 Conflict` | the status object of that run, plus `"error": "export already running"` |
| no export command | `501` | `{"error": "export not configured on this server"}` |
| wrong content type | `415` | see above |
| spawn failed (exec error) | `500` | `{"error": "could not start the export"}`; detail goes to the server's stderr only |

### 4.5 `GET /api/export/status`

Always `200` with the status object (§4.6), except `501` when no export command is configured.
It never returns 404 on an API-2 server. **A 404 here means API 1.**

### 4.6 The status object

```json
{
  "state": "running",
  "origin": "server",
  "id": "20261103T221501Z",
  "startedAt": "2026-11-03T22:15:01+00:00",
  "finishedAt": null,
  "elapsedSeconds": 412,
  "phase": "details",
  "progress": {"done": 350, "total": 1203},
  "exitCode": null,
  "reason": null,
  "lines": [
    "2026-11-03 22:15:01 starting export",
    "<profile>: 1874 games",
    "Steam Deck compatibility: everything cached.",
    "  (about 32 minutes for store details)",
    "Store details: fetching 1203 games...",
    "  350/1203"
  ],
  "libraryEtag": "\"3f1c0e9a7d2b4c58a9e6f0d1b2c3a4e5\"",
  "libraryChanged": false,
  "exportedAt": "2026-10-27T11:15:44+00:00"
}
```

| Field | Type | Meaning |
|---|---|---|
| `state` | `"idle" \| "running" \| "succeeded" \| "failed"` | `idle`: nothing has run since the server started and no external run is going. After a run ends, the server keeps reporting it (`succeeded`/`failed`) until the next one starts. |
| `origin` | `"server" \| "external" \| null` | who started the run. `external` = the lock is held by a process this server didn't start (cron, a shell, or a run left over from before a server restart). `null` when `idle`. |
| `id` | string or null | server runs: UTC start time `YYYYMMDDTHHMMSSZ`. `null` for external runs and `idle`. |
| `startedAt`, `finishedAt` | ISO 8601 UTC or null | server runs only. `finishedAt` is null while running. External runs: both null (the server can't know). |
| `elapsedSeconds` | int or null | server runs: running time so far, or the total once finished. External: null. |
| `phase` | see §4.7, or null | the last phase seen in the output. Server runs only. |
| `progress` | `{"done": int, "total": int}` or null | from the last `  N/M` line in the current phase; reset to null on a phase change. |
| `exitCode` | int or null | the child's exit status once finished. Negative = killed by that signal number (Python's convention). |
| `reason` | see below, or null | why a run `failed`. Null otherwise. |
| `lines` | array of strings | the last **20** output lines of a server run, **redacted** (§4.8), each at most 300 characters (longer lines are cut and end in `…`). Empty for external runs and `idle`. |
| `libraryEtag` | string or null | the library's current ETag (quoted, as in the header), or null if there's no export yet. |
| `libraryChanged` | bool or null | for a finished server run: whether the library ETag differs from the one at `startedAt`. Null while running, for external runs and when `idle`. |
| `exportedAt` | string or null | `exportedAt` from the library as it is now. |

`reason` values:

| `reason` | When |
|---|---|
| `"not_configured"` | exit code 78: `run-export.sh` found no `STEAM_API_KEY` / `STEAM_PROFILE` |
| `"busy"` | exit code 75: another export held the lock |
| `"timeout"` | the server stopped it at `--export-timeout` |
| `"error"` | any other non-zero exit or signal |

`state` is `succeeded` only for exit code 0.

### 4.7 Phases

The server derives `phase` from the output lines it reads (after stripping a leading timestamp).
The first matching rule wins; lines matching nothing leave the phase unchanged.

| Line starts with | `phase` |
|---|---|
| `starting export` (after the `date` prefix) | `"starting"` |
| a line matching `^.+: \d+ games$` | `"library"` |
| `Steam Deck compatibility:` | `"deck"` |
| `Store details:` or `  (about ` | `"details"` |
| `ProtonDB:` | `"proton"` |
| `HowLongToBeat:` | `"hltb"` |
| `Wrote ` | `"writing"` |

`progress` comes from lines matching `^\s+(\d+)/(\d+)$`.

These are the exporter's current messages at `729fa93`. The table is best-effort status for a
human, not a protocol: clients MUST NOT treat an unknown or missing phase as an error. If a PR
changes these messages, it updates this table and the status code in the same PR.

### 4.8 Redaction of `lines`

Applied in this order, to each line, before it is stored in the in-memory ring:

1. Drop control characters other than tab.
2. **Profile line:** a line matching `^(.+): (\d+) games$` becomes `<profile>: N games` (it is
   the persona name or SteamID).
3. **SteamIDs:** any `7656119\d{10}` becomes `7656119XXXXXXXXXX`.
4. **Persona:** if the current library has a non-empty `persona`, every occurrence of it
   (case-sensitive) becomes `<persona>`.
5. **Secrets in URLs:** query parameters named `key`, `apikey`, `api_key`, `token`, `access_token`,
   `password` (case-insensitive) have their values replaced with `<redacted>`.
6. **Paths:** the `--data` directory prefix becomes `<data>`; any other `/home/<user>` prefix
   becomes `~`.
7. **IP addresses:** IPv4 dotted quads become `<ip>`.
8. Cut to 300 characters.

Game names and Steam store hostnames are kept: they are useful and not personal.

The MCP applies its own `logsafe` redaction on top before anything reaches a model or a log.

### 4.9 Examples

Start:

```http
POST /api/export HTTP/1.1
Content-Type: application/json
Content-Length: 2

{}

HTTP/1.0 202 Accepted
Location: /api/export/status

{"state": "running", "origin": "server", "id": "20261103T221501Z",
 "startedAt": "2026-11-03T22:15:01+00:00", "finishedAt": null, "elapsedSeconds": 0,
 "phase": null, "progress": null, "exitCode": null, "reason": null, "lines": [],
 "libraryEtag": "\"3f1c0e9a7d2b4c58a9e6f0d1b2c3a4e5\"", "libraryChanged": null,
 "exportedAt": "2026-10-27T11:15:44+00:00"}
```

Start while the weekly cron run is going:

```http
POST /api/export HTTP/1.1
Content-Type: application/json

HTTP/1.0 409 Conflict

{"error": "export already running", "state": "running", "origin": "external", "id": null,
 "startedAt": null, "finishedAt": null, "elapsedSeconds": null, "phase": null, "progress": null,
 "exitCode": null, "reason": null, "lines": [], "libraryEtag": "\"3f1c...\"",
 "libraryChanged": null, "exportedAt": "2026-10-27T11:15:44+00:00"}
```

Finished, library changed:

```json
{"state": "succeeded", "origin": "server", "id": "20261103T221501Z",
 "startedAt": "2026-11-03T22:15:01+00:00", "finishedAt": "2026-11-03T22:31:40+00:00",
 "elapsedSeconds": 999, "phase": "writing", "progress": null, "exitCode": 0, "reason": null,
 "lines": ["...", "Wrote <data>/steam_backlog.json: 1874 games, 912 never launched, 1403 with HLTB times.",
           "Deck: 640 Verified, 588 Playable, 201 Unsupported, 445 Unknown",
           "Size: 1800 KB. Load it in the dashboard with 'Load export'."],
 "libraryEtag": "\"a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4\"", "libraryChanged": true,
 "exportedAt": "2026-11-03T22:31:39+00:00"}
```

Keys not set:

```json
{"state": "failed", "origin": "server", "exitCode": 78, "reason": "not_configured",
 "lines": ["2026-11-03 22:15:01 skipped: set STEAM_API_KEY and STEAM_PROFILE in ~/.steam-env"], "...": "..."}
```

Wrong content type:

```http
POST /api/export HTTP/1.1
Content-Type: application/x-www-form-urlencoded

HTTP/1.0 415 Unsupported Media Type

{"error": "expected Content-Type: application/json"}
```

API-1 server (today): `POST /api/export` gets the stdlib's `501` HTML page (no `do_POST`), and
`GET /api/export/status` gets `404 {"error": "not found"}`.

### 4.10 The page's Refresh control (PR A, A3)

The page gets a **Refresh library** control, shown only when self-hosted (`S.server`) and the
response carries `X-Backlog-Api: 2`. It POSTs, polls the status every 5 s while `running`, shows
`phase`/`progress`, and on `succeeded` with `libraryChanged: true` reloads the library and
re-renders without losing filters or triage. `failed` shows a short message per `reason`.
An `external` run shows "an export is already running" and keeps polling.

**Page: the control is new; nothing else on the page depends on §4.** The MCP doesn't depend on
the control.

---

## 5. Exporter and wrapper changes

### 5.1 Atomic library write (`steam_backlog_export.py`)

Today the exporter writes `steam_backlog.json` in place, so a GET during the write can return a
truncated file. It MUST instead write `<out>.tmp` in the same directory and `os.replace()` it over
`<out>`, the way `Cache.save()` already does. No schema change: `schema` stays `2`.

### 5.2 Lock and exit codes (`run-export.sh`)

Before `exec`-ing the exporter, `run-export.sh` MUST take an exclusive, non-blocking lock on
`$OUT/.export.lock` on file descriptor 9 and keep it across the `exec` (so the Python process
holds it until it exits):

```sh
exec 9>"$OUT/.export.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') skipped: another export is running"
  exit 75
fi
```

`flock` is a BusyBox applet on Alpine and part of util-linux elsewhere. BusyBox's and Python's
`fcntl.flock` both use `flock(2)`, so they see each other's locks.

Exit codes (sysexits):

| Code | Meaning |
|---|---|
| 0 | export finished (including "Interrupted; writing what's done so far") |
| 75 (`EX_TEMPFAIL`) | another export holds the lock |
| 78 (`EX_CONFIG`) | `STEAM_API_KEY` or `STEAM_PROFILE` missing (today this exits 0) |
| other | the exporter failed |

The messages printed stay the same, so the cron log reads as before. BusyBox crond ignores the
exit code.

---

## 6. Scoring spec (SCORING.md)

PR A adds `SCORING.md` to steam-backlog-triage with this content (wording may be adjusted, the
rules may not). The MCP serves the same text as the resource `backlog://scoring`.

### 6.1 Normalizing a game

The page normalizes every export game before scoring (`normalize()` in `backlog.html`). The rules
that affect the score:

- `playtime`, `deckPlaytime`: number, else 0; negative becomes 0.
- `deck`: one of 0, 1, 2, 3, else **0** (Unknown).
- `hltb`: kept only if it is an object whose `main` or `extra` is a non-zero finite number;
  otherwise `null`. Each of `main`, `extra`, `complete` is a finite number or `null`.
- `metacritic`: finite number or `null`.
- `controller`: `"full"` or `"partial"`, else `null`.

### 6.2 Derived values

- `TRIED_MIN = 120` minutes.
- **Play state:** `never` if `playtime == 0`; `tried` if `playtime < 120`; else `played`.
- **Length** `L = hltb.main || hltb.extra` (JavaScript truthiness: a `main` of 0 falls through
  to `extra`); `null` if `hltb` is null. Completionist time is never used for `L`.
- **Lane:** the triage lane, `""` if none.
- **Backlog:** play state is not `played` **and** lane is not `skip` or `done`.

### 6.3 The score

`score = play + deck + length + metacritic + deckPlayed + controller + next`

| Term | Value |
|---|---|
| `play` | `never` **+30**, `tried` **+22**, `played` **−20** |
| `deck` | Verified (3) **+30**, Playable (2) **+14**, Unknown (0) **0**, Unsupported (1) **−45** |
| `length` | if `L` is null: **0**. Else `L ≤ 5` **+24**, `≤ 12` **+18**, `≤ 25` **+9**, `≤ 50` **0**, `> 50` **−10** |
| `metacritic` | if `metacritic` is truthy (not null, not 0): **(metacritic − 75) / 2**. Else **0** |
| `deckPlayed` | `deckPlaytime > 0`: **+5** |
| `controller` | `controller == "full"`: **+4** |
| `next` | lane `next`: **+100** |

Plain IEEE-754 double arithmetic, no rounding. Implementations compare with an absolute tolerance
of `1e-9`.

### 6.4 Ranking

- **Couch picks** (page: `renderPicks`): games in the backlog that pass the active filters,
  sorted by score descending; ties keep **export order** (the page's sort is stable, and the
  exporter writes games sorted by lower-cased name). Top 6.
- **List sort "score":** score descending, ties by name (`localeCompare`).
- The `next` bonus makes Up-next games rank first **in practice, not by rule**. The highest
  score without it is 30 + 30 + 24 + 12.5 + 5 + 4 = 105.5 (never played, Verified, ≤ 5 h,
  Metacritic 100, played on the Deck, full controller). The lowest Up-next game still in the
  backlog is 22 − 45 − 10 + (MC − 75)/2 + 100, which is 67 at Metacritic 75. So a weak Up-next
  game can rank below a strong untriaged one. Implementations MUST sort by score alone, exactly
  as the page does, and MUST NOT force lane `next` to the top.

### 6.5 Buckets used elsewhere (not part of the score)

These are listed so the MCP's overview and filters match the page, because they use different
boundaries from the score's length bands:

- **Overview grid buckets** (`lenBucket`): on `L`: `< 5` short, `< 15` mid, `< 40` long, else
  epic; `null` → none.
- **Length filter presets** (filter panel): on `hltb[measure]` with **no fallback** (`measure` is
  `main`, `extra` or `complete`): short `≤ 4.9`, mid `5 – 14.9`, long `15 – 39.9`, epic `≥ 40`;
  "no data" means `hltb[measure]` is null.

### 6.6 Parity vectors

`tests/fixtures/score_vectors.json` (generated by `scripts/gen_score_vectors.mjs` from the page's
own `normalize()` and `score()`) holds 33 export-shaped games with a lane each and the expected
`score`, `playState` and `lengthHours` (`L`). Both repos test against the same file, byte for
byte. When `score()` changes, regenerate it and update both repos.

### 6.7 Store tags (not part of the score)

Added by steam-backlog-triage PR #11. Not in `SCORING.md`: tags never change the score. Additive:
the export's `schema` stays 2 and `X-Backlog-Api` stays 2.

**Library field.** Each export game MAY carry `"tags"`: a list of Steam store (user) tag names, the
top 10 by vote weight, in Steam's order, spelled as Steam spells them (e.g.
`["Souls-like", "Difficult", "RPG"]`). `[]` means unknown (not fetched). Exports made before the
feature have no `"tags"` key; that is also unknown. Neither case means "this game has no tags".
Clients MUST accept all three shapes. The server
serves the field as the exporter wrote it.

**Cleaning** (the page's `normalizeExtras()`): keep strings only, trim them, drop blanks, keep at
most 10. Anything that isn't a list becomes `[]`.

**Tag key** (the page's `tagKey()`), used for every comparison:

1. NFKC-normalize, then lower-case.
2. Keep only characters whose Unicode general category is a letter (`L*`) or a number (`N*`).
3. Map through the alias table `{"soulsborne": "soulslike", "soulsbourne": "soulslike"}`.

So `Souls-like`, `Soulslike`, `souls like`, `SOULS-LIKE`, `Soulsborne` and fullwidth
`Ｓｏｕｌｓ‐ｌｉｋｅ` share the key `soulslike`; `2.5D` is `25d`, `Co-op` is `coop`, and `+` has
the empty key. A game's tag keys are its tags mapped through the key, blanks dropped, deduplicated
(first occurrence kept). `Roguelike` and `Roguelite` stay distinct.

**Filter.** Inputs are mapped through the tag key, blanks dropped, deduplicated; the filter holds
keys. A game passes if **any** of its tag keys is in the filter. A game with no tag keys never
matches unless "include unknown" is on for the tag filter (the MCP's single `include_unknown`).
The MCP's `tags` argument takes at most 10 strings of up to 80 characters each.

**Relaxation** (MCP only; the page doesn't relax): `tags` is dropped after `publishers` and `developers`
(which follow `controller`) and before `genres`. Tags are crowd-voted, finer-grained and noisier than store genres, so they are the weaker
constraint. The order matches steam-backlog-triage's `backlog_query.py`.

### 6.8 Publishers, developers and reviews (not part of the score)

Not in `SCORING.md`: none of these change the score. Additive: the export's `schema` stays 2 and
`X-Backlog-Api` stays 2; no route changes.

**Library fields.** Each export game MAY carry:

- `"publishers"` and `"developers"`: lists of up to 10 names, as Steam spells them (e.g.
  `["Valve"]`).
- `"reviews"`: `{"total": <count>, "pct": <percent positive>, "desc": "<Steam's label>"}`.

Absent or empty is unknown, never "no publisher" or "no reviews". Cleaning is the same as for tags
(`normalizeExtras()`): names keep strings only, trimmed, blanks dropped, at most 10; anything that isn't a
list becomes `[]`. Clients MUST accept games without the keys. The server serves the fields as the
exporter wrote them.

**Name key** (`name_key`, the page's `nameKey()`), used for every publisher and developer comparison:

1. Drop the signs `™`, `®` and `©`.
2. NFKC-normalize, lower-case, and strip anything that isn't a letter or digit from both ends.
3. Split on whitespace. While more than one word is left and the last word, with its non-alphanumeric
   characters removed, is one of `inc llc ltd co corp corporation gmbh sa ab limited`, remove it and
   strip the new last word's ends again (dropping it if that leaves it empty).
4. Join the words with single spaces.

So `Valve Corporation` and `valve` share a key, `Ubisoft Entertainment, Inc.` is `ubisoft entertainment`,
and `Inc` alone stays `inc`. A game's keys are its names mapped through the key, blanks dropped,
deduplicated.

**Filter.** Inputs are mapped through the same key; the filter holds keys. A game passes if **any** of
its keys is in the filter. A game with no keys never matches unless "include unknown" is on (the MCP's
single `include_unknown`).

**Review levels.** The level is Steam's own label, matched exactly against `reviews.desc`:
`overwhelmingly-positive`, `very-positive`, `positive`, `mostly-positive`, `mixed`, `mostly-negative`,
`negative`, `very-negative`, `overwhelmingly-negative`. A `reviews` object whose label isn't one of
these (including none) is `few` ("too few reviews"). A game without a `reviews` object has no level and
is unknown. The filter takes any number of levels and passes a game at any of them.

**Metacritic max.** `metacritic_max` keeps games scoring at most that; it works with `metacritic_min`, and a
game without a score is unknown. The page's dropdown offers 59, 69, 79 and 89; the MCP takes any integer from
0 to 100.

**Relaxation** (MCP only): the MCP's steps are `metacritic_min`, then `metacritic_max`, then `reviews`, then
`year` (the page and `backlog_query.py` call the first two one step, `metacritic`); `publishers` and
`developers` are dropped after `controller` and before `tags`, publishers first. The order is
steam-backlog-triage's `backlog_query.py`.

---

## 7. Error format and status codes

All API errors stay `{"error": "<message>"}` with `Content-Type: application/json`, as today.
Messages MUST NOT include filesystem paths, exception text or command output; details go to the
server's stderr.

| Status | Used for |
|---|---|
| 200 | success |
| 202 | export started |
| 304 | `If-None-Match` matched |
| 400 | malformed request |
| 404 | unknown path; library before the first export |
| 409 | export already running |
| 412 | `If-Match` didn't match |
| 415 | `POST /api/export` without a JSON content type |
| 500 | unexpected failure (including a failed spawn) |
| 501 | export routes with no export command configured |

---

## 8. What the MCP does on an API-1 server

So PR B can ship before PR A, the MCP degrades as follows when `X-Backlog-Api` is absent:

| Feature | API 1 behavior in the MCP |
|---|---|
| Library cache | no conditional GET; refetches the library at most every 60 s and otherwise serves the cached copy |
| Previous values | read before write (GET then PUT); the tool result says the previous value was read non-atomically |
| Reorder | plain read-modify-write without `If-Match`; the result says the change wasn't protected against concurrent edits |
| Export tools | report "not available on this backlog server version" and suggest updating steam-backlog-triage |
| `doctor` | lists which API-2 features are missing |

---

## 9. Decisions

These go beyond, or differ from, section 3 of the design doc. All were **accepted on
2026-11-03**, D9 with the "match the page" option.

- **D1. `If-Match` scope.** The design doc asks for `If-Match` on triage and order. Triage (and
  notes) PUTs are per-game patches, so a whole-map precondition would fail on unrelated changes.
  Decision: support `If-Match` on all three for uniformity, but the MCP only sends it on order;
  for triage and notes the server returns `previous` from under the lock (§3.2), which makes
  "every write returns the previous value" atomic.
- **D2. Notes get ETags too.** Not in the design doc; added for uniformity and so `set_note` can
  get `previous` atomically.
- **D3. Atomic export write and a shared export lock** (§5). Not in the design doc; without them
  ETags can match a half-written file and 409 can't see cron runs.
- **D4. `run-export.sh` exit codes change**: missing keys exits 78 instead of 0. Cron doesn't care;
  anything else that checks the exit code would.
- **D5. `X-Backlog-Api: 2`** as the single feature switch, instead of probing each route.
- **D6. `POST /api/export` requires a JSON content type** (CSRF guard; the server has no auth of
  its own).
- **D7. No cooldown between exports.** One at a time only. The exporter's caches make a rerun cheap;
  a looping agent is limited by the MCP's own token bucket.
- **D8. The design doc's `protondb_min`** isn't a page filter (the page filters by a set of tiers).
  That's MCP-side only, so it isn't in this contract: the MCP will accept `protondb` (a tier set,
  as on the page) and `protondb_min` (ordered native > platinum > gold > silver > bronze > borked;
  pending and unknown never pass a minimum unless `include_unknown`).
- **D9. "Next-lane games come first."** Design doc §4 (`backlog_suggest_picks`) and §7 (server
  instructions) say Up-next games always rank first, "as on the page". The page doesn't do that:
  it sorts picks by score alone, and the +100 bonus usually but not always wins (§6.4).
  Decision: the MCP matches the page (score only), and the server instructions say "next-lane
  games get a large bonus" instead of "always rank first". The page doesn't change.
