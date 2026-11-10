"""Vendor backlog_query.py from steam-backlog-triage, locked to a commit, or check the copy.

steam-backlog-triage is the source of truth for the dashboard's rules, written as pure Python in
backlog_query.py. This repo carries a verbatim copy of that one file (so the server stays a single
package with no runtime dependency on the sibling) and query.lock pins where the copy came from:

    python scripts/sync_query.py --update              move the lock to steam-backlog-triage main, then write the copy
    python scripts/sync_query.py --update --ref REF    ... to this branch, tag or commit instead of main
    python scripts/sync_query.py --check               exit 1 if the copy was edited, or isn't the locked commit's
    python scripts/sync_query.py --check --offline     only the first half: the copy against the lock's sha256
    python scripts/sync_query.py --check --latest      exit 1 if the copy differs from main's file instead
    ... --source DIR                                   read DIR/backlog_query.py (a local checkout), not GitHub
    ... --force                                        with --update: overwrite a copy that was edited locally

query.lock holds `repo`, `commit`, `path` (the file in that repo), `dest` (where the copy goes here)
and `sha256` (of the file's bytes). With `--source`, `--update` takes the commit from
`git -C DIR rev-parse HEAD` and `--check` compares the copy with DIR's file, committed or not.

Exit status: 0 in sync, 1 the copy differs (--check), 2 anything else (bad arguments, an unreadable
lock, a copy with local edits, the network).

Why it works the way it does:
- The file is fetched by commit SHA, never by branch name. A SHA names one immutable snapshot, and
  raw.githubusercontent.com's caching of branch URLs (minutes) can't hand back a stale file.
  `--update` and `--latest` resolve the ref to a SHA first, with one GitHub API call.
- GITHUB_TOKEN (or GH_TOKEN), if set, is sent to api.github.com only, to raise its rate limit. Raw
  files are fetched anonymously: raw.githubusercontent.com answers 404 to a token that can't see the repo.
- The file is fetched before anything is written; the copy is written, then the lock, each to a
  temporary file beside the target and renamed, so an interrupted run leaves the old pair in place
  or (at worst) a copy the offline check flags.
- `--check` is two checks. The offline one (the copy hashes to the lock's sha256) needs nothing but
  this repo, so it runs anywhere. The online one (the locked commit's file hashes to that sha256)
  catches a lock that was edited by hand. A network failure in the online half prints a notice and
  leaves the exit status alone: the half that can run decides.
- A copy that doesn't match the lock has been edited by hand, which is against the rules (change
  steam-backlog-triage, then sync). `--update` refuses to overwrite it unless `--force`, so an edit
  made while debugging isn't thrown away silently.
- Standard library only (tomllib needs Python 3.11), so it runs before `pip install`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOCK = "query.lock"
TOKEN_HOSTS = ("api.github.com",)  # see the docstring for why raw.githubusercontent.com isn't here
TIMEOUT = 30.0

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

LOCK_HEADER = """\
# Where this repo's copy of steam-backlog-triage's backlog_query.py comes from: the repo, the commit,
# the file's path there, where the copy lives here, and the SHA-256 of its bytes. Written by
# `python scripts/sync_query.py --update`; see that script. Don't edit by hand.
"""


class SyncError(Exception):
    """Something the user has to fix; the message says what."""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the lock --------------------------------------------------------------------
@dataclass(frozen=True)
class Lock:
    repo: str
    commit: str
    path: str
    dest: str
    sha256: str

    KEYS = ("repo", "commit", "path", "dest", "sha256")

    def validate(self) -> Lock:
        if not _REPO.fullmatch(self.repo) or any(part in (".", "..") for part in self.repo.split("/")):
            raise SyncError(f"{LOCK}: repo must look like owner/name (got {self.repo!r})")
        if not _SHA.fullmatch(self.commit):
            raise SyncError(f"{LOCK}: commit must be a full 40-character SHA (got {self.commit!r})")
        if not _SHA256.fullmatch(self.sha256):
            raise SyncError(f"{LOCK}: sha256 must be 64 lowercase hex digits (got {self.sha256!r})")
        for field, value in (("path", self.path), ("dest", self.dest)):
            parts = Path(value).parts
            if not value or Path(value).is_absolute() or ".." in parts or "\\" in value:
                raise SyncError(f"{LOCK}: {field} must be a relative path inside the repo (got {value!r})")
        return self

    def dump(self) -> str:
        return (
            f'{LOCK_HEADER}repo = "{self.repo}"\ncommit = "{self.commit}"\npath = "{self.path}"\n'
            f'dest = "{self.dest}"\nsha256 = "{self.sha256}"\n'
        )


def read_lock(root: Path) -> Lock:
    try:
        data = tomllib.loads((root / LOCK).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SyncError(f"no {LOCK} in {root}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SyncError(f"{LOCK} isn't valid TOML: {exc}") from None
    unknown = sorted(set(data) - set(Lock.KEYS))
    if unknown:
        print(f"sync_query.py: warning: {LOCK} has keys this script doesn't use: {', '.join(unknown)}", file=sys.stderr)
    try:
        lock = Lock(*(str(data[key]) for key in Lock.KEYS))
    except KeyError as exc:
        raise SyncError(f"{LOCK} is missing {exc.args[0]!r}") from None
    return lock.validate()


# --- where the file comes from -------------------------------------------------------
def _get(url: str, accept: str | None = None) -> bytes:
    """GET a URL; HTTPS_PROXY and SSL_CERT_FILE are honored the usual urllib way."""
    request = urllib.request.Request(url, headers={"User-Agent": "sync_query.py"})
    if accept:
        request.add_header("Accept", accept)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname in TOKEN_HOSTS:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        hint = " (rate limited? set GITHUB_TOKEN)" if exc.code in (403, 429) else ""
        raise SyncError(f"GET {url}: HTTP {exc.code} {exc.reason}{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SyncError(f"GET {url}: {reason}") from None


def resolve(repo: str, ref: str) -> str:
    """The SHA `ref` (a branch, tag or commit) names in `repo` right now."""
    if _SHA.fullmatch(ref):
        return ref  # already immutable; the raw fetch fails if it doesn't exist
    quoted = urllib.parse.quote(ref, safe="")
    url = f"https://api.github.com/repos/{repo}/commits/{quoted}"
    sha = _get(url, "application/vnd.github.sha").decode().strip()
    if not _SHA.fullmatch(sha):
        raise SyncError(f"GitHub returned {sha[:60]!r} for {repo} {ref}, not a commit SHA")
    return sha


def fetch(repo: str, commit: str, path: str) -> bytes:
    return _get(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}")


def read_source(source: Path, path: str) -> bytes:
    root = source.resolve()
    target = (root / path).resolve()
    try:
        if not target.is_relative_to(root):
            raise SyncError(f"{path} resolves outside {source}")
        return target.read_bytes()
    except OSError as exc:
        raise SyncError(f"{source / path}: {exc.strerror}") from None


def source_head(source: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"couldn't run git in {source}: {exc}") from None
    sha = out.stdout.strip()
    if out.returncode != 0 or not _SHA.fullmatch(sha):
        raise SyncError(f"{source} isn't a git checkout with a commit (git rev-parse HEAD failed)")
    return sha


# --- writing ------------------------------------------------------------------------
def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` so readers see the old file or the new one, never a partial: a temporary file
    beside the target (same filesystem), flushed to disk, then renamed over it."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def dest_path(root: Path, lock: Lock) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / lock.dest).resolve()
    if not path.is_relative_to(root_resolved):
        raise SyncError(f"refusing to use {lock.dest}: resolves outside {root}")
    return path


# --- commands ----------------------------------------------------------------------------
def check(root: Path, lock: Lock, args: argparse.Namespace) -> int:
    dest = dest_path(root, lock)
    fix = f"python scripts/sync_query.py --update  (or `git checkout -- {lock.dest}`)"
    try:
        local = sha256_of(dest.read_bytes())
    except FileNotFoundError:
        print(f"sync_query.py: {lock.dest} is missing. Fix: {fix}", file=sys.stderr)
        return 1
    if local != lock.sha256:
        print(
            f"sync_query.py: {lock.dest} was edited: it hashes to {local[:12]}, {LOCK} says {lock.sha256[:12]}. "
            f"The copy is changed in {lock.repo}, not here. Fix: {fix}",
            file=sys.stderr,
        )
        return 1

    if args.source:
        theirs = sha256_of(read_source(args.source, lock.path))
        if theirs != local:
            print(
                f"sync_query.py: {lock.dest} differs from {args.source / lock.path}. Fix: --update --source",
                file=sys.stderr,
            )
            return 1
        print(f"{lock.dest} matches {args.source / lock.path}")
        return 0

    if args.offline:
        print(f"{lock.dest} matches {LOCK} (offline check only)")
        return 0

    try:
        if args.latest:
            commit = resolve(lock.repo, "main")
            if sha256_of(fetch(lock.repo, commit, lock.path)) != local:
                print(
                    f"sync_query.py: {lock.dest} differs from {lock.repo} main ({commit[:7]}). "
                    "Fix: python scripts/sync_query.py --update",
                    file=sys.stderr,
                )
                return 1
            print(f"{lock.dest} matches {lock.repo} main ({commit[:7]})")
        else:
            if sha256_of(fetch(lock.repo, lock.commit, lock.path)) != lock.sha256:
                print(
                    f"sync_query.py: {LOCK} is wrong: {lock.path} at {lock.commit[:7]} in {lock.repo} "
                    "doesn't hash to its sha256. "
                    "Fix: python scripts/sync_query.py --update",
                    file=sys.stderr,
                )
                return 1
            print(f"{lock.dest} matches {lock.repo} at {lock.commit[:7]} (locked)")
    except SyncError as exc:
        print(f"sync_query.py: notice: online check skipped ({exc})", file=sys.stderr)
    return 0


def update(root: Path, lock: Lock, args: argparse.Namespace) -> int:
    if args.source:
        commit = source_head(args.source)
        data = read_source(args.source, lock.path)
        origin = f"{args.source}"
    else:
        commit = resolve(lock.repo, args.ref)
        data = fetch(lock.repo, commit, lock.path)
        origin = f"{lock.repo} {args.ref}"
    dest = dest_path(root, lock)
    new_lock = Lock(lock.repo, commit, lock.path, lock.dest, sha256_of(data))

    current = dest.read_bytes() if dest.exists() else None
    if current is not None and sha256_of(current) != lock.sha256 and current != data and not args.force:
        raise SyncError(
            f"refusing to overwrite {lock.dest}: it differs from {LOCK}, so it was edited here. Change the file in "
            f"{lock.repo} instead (or `git checkout -- {lock.dest}`); `--force` overwrites."
        )

    changed = current != data
    if changed:
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(dest, data)  # bytes, so no newline translation: the copy stays byte-identical
    if new_lock != lock:
        write_atomic(root / LOCK, new_lock.dump().encode("utf-8"))  # last: only once the copy is in
    moved = f"{lock.commit[:7]} -> {commit[:7]}" if commit != lock.commit else f"already at {commit[:7]}"
    print(f"{LOCK}: {moved} (from {origin}); {lock.dest} {'updated' if changed else 'unchanged'}")
    return 0


# --- the command line ------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync_query.py",
        description="Vendor backlog_query.py from steam-backlog-triage, locked to a commit, or check the copy.",
    )
    parser.add_argument("--update", action="store_true", help="fetch the file and move query.lock to it")
    parser.add_argument(
        "--ref", default="main", help="with --update: the branch, tag or commit to take (default: main)"
    )
    parser.add_argument("--check", action="store_true", help="don't write; exit 1 if the copy differs")
    parser.add_argument("--offline", action="store_true", help="with --check: only compare the copy with the lock")
    parser.add_argument("--latest", action="store_true", help="with --check: compare against main, not the lock")
    parser.add_argument("--force", action="store_true", help="with --update: overwrite a copy that was edited locally")
    parser.add_argument("--source", type=Path, help="read the file from this local steam-backlog-triage checkout")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the repo to sync (default: the one this script is in)",
    )
    args = parser.parse_args(argv)
    if args.update == args.check:
        parser.error("give exactly one of --update and --check")
    if args.check and (args.force or args.ref != "main"):
        parser.error("--force and --ref go with --update; --check never writes")
    if args.update and (args.offline or args.latest):
        parser.error("--offline and --latest go with --check")
    if args.offline and args.latest:
        parser.error("--latest needs the network; --offline can't")
    if args.source and (args.latest or args.ref != "main"):
        parser.error("--source reads a local checkout, which has no main or ref to move to")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lock = read_lock(args.root)
        return update(args.root, lock, args) if args.update else check(args.root, lock, args)
    except SyncError as exc:
        print(f"sync_query.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
