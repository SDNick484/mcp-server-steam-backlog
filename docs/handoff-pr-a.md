# Handoff: implementing PR A in steam-backlog-triage

PR A adds the API in [`backlog-api-contract.md`](backlog-api-contract.md) to steam-backlog-triage.
The contract is the spec; this file is what I learned building against it. Everything here is
consistent with the contract; if the two ever disagree, stop and ask rather than picking one.

## It has already been prototyped

Before handing over, I built a throwaway stdlib-only version of the `backlog_server.py` changes (not
committed anywhere) and ran this repo's contract tests against it: all 20 real-server tests passed,
including the export lifecycle, cron-lock detection and redaction. So the contract is implementable
in the standard library as written, and the tests are known to be right about a real API-2 server.
The prototype took about 260 added lines in `backlog_server.py`. Write your own, with the README and
page changes; the notes below are the parts that took thought.

## Order of work (one commit each, as the build prompt asks)

| Commit | Contract | Files |
|---|---|---|
| A1 | §1 `X-Backlog-Api: 2`, §2 ETags and 304 on the library | `backlog_server.py` |
| A2 | §2 ETags on triage/notes/order, §3 If-Match, `previous`, validation fixes | `backlog_server.py` |
| A3 | §4 export routes, §5 atomic export write, lock and exit codes, the page's Refresh control | `backlog_server.py`, `steam_backlog_export.py`, `run-export.sh`, `backlog.html`, `install/alpine.sh` (if needed) |
| A4 | §6 `SCORING.md`, `tests/score_vectors.json` and its test | new files |

Put the README's API table and the new control in the same PR (A3 is a natural place).
Note that `X-Backlog-Api: 2` makes the MCP assume *all* of API 2: if you push A1 alone and deploy it,
the MCP's export tools will find no routes and report "not available", which is fine, but conditional
PUTs would be sent to a server that ignores them. Deploy PR A whole, not commit by commit.

## Notes, by section

### §1-§2 headers and ETags

- Put `X-Backlog-Api` in `send()`, so 304s, errors and the 404 for unknown paths all carry it.
- A 304 must not have a body, and must not send `Content-Length: <n>` for the 200 it replaces. The
  simplest `send()` change: skip `Content-Length` and the write when `status == 304`.
- The small resources' ETag is over the exact bytes you send, so compute it from the same
  `json.dumps(...)` call that produces the body, inside `app.lock`.
- Library ETag: hash the file once per `(st_ino, st_mtime_ns, st_size)`. It's 1–2 MB; hashing it on
  every 304 is wasteful but not wrong.
- `do_HEAD = do_GET` already exists; with the ETag in `send()`, HEAD gets it for free.

### §3 PUTs

- Check the precondition **inside** `with app.lock:`, after validation and before the read-modify-write.
  Compute the current ETag from the current file contents (`json.dumps(app.read_triage())` etc.).
- `PUT /api/triage` today calls `data.get(...)` on whatever was sent, so `[1]` raises AttributeError and
  the client gets no response. Add one `isinstance(data, dict)` check before the per-route branches.
- Use the existing `is_app_id()` for triage too (it rejects `true`); the notes route already does.
- `previous` for notes is the stored (stripped) text, or `""`.

### §4 export

- **`--export-cmd` default**: `run-export.sh` beside `backlog_server.py`, if executable. The installer puts
  both in `/opt/steam-backlog-triage`, so installed servers get exports with no new option. If you decide
  the installer should pass it explicitly anyway, make sure re-runs keep working.
- **Spawn** with `subprocess.Popen([cmd], stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, env={**os.environ,
  "EXPORT_DIR": data_dir})`. `run-export.sh` already honors `EXPORT_DIR`. Read the pipe on a thread; a
  second thread can own the timeout (`proc.wait(timeout)` then SIGINT → 30 s → SIGTERM → 30 s → SIGKILL).
