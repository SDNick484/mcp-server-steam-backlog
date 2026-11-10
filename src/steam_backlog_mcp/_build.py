"""The commit this build came from.

`git archive` replaces the placeholder below (see .gitattributes: export-subst), so a wheel built from
an archive of the repo carries the short commit. A checkout or a git-URL install leaves it untouched.
"""

COMMIT = "$Format:%h$"


def commit() -> str:
    """The short commit, or "unknown" when this copy was not made by `git archive`."""
    return "unknown" if COMMIT.startswith("$") else COMMIT
