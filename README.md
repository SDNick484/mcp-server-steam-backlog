# mcp-server-steam-backlog

An [MCP](https://modelcontextprotocol.io) server for
[Steam Backlog Triage](https://github.com/SDNick484/steam-backlog-triage), the self-hosted dashboard
for working through a Steam backlog with an eye on the Steam Deck. It lets an MCP client such as
Claude answer "what should I play tonight on the Deck, under 10 hours?" in one call, search and
filter the library with the dashboard's filters, and triage games into lanes, add notes, reorder
Up next and refresh the library, all saved to the same dashboard.

> **Status: alpha.** Tested against a wire-level fake of the backlog server, against
> steam-backlog-triage's real `backlog_server.py` (both today's version and a prototype of the
> API it's built for), and its installer against a stubbed Alpine run. Some features need a
> newer steam-backlog-triage (see [API versions](#api-versions)); everything else works with the
> current one.

This project isn't affiliated with Valve, HowLongToBeat or ProtonDB.

## How it works

```
MCP client ──stdio, or HTTPS through Cloudflare Access──▶ mcp-server-steam-backlog
                                                               │  HTTP, loopback only
                                                               ▼
                                         backlog_server.py (steam-backlog-triage)
                                                               │  one writer: lock + atomic writes
                                                               ▼
                                   steam_backlog.json, triage.json, notes.json, order.json
```

- The MCP server is **a client of `backlog_server.py`'s JSON API**. It never opens the backlog's
  files, so there's still one writer, and the dashboard and Claude never race each other. It runs as
  its own user and can't read the backlog's keys.
- The **couch score** is the dashboard's `score()`, ported term by term and checked against shared
  test vectors generated from `backlog.html` itself, so Claude and the dashboard never disagree
  about the best pick. `backlog://scoring` explains it.
- The interface is specified in [`docs/backlog-api-contract.md`](docs/backlog-api-contract.md).

## Tools

| Tool | What it does |
|---|---|
| `backlog_get_overview` | Start here: library size, never/tried/played, Deck ratings, the backlog by Deck rating × length, lane counts, Up next length, export age, data coverage |
| `backlog_find_games` | Search and filter with the dashboard's filters (genres, HLTB length, ProtonDB, Metacritic, year, Deck rating, controller, play state, lane, notes); sort and page; relaxes filters instead of answering "not found" |
| `backlog_get_game` | Everything about one game, with its score breakdown and store link |
| `backlog_suggest_picks` | Couch picks with the same filters, each with a score breakdown to explain it |
| `backlog_get_next_queue` | The Up next lane in order, with notes |
| `backlog_set_lane` | Move 1–50 games to next, someday, skip, done, or out of every lane |
| `backlog_set_note` | Write, replace or clear a game's note |
| `backlog_reorder_next` | Move a game within Up next, or set the order |
| `backlog_start_export` | Refresh the library (runs on the backlog server; minutes to an hour) |
| `backlog_get_export_status` | How the refresh is going, with redacted log lines |

Resources: `backlog://scoring` (how the score works) and `backlog://game/{appid}`.
Prompts: `triage_session` (walk untriaged games one at a time) and `pick_for_tonight`.

Not exposed, on purpose: replacing the library, deleting data, anything touching keys or settings.

## Safety design

- **Writes never guess.** A game argument is an app id or an exact name. A name that matches several
  games (two called "DOOM") or none returns candidates and changes nothing.
- **Confirmation for bulk and destructive-feeling changes.** A single game the user named can move to
  any lane, done and skip included. Moving more than five games, or replacing an existing note, returns
  `needs_confirmation` with the planned change until the call is repeated with `confirmed=true`. The
  server instructions also ask the model to confirm skip/done for games it inferred.
- **Bounded.** At most 50 games per call; a token bucket limits writes (60 a minute by default) and
  exports (two, then one per five minutes), so a looping agent can't churn the files.
- **Reversible.** Every write returns what it replaced: previous lanes, the previous note, the previous
  Up next order.
- **No lost updates.** Reorders use `If-Match`; if the dashboard reordered at the same moment, the
  server re-reads once and retries, then reports a conflict rather than overwriting.
- **Data isn't instructions.** Game names and notes only ever appear in their own fields.
- **Private.** The export's SteamID and persona are dropped when it's parsed; SteamIDs are redacted
  from every log line even with `--no-redact`. Errors to the client are generic and actionable;
  details go to the log.
- **Dry run.** `BACKLOG_DRY_RUN=1` (or `--dry-run`): writes validate and report, and send nothing.

## API versions

| Feature | API 1 (steam-backlog-triage today) | API 2 (after its API PR) |
|---|---|---|
| Reading, filtering, picks, triage, notes, reorder | ✓ | ✓ |
| Library cached and revalidated with ETags | refetched at most once a minute | ✓ |
| Previous values read atomically | read just before writing (flagged `atomic: false`) | ✓ |
| Reorders safe against a concurrent dashboard edit | no (flagged `protected: false`) | ✓ |
| Start an export, see its status | "not available" | ✓ |

`mcp-server-steam-backlog doctor` says which the server has.

## Install

You need steam-backlog-triage running (its `backlog_server.py` on port 8484).

### Trying it without the backlog

```sh
pip install git+https://github.com/SDNick484/mcp-server-steam-backlog
mcp-server-steam-backlog simulate --port 8484      # a fake backlog server with a sample library
# in another terminal:
mcp-server-steam-backlog doctor
mcp-server-steam-backlog call suggest_picks count=3
```

`simulate --api 1` imitates today's steam-backlog-triage.

### Claude Code or Claude Desktop (stdio)

On the machine that runs the backlog, or with `BACKLOG_URL` pointing at it over a private network:

```sh
claude mcp add steam-backlog -e BACKLOG_URL=http://127.0.0.1:8484 -- \
  uvx --from git+https://github.com/SDNick484/mcp-server-steam-backlog mcp-server-steam-backlog
```

(Once it's published to PyPI, plain `uvx mcp-server-steam-backlog` works.) Or in a client's JSON config:

```json
{
  "mcpServers": {
    "steam-backlog": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SDNick484/mcp-server-steam-backlog", "mcp-server-steam-backlog"],
      "env": { "BACKLOG_URL": "http://127.0.0.1:8484" }
    }
  }
}
```

### On Alpine (OpenRC), next to the backlog

```sh
git clone https://github.com/SDNick484/mcp-server-steam-backlog.git
cd mcp-server-steam-backlog
sh deploy/alpine/install.sh "$PWD"
mcp-server-steam-backlog doctor
rc-update add mcp-server-steam-backlog default && rc-service mcp-server-steam-backlog start
```

The installer creates the `mcp-steam-backlog` user, a venv in `/opt/mcp-server-steam-backlog`, settings in
`/etc/mcp-server-steam-backlog/env` (kept on re-runs), and an OpenRC service on `127.0.0.1:8486/mcp/backlog`.
Re-running it upgrades the code. To edit the settings: `vim /etc/mcp-server-steam-backlog/env`, then
`rc-service mcp-server-steam-backlog restart`.

systemd hosts: see `contrib/systemd/`. A non-root container: `deploy/docker/Dockerfile` (optional).

## Remote access: HTTP behind Cloudflare Access

For Claude on the web and mobile, the service speaks Streamable HTTP and checks a Cloudflare Access
assertion on every request (`remote.py`, shared with the sibling servers). With no Access settings it
answers only requests that start on the same machine.

1. **Snapshot first** if this shares a container with other services.
2. In Cloudflare Zero Trust, create an **Access application** for the MCP path (for example
   `backlog.example.com/mcp/backlog`) with **Managed OAuth**, and note its **AUD** tag.
3. Add a tunnel rule sending `^/mcp/` to `http://localhost:8486`, **above** any broader rule for the
   same hostname.
4. Put the settings in the env file (placeholders shown):
   ```sh
   MCP_PUBLIC_HOSTS=backlog.example.com
   CF_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
   CF_ACCESS_AUD=<the Access application's AUD tag>
   ```
   then `rc-service mcp-server-steam-backlog restart` and `mcp-server-steam-backlog doctor`.
5. Check that an unauthenticated request gets Access's **401** (with `WWW-Authenticate`), not a 302
   to a login page:
   ```sh
   curl -si -X POST https://backlog.example.com/mcp/backlog | head -5
   ```
   If it redirects, give the MCP its own hostname instead.
6. Add `https://backlog.example.com/mcp/backlog` as a custom connector in Claude.

## Settings

| Variable | Default | |
|---|---|---|
| `BACKLOG_URL` | `http://127.0.0.1:8484` | The backlog server |
| `BACKLOG_DRY_RUN` | off | Report writes without sending them |
| `BACKLOG_WRITE_RATE` | 60 | Writes per minute (1–600) |
| `BACKLOG_DEBUG` | off | Debug logging |
| `BACKLOG_LOG_UNREDACTED` | off | Show LAN addresses in logs (secrets and SteamIDs stay hidden) |
| `MCP_HTTP_BIND`, `MCP_HTTP_PORT`, `MCP_HTTP_PATH` | `127.0.0.1`, `8486`, `/mcp/backlog` | The HTTP service |
| `MCP_PUBLIC_HOSTS`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `MCP_ALLOWED_EMAILS` | | Cloudflare Access |

## Checking it with the MCP Inspector

```sh
npx @modelcontextprotocol/inspector mcp-server-steam-backlog
```

- [ ] Ten tools listed, each with a title, annotations and an output schema
- [ ] `backlog_get_overview` returns the library size and export age
- [ ] `backlog_suggest_picks` returns picks with `score_breakdown`
- [ ] `backlog_set_lane` with `games=["DOOM"]` (or any duplicated name) returns `not_resolved` and changes nothing
- [ ] `backlog_set_lane` with six games returns `needs_confirmation`; with `confirmed=true`, `done`
- [ ] `backlog_set_note` over an existing note returns `needs_confirmation`
- [ ] With `BACKLOG_DRY_RUN=1`, writes return `dry_run` and the dashboard doesn't change
- [ ] `backlog://scoring` reads; `backlog://game/620` reads for a game in the library
- [ ] Prompts `triage_session` and `pick_for_tonight` render

## Development

```sh
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest --cov
```

- The contract tests run against a fake, against the fake imitating API 1, and against a real
  `backlog_server.py`: `BACKLOG_REF=<ref>` clones steam-backlog-triage at that ref (default `main`),
  `BACKLOG_CHECKOUT=<dir>` uses a local checkout. See `docs/handoff-pr-a.md`.
- `tests/fixtures/score_vectors.json` comes from `node scripts/gen_score_vectors.mjs path/to/backlog.html`.
  CI regenerates it and fails if the page's `score()` changed.
- Files shared with the sibling MCP servers (`remote.py`, `logsafe.py`, `lookup.py`, `ratelimit.py`,
  `tools.py`, `tasks.py` and their tests) come from
  [mcp-server-shared](https://github.com/SDNick484/mcp-server-shared); change them there, then
  `python scripts/sync_shared.py --update`.

## License

MIT
