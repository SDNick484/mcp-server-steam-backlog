"""The build commit: placeholder handling, --version / doctor output, and the git-archive wheel path."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from steam_backlog_mcp import _build, cli, doctor

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_unsubstituted_placeholder_reports_unknown(monkeypatch):
    assert _build.commit() == "unknown"
    monkeypatch.setattr(_build, "COMMIT", "abc1234")
    assert _build.commit() == "abc1234"


def test_version_and_doctor_show_the_build(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip().endswith("(unknown)")
    report = doctor.Report(doctor.versions(), "http://backlog.invalid", False, build=_build.commit())
    assert '"build": "unknown"' in doctor.to_json(report)
    assert doctor.versions()["mcp-server-steam-backlog"].endswith("(unknown)")


def test_wheel_from_git_archive_carries_the_commit(tmp_path):
    try:
        head = _git("rev-parse", "--short", "HEAD")
        files = _git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if "src/steam_backlog_mcp/_build.py" not in files or ".gitattributes" not in files:
        pytest.skip("HEAD does not contain _build.py and .gitattributes yet")
    src = tmp_path / "src"
    src.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(src)], input=archive, check=True)
    wheels = tmp_path / "whl"
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheels), str(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    (wheel,) = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as z:
        text = z.read("steam_backlog_mcp/_build.py").decode()
    line = next(ln for ln in text.splitlines() if ln.startswith("COMMIT"))
    built = line.split("=", 1)[1].strip().strip('"')
    assert built.startswith(head) or head.startswith(built)
    assert not built.startswith("$")