- **Lock detection**: open `<data>/.export.lock` (create it if missing), try
  `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, close it straight away. `BlockingIOError` means running. When the
  server's own child is running, report it as `origin: "server"` even though the lock is held (the child
  holds it); only report `external` when the lock is held and no own run is in progress.
- **One mutex** around "check running + spawn", and the same one when building the status object, so a
  status read never sees half an update.
- **Phase lines** start with a `date` timestamp from `run-export.sh`'s own `echo`s but not from the
  exporter's `log()` lines. Match the phase table both with and without a leading
  `YYYY-MM-DD HH:MM:SS ` prefix.
- **Redaction order matters**: do the profile line (`^.+: \d+ games$`) before the SteamID rule, so a
  SteamID-only profile line becomes `<profile>: N games`, not `7656119XXXXXXXXXX: N games`. Read the
  persona from the current library on each run (it's in the export's top level).
- `libraryChanged` compares the library ETag at spawn with the one after exit; that's why §5.1's atomic
  write matters (an in-place write could be hashed half-written).
- **`export-last.log`**: truncate at spawn, write every raw line. It's in the data dir, which is
  `steam`'s; the MCP never reads it.
- The 409 body is the status object plus `"error"`. The MCP drops the `error` key and uses the rest.
- `POST` must accept `Content-Type: application/json; charset=utf-8` (compare only the media type), and
  an empty body. Read and discard any body so the connection stays clean.

### §5 exporter and wrapper

- `steam_backlog_export.py`: write `args.out + ".tmp"`, then `os.replace`. `Cache.save()` already does this.
- `run-export.sh`: the `exec 9>...; flock -n 9` must come after `mkdir -p "$OUT"` and before the final
  `exec "$PY" ...`, and the missing-keys branch changes `exit 0` to `exit 78`. Keep its messages identical
  so the cron log reads the same.
- BusyBox has `flock`; check `install/alpine.sh` doesn't need a new package (on stock Alpine it doesn't).
  For systemd hosts, `flock` is in util-linux.
- If a weekly cron run starts while a server-started run holds the lock, the cron run exits 75 with
  "skipped: another export is running". That's intended.

### §4.10 the page's Refresh control

- Show it only when `S.server` and the response had `X-Backlog-Api: 2` (read the header in the existing
  `fetch("api/library")` in the self-hosted check).
- Poll every 5 s while `running`; on `succeeded` with `libraryChanged`, reload through the same path the
  page uses at start-up, so filters (URL hash) and triage survive.
- Keep it a single file with no build step, and no new dependencies.

### §6 scoring

- Copy `tests/fixtures/score_vectors.json` from this repo **byte for byte**. Its `source.functionsSha256`
  is the SHA-256 of the extracted `normalize()`/`score()` text from `backlog.html` at `729fa93`; this
  repo's CI regenerates the vectors from steam-backlog-triage and fails if that changes.
- `SCORING.md` can start from this repo's `src/steam_backlog_mcp/scoring.md` (what Claude reads as
  `backlog://scoring`) plus the contract's §6 for the precise rules. Keep both saying the same thing.
- The stdlib test: extract the functions the way `scripts/gen_score_vectors.mjs` does (or copy that
  script into steam-backlog-triage) and compare under `node` when `shutil.which("node")`; skip otherwise.

## Running this repo's contract tests against your branch

From a checkout of this repo (PR B's branch), with your steam-backlog-triage branch checked out beside it:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Against your local working tree (uncommitted changes included):
BACKLOG_CHECKOUT=../steam-backlog-triage pytest tests/test_contract.py -k real -rs

# Against a pushed branch, as CI does:
BACKLOG_REF=feature/your-branch BACKLOG_REQUIRE_REAL=1 pytest tests/test_contract.py -k real -rs
```

What to expect:

- On today's `main` (API 1): 14 pass, 8 skip (each skip names the API-2 feature it's waiting for).
- On a correct PR A: 20 pass, 2 skip (`API 2` on the API-1-only test, and one test that drives the fake's
  exit code). Any other skip on PR A means a feature isn't detected: check `X-Backlog-Api` and that
  `--help` lists `--export-cmd` (the fixture uses it to point exports at `tests/fixtures/fake_export.sh`
  instead of the real exporter).
- The fixture starts `backlog_server.py --port <free> --bind 127.0.0.1 --data <tmp>` with
  `src/steam_backlog_mcp/sim/sample_library.json` as the library. Nothing touches the network or needs keys.

Then the whole suite, which also exercises the MCP tools end to end:

```sh
pytest --cov
```

## After PR A merges

In PR B: set `BACKLOG_REF` in `.github/workflows/ci.yml` to the merged PR A commit, run the full suite,
push, and mark PR B ready. Nothing else in PR B should need to change. If something outside the contract
tests fails, stop and report it.
