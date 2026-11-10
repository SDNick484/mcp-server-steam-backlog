"""scripts/sync_query.py and the vendored backlog_query.py it locks."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_query.py"
LOCK = "query.lock"
DEST = "src/steam_backlog_mcp/backlog_query.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=False)


def lock_of(root: Path) -> dict[str, str]:
    with open(root / LOCK, "rb") as f:
        return tomllib.load(f)


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / DEST).parent.mkdir(parents=True)
    shutil.copy(ROOT / LOCK, root / LOCK)
    shutil.copy(ROOT / DEST, root / DEST)
    return root


@pytest.fixture
def triage(tmp_path: Path) -> Path:
    src = tmp_path / "triage"
    src.mkdir()
    (src / "backlog_query.py").write_text('"""fake"""\nVALUE = 1\n', encoding="utf-8")
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return src


def head(src: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_vendored_file_matches_the_lock() -> None:
    lock = lock_of(ROOT)
    assert lock["repo"] == "SDNick484/steam-backlog-triage"
    assert lock["path"] == "backlog_query.py"
    assert sha256(ROOT / DEST) == lock["sha256"]


def test_check_offline_detects_an_edited_copy(copy_root: Path) -> None:
    assert run("--root", copy_root, "--check", "--offline").returncode == 0
    with open(copy_root / DEST, "ab") as f:
        f.write(b"\n")
    result = run("--root", copy_root, "--check", "--offline")
    assert result.returncode == 1
    assert "backlog_query.py" in result.stderr


def test_source_update_and_check(copy_root: Path, triage: Path) -> None:
    assert run("--root", copy_root, "--source", triage, "--update").returncode == 0
    lock = lock_of(copy_root)
    assert lock["commit"] == head(triage)
    assert lock["sha256"] == sha256(triage / "backlog_query.py")
    assert (copy_root / DEST).read_bytes() == (triage / "backlog_query.py").read_bytes()
    assert run("--root", copy_root, "--source", triage, "--check").returncode == 0
    (triage / "backlog_query.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert run("--root", copy_root, "--source", triage, "--check").returncode == 1


def test_update_refuses_to_overwrite_a_local_edit(copy_root: Path, triage: Path) -> None:
    with open(copy_root / DEST, "ab") as f:
        f.write(b"# local edit\n")
    refused = run("--root", copy_root, "--source", triage, "--update")
    assert refused.returncode == 2
    assert b"# local edit" in (copy_root / DEST).read_bytes()
    assert run("--root", copy_root, "--source", triage, "--update", "--force").returncode == 0
    assert b"# local edit" not in (copy_root / DEST).read_bytes()


def test_restore_writes_the_locked_bytes_and_leaves_the_lock(copy_root: Path, tmp_path: Path) -> None:
    locked = (copy_root / DEST).read_bytes()
    lock_before = (copy_root / LOCK).read_bytes()
    source = tmp_path / "locked-source"
    source.mkdir()
    (source / "backlog_query.py").write_bytes(locked)
    (copy_root / DEST).write_bytes(b"corrupted\n")
    assert run("--root", copy_root, "--source", source).returncode == 2  # an edited copy needs --force
    assert (copy_root / DEST).read_bytes() == b"corrupted\n"
    assert run("--root", copy_root, "--source", source, "--force").returncode == 0
    assert (copy_root / DEST).read_bytes() == locked
    assert (copy_root / LOCK).read_bytes() == lock_before
    (copy_root / DEST).unlink()  # a missing copy needs no --force
    assert run("--root", copy_root, "--source", source).returncode == 0
    assert (copy_root / DEST).read_bytes() == locked


def test_restore_refuses_a_source_that_is_not_the_locked_file(copy_root: Path, triage: Path) -> None:
    (copy_root / DEST).unlink()
    lock_before = (copy_root / LOCK).read_bytes()
    result = run("--root", copy_root, "--source", triage, "--force")
    assert result.returncode == 1
    assert "nothing was written" in result.stderr
    assert not (copy_root / DEST).exists()
    assert (copy_root / LOCK).read_bytes() == lock_before
