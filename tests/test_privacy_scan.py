"""The privacy scanner (.github/scripts/privacy_scan.py) catches what it should, allows what it should, and -
the part that matters in a public repo's CI log - never prints a matched value.

Every "secret" below is built at run time, so this file itself holds nothing the scanner would flag.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "privacy_scan.py"

REAL_ID = "7656119" + "8" * 10
PUBLIC_IP = ".".join(["8", "8", "4", "4"])
OWN_IP = ".".join(["73", "12", "200", "9"])
PERSONAL_MAIL = "someone" + "@" + "gmail" + ".com"
HOST = "".join(["my", "box", "-", "p", "v", "e"])


class Repo:
    def __init__(self, case: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "SDNick484")
        self.git("config", "user.email", "SDNick484@users.noreply.github.com")

    def git(self, *a: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True)

    def add(self, rel: str, text: str) -> "Repo":
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return self

    def commit(self, msg: str = "Add files", author: str | None = None) -> "Repo":
        self.git("add", "-A")
        extra = ["--author", author] if author else []
        self.git("commit", "-q", "-m", msg, *extra)
        return self

    def scan(self, *args: str, denylist: str | None = None):
        env = {k: v for k, v in os.environ.items() if k != "PRIVACY_DENYLIST"}
        if denylist is not None:
            env["PRIVACY_DENYLIST"] = denylist
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), *args], env=env,
                              capture_output=True, text=True)


class Scanner(unittest.TestCase):
    def repo(self) -> Repo:
        return Repo(self)

    def assertFlags(self, p, rule: str, where: str, *secrets: str):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"{where}", p.stdout)
        self.assertIn(rule, p.stdout)
        for s in secrets:
            self.assertNotIn(s, p.stdout + p.stderr, "a matched value was printed")

    def test_a_clean_repo_passes(self):
        r = self.repo().add("README.md", "Use 7656119XXXXXXXXXX, 192.0.2.7, 10.0.0.5, 127.0.0.1, "
                                          "deals.example.com and SDNick484@users.noreply.github.com.\n").commit()
        p = r.scan()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("no findings", p.stdout)

    def test_a_steamid_is_flagged_without_being_printed(self):
        r = self.repo().add("docs/notes.md", f"line one\nmy id {REAL_ID} here\n").commit()
        self.assertFlags(r.scan(), "steamid", "docs/notes.md:2", REAL_ID)

    def test_a_steamid_in_a_fixture_is_still_flagged(self):
        """Only IP addresses are exempt in fixtures."""
        r = self.repo().add("tests/fixtures/library.json", f'{{"steamid": "{REAL_ID}"}}\n').commit()
        self.assertFlags(r.scan(), "steamid", "tests/fixtures/library.json:1", REAL_ID)

    def test_public_and_odd_ip_addresses_are_flagged(self):
        r = self.repo().add("a.txt", f"server at {PUBLIC_IP}\n").add("b.txt", f"home {OWN_IP}:8080\n").commit()
        p = r.scan()
        self.assertFlags(p, "ip", "a.txt:1", PUBLIC_IP, OWN_IP)
        self.assertIn("b.txt:1", p.stdout)

    def test_documentation_private_and_loopback_ranges_pass(self):
        ok = ["192.0.2.1", "198.51.100.77", "203.0.113.200", "10.1.2.3", "172.16.0.9", "172.31.255.1",
              "192.168.1.1", "127.0.0.1", "0.0.0.0"]
        r = self.repo().add("ips.txt", "\n".join(ok) + "\n").commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_172_outside_the_private_block_is_flagged(self):
        r = self.repo().add("x.txt", "172." + "32.0.1\n").commit()
        self.assertEqual(r.scan().returncode, 1)

    def test_version_numbers_are_not_mistaken_for_addresses(self):
        r = self.repo().add("v.txt", "build 1.2.3.4.5 and 999.1.1.1 and v1.2.3\n").commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_ips_under_fixtures_are_exempt(self):
        r = self.repo().add("tests/fixtures/log.txt", f"peer {PUBLIC_IP}\n").commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_a_personal_email_is_flagged_without_being_printed(self):
        r = self.repo().add("CONTRIBUTING.md", f"write to {PERSONAL_MAIL}\n").commit()
        self.assertFlags(r.scan(), "email", "CONTRIBUTING.md:1", PERSONAL_MAIL, "gmail")

    def test_allowed_email_forms_pass(self):
        r = self.repo().add("e.txt", "a@users.noreply.github.com b@example.com c@sub.example.org "
                                      "git@github.com:SDNick484/x.git noreply@anthropic.com\n").commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_the_allow_marker_skips_a_line(self):
        r = self.repo().add("a.txt", f"{PUBLIC_IP}  # privacy-scan: allow (a public DNS resolver)\n").commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_denylist_entries_match_case_insensitively_by_number(self):
        r = self.repo().add("notes.md", f"ssh {HOST.upper()}\n").commit()
        p = r.scan(denylist=f"# comment\nsomething-else\n{HOST}\n")
        self.assertFlags(p, "denylist entry #2", "notes.md:1", HOST, HOST.upper())

    def test_denylist_also_checks_file_names(self):
        r = self.repo().add(f"{HOST}.conf", "nothing\n").commit()
        p = r.scan(denylist=HOST)
        self.assertFlags(p, "denylist entry #1", "<file name matched entry #1>", HOST)

    def test_without_a_denylist_it_says_so_and_still_checks_patterns(self):
        r = self.repo().add("a.txt", f"{REAL_ID}\n").commit()
        p = r.scan()
        self.assertIn("PRIVACY_DENYLIST is not set", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_require_denylist_fails_when_it_is_missing(self):
        r = self.repo().add("a.txt", "fine\n").commit()
        self.assertEqual(r.scan("--require-denylist").returncode, 2)
        self.assertEqual(r.scan("--require-denylist", denylist="zzzqq").returncode, 0)

    def test_commit_authors_and_messages_are_checked(self):
        r = self.repo().add("a.txt", "fine\n").commit(f"Fix thing for {HOST}", author=f"Real Name <{PERSONAL_MAIL}>")
        p = r.scan(denylist=HOST)
        self.assertEqual(p.returncode, 1)
        self.assertIn("author:1: email", p.stdout)
        self.assertIn("message:1: denylist entry #1", p.stdout)
        for s in (PERSONAL_MAIL, HOST, "Real Name"):
            self.assertNotIn(s, p.stdout + p.stderr)
        self.assertEqual(r.scan("--no-history", denylist="unrelated").returncode, 0)

    def test_an_allowed_commit_skips_its_identity_only(self):
        who = f"{HOST} <SDNick484@users.noreply.github.com>"
        r = self.repo().add("a.txt", "fine\n").commit("Listed", author=who)
        listed = subprocess.run(["git", "-C", str(r.root), "rev-parse", "HEAD"], check=True, capture_output=True,
                                text=True).stdout.strip()
        r.add(".github/privacy-scan-allowed-commits", f"# pushed before the scan\n{listed}  old sync commit\n")
        r.add("b.txt", "fine\n").commit("Not listed", author=who)
        p = r.scan(denylist=HOST)
        self.assertNotIn(f"commit {listed[:10]}", p.stdout)
        self.assertIn("author:1: denylist entry #1", p.stdout)  # the unlisted commit, same author
        self.assertEqual(p.stdout.count("author:1"), 1, p.stdout)
        self.assertNotIn(HOST, p.stdout + p.stderr)

        r.add("c.txt", "fine\n").commit(f"Message names {HOST}")
        msg_sha = subprocess.run(["git", "-C", str(r.root), "rev-parse", "HEAD"], check=True, capture_output=True,
                                 text=True).stdout.strip()
        r.add(".github/privacy-scan-allowed-commits", f"{listed} old sync commit\n{msg_sha} listed, but\n")
        p = r.scan(denylist=HOST)
        self.assertIn(f"commit {msg_sha[:10]} message:1: denylist entry #1", p.stdout)  # messages are still scanned

    def test_a_malformed_allowed_commits_line_is_a_setup_error(self):
        r = self.repo().add(".github/privacy-scan-allowed-commits", "abc123 too short\n").commit()
        p = r.scan()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("full 40-character commit SHA", p.stderr)

    def test_binary_files_are_skipped(self):
        r = self.repo()
        (r.root / "img.bin").write_bytes(b"\0\1\2" + REAL_ID.encode())
        r.commit()
        self.assertEqual(r.scan().returncode, 0)

    def test_the_scanner_passes_on_this_repository(self):
        """The repo these tests ship in must itself be clean (the CI job runs the same command)."""
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").exists():
            self.skipTest("not a git checkout")
        p = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--no-history"], capture_output=True,
                           text=True, env={k: v for k, v in os.environ.items() if k != "PRIVACY_DENYLIST"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
