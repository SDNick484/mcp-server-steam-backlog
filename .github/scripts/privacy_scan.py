#!/usr/bin/env python3
"""Fail if a tracked file or a commit carries personal details. This repo is public.

Checks, over every tracked file (and untracked ones that are not ignored, so it also works before a commit) and over commit authors, committers and messages:
  steamid    a real-looking SteamID64 (7656119 + 10 digits). The placeholder 7656119XXXXXXXXXX is fine.
  ip         an IPv4 address that is not loopback, RFC 5737 documentation (192.0.2.0/24, 198.51.100.0/24,
             203.0.113.0/24) or RFC 1918 private (10/8, 172.16/12, 192.168/16). Files under tests/fixtures/ are
             exempt, since fixtures may hold anything shaped like data.
  email      an address that is not @users.noreply.github.com, noreply@github.com, noreply@anthropic.com,
             git@github.com, or at example.com / .org / .net / .test / .invalid
  denylist   any line of the PRIVACY_DENYLIST environment variable (case-insensitive substring), for the
             things no pattern can know: your hostnames, domain, name, time zone, device names.

A four-part version number (a build number after a three-part version) looks like an address and is flagged; write it differently or mark the line.
A line may carry the text `privacy-scan: allow` to be skipped (use sparingly, and say why beside it).

THIS OUTPUT IS PUBLIC (CI logs of a public repo are readable by anyone). So it prints only where and which
rule - file:line and the rule name, or the commit and the rule name - and never the matched text. For the
denylist it names the entry's position (#2), not its content.

Usage: privacy_scan.py [--root DIR] [--no-history] [--require-denylist]
Exit status: 0 clean, 1 findings, 2 usage or setup error.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

STEAMID = re.compile(r"(?<![0-9A-Za-z])7656119[0-9]{10}(?![0-9A-Za-z])")
IPV4 = re.compile(r"(?<![0-9A-Za-z.])((?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3})(?![0-9A-Za-z]|\.[0-9])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
ALLOWED_EMAIL = re.compile(
    r"^(?:[^@]+@users\.noreply\.github\.com|noreply@github\.com|noreply@anthropic\.com|git@github\.com|"
    r"[^@]+@(?:[a-z0-9-]+\.)*example\.(?:com|org|net)|[^@]+@[a-z0-9.-]*\.(?:test|invalid|example))$", re.I)
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
FIXTURE_DIRS = ("tests/fixtures/",)
ALLOW_MARK = "privacy-scan: allow"


def ip_is_fine(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an address after all (e.g. 300.1.1.1)
    if ip.is_loopback or ip == ipaddress.ip_address("0.0.0.0"):
        return True
    return any(ip in n for n in DOC_NETS + PRIVATE_NETS)


def scan_text(text: str, where: str, denylist: list[str], *, check_ips: bool, findings: list[tuple[str, str]]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        here = f"{where}:{lineno}"
        if STEAMID.search(line):
            findings.append((here, "steamid"))
        if check_ips and any(not ip_is_fine(m.group(1)) for m in IPV4.finditer(line)):
            findings.append((here, "ip"))
        if any(not ALLOWED_EMAIL.match(m.group(0)) for m in EMAIL.finditer(line)):
            findings.append((here, "email"))
        low = line.lower()
        for n, entry in enumerate(denylist, 1):
            if entry in low:
                findings.append((here, f"denylist entry #{n}"))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".", help="repository to scan (default: current directory)")
    ap.add_argument("--no-history", action="store_true", help="skip commit authors, committers and messages")
    ap.add_argument("--require-denylist", action="store_true",
                    help="fail if PRIVACY_DENYLIST is unset (CI sets this where the secret is available)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    raw = os.environ.get("PRIVACY_DENYLIST", "")
    denylist = [x.strip().lower() for x in raw.splitlines() if x.strip() and not x.strip().startswith("#")]
    if not denylist:
        msg = "PRIVACY_DENYLIST is not set: the denylist check was skipped (the pattern checks still ran)."
        if args.require_denylist:
            print(f"error: {msg}", file=sys.stderr)
            return 2
        print(f"notice: {msg}")

    try:
        files = [f.decode("utf-8", "surrogateescape") for f in git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"error: could not list tracked files in {root}: {type(e).__name__}", file=sys.stderr)
        return 2

    findings: list[tuple[str, str]] = []
    scanned = 0
    for rel in files:
        path = root / rel
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        scanned += 1
        text = data.decode("utf-8", "replace")
        # A file name is a place for a hostname to hide too. If it matches, the name itself must not be printed.
        name_hits = [n for n, entry in enumerate(denylist, 1) if entry in rel.lower()]
        shown = f"<file name matched entry #{name_hits[0]}>" if name_hits else rel
        for n in name_hits:
            findings.append((shown, f"denylist entry #{n}"))
        scan_text(text, shown, denylist, check_ips=not rel.startswith(FIXTURE_DIRS), findings=findings)

    commits = 0
    if not args.no_history:
        try:
            out = git(root, "log", "--all", "-z", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B").decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            out = ""  # no commits yet
        for rec in filter(None, out.split("\0")):
            parts = rec.split("\x1f", 3)
            if len(parts) < 4:
                continue
            commits += 1
            sha = parts[0].strip()[:10]
            for label, chunk in (("author", parts[1]), ("committer", parts[2]), ("message", parts[3])):
                scan_text(chunk, f"commit {sha} {label}", denylist, check_ips=True, findings=findings)

    seen = set()
    unique = [f for f in findings if not (f in seen or seen.add(f))]
    for where, rule in unique:
        print(f"{where}: {rule}")
    print(f"scanned {scanned} files and {commits} commits: "
          f"{'no findings' if not unique else str(len(unique)) + ' finding(s); values are not printed'}")
    return 1 if unique else 0


if __name__ == "__main__":
    sys.exit(main())
