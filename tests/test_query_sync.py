"""scripts/sync_query.py and the vendored backlog_query.py it locks."""

from __future__ import annotations

import hashlib
import importlib.util
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


# --- the online half of --check, with the network stubbed at the script's HTTP helper ----------
def load_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("sync_query_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks the module up by name
    spec.loader.exec_module(module)
    return module


def stub_get(monkeypatch: pytest.MonkeyPatch, module, error: Exception | None = None, body: bytes = b""):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake(url: str, accept: str | None = None) -> bytes:
        calls.append(url)
        if error is not None:
            raise error
        return body

    monkeypatch.setattr(module, "_get", fake)
    return calls


def test_check_fails_when_the_locked_commit_is_not_on_github(
    copy_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_script()
    stub_get(monkeypatch, module, module.NotFoundError("GET x: HTTP 404 Not Found"))
    assert module.main(["--root", str(copy_root), "--check"]) == 1
    assert "locked commit isn't in" in capsys.readouterr().err


def test_check_skips_the_online_half_on_a_network_error(
    copy_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_script()
    stub_get(monkeypatch, module, module.SyncError("GET x: connection refused"))
    assert module.main(["--root", str(copy_root), "--check"]) == 0
    assert "notice: online check skipped" in capsys.readouterr().err


def test_check_require_online_fails_on_a_network_error(
    copy_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_script()
    stub_get(monkeypatch, module, module.SyncError("GET x: connection refused"))
    assert module.main(["--root", str(copy_root), "--check", "--require-online"]) == 2
    assert "--require-online" in capsys.readouterr().err


def test_check_passes_when_the_locked_commit_has_the_locked_bytes(
    copy_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script()
    stub_get(monkeypatch, module, body=(copy_root / DEST).read_bytes())
    assert module.main(["--root", str(copy_root), "--check", "--require-online"]) == 0


def test_check_latest_fails_when_main_has_no_such_file(
    copy_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_script()
    sha = "a" * 40
    calls: list[str] = []

    def fake(url: str, accept: str | None = None) -> bytes:
        calls.append(url)
        if "api.github.com" in url:
            return sha.encode()
        raise module.NotFoundError("GET x: HTTP 404 Not Found")

    monkeypatch.setattr(module, "_get", fake)
    assert module.main(["--root", str(copy_root), "--check", "--latest"]) == 1
    assert "isn't in" in capsys.readouterr().err
    assert len(calls) == 2


def test_require_online_is_only_for_the_online_check(copy_root: Path) -> None:
    assert run("--root", copy_root, "--require-online").returncode == 2
    assert run("--root", copy_root, "--check", "--offline", "--require-online").returncode == 2


def test_update_refuses_a_source_file_that_is_not_committed(copy_root: Path, triage: Path) -> None:
    (triage / "backlog_query.py").write_text("VALUE = 2  # dirty\n", encoding="utf-8")
    before = (copy_root / DEST).read_bytes(), (copy_root / LOCK).read_bytes()
    for extra in ((), ("--force",)):
        result = run("--root", copy_root, "--source", triage, "--update", *extra)
        assert result.returncode == 2
        assert "uncommitted" in result.stderr
    assert ((copy_root / DEST).read_bytes(), (copy_root / LOCK).read_bytes()) == before
